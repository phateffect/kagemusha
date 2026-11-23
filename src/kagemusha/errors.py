from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_REF = "INVALID_REF"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    UNAUTHORIZED = "UNAUTHORIZED"
    GIT_AUTH_FAILED = "GIT_AUTH_FAILED"
    GIT_ACCESS_DENIED = "GIT_ACCESS_DENIED"
    GIT_REMOTE_ERROR = "GIT_REMOTE_ERROR"
    REF_NOT_FOUND = "REF_NOT_FOUND"
    CONFLICT = "CONFLICT"
    RESOURCE_NOT_READY = "RESOURCE_NOT_READY"
    INSUFFICIENT_STORAGE = "INSUFFICIENT_STORAGE"
    GIT_TIMEOUT = "GIT_TIMEOUT"
    ARCHIVE_TOO_LARGE = "ARCHIVE_TOO_LARGE"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ERROR_DEFINITIONS: Mapping[ErrorCode, tuple[int, str]] = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: (400, "Invalid request"),
        ErrorCode.INVALID_REF: (400, "Invalid Git ref"),
        ErrorCode.NOT_FOUND: (404, "Resource not found"),
        ErrorCode.METHOD_NOT_ALLOWED: (405, "Method not allowed"),
        ErrorCode.UNAUTHORIZED: (401, "Invalid service API token"),
        ErrorCode.GIT_AUTH_FAILED: (401, "Git authentication failed"),
        ErrorCode.GIT_ACCESS_DENIED: (403, "Git repository access denied"),
        ErrorCode.GIT_REMOTE_ERROR: (502, "Git remote operation failed"),
        ErrorCode.REF_NOT_FOUND: (404, "Requested Git ref was not found"),
        ErrorCode.CONFLICT: (409, "Resource state conflict"),
        ErrorCode.RESOURCE_NOT_READY: (409, "Resource is not ready"),
        ErrorCode.INSUFFICIENT_STORAGE: (507, "Insufficient storage"),
        ErrorCode.GIT_TIMEOUT: (504, "Git operation timed out"),
        ErrorCode.ARCHIVE_TOO_LARGE: (413, "Workspace archive is too large"),
        ErrorCode.REQUEST_TOO_LARGE: (413, "Request body is too large"),
        ErrorCode.INTERNAL_ERROR: (500, "Internal service error"),
    }
)


class ServiceError(Exception):
    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        status_code, default_message = ERROR_DEFINITIONS[code]
        resolved_message = default_message if message is None else message
        super().__init__(resolved_message)
        self.status_code = status_code
        self.code = code
        self.message = resolved_message


class ProcessFailure(Exception):
    def __init__(self, operation: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{operation} exited with status {returncode}")
        self.stderr = stderr


def invalid_request(message: str | None = None) -> ServiceError:
    return ServiceError(ErrorCode.INVALID_REQUEST, message)


def invalid_ref(message: str | None = None) -> ServiceError:
    return ServiceError(ErrorCode.INVALID_REF, message)


def internal_error() -> ServiceError:
    return ServiceError(ErrorCode.INTERNAL_ERROR)
