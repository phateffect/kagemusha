from __future__ import annotations

import pytest

from .conftest import initialize
from .helpers import extract_workspace, git


@pytest.mark.parametrize(
    ("depth", "expected"), [(1, 1), (10, 10), (50, 50), (100, 100), (0, 120)]
)
def test_depth(client, remote, auth, credential, tmp_path, depth, expected):
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": depth, **credential},
    )
    assert response.status_code == 200, response.text
    workspace = extract_workspace(response.content, tmp_path / "extracted")
    assert int(git("rev-list", "--count", "HEAD", cwd=workspace)) == expected
    shallow = git("rev-parse", "--is-shallow-repository", cwd=workspace)
    assert shallow == ("false" if depth == 0 else "true")
    cache = client.app.state.settings.projects_dir / "project-a.git"
    assert (
        git("--git-dir", str(cache), "rev-parse", "--is-shallow-repository") == "false"
    )


@pytest.mark.parametrize("depth", [-1, 1001, 1.5, "50", True])
def test_invalid_depth(client, remote, auth, credential, depth):
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": depth, **credential},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    ("ref", "provider"), [("pr:123", "github"), ("mr:456", "gitlab")]
)
def test_special_ref_shallow_history(
    client, remote, auth, credential, tmp_path, ref, provider
):
    assert initialize(client, remote, auth, provider=provider).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": ref, "depth": 10, **credential},
    )
    assert response.status_code == 200, response.text
    workspace = extract_workspace(response.content, tmp_path / "extracted")
    assert int(git("rev-list", "--count", "HEAD", cwd=workspace)) == 10
    assert git("rev-parse", "--is-shallow-repository", cwd=workspace) == "true"


def test_history_shorter_than_depth_is_not_an_error(
    client, remote, auth, credential, tmp_path
):
    fifth = git(
        "--git-dir", str(remote.bare), "rev-list", "--skip=115", "--max-count=1", "main"
    )
    git("--git-dir", str(remote.bare), "update-ref", "refs/heads/short", fifth)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "short", "depth": 50, **credential},
    )
    assert response.status_code == 200, response.text
    workspace = extract_workspace(response.content, tmp_path / "extracted")
    assert int(git("rev-list", "--count", "HEAD", cwd=workspace)) == 5
    assert git("rev-parse", "--is-shallow-repository", cwd=workspace) == "false"
