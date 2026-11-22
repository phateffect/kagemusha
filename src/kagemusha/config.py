from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, FilePath, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?$")


def _hosts(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        entries = frozenset(entry.strip().lower() for entry in value.split(","))
    else:
        entries = frozenset(str(entry).strip().lower() for entry in value)
    if not entries or "" in entries:
        raise ValueError("KAGEMUSHA_ALLOWED_GIT_HOSTS must contain explicit hosts")
    for entry in entries:
        if not _HOST.fullmatch(entry) or entry.endswith("."):
            raise ValueError(f"Invalid Git host allowlist entry: {entry}")
        if entry.startswith("["):
            closing_bracket = entry.index("]")
            try:
                ipaddress.IPv6Address(entry[1:closing_bracket])
            except ipaddress.AddressValueError as error:
                raise ValueError(
                    f"Invalid Git host allowlist entry: {entry}"
                ) from error
            _, separator, port_text = entry.rpartition("]:")
        else:
            _, separator, port_text = entry.rpartition(":")
        if separator and not 1 <= int(port_text) <= 65535:
            raise ValueError(f"Invalid Git host port: {entry}")
    return entries


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KAGEMUSHA_",
        env_ignore_empty=True,
        extra="forbid",
        frozen=True,
    )

    data_dir: Path
    api_token: str = Field(min_length=16, max_length=4096)
    allowed_git_hosts: Annotated[frozenset[str], NoDecode, BeforeValidator(_hosts)] = (
        frozenset({"github.com", "gitlab.com"})
    )
    max_concurrent_git: int = Field(default=4, ge=1)
    git_timeout_seconds: int = Field(default=300, ge=1)
    default_depth: int = Field(default=50, ge=0, le=1000)
    max_depth: int = Field(default=1000, ge=1, le=1000)
    zstd_level: int = Field(default=3, ge=1, le=19)
    zstd_threads: int = Field(default=1, ge=1, le=32)
    max_archive_bytes: int = Field(default=1024**3, ge=1)
    max_request_body_bytes: int = Field(default=64 * 1024, ge=1)
    temp_ttl_seconds: int = Field(default=3600, ge=1)
    temp_budget_bytes: int = Field(default=2 * 1024**3, ge=1)
    max_submodules: int = Field(default=100, ge=0, le=1000)
    max_submodule_depth: int = Field(default=10, ge=0, le=100)
    ca_bundle: FilePath | None = None

    @model_validator(mode="after")
    def validate_depths(self) -> Settings:
        if self.default_depth > self.max_depth:
            raise ValueError("Invalid depth configuration")
        if self.ca_bundle is not None:
            try:
                with self.ca_bundle.open("rb") as stream:
                    stream.read(1)
            except OSError as error:
                raise ValueError(
                    "KAGEMUSHA_CA_BUNDLE must name a readable file"
                ) from error
        return self

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def metadata_dir(self) -> Path:
        return self.data_dir / "metadata"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def askpass_path(self) -> Path:
        return Path(__file__).with_name("askpass.py")

    def ensure_directories(self) -> None:
        for directory in (
            self.projects_dir,
            self.metadata_dir,
            self.locks_dir,
            self.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
