from __future__ import annotations

import json

import pytest

from .conftest import initialize


def test_health_requires_no_authentication(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_initialize_project(client, remote, auth):
    response = initialize(client, remote, auth)
    assert response.status_code == 201, response.text
    assert response.json() == {
        "project_id": "project-a",
        "repository": remote.url,
        "provider": "gitlab",
        "default_branch": "main",
    }
    duplicate = initialize(client, remote, auth)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PROJECT_ALREADY_EXISTS"
    metadata = json.loads(
        (client.app.state.settings.metadata_dir / "project-a.json").read_text()
    )
    assert set(metadata) == {"id", "repository", "provider", "default_branch"}
    assert "test-git-token" not in json.dumps(metadata)
    config = client.app.state.settings.projects_dir / "project-a.git" / "config"
    assert "test-git-token" not in config.read_text()


def test_business_apis_require_service_token(client, remote, credential):
    response = client.post(
        "/projects/project-a",
        json={"repository": remote.url, "provider": "gitlab", **credential},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    ["Basic dXNlcjpwYXNz", "Bearer wrong-service-token", "NotBearer token"],
)
def test_business_apis_reject_invalid_authorization_schemes(
    client, remote, credential, authorization
):
    response = client.post(
        "/projects/project-a",
        headers={"Authorization": authorization},
        json={"repository": remote.url, "provider": "gitlab", **credential},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["www-authenticate"] == "Bearer"


def test_request_models_reject_unknown_fields(client, remote, auth, credential):
    response = client.post(
        "/projects/project-a",
        headers=auth,
        json={
            "repository": remote.url,
            "provider": "gitlab",
            "unexpected": True,
            **credential,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_project_id_and_url_validation(client, remote, auth, credential):
    invalid_id = client.post(
        "/projects/..",
        headers=auth,
        json={"repository": remote.url, "provider": "gitlab", **credential},
    )
    assert invalid_id.status_code in {400, 404}
    for repository in (
        remote.url.replace("https:", "http:"),
        remote.url.replace("https://", "https://oauth2:test-git-token@"),
        "https://github.com/example/project.git",
    ):
        response = client.post(
            "/projects/project-a",
            headers=auth,
            json={"repository": repository, "provider": "gitlab", **credential},
        )
        assert response.status_code == 400
        assert "test-git-token" not in response.text


def test_invalid_git_token_is_not_returned(client, remote, auth):
    response = client.post(
        "/projects/project-a",
        headers=auth,
        json={
            "repository": remote.url,
            "provider": "gitlab",
            "credential": {"type": "token", "token": "invalid-secret-token"},
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "GIT_AUTH_FAILED"
    assert "invalid-secret-token" not in response.text


def test_corrupt_project_metadata_is_an_internal_error(
    client, remote, auth, credential
):
    assert initialize(client, remote, auth).status_code == 201
    metadata_path = client.app.state.settings.metadata_dir / "project-a.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["provider"] = "unknown"
    metadata_path.write_text(json.dumps(metadata))

    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", **credential},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
