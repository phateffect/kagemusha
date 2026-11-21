from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .errors import (
    ErrorCode,
    ServiceError,
    internal_error,
    invalid_request,
)
from .git import run_git, validate_repository_url, validate_resolved_addresses
from .locks import file_lock
from .process import ProcessRunner
from .refs import resolve_ref

_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=_PROJECT_ID.pattern)
    repository: str = Field(min_length=1, max_length=2048)
    provider: Literal["github", "gitlab"]
    default_branch: str = Field(min_length=1, max_length=255)


class RepositoryManager:
    def __init__(self, settings: Settings, runner: ProcessRunner) -> None:
        self.settings = settings
        self.runner = runner

    def validate_id(self, project_id: str) -> None:
        if not _PROJECT_ID.fullmatch(project_id):
            raise invalid_request("Invalid project ID")

    def bare_path(self, project_id: str) -> Path:
        self.validate_id(project_id)
        return self.settings.projects_dir / f"{project_id}.git"

    def metadata_path(self, project_id: str) -> Path:
        self.validate_id(project_id)
        return self.settings.metadata_dir / f"{project_id}.json"

    def lock_path(self, project_id: str) -> Path:
        self.validate_id(project_id)
        return self.settings.locks_dir / f"{project_id}.lock"

    async def create(
        self,
        project_id: str,
        repository: str,
        provider: Literal["github", "gitlab"],
        token: str,
    ) -> Project:
        self.validate_id(project_id)
        validate_repository_url(repository, self.settings)
        await validate_resolved_addresses(repository)
        bare = self.bare_path(project_id)
        metadata = self.metadata_path(project_id)
        temporary = self.settings.tmp_dir / f"clone-{uuid.uuid4().hex}"
        moved = False
        async with file_lock(self.lock_path(project_id)):
            await self._recover_project(project_id)
            if bare.exists() or metadata.exists():
                raise ServiceError(ErrorCode.PROJECT_ALREADY_EXISTS)
            try:
                await run_git(
                    self.settings,
                    self.runner,
                    ("clone", "--bare", "--quiet", "--", repository, str(temporary)),
                    operation="git clone",
                    provider=provider,
                    token=token,
                )
                await self._configure_bare(temporary, repository)
                is_bare = await run_git(
                    self.settings,
                    self.runner,
                    (f"--git-dir={temporary}", "rev-parse", "--is-bare-repository"),
                    operation="verify bare repository",
                    local=True,
                )
                if is_bare != "true":
                    raise internal_error()
                await run_git(
                    self.settings,
                    self.runner,
                    (f"--git-dir={temporary}", "fsck", "--full", "--no-dangling"),
                    operation="verify repository objects",
                    local=True,
                )
                head = await run_git(
                    self.settings,
                    self.runner,
                    (f"--git-dir={temporary}", "symbolic-ref", "HEAD"),
                    operation="resolve default branch",
                    local=True,
                )
                if not head.startswith("refs/heads/"):
                    raise internal_error()
                default_branch = head.removeprefix("refs/heads/")
                resolve_ref(head, provider)
                config_text = (temporary / "config").read_text(errors="replace")
                if token in config_text or "[url" in config_text.lower():
                    raise internal_error()
                os.replace(temporary, bare)
                moved = True
                project = Project(
                    id=project_id,
                    repository=repository,
                    provider=provider,
                    default_branch=default_branch,
                )
                self._write_metadata(metadata, project)
                return project
            finally:
                if temporary.exists():
                    await anyio.to_thread.run_sync(shutil.rmtree, temporary, True)
                if moved and not metadata.exists():
                    await anyio.to_thread.run_sync(shutil.rmtree, bare, True)

    async def get(self, project_id: str) -> Project:
        path = self.metadata_path(project_id)
        if not path.is_file() or not self.bare_path(project_id).is_dir():
            raise ServiceError(ErrorCode.PROJECT_NOT_FOUND)
        try:
            project = Project.model_validate_json(path.read_text())
            if project.id != project_id:
                raise ValueError("Project metadata ID does not match its filename")
            self.validate_id(project.id)
            resolve_ref(f"refs/heads/{project.default_branch}", project.provider)
            validate_repository_url(project.repository, self.settings)
            return project
        except (ValidationError, ValueError, ServiceError) as error:
            raise internal_error() from error

    async def fetch_snapshot(
        self, project: Project, remote_ref: str, token: str
    ) -> tuple[str, str, int]:
        validate_repository_url(project.repository, self.settings)
        await validate_resolved_addresses(project.repository)
        bare = self.bare_path(project.id)
        snapshot = f"refs/gws/snapshots/{uuid.uuid4().hex}"
        started = time.monotonic()
        async with file_lock(self.lock_path(project.id)):
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
                    provider=project.provider,
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
                duration = int((time.monotonic() - started) * 1000)
                return snapshot, commit, duration
            except BaseException:
                await self._delete_snapshot_unlocked(bare, snapshot)
                raise

    async def delete_snapshot(self, project_id: str, snapshot: str) -> None:
        if not snapshot.startswith("refs/gws/snapshots/"):
            return
        async with file_lock(self.lock_path(project_id)):
            await self._delete_snapshot_unlocked(self.bare_path(project_id), snapshot)

    async def _delete_snapshot_unlocked(self, bare: Path, snapshot: str) -> None:
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

    async def _configure_bare(self, bare: Path, repository: str) -> None:
        for key, value in (
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            ("credential.helper", ""),
            ("remote.origin.url", repository),
        ):
            await run_git(
                self.settings,
                self.runner,
                (f"--git-dir={bare}", "config", "--local", key, value),
                operation="sanitize repository config",
                local=True,
            )

    def _write_metadata(self, path: Path, project: Project) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                stream.write(project.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def _recover_project(self, project_id: str) -> None:
        bare = self.bare_path(project_id)
        metadata = self.metadata_path(project_id)
        if metadata.exists() and not bare.is_dir():
            metadata.unlink(missing_ok=True)
        elif bare.exists() and not metadata.exists():
            if bare.is_dir() and not bare.is_symlink():
                await anyio.to_thread.run_sync(shutil.rmtree, bare)
            else:
                bare.unlink(missing_ok=True)

    async def startup_recovery(self) -> None:
        now = time.time()
        for metadata in self.settings.metadata_dir.glob("*.json"):
            project_id = metadata.stem
            if _PROJECT_ID.fullmatch(project_id):
                await self._recover_project(project_id)
        for candidate in self.settings.projects_dir.glob("*.git"):
            project_id = candidate.name.removesuffix(".git")
            if _PROJECT_ID.fullmatch(project_id):
                await self._recover_project(project_id)
                if candidate.is_dir():
                    await self._cleanup_snapshots(candidate, now)
        for candidate in self.settings.tmp_dir.iterdir():
            if not candidate.name.startswith(("clone-", "copy-", "home-")):
                continue
            try:
                if now - candidate.stat().st_mtime < self.settings.temp_ttl_seconds:
                    continue
                if candidate.is_dir() and not candidate.is_symlink():
                    await anyio.to_thread.run_sync(shutil.rmtree, candidate)
                else:
                    candidate.unlink(missing_ok=True)
            except FileNotFoundError:
                pass

    async def _cleanup_snapshots(self, bare: Path, now: float) -> None:
        root = bare / "refs" / "gws" / "snapshots"
        if not root.is_dir():
            return
        for ref in root.iterdir():
            if not ref.is_file():
                continue
            try:
                if now - ref.stat().st_mtime < self.settings.temp_ttl_seconds:
                    continue
            except FileNotFoundError:
                continue
            await run_git(
                self.settings,
                self.runner,
                (
                    f"--git-dir={bare}",
                    "update-ref",
                    "-d",
                    f"refs/gws/snapshots/{ref.name}",
                ),
                operation="clean stale snapshot",
                local=True,
            )
