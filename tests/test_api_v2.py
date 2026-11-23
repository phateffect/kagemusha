from __future__ import annotations

import hashlib

from kagemusha import __version__

from .conftest import register_repository, run_worker
from .helpers import extract_workspace, git


def test_health_openapi_and_authentication(client, auth):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ok"}
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/openapi.json").json()["info"]["version"] == __version__
    assert client.get("/repositories").status_code == 401
    assert client.get("/repositories", headers=auth).status_code == 200


def test_repository_registration_is_idempotent_and_fetch_is_durable(
    client, settings, remote, auth
):
    first = register_repository(client, remote, auth)
    second = register_repository(client, remote, auth)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == "repository-a"
    assert first.json()["status"] == "registered"

    queued = client.post("/repositories/repository-a/fetch", headers=auth)
    assert queued.status_code == 202
    job_id = queued.json()["id"]
    run_worker(settings)
    job = client.get(f"/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "succeeded"
    repository = client.get("/repositories/repository-a", headers=auth).json()
    assert repository["status"] == "ready"
    assert repository["last_fetched_at"]
    assert (settings.mirrors_dir / "repository-a.git").is_dir()


def test_git_copy_job_download_and_delete(client, settings, remote, auth, tmp_path):
    assert register_repository(client, remote, auth).status_code == 200
    request = client.post(
        "/repositories/repository-a/copies",
        headers=auth,
        json={
            "ref": "mr:456",
            "depth": 10,
            "mode": "git",
            "branch_prefix": "agent/task-1",
        },
    )
    assert request.status_code == 202, request.text
    copy_id = request.json()["copy"]["id"]
    job_id = request.json()["job"]["id"]
    run_worker(settings)
    assert client.get(f"/jobs/{job_id}", headers=auth).json()["status"] == "succeeded"
    metadata = client.get(f"/copies/{copy_id}", headers=auth).json()
    assert metadata["status"] == "ready"
    assert metadata["commit_sha"]
    download = client.get(f"/copies/{copy_id}/download", headers=auth)
    assert download.status_code == 200
    assert (
        download.headers["x-artifact-sha256"]
        == hashlib.sha256(download.content).hexdigest()
    )
    workspace = extract_workspace(download.content, tmp_path / "extracted")
    assert git("branch", "--show-current", cwd=workspace) == "agent/task-1/mr-456"
    assert git("status", "--porcelain", cwd=workspace) == ""
    assert client.get(f"/copies/{copy_id}", headers=auth).json()["status"] == "ready"
    assert client.delete(f"/copies/{copy_id}", headers=auth).status_code == 204
    assert client.get(f"/copies/{copy_id}", headers=auth).status_code == 404


def test_source_copy_has_no_git_metadata(client, settings, remote, auth, tmp_path):
    register_repository(client, remote, auth)
    response = client.post(
        "/repositories/repository-a/copies",
        headers=auth,
        json={"ref": "main", "mode": "source"},
    )
    copy_id = response.json()["copy"]["id"]
    run_worker(settings)
    archive = client.get(f"/copies/{copy_id}/download", headers=auth)
    workspace = extract_workspace(archive.content, tmp_path / "source")
    assert not any(path.name == ".git" for path in workspace.rglob(".git"))
    assert (workspace / "README.md").is_file()


def test_repository_delete_runs_in_worker(client, settings, remote, auth):
    register_repository(client, remote, auth)
    client.post("/repositories/repository-a/fetch", headers=auth)
    run_worker(settings)
    deletion = client.delete("/repositories/repository-a", headers=auth)
    assert deletion.status_code == 202
    run_worker(settings)
    assert client.get("/repositories/repository-a", headers=auth).status_code == 404
    assert not (settings.mirrors_dir / "repository-a.git").exists()


def test_api_never_exposes_internal_artifact_paths(client, settings, remote, auth):
    register_repository(client, remote, auth)
    response = client.post(
        "/repositories/repository-a/copies",
        headers=auth,
        json={"ref": "main"},
    )
    copy_id = response.json()["copy"]["id"]
    run_worker(settings)
    body = client.get(f"/copies/{copy_id}", headers=auth).text
    assert str(settings.data_dir) not in body
    assert "artifact_path" not in body
