from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from .archive import create_archive, ensure_temp_capacity
from .config import Settings
from .errors import ErrorCode, ServiceError, internal_error
from .git import (
    run_git,
    validate_repository_url,
    validate_resolved_addresses,
)
from .process import ProcessRunner
from .repository import Repository, RepositoryManager


@dataclass(frozen=True, slots=True)
class WorkspaceResult:
    root: Path
    archive: Path | None
    commit: str
    fetch_duration_ms: int
    workspace_duration_ms: int
    archive_duration_ms: int
    archive_size_bytes: int
    branch: str | None


class WorkspaceManager:
    def __init__(
        self, settings: Settings, runner: ProcessRunner, repositories: RepositoryManager
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.repositories = repositories

    async def create(
        self,
        repository: Repository,
        remote_ref: str,
        branch: str | None,
        tracks_remote: bool,
        token: str | None,
        depth: int,
        mode: str = "git",
        *,
        produce_archive: bool = True,
    ) -> WorkspaceResult:
        if not 0 <= depth <= self.settings.max_depth:
            raise ServiceError(
                ErrorCode.INVALID_REQUEST, "Depth is outside the configured range"
            )
        snapshot = ""
        root = self.settings.tmp_dir / f"copy-{uuid.uuid4().hex}"
        workspace = root / "workspace"
        try:
            await ensure_temp_capacity(self.settings)
            snapshot, commit, fetch_ms = await self.repositories.fetch_snapshot(
                repository, remote_ref, token
            )
            workspace_started = time.monotonic()
            root.mkdir(mode=0o700)
            await run_git(
                self.settings,
                self.runner,
                ("init", "--quiet", str(workspace)),
                operation="initialize workspace",
                local=True,
            )
            fetch_arguments = [
                "-C",
                str(workspace),
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-auto-gc",
            ]
            if depth > 0:
                fetch_arguments.append(f"--depth={depth}")
            fetch_arguments.extend(
                (f"file://{self.repositories.bare_path(repository.id)}", snapshot)
            )
            await run_git(
                self.settings,
                self.runner,
                tuple(fetch_arguments),
                operation="fetch independent workspace",
                local=True,
            )
            await run_git(
                self.settings,
                self.runner,
                (
                    "-C",
                    str(workspace),
                    "remote",
                    "add",
                    "origin",
                    repository.remote_url,
                ),
                operation="configure workspace origin",
                local=True,
            )
            checkout_arguments = [
                "-C",
                str(workspace),
                "checkout",
                "--quiet",
                "--force",
            ]
            if branch is None:
                checkout_arguments.extend(("--detach", commit))
            else:
                checkout_arguments.extend(("-B", branch, commit))
            await run_git(
                self.settings,
                self.runner,
                tuple(checkout_arguments),
                operation="checkout pinned commit",
                local=True,
            )
            if branch is not None and tracks_remote:
                tracking_ref = f"refs/remotes/origin/{branch}"
                await run_git(
                    self.settings,
                    self.runner,
                    ("-C", str(workspace), "update-ref", tracking_ref, commit),
                    operation="pin remote tracking branch",
                    local=True,
                )
                await run_git(
                    self.settings,
                    self.runner,
                    (
                        "-C",
                        str(workspace),
                        "branch",
                        f"--set-upstream-to=origin/{branch}",
                        branch,
                    ),
                    operation="configure branch upstream",
                    local=True,
                )
            for name in ("FETCH_HEAD", "ORIG_HEAD"):
                (workspace / ".git" / name).unlink(missing_ok=True)
            await self._hydrate_lfs(
                workspace, repository.remote_url, repository.git_provider, token, root
            )
            counter = [0]
            await self._hydrate_submodules(
                workspace,
                repository.remote_url,
                repository.git_provider,
                token,
                root,
                level=0,
                counter=counter,
            )
            await self._verify_workspace(workspace, commit, depth, branch)
            if mode == "source":
                await anyio.to_thread.run_sync(_strip_git_metadata, workspace)
            elif mode != "git":
                raise ServiceError(ErrorCode.INVALID_REQUEST, "Invalid copy mode")
            workspace_ms = int((time.monotonic() - workspace_started) * 1000)
            archive = None
            size = 0
            archive_ms = 0
            if produce_archive:
                archive_started = time.monotonic()
                archive, size = await create_archive(self.settings, self.runner, root)
                archive_ms = int((time.monotonic() - archive_started) * 1000)
            await self.repositories.delete_snapshot(repository.id, snapshot)
            snapshot = ""
            return WorkspaceResult(
                root=root,
                archive=archive,
                commit=commit,
                fetch_duration_ms=fetch_ms,
                workspace_duration_ms=workspace_ms,
                archive_duration_ms=archive_ms,
                archive_size_bytes=size,
                branch=branch,
            )
        except BaseException:
            await cleanup_workspace(root)
            raise
        finally:
            if snapshot:
                await self.repositories.delete_snapshot(repository.id, snapshot)

    async def _hydrate_submodules(
        self,
        repository: Path,
        repository_url: str,
        provider: str,
        token: str | None,
        root: Path,
        *,
        level: int,
        counter: list[int],
    ) -> None:
        gitmodules = repository / ".gitmodules"
        if not gitmodules.exists():
            return
        if level >= self.settings.max_submodule_depth:
            raise ServiceError(
                ErrorCode.INVALID_REQUEST, "Submodule nesting limit exceeded"
            )
        if not gitmodules.is_file() or gitmodules.is_symlink():
            raise ServiceError(ErrorCode.INVALID_REQUEST, "Invalid .gitmodules file")

        paths = await self._read_submodule_values(repository, "path")
        await run_git(
            self.settings,
            self.runner,
            ("-C", str(repository), "submodule", "init"),
            operation="initialize submodule configuration",
            local=True,
        )
        urls = await self._read_submodule_values(repository, "url", local=True)
        if paths.keys() != urls.keys():
            raise ServiceError(
                ErrorCode.INVALID_REQUEST, "Invalid submodule configuration"
            )

        repository_root = repository.resolve()
        parent_authority = _url_authority(repository_url)
        for name, relative_path in paths.items():
            counter[0] += 1
            if counter[0] > self.settings.max_submodules:
                raise ServiceError(
                    ErrorCode.INVALID_REQUEST, "Submodule count limit exceeded"
                )
            submodule = (repository / relative_path).resolve()
            if submodule == repository_root or not submodule.is_relative_to(
                repository_root
            ):
                raise ServiceError(ErrorCode.INVALID_REQUEST, "Invalid submodule path")

            url = urls[name]
            validate_repository_url(url, self.settings)
            await validate_resolved_addresses(url)
            authenticated = _url_authority(url) == parent_authority
            submodule_token = token if authenticated else None
            await run_git(
                self.settings,
                self.runner,
                (
                    "-C",
                    str(repository),
                    "submodule",
                    "update",
                    "--init",
                    "--depth=1",
                    "--",
                    relative_path,
                ),
                operation="checkout submodule",
                provider=provider if submodule_token is not None else None,
                token=submodule_token,
            )
            if not submodule.is_dir():
                raise ServiceError(
                    ErrorCode.INTERNAL_ERROR, "Submodule checkout failed"
                )
            await ensure_temp_capacity(self.settings, root)
            await self._hydrate_lfs(submodule, url, provider, submodule_token, root)
            await self._hydrate_submodules(
                submodule,
                url,
                provider,
                submodule_token,
                root,
                level=level + 1,
                counter=counter,
            )

    async def _read_submodule_values(
        self, repository: Path, field: str, *, local: bool = False
    ) -> dict[str, str]:
        arguments = ["-C", str(repository), "config", "--null"]
        if not local:
            arguments.append("--file=.gitmodules")
        else:
            arguments.append("--local")
        arguments.extend(("--get-regexp", rf"^submodule\..*\.{field}$"))
        output = await run_git(
            self.settings,
            self.runner,
            tuple(arguments),
            operation="read submodule configuration",
            local=True,
        )
        values: dict[str, str] = {}
        suffix = f".{field}"
        for entry in output.split("\0"):
            if not entry:
                continue
            key, separator, value = entry.partition("\n")
            if (
                not separator
                or not key.startswith("submodule.")
                or not key.endswith(suffix)
            ):
                raise ServiceError(
                    ErrorCode.INVALID_REQUEST, "Invalid submodule configuration"
                )
            name = key[len("submodule.") : -len(suffix)]
            if not name or name in values or not value:
                raise ServiceError(
                    ErrorCode.INVALID_REQUEST, "Invalid submodule configuration"
                )
            values[name] = value
        if not values:
            raise ServiceError(
                ErrorCode.INVALID_REQUEST, "Invalid submodule configuration"
            )
        return values

    async def _hydrate_lfs(
        self,
        repository: Path,
        repository_url: str,
        provider: str,
        token: str | None,
        root: Path,
    ) -> None:
        files = await run_git(
            self.settings,
            self.runner,
            ("-C", str(repository), "lfs", "ls-files", "--name-only"),
            operation="detect Git LFS files",
            local=True,
        )
        if not files:
            return
        await run_git(
            self.settings,
            self.runner,
            ("-C", str(repository), "lfs", "install", "--local", "--force"),
            operation="configure Git LFS",
            local=True,
            allow_hooks=True,
        )
        environment = await run_git(
            self.settings,
            self.runner,
            ("-C", str(repository), "lfs", "env"),
            operation="resolve Git LFS endpoint",
            local=True,
        )
        endpoints = [
            line.removeprefix("Endpoint=").partition(" ")[0]
            for line in environment.splitlines()
            if line.startswith("Endpoint=")
        ]
        if not endpoints:
            raise ServiceError(
                ErrorCode.GIT_REMOTE_ERROR, "Unable to resolve LFS endpoint"
            )
        for endpoint in endpoints:
            validate_repository_url(endpoint, self.settings)
            await validate_resolved_addresses(endpoint)
        authenticated = all(
            _url_authority(endpoint) == _url_authority(repository_url)
            for endpoint in endpoints
        )
        lfs_token = token if authenticated else None
        await run_git(
            self.settings,
            self.runner,
            ("-C", str(repository), "lfs", "pull", "origin"),
            operation="download Git LFS objects",
            provider=provider if lfs_token is not None else None,
            token=lfs_token,
        )
        await run_git(
            self.settings,
            self.runner,
            ("-C", str(repository), "lfs", "fsck"),
            operation="verify Git LFS objects",
            local=True,
        )
        await ensure_temp_capacity(self.settings, root)

    async def _verify_workspace(
        self, workspace: Path, commit: str, depth: int, branch: str | None
    ) -> None:
        head = await run_git(
            self.settings,
            self.runner,
            ("-C", str(workspace), "rev-parse", "HEAD"),
            operation="verify workspace commit",
            local=True,
        )
        if head != commit:
            raise internal_error()
        actual_branch = await run_git(
            self.settings,
            self.runner,
            ("-C", str(workspace), "branch", "--show-current"),
            operation="verify workspace branch",
            local=True,
        )
        if actual_branch != (branch or ""):
            raise internal_error()
        shallow = await run_git(
            self.settings,
            self.runner,
            ("-C", str(workspace), "rev-parse", "--is-shallow-repository"),
            operation="verify workspace depth",
            local=True,
        )
        if shallow not in {"true", "false"} or (depth == 0 and shallow != "false"):
            raise internal_error()
        alternates = workspace / ".git" / "objects" / "info" / "alternates"
        if alternates.exists() and alternates.read_text(errors="replace").strip():
            raise internal_error()
        status = await run_git(
            self.settings,
            self.runner,
            ("-C", str(workspace), "status", "--porcelain=v1"),
            operation="verify clean workspace",
            local=True,
        )
        if status:
            raise internal_error()
        submodules = await run_git(
            self.settings,
            self.runner,
            ("-C", str(workspace), "submodule", "status", "--recursive"),
            operation="verify submodules",
            local=True,
        )
        if any(line.startswith(("-", "+", "U")) for line in submodules.splitlines()):
            raise internal_error()
        await run_git(
            self.settings,
            self.runner,
            ("-C", str(workspace), "fsck", "--full", "--no-dangling"),
            operation="verify workspace objects",
            local=True,
        )


async def cleanup_workspace(root: Path) -> None:
    with anyio.CancelScope(shield=True):
        await anyio.to_thread.run_sync(shutil.rmtree, root, True)


def _url_authority(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.hostname is None:
        raise ServiceError(ErrorCode.INVALID_REQUEST, "Invalid repository URL")
    return parsed.hostname.lower(), parsed.port or 443


def _strip_git_metadata(workspace: Path) -> None:
    paths = sorted(
        workspace.rglob(".git"), key=lambda path: len(path.parts), reverse=True
    )
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
