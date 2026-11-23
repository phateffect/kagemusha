from __future__ import annotations

import hashlib
from pathlib import Path

from .conftest import register_repository, run_worker
from .helpers import extract_workspace, git


def _add_recursive_submodules(remote, tmp_path: Path) -> tuple[bytes, bytes]:
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

    module_bare = remote.bare.parent / "module.git"
    module_work = tmp_path / "module-work"
    git("init", "--quiet", "--bare", str(module_bare))
    git("init", "--quiet", "-b", "main", str(module_work))
    git("config", "user.name", "Module Test", cwd=module_work)
    git("config", "user.email", "module@example.invalid", cwd=module_work)
    module_content = b"authenticated submodule content\n"
    (module_work / "library.txt").write_bytes(module_content)
    git("add", "library.txt", cwd=module_work)
    git("commit", "--quiet", "-m", "Add module content", cwd=module_work)
    git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(nested_bare),
        "nested/dependency",
        cwd=module_work,
    )
    git(
        "config",
        "--file=.gitmodules",
        "submodule.nested/dependency.url",
        "../nested.git",
        cwd=module_work,
    )
    git("add", ".gitmodules", "nested/dependency", cwd=module_work)
    git("commit", "--quiet", "-m", "Add nested module", cwd=module_work)
    git("remote", "add", "origin", str(module_bare), cwd=module_work)
    git("push", "--quiet", "origin", "main", cwd=module_work)
    git("--git-dir", str(module_bare), "symbolic-ref", "HEAD", "refs/heads/main")

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
        str(module_bare),
        "vendor/library",
        cwd=superproject,
    )
    git(
        "config",
        "--file=.gitmodules",
        "submodule.vendor/library.url",
        "../module.git",
        cwd=superproject,
    )
    git("add", ".gitmodules", "vendor/library", cwd=superproject)
    git("commit", "--quiet", "-m", "Add recursive module", cwd=superproject)
    git("push", "--quiet", "origin", "main", cwd=superproject)
    return module_content, nested_content


def _add_lfs_pointer(remote, tmp_path: Path) -> bytes:
    content = (b"large authenticated payload\n" * 4096) + b"complete\n"
    oid = hashlib.sha256(content).hexdigest()
    object_path = remote.bare / "lfs" / "objects" / oid
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
        f"oid sha256:{oid}\nsize {len(content)}\n"
    )
    git("add", ".gitattributes", "large.bin", cwd=work)
    git("commit", "--quiet", "-m", "Add LFS pointer", cwd=work)
    git("push", "--quiet", "origin", "main", cwd=work)
    return content


def _create_and_download(client, settings, remote, auth, tmp_path: Path) -> Path:
    assert register_repository(client, remote, auth).status_code == 200
    response = client.post(
        "/repositories/repository-a/copies",
        headers=auth,
        json={"ref": "main", "depth": 10, "mode": "git"},
    )
    assert response.status_code == 202, response.text
    run_worker(settings)
    job_id = response.json()["job"]["id"]
    job = client.get(f"/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "succeeded", job
    copy_id = response.json()["copy"]["id"]
    archive = client.get(f"/copies/{copy_id}/download", headers=auth)
    assert archive.status_code == 200, archive.text
    return extract_workspace(archive.content, tmp_path / "extracted")


def test_recursive_authenticated_submodules_are_hydrated(
    client, settings, remote, auth, tmp_path
):
    module_content, nested_content = _add_recursive_submodules(remote, tmp_path)
    workspace = _create_and_download(client, settings, remote, auth, tmp_path)
    assert (workspace / "vendor/library/library.txt").read_bytes() == module_content
    assert (
        workspace / "vendor/library/nested/dependency/nested.txt"
    ).read_bytes() == nested_content
    assert (
        len(git("submodule", "status", "--recursive", cwd=workspace).splitlines()) == 2
    )
    assert git("status", "--porcelain", cwd=workspace) == ""


def test_authenticated_lfs_object_is_hydrated(client, settings, remote, auth, tmp_path):
    expected = _add_lfs_pointer(remote, tmp_path)
    workspace = _create_and_download(client, settings, remote, auth, tmp_path)
    assert (workspace / "large.bin").read_bytes() == expected
    assert git("lfs", "fsck", cwd=workspace).startswith("Git LFS fsck OK")
    assert git("status", "--porcelain", cwd=workspace) == ""
