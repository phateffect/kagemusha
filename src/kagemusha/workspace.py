from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio

from .archive import create_archive, ensure_temp_capacity
from .config import Settings
from .errors import ErrorCode, ServiceError, internal_error
from .git import run_git
from .process import ProcessRunner
from .repository import Project, RepositoryManager


@dataclass(frozen=True, slots=True)
class WorkspaceResult:
    root: Path
    archive: Path
    commit: str
    fetch_duration_ms: int
    workspace_duration_ms: int
    archive_duration_ms: int
    archive_size_bytes: int


class WorkspaceManager:
    def __init__(
        self, settings: Settings, runner: ProcessRunner, repositories: RepositoryManager
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.repositories = repositories

    async def create(
        self, project: Project, remote_ref: str, token: str, depth: int
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
                project, remote_ref, token
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
                (f"file://{self.repositories.bare_path(project.id)}", snapshot)
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
                    "checkout",
                    "--quiet",
                    "--detach",
                    "--force",
                    commit,
                ),
                operation="checkout pinned commit",
                local=True,
            )
            for name in ("FETCH_HEAD", "ORIG_HEAD"):
                (workspace / ".git" / name).unlink(missing_ok=True)
            await self._verify_workspace(workspace, commit, depth)
            workspace_ms = int((time.monotonic() - workspace_started) * 1000)
            archive_started = time.monotonic()
            archive, size = await create_archive(self.settings, self.runner, root)
            archive_ms = int((time.monotonic() - archive_started) * 1000)
            await self.repositories.delete_snapshot(project.id, snapshot)
            snapshot = ""
            return WorkspaceResult(
                root=root,
                archive=archive,
                commit=commit,
                fetch_duration_ms=fetch_ms,
                workspace_duration_ms=workspace_ms,
                archive_duration_ms=archive_ms,
                archive_size_bytes=size,
            )
        except BaseException:
            await cleanup_workspace(root)
            raise
        finally:
            if snapshot:
                await self.repositories.delete_snapshot(project.id, snapshot)

    async def _verify_workspace(self, workspace: Path, commit: str, depth: int) -> None:
        head = await run_git(
            self.settings,
            self.runner,
            ("-C", str(workspace), "rev-parse", "HEAD"),
            operation="verify workspace commit",
            local=True,
        )
        if head != commit:
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
