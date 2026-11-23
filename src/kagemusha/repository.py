from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .errors import ServiceError, internal_error, invalid_request
from .git import run_git, validate_repository_url, validate_resolved_addresses
from .locks import file_lock
from .process import ProcessRunner

_REPOSITORY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")


class Repository(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=_REPOSITORY_ID.pattern)
    name: str = Field(min_length=1, max_length=255)
    remote_url: str = Field(min_length=1, max_length=2048)
    git_provider: Literal["github", "gitlab", "generic"]
    default_ref: str = Field(min_length=1, max_length=255)
    credential_ref: str | None = Field(default=None, max_length=255)


class RepositoryManager:
    def __init__(self, settings: Settings, runner: ProcessRunner) -> None:
        self.settings = settings
        self.runner = runner

    @staticmethod
    def validate_id(repository_id: str) -> None:
        if not _REPOSITORY_ID.fullmatch(repository_id):
            raise invalid_request("Invalid repository ID")

    def bare_path(self, repository_id: str) -> Path:
        self.validate_id(repository_id)
        return self.settings.mirrors_dir / f"{repository_id}.git"

    def lock_path(self, repository_id: str) -> Path:
        self.validate_id(repository_id)
        return self.settings.locks_dir / f"{repository_id}.lock"

    async def synchronize(self, repository: Repository, token: str | None) -> int:
        validate_repository_url(repository.remote_url, self.settings)
        await validate_resolved_addresses(repository.remote_url)
        bare = self.bare_path(repository.id)
        started = time.monotonic()
        async with file_lock(self.lock_path(repository.id)):
            if not bare.exists():
                temporary = self.settings.tmp_dir / f"mirror-{uuid.uuid4().hex}.git"
                try:
                    await run_git(
                        self.settings,
                        self.runner,
                        (
                            "clone",
                            "--mirror",
                            "--quiet",
                            "--",
                            repository.remote_url,
                            str(temporary),
                        ),
                        operation="clone repository mirror",
                        provider=repository.git_provider,
                        token=token,
                    )
                    await self._configure_bare(temporary, repository.remote_url)
                    await run_git(
                        self.settings,
                        self.runner,
                        (f"--git-dir={temporary}", "fsck", "--full", "--no-dangling"),
                        operation="verify repository mirror",
                        local=True,
                    )
                    temporary.replace(bare)
                finally:
                    if temporary.exists():
                        await anyio.to_thread.run_sync(shutil.rmtree, temporary, True)
            else:
                await run_git(
                    self.settings,
                    self.runner,
                    (
                        f"--git-dir={bare}",
                        "fetch",
                        "--quiet",
                        "--prune",
                        "--prune-tags",
                        "--force",
                        "origin",
                        "+refs/heads/*:refs/heads/*",
                        "+refs/tags/*:refs/tags/*",
                    ),
                    operation="fetch repository mirror",
                    provider=repository.git_provider,
                    token=token,
                )
        return int((time.monotonic() - started) * 1000)

    async def fetch_snapshot(
        self, repository: Repository, remote_ref: str, token: str | None
    ) -> tuple[str, str, int]:
        bare = self.bare_path(repository.id)
        if not bare.is_dir():
            await self.synchronize(repository, token)
        validate_repository_url(repository.remote_url, self.settings)
        await validate_resolved_addresses(repository.remote_url)
        snapshot = f"refs/kagemusha/snapshots/{uuid.uuid4().hex}"
        started = time.monotonic()
        async with file_lock(self.lock_path(repository.id)):
            try:
                await run_git(
                    self.settings,
                    self.runner,
                    (
                        f"--git-dir={bare}",
                        "fetch",
                        "--quiet",
                        "--no-tags",
                        "--no-write-fetch-head",
                        "--no-auto-gc",
                        "--force",
                        "origin",
                        f"+{remote_ref}:{snapshot}",
                    ),
                    operation="git fetch remote ref",
                    provider=repository.git_provider,
                    token=token,
                    missing_ref=True,
                )
                commit = await run_git(
                    self.settings,
                    self.runner,
                    (
                        f"--git-dir={bare}",
                        "rev-parse",
                        "--verify",
                        f"{snapshot}^{{commit}}",
                    ),
                    operation="resolve fetched commit",
                    local=True,
                )
                if not _SHA.fullmatch(commit):
                    raise internal_error()
                await run_git(
                    self.settings,
                    self.runner,
                    (f"--git-dir={bare}", "update-ref", snapshot, commit),
                    operation="pin fetched commit",
                    local=True,
                )
                return snapshot, commit, int((time.monotonic() - started) * 1000)
            except BaseException:
                await self._delete_snapshot_unlocked(bare, snapshot)
                raise

    async def delete_snapshot(self, repository_id: str, snapshot: str) -> None:
        if not snapshot.startswith("refs/kagemusha/snapshots/"):
            return
        async with file_lock(self.lock_path(repository_id)):
            await self._delete_snapshot_unlocked(
                self.bare_path(repository_id), snapshot
            )

    async def delete_mirror(self, repository_id: str) -> None:
        async with file_lock(self.lock_path(repository_id)):
            bare = self.bare_path(repository_id)
            if bare.exists() and not bare.is_symlink():
                await anyio.to_thread.run_sync(shutil.rmtree, bare, True)

    async def startup_recovery(self) -> None:
        for bare in self.settings.mirrors_dir.glob("*.git"):
            if bare.is_dir() and not bare.is_symlink():
                await self._clean_stale_snapshot_refs(bare)
        cutoff = time.time() - self.settings.temp_ttl_seconds
        for path in self.settings.tmp_dir.iterdir():
            try:
                if path.stat().st_mtime < cutoff and not path.is_symlink():
                    await anyio.to_thread.run_sync(shutil.rmtree, path, True)
            except FileNotFoundError:
                pass

    async def _delete_snapshot_unlocked(self, bare: Path, snapshot: str) -> None:
        if not bare.exists():
            return
        try:
            await run_git(
                self.settings,
                self.runner,
                (f"--git-dir={bare}", "update-ref", "-d", snapshot),
                operation="delete snapshot ref",
                local=True,
            )
        except ServiceError as error:
            logging.getLogger("kagemusha").warning(
                "Failed to delete snapshot ref",
                extra={"snapshot": snapshot, "error_code": error.code},
            )

    async def _configure_bare(self, bare: Path, remote_url: str) -> None:
        for key, value in (
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            ("credential.helper", ""),
            ("remote.origin.url", remote_url),
            ("remote.origin.mirror", "true"),
        ):
            await run_git(
                self.settings,
                self.runner,
                (f"--git-dir={bare}", "config", "--local", key, value),
                operation="configure repository mirror",
                local=True,
            )

    async def _clean_stale_snapshot_refs(self, bare: Path) -> None:
        root = bare / "refs" / "kagemusha" / "snapshots"
        if not root.is_dir():
            return
        cutoff = time.time() - self.settings.temp_ttl_seconds
        for ref in root.iterdir():
            try:
                if ref.is_file() and ref.stat().st_mtime < cutoff:
                    await self._delete_snapshot_unlocked(
                        bare, f"refs/kagemusha/snapshots/{ref.name}"
                    )
            except FileNotFoundError:
                pass
