from __future__ import annotations

import hashlib
from pathlib import Path

from .conftest import initialize
from .helpers import extract_workspace, git


def _commit_submodule(remote, tmp_path: Path) -> tuple[bytes, bytes]:
    nested_bare = remote.bare.parent / "nested.git"
    nested_work = tmp_path / "nested-work"
    git("init", "--quiet", "--bare", str(nested_bare))
    git("init", "--quiet", "-b", "main", str(nested_work))
    git("config", "user.name", "Nested Test", cwd=nested_work)
    git("config", "user.email", "nested@example.invalid", cwd=nested_work)
    nested_content = b"recursive submodule content\n"
    (nested_work / "nested.txt").write_bytes(nested_content)
    git("add", "nested.txt", cwd=nested_work)
    git("commit", "--quiet", "-m", "Add nested content", cwd=nested_work)
    git("remote", "add", "origin", str(nested_bare), cwd=nested_work)
    git("push", "--quiet", "origin", "main", cwd=nested_work)
    git("--git-dir", str(nested_bare), "symbolic-ref", "HEAD", "refs/heads/main")

    submodule_bare = remote.bare.parent / "submodule.git"
    submodule_work = tmp_path / "submodule-work"
    git("init", "--quiet", "--bare", str(submodule_bare))
    git("init", "--quiet", "-b", "main", str(submodule_work))
    git("config", "user.name", "Submodule Test", cwd=submodule_work)
    git("config", "user.email", "submodule@example.invalid", cwd=submodule_work)
    content = b"authenticated submodule content\n"
    (submodule_work / "library.txt").write_bytes(content)
    git("add", "library.txt", cwd=submodule_work)
    git("commit", "--quiet", "-m", "Add submodule content", cwd=submodule_work)
    git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(nested_bare),
        "nested/dependency",
        cwd=submodule_work,
    )
    git(
        "config",
        "--file=.gitmodules",
        "submodule.nested/dependency.url",
        "../nested.git",
        cwd=submodule_work,
    )
    git("add", ".gitmodules", "nested/dependency", cwd=submodule_work)
    git("commit", "--quiet", "-m", "Add nested submodule", cwd=submodule_work)
    git("remote", "add", "origin", str(submodule_bare), cwd=submodule_work)
    git("push", "--quiet", "origin", "main", cwd=submodule_work)
    git("--git-dir", str(submodule_bare), "symbolic-ref", "HEAD", "refs/heads/main")

    superproject = tmp_path / "superproject"
    git("clone", "--quiet", str(remote.bare), str(superproject))
    git("config", "user.name", "Superproject Test", cwd=superproject)
    git("config", "user.email", "superproject@example.invalid", cwd=superproject)
    git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(submodule_bare),
        "vendor/library",
        cwd=superproject,
    )
    git(
        "config",
        "--file=.gitmodules",
        "submodule.vendor/library.url",
        "../submodule.git",
        cwd=superproject,
    )
    git("add", ".gitmodules", "vendor/library", cwd=superproject)
    git("commit", "--quiet", "-m", "Add authenticated submodule", cwd=superproject)
    git("push", "--quiet", "origin", "main", cwd=superproject)
    return content, nested_content


def _commit_lfs_pointer(remote, tmp_path: Path, *, store_object: bool = True) -> bytes:
    content = (b"large authenticated payload\n" * 4096) + b"complete\n"
    oid = hashlib.sha256(content).hexdigest()
    object_path = remote.bare / "lfs" / "objects" / oid
    if store_object:
        object_path.parent.mkdir(parents=True)
        object_path.write_bytes(content)

    work = tmp_path / "lfs-work"
    git("clone", "--quiet", str(remote.bare), str(work))
    git("config", "user.name", "LFS Test", cwd=work)
    git("config", "user.email", "lfs@example.invalid", cwd=work)
    (work / ".gitattributes").write_text(
        "large.bin filter=lfs diff=lfs merge=lfs -text\n"
    )
    (work / "large.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {len(content)}\n"
    )
    git("add", ".gitattributes", "large.bin", cwd=work)
    git("commit", "--quiet", "-m", "Add LFS pointer", cwd=work)
    git("push", "--quiet", "origin", "main", cwd=work)
    return content


def test_authenticated_submodule_is_recursively_hydrated(
    client, remote, auth, credential, tmp_path
):
    expected, nested_expected = _commit_submodule(remote, tmp_path)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": 10, **credential},
    )
    assert response.status_code == 200, response.text
    workspace = extract_workspace(response.content, tmp_path / "submodule-extracted")
    assert (workspace / "vendor/library/library.txt").read_bytes() == expected
    assert (
        workspace / "vendor/library/nested/dependency/nested.txt"
    ).read_bytes() == nested_expected
    submodules = git("submodule", "status", "--recursive", cwd=workspace).splitlines()
    assert len(submodules) == 2
    assert all(len(line.split()[0]) == 40 for line in submodules)
    assert git("status", "--porcelain", cwd=workspace) == ""
    assert remote.used_tokens.count("test-git-token") >= 4


def test_authenticated_lfs_objects_are_hydrated(
    client, remote, auth, credential, tmp_path
):
    expected = _commit_lfs_pointer(remote, tmp_path)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": 10, **credential},
    )
    assert response.status_code == 200, response.text
    workspace = extract_workspace(response.content, tmp_path / "lfs-extracted")
    assert (workspace / "large.bin").read_bytes() == expected
    assert git("lfs", "fsck", cwd=workspace).startswith("Git LFS fsck OK")
    assert git("status", "--porcelain", cwd=workspace) == ""
    for path in workspace.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert b"test-git-token" not in path.read_bytes(), path


def test_submodule_limit_rejects_partial_workspace(
    client, settings, remote, auth, credential, tmp_path
):
    _commit_submodule(remote, tmp_path)
    object.__setattr__(settings, "max_submodules", 1)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", **credential},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert list(settings.tmp_dir.glob("copy-*")) == []


def test_missing_lfs_object_fails_the_copy(client, remote, auth, credential, tmp_path):
    _commit_lfs_pointer(remote, tmp_path, store_object=False)
    assert initialize(client, remote, auth).status_code == 201
    response = client.post(
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", **credential},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "GIT_REMOTE_ERROR"
