from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .errors import (
    ErrorCode,
    ProcessFailure,
    ServiceError,
    invalid_request,
)
from .process import ProcessRunner, isolated_environment

_BLOCKED_HOSTS = {"metadata", "metadata.google.internal", "169.254.169.254"}


def validate_repository_url(repository: str, settings: Settings) -> None:
    try:
        parsed = urlsplit(repository)
        port = parsed.port
    except ValueError as error:
        raise invalid_request("Invalid repository URL") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path in {"", "/"}
    ):
        raise invalid_request("Repository must be an HTTPS URL without userinfo")
    host = parsed.hostname.lower()
    if host.endswith(".") or host in _BLOCKED_HOSTS:
        raise invalid_request("Repository host is not allowed")
    try:
        address = ipaddress.ip_address(host)
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise invalid_request("Repository address is not allowed")
    except ValueError:
        pass
    effective_port = port or 443
    bracketed = f"[{host}]" if ":" in host else host
    authority = f"{bracketed}:{effective_port}"
    allowed = any(
        entry == authority or (entry == host and effective_port == 443)
        for entry in settings.allowed_git_hosts
    )
    if not allowed:
        raise invalid_request("Repository host is not in the allowlist")


async def validate_resolved_addresses(repository: str) -> None:
    parsed = urlsplit(repository)
    host = parsed.hostname
    if host is None:
        raise invalid_request("Invalid repository URL")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as error:
        raise ServiceError(
            ErrorCode.GIT_REMOTE_ERROR, "Unable to resolve repository host"
        ) from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise invalid_request("Repository address is not allowed")


def _git_arguments(settings: Settings, *, local: bool = False) -> tuple[str, ...]:
    arguments = [
        "git",
        "--no-pager",
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "http.sslVerify=true",
        "-c",
        "http.followRedirects=false",
        "-c",
        f"protocol.{'file' if local else 'https'}.allow=always",
        "-c",
        f"protocol.{'https' if local else 'file'}.allow=never",
    ]
    if settings.ca_bundle is not None:
        arguments.extend(("-c", f"http.sslCAInfo={settings.ca_bundle}"))
    return tuple(arguments)


@contextmanager
def _git_environment(
    settings: Settings,
    *,
    provider: str | None = None,
    token: str | None = None,
    local: bool = False,
) -> Generator[Mapping[str, str]]:
    with isolated_environment(settings) as env:
        home_path = Path(env["HOME"])
        templates = home_path / "templates"
        templates.mkdir()
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TEMPLATE_DIR": str(templates),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ALLOW_PROTOCOL": "file" if local else "https",
                "GIT_PROTOCOL_FROM_USER": "0",
            }
        )
        if settings.ca_bundle is not None:
            env["SSL_CERT_FILE"] = str(settings.ca_bundle)
        if token is not None:
            if provider not in {"github", "gitlab"}:
                raise ValueError("A provider is required for authenticated Git")
            env.update(
                {
                    "GIT_ASKPASS": str(settings.askpass_path),
                    "GWS_GIT_USERNAME": "x-access-token"
                    if provider == "github"
                    else "oauth2",
                    "GWS_GIT_TOKEN": token,
                }
            )
        yield env


def process_error(
    error: ProcessFailure, *, remote: bool, missing_ref: bool = False
) -> ServiceError:
    message = error.stderr.lower()
    if "no space left on device" in message or "disk quota exceeded" in message:
        return ServiceError(ErrorCode.INSUFFICIENT_STORAGE, "Insufficient Git storage")
    if remote:
        if missing_ref and (
            "couldn't find remote ref" in message
            or "could not find remote ref" in message
            or "remote ref does not exist" in message
        ):
            return ServiceError(ErrorCode.REF_NOT_FOUND)
        if any(
            text in message
            for text in (
                "authentication failed",
                "invalid username or password",
                "could not read username",
                "could not read password",
                "http 401",
                " 401 ",
            )
        ):
            return ServiceError(ErrorCode.GIT_AUTH_FAILED)
        if any(
            text in message
            for text in (
                "permission denied",
                "access denied",
                "repository not found",
                "not authorized",
                "http 403",
                " 403 ",
            )
        ):
            return ServiceError(ErrorCode.GIT_ACCESS_DENIED)
        return ServiceError(ErrorCode.GIT_REMOTE_ERROR)
    return ServiceError(ErrorCode.INTERNAL_ERROR, "Local Git operation failed")


async def run_git(
    settings: Settings,
    runner: ProcessRunner,
    arguments: Sequence[str],
    *,
    operation: str,
    provider: str | None = None,
    token: str | None = None,
    local: bool = False,
    missing_ref: bool = False,
) -> str:
    remote = not local and token is not None
    with _git_environment(settings, provider=provider, token=token, local=local) as env:
        try:
            return await runner.run(
                (*_git_arguments(settings, local=local), *arguments),
                env=env,
                secrets=(token,) if token else (),
                operation=operation,
            )
        except ProcessFailure as error:
            raise process_error(
                error, remote=remote, missing_ref=missing_ref
            ) from error
