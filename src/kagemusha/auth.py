from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ErrorCode, ServiceError

_bearer = HTTPBearer(auto_error=False)


class ApiAuthenticator:
    def __init__(self, api_token: str) -> None:
        self._api_token = api_token

    def __call__(
        self,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        valid = credentials is not None and secrets.compare_digest(
            credentials.credentials, self._api_token
        )
        if not valid:
            raise ServiceError(ErrorCode.UNAUTHORIZED)
