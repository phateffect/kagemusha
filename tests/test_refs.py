from __future__ import annotations

import pytest

from kagemusha.errors import ServiceError
from kagemusha.refs import resolve_ref

from .conftest import initialize
from .helpers import extract_workspace, git


@pytest.mark.parametrize(
    ("value", "provider", "remote"),
    [
        ("main", "gitlab", "refs/heads/main"),
        ("feature/login", "github", "refs/heads/feature/login"),
        ("refs/heads/main", "github", "refs/heads/main"),
        ("refs/tags/v1.0.0", "gitlab", "refs/tags/v1.0.0"),
        ("pr:123", "github", "refs/pull/123/head"),
        ("mr:123", "gitlab", "refs/merge-requests/123/head"),
    ],
)
def test_ref_mapping(value, provider, remote):
    assert resolve_ref(value, provider) == remote


@pytest.mark.parametrize(
    "value",
    [
        "HEAD~3",
        "main^{tree}",
        "a" * 40,
        "abc123...",
        "pr:0",
        "pr:abc",
        "refs/remotes/origin/main",
        "refs/gws/snapshots/abc",
        "/main",
        "main/",
        "main..next",
        "main.lock",
        ".hidden",
        "main@{upstream}",
        "-main",
    ],
)
def test_invalid_refs(value):
    with pytest.raises(ServiceError) as error:
        resolve_ref(value, "gitlab")
    assert error.value.code == "INVALID_REF"


@pytest.mark.parametrize(
    ("value", "provider"), [("pr:1", "gitlab"), ("mr:1", "github")]
)
def test_special_ref_provider_mismatch(value, provider):
    with pytest.raises(ServiceError):
        resolve_ref(value, provider)


@pytest.mark.parametrize(
    ("ref", "provider", "expected_file"),
    [
        ("main", "gitlab", "history.txt"),
        ("feature/login", "github", "history.txt"),
        ("refs/tags/v1.0.0", "gitlab", "history.txt"),
        ("refs/tags/annotated-v1.0.0", "github", "history.txt"),
        ("pr:123", "github", "pr.txt"),
        ("mr:456", "gitlab", "mr.txt"),
    ],
)
def test_fetch_supported_refs(
    client, remote, auth, credential, tmp_path, ref, provider, expected_file
):
    assert initialize(client, remote, auth, provider=provider).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": ref, "depth": 10, **credential},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zstd"
    assert response.headers["x-git-ref"] == ref
    assert response.headers["x-git-depth"] == "10"
    expected_commit = git(
        "--git-dir",
        str(remote.bare),
        "rev-parse",
        f"{resolve_ref(ref, provider)}^{{commit}}",
    )
    assert response.headers["x-git-commit"] == expected_commit
    workspace = extract_workspace(response.content, tmp_path / "extracted")
    assert (workspace / expected_file).is_file()
    assert git("rev-parse", "HEAD", cwd=workspace) == expected_commit
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace) == "HEAD"


def test_branch_update_is_fetched_and_deleted_branch_does_not_use_cache(
    client, remote, auth, credential, tmp_path
):
    assert initialize(client, remote, auth).status_code == 201
    work = tmp_path / "update"
    git("clone", "--quiet", str(remote.bare), str(work))
    git("config", "user.name", "Updater", cwd=work)
    git("config", "user.email", "updater@example.invalid", cwd=work)
    (work / "updated.txt").write_text("new remote commit\n")
    git("add", "updated.txt", cwd=work)
    git("commit", "--quiet", "-m", "Remote update", cwd=work)
    git("push", "--quiet", "origin", "main", cwd=work)
    updated = git("rev-parse", "HEAD", cwd=work)
    response = client.post(
        "/projects/project-a/copies", headers=auth, json={"ref": "main", **credential}
    )
    assert response.status_code == 200
    assert response.headers["x-git-commit"] == updated

    git("--git-dir", str(remote.bare), "update-ref", "-d", "refs/heads/main")
    missing = client.post(
        "/projects/project-a/copies", headers=auth, json={"ref": "main", **credential}
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "REF_NOT_FOUND"


def test_missing_ref_does_not_fall_back_to_cached_objects(
    client, remote, auth, credential
):
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "does-not-exist", **credential},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REF_NOT_FOUND"
