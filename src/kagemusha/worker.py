from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import anyio

from .cache import CacheManager
from .config import Settings
from .database import Database, utc_now
from .errors import ErrorCode, ServiceError
from .jobs import JobQueue
from .process import ProcessRunner
from .refs import resolve_ref, workspace_branch
from .repository import Repository, RepositoryManager
from .workspace import WorkspaceManager, cleanup_workspace


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.database_path)
        self.queue = JobQueue(self.database, settings.job_lease_seconds)
        self.runner = ProcessRunner(settings)
        self.repositories = RepositoryManager(settings, self.runner)
        self.workspaces = WorkspaceManager(settings, self.runner, self.repositories)
        self.caches = CacheManager(
            self.database,
            settings,
            self.runner,
            self.repositories,
            self.workspaces,
        )
        self.worker_id = f"worker-{uuid.uuid4().hex}"

    async def startup(self) -> None:
        self.settings.ensure_directories()
        await self.database.migrate()
        await self.repositories.startup_recovery()
        await self.database.execute(
            "UPDATE working_copies SET status='ready' WHERE status='downloading'"
        )
        await self._recover_artifacts()
        await self.caches.recover_publications()
        self._write_health()

    async def run(self, *, once: bool = False) -> None:
        await self.startup()
        while True:
            self._write_health()
            job = await self.queue.claim(self.worker_id)
            if job is None:
                await self.cleanup_expired_copies()
                if once:
                    return
                await asyncio.sleep(self.settings.worker_poll_seconds)
                continue
            await self._execute(job)
            self._write_health()
            if once:
                return

    async def _execute(self, job: dict[str, Any]) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job["id"]))
        operation = asyncio.create_task(self._dispatch(job["type"], job["payload"]))
        cancellation = asyncio.create_task(self._watch_cancellation(job["id"]))
        try:
            done, _pending = await asyncio.wait(
                {operation, cancellation}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation in done and cancellation.result():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                return
            cancellation.cancel()
            result = await operation
            await self.queue.succeed(job["id"], result)
        except asyncio.CancelledError:
            await self.queue.fail(job["id"], "Worker cancelled")
            raise
        except ServiceError as error:
            await self.queue.fail(job["id"], f"{error.code}: {error.message}")
        except Exception as error:
            logging.getLogger("kagemusha").exception(
                "Worker job failed", extra={"job_id": job["id"]}
            )
            await self.queue.fail(job["id"], str(error))
        finally:
            heartbeat.cancel()
            cancellation.cancel()
            await asyncio.gather(heartbeat, cancellation, return_exceptions=True)

    async def _heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(max(10, self.settings.job_lease_seconds // 3))
            self._write_health()
            if not await self.queue.heartbeat(job_id, self.worker_id):
                return

    def _write_health(self) -> None:
        path = self.settings.jobs_dir / "worker-health"
        path.write_text(utc_now(), encoding="utf-8")

    async def _watch_cancellation(self, job_id: str) -> bool:
        while True:
            await asyncio.sleep(min(1.0, self.settings.worker_poll_seconds))
            if (await self.queue.get(job_id))["status"] == "cancelled":
                return True

    async def _dispatch(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if job_type == "repository.fetch":
            repository = await self._repository(payload["repository_id"])
            await self.database.execute(
                "UPDATE repositories SET status='syncing', updated_at=? WHERE id=?",
                (utc_now(), repository.id),
            )
            try:
                duration = await self.repositories.synchronize(
                    repository, self._credential(repository)
                )
            except BaseException:
                await self.database.execute(
                    "UPDATE repositories SET status='error', updated_at=? WHERE id=?",
                    (utc_now(), repository.id),
                )
                raise
            now = utc_now()
            await self.database.execute(
                "UPDATE repositories SET status='ready', last_fetched_at=?, updated_at=? WHERE id=?",
                (now, now, repository.id),
            )
            return {"repository_id": repository.id, "fetch_duration_ms": duration}
        if job_type == "repository.delete":
            repository_id = payload["repository_id"]
            await self.repositories.delete_mirror(repository_id)
            await self.database.execute(
                "DELETE FROM repositories WHERE id=?", (repository_id,)
            )
            return {"repository_id": repository_id, "deleted": True}
        if job_type == "copy.create":
            return await self._create_copy(payload["copy_id"])
        if job_type == "cache.discover":
            repository = await self._repository(payload["repository_id"])
            targets = await self.caches.discover(
                repository, self._credential(repository)
            )
            return {
                "repository_id": repository.id,
                "target_ids": [t["id"] for t in targets],
            }
        if job_type in {"cache.seed", "cache.full_rebuild"}:
            ids = payload.get("repository_ids")
            repositories = await self._repositories(ids)
            credentials = {
                repository.id: self._credential(repository)
                for repository in repositories
            }
            requested_group = None
            if group_id := payload.get("group_id"):
                requested_group = await self.database.fetch_one(
                    "SELECT * FROM cache_groups WHERE id=?", (group_id,)
                )
                if requested_group is None:
                    raise KeyError(group_id)
            providers = set(payload["providers"]) if payload.get("providers") else None
            if requested_group is not None:
                providers = {
                    "gradle"
                    if requested_group["provider"] == "gradle-wrapper"
                    else requested_group["provider"]
                }
            return await self.caches.seed(
                repositories,
                credentials,
                providers=providers,
                mode=payload.get("mode", "incremental"),
                requested_group=requested_group,
            )
        if job_type == "cache.verify":
            verified = await self.caches.verify_snapshot(payload["snapshot_id"])
            return {"snapshot_id": payload["snapshot_id"], "verified": verified}
        if job_type == "cache.gc":
            return await self.caches.gc()
        raise ValueError(f"Unsupported job type: {job_type}")

    async def _create_copy(self, copy_id: str) -> dict[str, Any]:
        copy = await self.database.fetch_one(
            "SELECT * FROM working_copies WHERE id=?", (copy_id,)
        )
        if copy is None:
            raise KeyError(copy_id)
        await self.database.execute(
            "UPDATE working_copies SET status='creating', error=NULL WHERE id=?",
            (copy_id,),
        )
        repository = await self._repository(copy["repository_id"])
        remote_ref = resolve_ref(copy["requested_ref"], repository.git_provider)
        branch, tracks = workspace_branch(
            copy["requested_ref"], remote_ref, copy["branch_prefix"]
        )
        try:
            result = await self.workspaces.create(
                repository,
                remote_ref,
                branch,
                tracks,
                self._credential(repository),
                copy["depth"],
                copy["mode"],
            )
            if result.archive is None:
                raise RuntimeError("Working Copy archive was not created")
            destination = self.settings.copies_dir / f"{copy_id}.tar.zst"
            destination.parent.mkdir(parents=True, exist_ok=True)
            await anyio.to_thread.run_sync(shutil.move, result.archive, destination)
            digest = await anyio.to_thread.run_sync(_sha256, destination)
            await self.database.execute(
                "UPDATE working_copies SET status='ready', commit_sha=?, artifact_path=?, "
                "artifact_sha256=?, artifact_size=?, error=NULL WHERE id=?",
                (
                    result.commit,
                    str(destination),
                    digest,
                    destination.stat().st_size,
                    copy_id,
                ),
            )
            return {"copy_id": copy_id, "commit_sha": result.commit, "branch": branch}
        except BaseException as error:
            await self.database.execute(
                "UPDATE working_copies SET status='failed', error=? WHERE id=?",
                (str(error)[-8192:], copy_id),
            )
            raise
        finally:
            if "result" in locals():
                await cleanup_workspace(result.root)

    async def cleanup_expired_copies(self) -> None:
        now = utc_now()
        rows = await self.database.fetch_all(
            "SELECT id, artifact_path FROM working_copies "
            "WHERE status='ready' AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        for row in rows:
            changed = await self.database.execute(
                "UPDATE working_copies SET status='deleting' WHERE id=? AND status='ready'",
                (row["id"],),
            )
            if changed != 1:
                continue
            if row["artifact_path"]:
                Path(row["artifact_path"]).unlink(missing_ok=True)
            await self.database.execute(
                "DELETE FROM working_copies WHERE id=? AND status='deleting'",
                (row["id"],),
            )

    async def _recover_artifacts(self) -> None:
        referenced = {
            Path(row["artifact_path"])
            for row in await self.database.fetch_all(
                "SELECT artifact_path FROM working_copies WHERE artifact_path IS NOT NULL"
            )
        }
        for artifact in self.settings.copies_dir.glob("*.tar.zst"):
            if artifact not in referenced:
                artifact.unlink(missing_ok=True)

    async def _repository(self, repository_id: str) -> Repository:
        row = await self.database.fetch_one(
            "SELECT * FROM repositories WHERE id=?", (repository_id,)
        )
        if row is None:
            raise KeyError(repository_id)
        return _repository_model(row)

    async def _repositories(self, ids: list[str] | None) -> list[Repository]:
        if ids is None:
            rows = await self.database.fetch_all(
                "SELECT * FROM repositories ORDER BY id"
            )
        else:
            rows = []
            for repository_id in ids:
                row = await self.database.fetch_one(
                    "SELECT * FROM repositories WHERE id=?", (repository_id,)
                )
                if row is None:
                    raise KeyError(repository_id)
                rows.append(row)
        return [_repository_model(row) for row in rows]

    def _credential(self, repository: Repository) -> str | None:
        token = self.settings.credential(repository.credential_ref)
        if repository.credential_ref is not None and token is None:
            raise ServiceError(
                code=ErrorCode.GIT_AUTH_FAILED,
                message="Configured Git credential is unavailable",
            )
        return token


def _repository_model(row: dict[str, Any]) -> Repository:
    return Repository(
        id=row["id"],
        name=row["name"],
        remote_url=row["remote_url"],
        git_provider=row["git_provider"],
        default_ref=row["default_ref"],
        credential_ref=row["credential_ref"],
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
