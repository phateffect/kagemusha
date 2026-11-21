from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send

from . import __version__
from .auth import ApiAuthenticator
from .config import Settings
from .errors import ErrorCode, ServiceError
from .log import log_request
from .models import CopyRequest, ProjectRequest, ProjectResponse
from .process import ProcessRunner
from .refs import resolve_ref
from .repository import RepositoryManager
from .workspace import WorkspaceManager, cleanup_workspace


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


class ArchiveResponse(FileResponse):
    def __init__(self, *args: Any, cleanup_root: Path, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cleanup_root = cleanup_root

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await cleanup_workspace(self.cleanup_root)


def create_app(settings: Settings) -> FastAPI:
    runner = ProcessRunner(settings)
    repositories = RepositoryManager(settings, runner)
    workspaces = WorkspaceManager(settings, runner, repositories)
    authenticate = ApiAuthenticator(settings.api_token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_directories()
        await repositories.startup_recovery()
        yield

    app = FastAPI(
        title="Kagemusha",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        max_body_size=settings.max_request_body_bytes,
    )
    app.state.settings = settings
    app.state.runner = runner
    app.state.repositories = repositories
    app.state.workspaces = workspaces
    app.middleware("http")(log_request)

    @app.exception_handler(ServiceError)
    async def handle_service_error(
        request: Request, error: ServiceError
    ) -> JSONResponse:
        return _error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return _error_response(request, ServiceError(ErrorCode.INVALID_REQUEST))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        if error.status_code == 413:
            code = ErrorCode.REQUEST_TOO_LARGE
        elif error.status_code == 404:
            code = ErrorCode.NOT_FOUND
        elif error.status_code == 405:
            code = ErrorCode.METHOD_NOT_ALLOWED
        elif error.status_code < 500:
            code = ErrorCode.INVALID_REQUEST
        else:
            code = ErrorCode.INTERNAL_ERROR
        return _error_response(request, ServiceError(code))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _error: Exception) -> JSONResponse:
        logging.getLogger("kagemusha").exception(
            "Unhandled request error",
            extra={
                "request_id": getattr(request.state, "log_fields", {}).get("request_id")
            },
        )
        return _error_response(request, ServiceError(ErrorCode.INTERNAL_ERROR))

    @app.get("/healthz")
    async def health(request: Request) -> dict[str, str]:
        request.state.log_fields["operation"] = "health"
        return {"status": "ok"}

    @app.post(
        "/projects/{project_id}",
        response_model=ProjectResponse,
        status_code=201,
        dependencies=[Depends(authenticate)],
    )
    async def initialize_project(
        project_id: str, payload: ProjectRequest, request: Request
    ) -> ProjectResponse:
        fields = request.state.log_fields
        fields.update(
            {
                "project_id": project_id,
                "operation": "initialize_project",
                "provider": payload.provider,
            }
        )
        project = await repositories.create(
            project_id,
            payload.repository,
            payload.provider,
            payload.credential.token.get_secret_value(),
        )
        return ProjectResponse(
            project_id=project.id,
            repository=project.repository,
            provider=project.provider,
            default_branch=project.default_branch,
        )

    @app.post(
        "/projects/{project_id}/copies",
        dependencies=[Depends(authenticate)],
    )
    async def create_copy(
        project_id: str, payload: CopyRequest, request: Request
    ) -> FileResponse:
        fields = request.state.log_fields
        fields.update(
            {
                "project_id": project_id,
                "operation": "create_copy",
                "ref": payload.ref,
                "depth": payload.depth,
            }
        )
        project = await repositories.get(project_id)
        ref = resolve_ref(payload.ref, project.provider)
        depth = settings.default_depth if payload.depth is None else payload.depth
        fields.update({"provider": project.provider, "depth": depth})
        result = await workspaces.create(
            project, ref, payload.credential.token.get_secret_value(), depth
        )
        fields.update(
            {
                "resolved_commit": result.commit,
                "fetch_duration_ms": result.fetch_duration_ms,
                "workspace_duration_ms": result.workspace_duration_ms,
                "archive_duration_ms": result.archive_duration_ms,
                "archive_size_bytes": result.archive_size_bytes,
            }
        )
        return ArchiveResponse(
            result.archive,
            media_type="application/zstd",
            filename="workspace.tar.zst",
            headers={
                "Content-Length": str(result.archive_size_bytes),
                "X-Git-Commit": result.commit,
                "X-Git-Ref": payload.ref,
                "X-Git-Depth": str(depth),
            },
            cleanup_root=result.root,
        )

    return app
