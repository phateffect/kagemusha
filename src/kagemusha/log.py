from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from .errors import ErrorCode

_LOG_FIELDS = (
    "project_id",
    "operation",
    "provider",
    "ref",
    "resolved_commit",
    "depth",
    "fetch_duration_ms",
    "workspace_duration_ms",
    "archive_duration_ms",
    "archive_size_bytes",
)


async def log_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.monotonic()
    fields: dict[str, object] = {key: None for key in _LOG_FIELDS}
    fields["request_id"] = uuid.uuid4().hex
    request.state.log_fields = fields
    result = "success"
    try:
        response = await call_next(request)
        if response.status_code >= 400:
            result = "error"
            if fields.get("error_code") is None:
                fields["error_code"] = (
                    ErrorCode.INVALID_REQUEST
                    if response.status_code < 500
                    else ErrorCode.INTERNAL_ERROR
                )
        response.headers["X-Request-ID"] = str(fields["request_id"])
        return response
    except Exception:
        result = "error"
        fields["error_code"] = ErrorCode.INTERNAL_ERROR
        raise
    finally:
        route = request.scope.get("route")
        if fields["operation"] is None and route is not None:
            fields["operation"] = getattr(route, "path", None)
        fields.update(
            {
                "total_duration_ms": int((time.monotonic() - started) * 1000),
                "result": result,
            }
        )
        logging.getLogger("kagemusha").info(
            json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        )
