from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send

from . import __version__
from .auth import ApiAuthenticator
from .config import Settings
from .database import Database, json_text, utc_now
from .errors import ErrorCode, ServiceError
from .git import (
    canonicalize_repository_url,
    validate_repository_url,
    validate_resolved_addresses,
)
from .jobs import JobQueue
from .log import log_request
from .models import (
    CacheJobRequest,
    CopyRequest,
    CopyResponse,
    JobResponse,
    RepositoryRequest,
    RepositoryResponse,
    SeedTargetRequest,
    SeedTargetResponse,
)
from .process import ProcessRunner
from .refs import resolve_ref, workspace_branch
from .repository import RepositoryManager


def _error_response(request: Request, error: ServiceError) -> JSONResponse:
    if hasattr(request.state, "log_fields"):
        request.state.log_fields["error_code"] = error.code
    response = JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
    )
    if error.code is ErrorCode.UNAUTHORIZED:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


class ArtifactResponse(FileResponse):
    def __init__(
        self,
        *args: Any,
        database: Database,
        resource_id: str,
        table: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.database = database
        self.resource_id = resource_id
        self.table = table

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self.table == "working_copies":
                await self.database.execute(
                    "UPDATE working_copies SET status='ready' "
                    "WHERE id=? AND status='downloading'",
                    (self.resource_id,),
                )
            elif self.table == "cache_snapshots":
                await self.database.execute(
                    "UPDATE cache_snapshots SET download_count=MAX(download_count-1, 0) "
                    "WHERE id=?",
                    (self.resource_id,),
                )


def create_app(settings: Settings) -> FastAPI:
    if settings.api_token is None:
        raise ValueError("KAGEMUSHA_API_TOKEN is required for the API process")
    database = Database(settings.database_path)
    queue = JobQueue(database, settings.job_lease_seconds)
    repositories = RepositoryManager(settings, ProcessRunner(settings))
    authenticate = ApiAuthenticator(settings.api_token)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.ensure_directories()
        await database.migrate()
        yield

    app = FastAPI(
        title="Kagemusha",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        max_body_size=settings.max_request_body_bytes,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.queue = queue
    app.middleware("http")(log_request)

    @app.exception_handler(ServiceError)
    async def handle_service_error(
        request: Request, error: ServiceError
    ) -> JSONResponse:
        return _error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return _error_response(request, ServiceError(ErrorCode.INVALID_REQUEST))

    @app.exception_handler(KeyError)
    async def missing_resource(request: Request, _error: KeyError) -> JSONResponse:
        return _error_response(request, ServiceError(ErrorCode.NOT_FOUND))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        code = {
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.METHOD_NOT_ALLOWED,
            413: ErrorCode.REQUEST_TOO_LARGE,
        }.get(error.status_code, ErrorCode.INVALID_REQUEST)
        return _error_response(request, ServiceError(code))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _error: Exception) -> JSONResponse:
        logging.getLogger("kagemusha").exception("Unhandled request error")
        return _error_response(request, ServiceError(ErrorCode.INTERNAL_ERROR))

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        if not await database.integrity_check():
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR, "Database integrity check failed"
            )
        return {"status": "ok"}

    router = APIRouter(dependencies=[Depends(authenticate)])

    @router.put("/repositories/{repository_id}", response_model=RepositoryResponse)
    async def put_repository(
        repository_id: str, payload: RepositoryRequest
    ) -> RepositoryResponse:
        repositories.validate_id(repository_id)
        remote_url = canonicalize_repository_url(payload.remote_url)
        validate_repository_url(remote_url, settings)
        await validate_resolved_addresses(remote_url)
        resolve_ref(payload.default_ref, payload.git_provider)
        existing = await database.fetch_one(
            "SELECT * FROM repositories WHERE id=?", (repository_id,)
        )
        now = utc_now()
        await database.execute(
            "INSERT INTO repositories(id, name, remote_url, git_provider, default_ref, "
            "credential_ref, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'registered', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, remote_url=excluded.remote_url, "
            "git_provider=excluded.git_provider, default_ref=excluded.default_ref, "
            "credential_ref=excluded.credential_ref, "
            "status=CASE WHEN repositories.remote_url != excluded.remote_url OR "
            "repositories.credential_ref IS NOT excluded.credential_ref THEN 'registered' "
            "ELSE repositories.status END, updated_at=excluded.updated_at",
            (
                repository_id,
                payload.name,
                remote_url,
                payload.git_provider,
                payload.default_ref,
                payload.credential_ref,
                now,
                now,
            ),
        )
        row = await _repository_row(database, repository_id)
        if existing and (
            existing["remote_url"] != remote_url
            or existing["credential_ref"] != payload.credential_ref
        ):
            await queue.enqueue("repository.fetch", {"repository_id": repository_id})
        return RepositoryResponse.model_validate(row)

    @router.get("/repositories")
    async def list_repositories(
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        rows = await database.fetch_all(
            "SELECT * FROM repositories ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
        )
        return {"items": rows, "limit": limit, "offset": offset}

    @router.get("/repositories/{repository_id}", response_model=RepositoryResponse)
    async def get_repository(repository_id: str) -> RepositoryResponse:
        return RepositoryResponse.model_validate(
            await _repository_row(database, repository_id)
        )

    @router.delete("/repositories/{repository_id}", status_code=202)
    async def delete_repository(repository_id: str) -> JobResponse:
        await _repository_row(database, repository_id)
        job = await queue.enqueue("repository.delete", {"repository_id": repository_id})
        return JobResponse.model_validate(job)

    @router.post("/repositories/{repository_id}/fetch", status_code=202)
    async def fetch_repository(repository_id: str) -> JobResponse:
        await _repository_row(database, repository_id)
        return JobResponse.model_validate(
            await queue.enqueue("repository.fetch", {"repository_id": repository_id})
        )

    @router.post("/repositories/{repository_id}/copies", status_code=202)
    async def create_copy(repository_id: str, payload: CopyRequest) -> dict[str, Any]:
        repository = await _repository_row(database, repository_id)
        remote_ref = resolve_ref(payload.ref, repository["git_provider"])
        workspace_branch(payload.ref, remote_ref, payload.branch_prefix)
        ttl = payload.ttl_seconds or settings.copy_default_ttl_seconds
        if ttl > settings.copy_max_ttl_seconds:
            raise ServiceError(
                ErrorCode.INVALID_REQUEST, "Copy TTL exceeds configured maximum"
            )
        copy_id = f"copy-{uuid.uuid4().hex}"
        created_at = datetime.now(UTC)
        expires_at = (created_at + timedelta(seconds=ttl)).isoformat()
        await database.execute(
            "INSERT INTO working_copies(id, repository_id, requested_ref, mode, "
            "branch_prefix, depth, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (
                copy_id,
                repository_id,
                payload.ref,
                payload.mode,
                payload.branch_prefix,
                payload.depth,
                created_at.isoformat(),
                expires_at,
            ),
        )
        job = await queue.enqueue("copy.create", {"copy_id": copy_id})
        copy = await _copy_row(database, copy_id)
        return {
            "copy": CopyResponse.model_validate(copy).model_dump(),
            "job": JobResponse.model_validate(job).model_dump(),
        }

    @router.get("/copies/{copy_id}", response_model=CopyResponse)
    async def get_copy(copy_id: str) -> CopyResponse:
        return CopyResponse.model_validate(await _copy_row(database, copy_id))

    @router.get("/copies/{copy_id}/download")
    async def download_copy(copy_id: str) -> FileResponse:
        changed = await database.execute(
            "UPDATE working_copies SET status='downloading' WHERE id=? AND status='ready'",
            (copy_id,),
        )
        if changed != 1:
            copy = await _copy_row(database, copy_id)
            raise ServiceError(
                ErrorCode.RESOURCE_NOT_READY,
                f"Working Copy is {copy['status']}",
            )
        copy = await _copy_artifact_row(database, copy_id)
        artifact = Path(copy["artifact_path"])
        if not artifact.is_file():
            await database.execute(
                "UPDATE working_copies SET status='failed', error='Artifact is missing' WHERE id=?",
                (copy_id,),
            )
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR, "Working Copy artifact is missing"
            )
        return ArtifactResponse(
            artifact,
            media_type="application/zstd",
            filename=f"{copy_id}.tar.zst",
            headers={"X-Artifact-SHA256": copy["artifact_sha256"]},
            database=database,
            resource_id=copy_id,
            table="working_copies",
        )

    @router.delete("/copies/{copy_id}", status_code=204)
    async def delete_copy(copy_id: str) -> None:
        copy = await _copy_artifact_row(database, copy_id)
        changed = await database.execute(
            "UPDATE working_copies SET status='deleting' "
            "WHERE id=? AND status IN ('ready', 'failed')",
            (copy_id,),
        )
        if changed != 1:
            raise ServiceError(ErrorCode.CONFLICT, "Working Copy is busy")
        if copy["artifact_path"]:
            Path(copy["artifact_path"]).unlink(missing_ok=True)
        await database.execute("DELETE FROM working_copies WHERE id=?", (copy_id,))

    @router.get("/repositories/{repository_id}/seed-targets")
    async def list_seed_targets(
        repository_id: str,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        await _repository_row(database, repository_id)
        rows = await database.fetch_all(
            "SELECT * FROM seed_targets WHERE repository_id=? "
            "ORDER BY relative_path, provider LIMIT ? OFFSET ?",
            (repository_id, limit, offset),
        )
        return {
            "items": [_target_response(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    @router.put(
        "/repositories/{repository_id}/seed-targets/{target_id}",
        response_model=SeedTargetResponse,
    )
    async def put_seed_target(
        repository_id: str, target_id: str, payload: SeedTargetRequest
    ) -> SeedTargetResponse:
        await _repository_row(database, repository_id)
        relative = _safe_relative_path(payload.relative_path)
        existing_target = await database.fetch_one(
            "SELECT repository_id FROM seed_targets WHERE id=?", (target_id,)
        )
        if existing_target and existing_target["repository_id"] != repository_id:
            raise ServiceError(ErrorCode.CONFLICT, "Seed Target ID is already in use")
        now = utc_now()
        await database.execute(
            "INSERT INTO seed_targets(id, repository_id, relative_path, provider, enabled, "
            "config_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET relative_path=excluded.relative_path, "
            "provider=excluded.provider, enabled=excluded.enabled, "
            "config_json=excluded.config_json, updated_at=excluded.updated_at",
            (
                target_id,
                repository_id,
                relative,
                payload.provider,
                int(payload.enabled),
                json_text(payload.config),
                now,
                now,
            ),
        )
        row = await database.fetch_one(
            "SELECT * FROM seed_targets WHERE id=?", (target_id,)
        )
        assert row is not None
        return SeedTargetResponse.model_validate(_target_response(row))

    @router.delete(
        "/repositories/{repository_id}/seed-targets/{target_id}", status_code=204
    )
    async def delete_seed_target(repository_id: str, target_id: str) -> None:
        if (
            await database.execute(
                "DELETE FROM seed_targets WHERE id=? AND repository_id=?",
                (target_id, repository_id),
            )
            != 1
        ):
            raise KeyError(target_id)

    @router.post("/repositories/{repository_id}/seed-targets/discover", status_code=202)
    async def discover_seed_targets(repository_id: str) -> JobResponse:
        await _repository_row(database, repository_id)
        return JobResponse.model_validate(
            await queue.enqueue("cache.discover", {"repository_id": repository_id})
        )

    @router.post("/cache-jobs", status_code=202)
    async def create_cache_job(payload: CacheJobRequest) -> JobResponse:
        job_type = "cache.full_rebuild" if payload.mode == "full" else "cache.seed"
        return JobResponse.model_validate(
            await queue.enqueue(job_type, payload.model_dump())
        )

    @router.get("/cache-jobs")
    async def list_cache_jobs(
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        jobs = await queue.list_jobs(
            job_type_prefix="cache.", limit=limit, offset=offset
        )
        return {"items": jobs, "limit": limit, "offset": offset}

    @router.get("/cache-jobs/{job_id}", response_model=JobResponse)
    async def get_cache_job(job_id: str) -> JobResponse:
        job = await queue.get(job_id)
        if not job["type"].startswith("cache."):
            raise KeyError(job_id)
        return JobResponse.model_validate(job)

    @router.post("/cache-jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_cache_job(job_id: str) -> JobResponse:
        await queue.cancel(job_id)
        return JobResponse.model_validate(await queue.get(job_id))

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: str) -> JobResponse:
        return JobResponse.model_validate(await queue.get(job_id))

    @router.get("/cache-groups")
    async def list_cache_groups(
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        return {
            "items": await database.fetch_all(
                "SELECT * FROM cache_groups ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset),
            ),
            "limit": limit,
            "offset": offset,
        }

    @router.get("/cache-groups/{group_id}")
    async def get_cache_group(group_id: str) -> dict[str, Any]:
        row = await database.fetch_one(
            "SELECT * FROM cache_groups WHERE id=?", (group_id,)
        )
        if row is None:
            raise KeyError(group_id)
        return row

    @router.get("/cache-snapshots")
    async def list_cache_snapshots(
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        rows = await database.fetch_all(
            "SELECT id, cache_group_id, status, artifact_sha256, artifact_size, pinned, "
            "created_at, published_at FROM cache_snapshots ORDER BY published_at DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return {"items": rows, "limit": limit, "offset": offset}

    @router.get("/cache-snapshots/{snapshot_id}")
    async def get_cache_snapshot(snapshot_id: str) -> dict[str, Any]:
        return await _snapshot_row(database, snapshot_id, public=True)

    @router.get("/cache-snapshots/{snapshot_id}/download")
    async def download_cache_snapshot(snapshot_id: str) -> FileResponse:
        if (
            await database.execute(
                "UPDATE cache_snapshots SET download_count=download_count+1 "
                "WHERE id=? AND status='published'",
                (snapshot_id,),
            )
            != 1
        ):
            raise KeyError(snapshot_id)
        row = await _snapshot_row(database, snapshot_id, public=False)
        return ArtifactResponse(
            row["artifact_path"],
            media_type="application/zstd",
            filename=f"{snapshot_id}.tar.zst",
            headers={"X-Artifact-SHA256": row["artifact_sha256"]},
            database=database,
            resource_id=snapshot_id,
            table="cache_snapshots",
        )

    @router.post("/cache-snapshots/{snapshot_id}/pin")
    async def pin_cache_snapshot(snapshot_id: str) -> dict[str, Any]:
        if (
            await database.execute(
                "UPDATE cache_snapshots SET pinned=1 WHERE id=?", (snapshot_id,)
            )
            != 1
        ):
            raise KeyError(snapshot_id)
        return await _snapshot_row(database, snapshot_id, public=True)

    @router.post("/cache-snapshots/{snapshot_id}/unpin")
    async def unpin_cache_snapshot(snapshot_id: str) -> dict[str, Any]:
        if (
            await database.execute(
                "UPDATE cache_snapshots SET pinned=0 WHERE id=?", (snapshot_id,)
            )
            != 1
        ):
            raise KeyError(snapshot_id)
        return await _snapshot_row(database, snapshot_id, public=True)

    @router.post("/cache-groups/{group_id}/verify", status_code=202)
    async def verify_cache_group(group_id: str) -> JobResponse:
        group = await database.fetch_one(
            "SELECT * FROM cache_groups WHERE id=?", (group_id,)
        )
        if group is None or group["active_snapshot_id"] is None:
            raise ServiceError(ErrorCode.RESOURCE_NOT_READY)
        return JobResponse.model_validate(
            await queue.enqueue(
                "cache.verify", {"snapshot_id": group["active_snapshot_id"]}
            )
        )

    @router.post("/cache-groups/{group_id}/rebuild", status_code=202)
    async def rebuild_cache_group(group_id: str) -> JobResponse:
        group = await database.fetch_one(
            "SELECT * FROM cache_groups WHERE id=?", (group_id,)
        )
        if group is None:
            raise KeyError(group_id)
        return JobResponse.model_validate(
            await queue.enqueue(
                "cache.full_rebuild", {"group_id": group_id, "mode": "full"}
            )
        )

    @router.post("/maintenance/gc", status_code=202)
    async def maintenance_gc() -> JobResponse:
        return JobResponse.model_validate(await queue.enqueue("cache.gc", {}))

    app.include_router(router)
    return app


async def _repository_row(database: Database, repository_id: str) -> dict[str, Any]:
    row = await database.fetch_one(
        "SELECT * FROM repositories WHERE id=?", (repository_id,)
    )
    if row is None:
        raise KeyError(repository_id)
    return row


async def _copy_row(database: Database, copy_id: str) -> dict[str, Any]:
    row = await database.fetch_one(
        "SELECT id, repository_id, requested_ref, commit_sha, mode, branch_prefix, depth, "
        "status, artifact_sha256, artifact_size, created_at, expires_at, error "
        "FROM working_copies WHERE id=?",
        (copy_id,),
    )
    if row is None:
        raise KeyError(copy_id)
    return row


async def _copy_artifact_row(database: Database, copy_id: str) -> dict[str, Any]:
    row = await database.fetch_one(
        "SELECT * FROM working_copies WHERE id=?", (copy_id,)
    )
    if row is None:
        raise KeyError(copy_id)
    return row


async def _snapshot_row(
    database: Database, snapshot_id: str, *, public: bool
) -> dict[str, Any]:
    row = await database.fetch_one(
        "SELECT * FROM cache_snapshots WHERE id=?", (snapshot_id,)
    )
    if row is None:
        raise KeyError(snapshot_id)
    if public:
        row.pop("artifact_path", None)
        row.pop("manifest_path", None)
        row.pop("download_count", None)
    return row


def _target_response(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    result["config"] = json.loads(result.pop("config_json"))
    return result


def _safe_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ServiceError(ErrorCode.INVALID_REQUEST, "Invalid relative path")
    normalized = path.as_posix().rstrip("/") or "."
    return normalized
