from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credential(ApiModel):
    type: Literal["token"]
    token: SecretStr = Field(min_length=1, max_length=4096)


class ProjectRequest(ApiModel):
    repository: str = Field(min_length=1, max_length=2048)
    provider: Literal["github", "gitlab"]
    credential: Credential


class CopyRequest(ApiModel):
    ref: str = Field(min_length=1, max_length=255)
    depth: int | None = Field(default=None, strict=True, ge=0, le=1000)
    credential: Credential


class ProjectResponse(ApiModel):
    project_id: str
    repository: str
    provider: Literal["github", "gitlab"]
    default_branch: str
