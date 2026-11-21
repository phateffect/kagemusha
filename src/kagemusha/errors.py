from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
    PROJECT_ALREADY_EXISTS = "PROJECT_ALREADY_EXISTS"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    INSUFFICIENT_STORAGE = "INSUFFICIENT_STORAGE"
    GIT_TIMEOUT = "GIT_TIMEOUT"
    ARCHIVE_TOO_LARGE = "ARCHIVE_TOO_LARGE"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    status_code: int
    message: str


ERROR_DEFINITIONS: Mapping[ErrorCode, ErrorDefinition] = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: ErrorDefinition(400, "Invalid request"),
        ErrorCode.INVALID_REF: ErrorDefinition(400, "Invalid Git ref"),
        ErrorCode.NOT_FOUND: ErrorDefinition(404, "Resource not found"),
        ErrorCode.METHOD_NOT_ALLOWED: ErrorDefinition(405, "Method not allowed"),
        ErrorCode.UNAUTHORIZED: ErrorDefinition(401, "Invalid service API token"),
        ErrorCode.GIT_AUTH_FAILED: ErrorDefinition(401, "Git authentication failed"),
        ErrorCode.GIT_ACCESS_DENIED: ErrorDefinition(
            403, "Git repository access denied"
        ),
        ErrorCode.GIT_REMOTE_ERROR: ErrorDefinition(502, "Git remote operation failed"),
        ErrorCode.REF_NOT_FOUND: ErrorDefinition(
            404, "Requested Git ref was not found"
        ),
        ErrorCode.PROJECT_ALREADY_EXISTS: ErrorDefinition(
            409, "Project already exists"
        ),
        ErrorCode.PROJECT_NOT_FOUND: ErrorDefinition(404, "Project not found"),
        ErrorCode.INSUFFICIENT_STORAGE: ErrorDefinition(507, "Insufficient storage"),
        ErrorCode.GIT_TIMEOUT: ErrorDefinition(504, "Git operation timed out"),
        ErrorCode.ARCHIVE_TOO_LARGE: ErrorDefinition(
            413, "Workspace archive is too large"
        ),
        ErrorCode.REQUEST_TOO_LARGE: ErrorDefinition(413, "Request body is too large"),
        ErrorCode.INTERNAL_ERROR: ErrorDefinition(500, "Internal service error"),
    }
)


class ServiceError(Exception):
    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        definition = ERROR_DEFINITIONS[code]
        resolved_message = definition.message if message is None else message
        super().__init__(resolved_message)
        self.status_code = definition.status_code
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
