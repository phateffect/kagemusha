from __future__ import annotations

import logging
import time
from importlib.metadata import version

import pytest

from kagemusha import __version__
from kagemusha.errors import ERROR_DEFINITIONS, ErrorCode

from .conftest import initialize


def test_missing_project_and_invalid_ref_errors(client, auth, credential):
    missing = client.post(
        "/projects/missing/copies", headers=auth, json={"ref": "main", **credential}
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert set(missing.json()["error"]) == {"code", "message"}


def test_invalid_ref_is_rejected_before_remote_fetch(client, remote, auth, credential):
    assert initialize(client, remote, auth).status_code == 201
    before = len(remote.used_tokens)
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "HEAD~3", **credential},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REF"
    assert len(remote.used_tokens) == before


def test_git_timeout_and_cleanup(client, remote, settings, auth, credential):
    assert initialize(client, remote, auth).status_code == 201
    object.__setattr__(settings, "git_timeout_seconds", 1)
    remote.controller.delay_seconds = 2
    started = time.monotonic()
    response = client.post(
        "/projects/project-a/copies", headers=auth, json={"ref": "main", **credential}
    )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "GIT_TIMEOUT"
    assert time.monotonic() - started < 4
    assert not list(settings.tmp_dir.glob("copy-*"))


def test_temp_budget_is_enforced(client, remote, settings, auth, credential):
    object.__setattr__(settings, "temp_budget_bytes", 1)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies", headers=auth, json={"ref": "main", **credential}
    )
    assert response.status_code == 507
    assert response.json()["error"]["code"] == "INSUFFICIENT_STORAGE"


def test_request_logging_contains_operation_metrics(
    client, remote, auth, credential, caplog
):
    caplog.set_level(logging.INFO, logger="kagemusha")
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": 1, **credential},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert "request_id" in caplog.text
    assert "fetch_duration_ms" in caplog.text
    assert "archive_size_bytes" in caplog.text


def test_error_logging_preserves_error_code(client, caplog):
    caplog.set_level(logging.INFO, logger="kagemusha")
    response = client.post("/projects/project-a", json={})
    assert response.status_code == 401
    assert '"error_code":"UNAUTHORIZED"' in caplog.text


def test_request_body_limit_runs_before_authentication(client, caplog):
    caplog.set_level(logging.INFO, logger="kagemusha")
    response = client.post(
        "/projects/project-a",
        content=b"{" + b'"padding":"' + b"x" * 70_000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert '"error_code":"REQUEST_TOO_LARGE"' in caplog.text


def test_unexpected_errors_include_traceback(
    client, auth, credential, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR, logger="kagemusha")

    async def fail(_project_id):
        raise RuntimeError("unexpected-test-marker")

    monkeypatch.setattr(client.app.state.repositories, "get", fail)
    with pytest.raises(RuntimeError, match="unexpected-test-marker"):
        client.post(
            "/projects/project-a/copies",
            headers=auth,
            json={"ref": "main", **credential},
        )
    assert "RuntimeError: unexpected-test-marker" in caplog.text


def test_default_api_surface_has_no_documentation_endpoints(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_application_version_comes_from_project_metadata(client):
    assert __version__ == version("kagemusha")
    assert client.app.version == __version__


def test_every_public_error_has_a_definition():
    assert set(ERROR_DEFINITIONS) == set(ErrorCode)
    assert all(
        400 <= definition.status_code < 600 and definition.message
        for definition in ERROR_DEFINITIONS.values()
    )
