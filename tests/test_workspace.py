from __future__ import annotations

import os
import shutil
import stat

from .conftest import initialize
from .helpers import extract_workspace, git


def test_archive_is_portable_and_independent(
    client, remote, auth, credential, tmp_path
):
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": 10, **credential},
    )
    assert response.status_code == 200, response.text
    settings = client.app.state.settings
    shutil.rmtree(remote.bare)
    shutil.rmtree(settings.projects_dir / "project-a.git")

    workspace = extract_workspace(response.content, tmp_path / "portable")
    assert git("status", "--porcelain", cwd=workspace) == ""
    assert "Commit 120" in git("log", "-1", "--format=%s", cwd=workspace)
    assert git("diff", "--quiet", cwd=workspace) == ""
    assert git("fsck", "--full", "--no-dangling", cwd=workspace) == ""
    assert not (workspace / ".git" / "objects" / "info" / "alternates").exists()
    assert stat.S_IMODE((workspace / "run.sh").stat().st_mode) & stat.S_IXUSR
    assert os.readlink(workspace / "source-link") == "README.md"

    git("switch", "--quiet", "-c", "agent/task-123", cwd=workspace)
    git("config", "user.name", "Agent", cwd=workspace)
    git("config", "user.email", "agent@example.invalid", cwd=workspace)
    (workspace / "agent.txt").write_text("independent change\n")
    git("add", "agent.txt", cwd=workspace)
    git("commit", "--quiet", "-m", "Agent commit", cwd=workspace)
    assert git("log", "-1", "--format=%s", cwd=workspace) == "Agent commit"
    assert not list(settings.tmp_dir.glob("copy-*"))


def test_archive_contains_no_credentials_or_server_paths(
    client, remote, auth, credential, tmp_path
):
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies", headers=auth, json={"ref": "main", **credential}
    )
    assert response.status_code == 200
    workspace = extract_workspace(response.content, tmp_path / "security")
    forbidden = [
        b"test-git-token",
        b"service-test-token",
        str(client.app.state.settings.data_dir).encode(),
        str(remote.bare).encode(),
    ]
    for path in workspace.rglob("*"):
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            for value in forbidden:
                assert value not in content, path


def test_archive_size_limit(client, remote, settings, auth, credential):
    object.__setattr__(settings, "max_archive_bytes", 128)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies", headers=auth, json={"ref": "main", **credential}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "ARCHIVE_TOO_LARGE"
    assert not list(settings.tmp_dir.glob("copy-*"))
