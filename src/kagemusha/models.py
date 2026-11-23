from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryRequest(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    remote_url: str = Field(min_length=1, max_length=2048)
    git_provider: Literal["github", "gitlab", "generic"] = "generic"
    default_ref: str = Field(default="main", min_length=1, max_length=255)
    credential_ref: str | None = Field(default=None, min_length=1, max_length=255)


class RepositoryResponse(ApiModel):
    id: str
    name: str
    remote_url: str
    git_provider: str
    default_ref: str
    credential_ref: str | None
    status: str
    last_fetched_at: str | None
    created_at: str
    updated_at: str


class CopyRequest(ApiModel):
    ref: str = Field(min_length=1, max_length=255)
    depth: int = Field(default=1, strict=True, ge=0, le=1000)
    ttl_seconds: int | None = Field(default=None, strict=True, ge=60)
    mode: Literal["source", "git"] = "git"
    branch_prefix: str | None = Field(default=None, min_length=1, max_length=128)


class CopyResponse(ApiModel):
    id: str
    repository_id: str
    requested_ref: str
    commit_sha: str | None
    mode: str
    branch_prefix: str | None
    depth: int
    status: str
    artifact_sha256: str | None
    artifact_size: int | None
    created_at: str
    expires_at: str | None
    error: str | None


class JobResponse(ApiModel):
    id: str
    type: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    attempt: int
    worker_id: str | None
    lease_expires_at: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None


class SeedTargetRequest(ApiModel):
    relative_path: str = Field(min_length=1, max_length=1024)
    provider: Literal["gradle", "pnpm"]
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class SeedTargetResponse(ApiModel):
    id: str
    repository_id: str
    relative_path: str
    provider: str
    enabled: bool
    config: dict[str, Any]
    created_at: str
    updated_at: str


class CacheJobRequest(ApiModel):
    repository_ids: list[str] | None = None
    providers: list[Literal["gradle", "pnpm"]] | None = None
    mode: Literal["incremental", "full", "force"] = "incremental"
