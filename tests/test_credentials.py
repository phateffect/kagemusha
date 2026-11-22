from __future__ import annotations

import logging

import pytest

from kagemusha.config import Settings
from kagemusha.errors import ProcessFailure, ServiceError
from kagemusha.git import process_error, validate_repository_url
from kagemusha.process import ProcessRunner

from .conftest import initialize


def test_tokens_and_authorization_do_not_enter_logs(
    client, remote, auth, credential, caplog
):
    caplog.set_level(logging.INFO)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": 1, **credential},
    )
    assert response.status_code == 200
    assert "test-git-token" not in caplog.text
    assert "service-test-token" not in caplog.text
    assert "Authorization" not in caplog.text
    assert "KAGEMUSHA_GIT_TOKEN" not in caplog.text


def test_validation_error_does_not_echo_token(client, remote, auth):
    response = client.post(
        "/projects/project-a",
        headers=auth,
        json={
            "repository": remote.url,
            "provider": "gitlab",
            "credential": {"type": "token", "token": 123456},
        },
    )
    assert response.status_code == 400
    assert "123456" not in response.text


def test_tls_verification_cannot_be_disabled(
    client, remote, settings, auth, credential
):
    object.__setattr__(settings, "ca_bundle", None)
    response = initialize(client, remote, auth)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "GIT_REMOTE_ERROR"


def test_empty_host_allowlist_fails(tmp_path):
    with pytest.raises(ValueError):
        Settings(
            data_dir=tmp_path,
            api_token="long-service-secret",
            allowed_git_hosts=frozenset(),
        )


def test_data_directory_is_required(monkeypatch):
    monkeypatch.delenv("KAGEMUSHA_DATA_DIR", raising=False)
    with pytest.raises(ValueError):
        Settings(
            api_token="long-service-secret",
            allowed_git_hosts=frozenset({"github.com"}),
        )


def test_settings_load_comma_separated_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("KAGEMUSHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KAGEMUSHA_API_TOKEN", "environment-secret")
    monkeypatch.setenv("KAGEMUSHA_ALLOWED_GIT_HOSTS", "GitHub.com,git.example.com:8443")
    monkeypatch.setenv("KAGEMUSHA_MAX_REQUEST_BODY_BYTES", "8192")
    monkeypatch.setenv("KAGEMUSHA_CA_BUNDLE", "")
    settings = Settings()
    assert settings.data_dir == tmp_path
    assert settings.allowed_git_hosts == frozenset(
        {"github.com", "git.example.com:8443"}
    )
    assert settings.max_request_body_bytes == 8192
    assert settings.ca_bundle is None


@pytest.mark.parametrize("host", ["[::1]:99999", "[::::]:443"])
def test_invalid_ipv6_allowlist_entry_is_rejected(tmp_path, host):
    with pytest.raises(ValueError):
        Settings(
            data_dir=tmp_path,
            api_token="long-service-secret",
            allowed_git_hosts=frozenset({host}),
        )


def test_git_error_classification():
    unauthorized = process_error(
        ProcessFailure("fetch", 128, "fatal: Authentication failed"), remote=True
    )
    assert unauthorized.status_code == 401
    denied = process_error(
        ProcessFailure("fetch", 128, "fatal: HTTP 403 access denied"), remote=True
    )
    assert denied.status_code == 403
    missing = process_error(
        ProcessFailure("fetch", 128, "fatal: couldn't find remote ref refs/heads/x"),
        remote=True,
        missing_ref=True,
    )
    assert missing.status_code == 404
    storage = process_error(
        ProcessFailure("clone", 128, "fatal: No space left on device"), remote=True
    )
    assert storage.status_code == 507


def test_git_token_never_appears_in_subprocess_arguments(
    client, remote, auth, credential, monkeypatch
):
    original = ProcessRunner.run
    arguments_seen = []

    async def capture(self, arguments, **kwargs):
        arguments_seen.append(tuple(arguments))
        return await original(self, arguments, **kwargs)

    monkeypatch.setattr(ProcessRunner, "run", capture)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": 1, **credential},
    )
    assert response.status_code == 200
    assert arguments_seen
    assert all(
        "test-git-token" not in argument
        for arguments in arguments_seen
        for argument in arguments
    )


def test_metadata_and_non_default_ports_are_rejected(settings):
    for repository in (
        "https://169.254.169.254/latest/meta-data",
        "https://metadata.google.internal/computeMetadata/v1",
    ):
        with pytest.raises(ServiceError):
            validate_repository_url(repository, settings)
    host = next(iter(settings.allowed_git_hosts))
    hostname, port = host.rsplit(":", 1)
    wrong_port = int(port) + 1
    with pytest.raises(ServiceError):
        validate_repository_url(f"https://{hostname}:{wrong_port}/remote.git", settings)


def test_repository_url_requires_a_path(settings):
    host = next(iter(settings.allowed_git_hosts))
    with pytest.raises(ServiceError):
        validate_repository_url(f"https://{host}/", settings)
