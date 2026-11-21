from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run(
    *arguments: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    if env:
        environment.update(env)
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git(*arguments: str, cwd: Path | None = None) -> str:
    return run("git", *arguments, cwd=cwd)


def extract_workspace(archive: bytes, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / "workspace.tar.zst"
    archive_path.write_bytes(archive)
    run("tar", "--zstd", "-xf", str(archive_path), "-C", str(destination))
    return destination / "workspace"


def seed_repository(path: Path) -> None:
    bare = path / "remote.git"
    work = path / "seed-work"
    git("init", "--quiet", "--bare", str(bare))
    git("init", "--quiet", "-b", "main", str(work))
    git("config", "user.name", "Kagemusha Tests", cwd=work)
    git("config", "user.email", "tests@example.invalid", cwd=work)
    for number in range(1, 121):
        (work / "history.txt").write_text(f"{number}\n")
        (work / "README.md").write_text("# Kagemusha test repository\n")
        if number == 1:
            (work / "run.sh").write_text("#!/bin/sh\nexit 0\n")
            (work / "run.sh").chmod(0o755)
            (work / "source-link").symlink_to("README.md")
        git("add", "history.txt", "README.md", cwd=work)
        if number == 1:
            git("add", "run.sh", "source-link", cwd=work)
        git("commit", "--quiet", "-m", f"Commit {number}", cwd=work)
    git("branch", "feature/login", cwd=work)
    git("tag", "v1.0.0", cwd=work)
    git("tag", "-a", "annotated-v1.0.0", "-m", "Annotated release", cwd=work)
    git("remote", "add", "origin", str(bare), cwd=work)
    git("push", "--quiet", "origin", "main", "feature/login", "--tags", cwd=work)
    for branch, remote_ref, filename in (
        ("pr-source", "refs/pull/123/head", "pr.txt"),
        ("mr-source", "refs/merge-requests/456/head", "mr.txt"),
    ):
        git("checkout", "--quiet", "-b", branch, "main", cwd=work)
        (work / filename).write_text(f"{branch}\n")
        git("add", filename, cwd=work)
        git("commit", "--quiet", "-m", f"{branch} commit", cwd=work)
        git("push", "--quiet", "origin", f"HEAD:{remote_ref}", cwd=work)
    git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
