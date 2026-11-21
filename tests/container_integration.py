from __future__ import annotations

import argparse
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx2 as httpx
import trustme
from git_server import GitHttpsServer
from helpers import extract_workspace, git, seed_repository

ROOT = Path(__file__).resolve().parent.parent
SERVICE_TOKEN = "integration-service-token"
GIT_TOKEN = "test-git-token"


def command(
    arguments: list[str], *, env: dict[str, str], capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def container_name(project: str, env: dict[str, str]) -> str:
    result = command(
        [
            "podman",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}",
        ],
        env=env,
        capture=True,
    )
    names = result.stdout.splitlines()
    if len(names) != 1:
        raise AssertionError(f"Expected one integration container, found {names}")
    return names[0]


def wait_healthy(port: int, project: str, env: dict[str, str]) -> None:
    deadline = time.monotonic() + 120
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/healthz", trust_env=False, timeout=3
            )
            name = container_name(project, env)
            health = command(
                ["podman", "inspect", "--format", "{{.State.Health.Status}}", name],
                env=env,
                capture=True,
            ).stdout.strip()
            if response.status_code == 200 and health == "healthy":
                return
            last_error = f"HTTP {response.status_code}, health {health}"
        except (OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
            last_error = str(error)
        time.sleep(1)
    raise AssertionError(f"Container did not become healthy: {last_error}")


def verify_archive(archive: bytes, destination: Path, expected_commit: str) -> None:
    workspace = extract_workspace(archive, destination)
    assert git("rev-parse", "HEAD", cwd=workspace) == expected_commit
    assert git("status", "--porcelain", cwd=workspace) == ""
    assert int(git("rev-list", "--count", "HEAD", cwd=workspace)) == 10
    assert git("rev-parse", "--is-shallow-repository", cwd=workspace) == "true"
    assert git("log", "-1", "--format=%s", cwd=workspace) == "mr-source commit"
    assert git("diff", "--quiet", cwd=workspace) == ""
    assert git("fsck", "--full", "--no-dangling", cwd=workspace) == ""
    assert not (workspace / ".git" / "objects" / "info" / "alternates").exists()
    git("switch", "--quiet", "-c", "agent/container-test", cwd=workspace)
    git("config", "user.name", "Container Test", cwd=workspace)
    git("config", "user.email", "container@example.invalid", cwd=workspace)
    (workspace / "container.txt").write_text("portable workspace\n")
    git("add", "container.txt", cwd=workspace)
    git("commit", "--quiet", "-m", "Container workspace commit", cwd=workspace)
    assert (
        git("log", "-1", "--format=%s", cwd=workspace) == "Container workspace commit"
    )
    for path in workspace.rglob("*"):
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            assert GIT_TOKEN.encode() not in content, path
            assert SERVICE_TOKEN.encode() not in content, path
            assert b"/data/projects" not in content, path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true")
    arguments = parser.parse_args()
    project = f"kagemusha-test-{uuid.uuid4().hex[:12]}"
    env = os.environ.copy()
    port = free_port()
    env.update(
        {
            "GWS_API_TOKEN": SERVICE_TOKEN,
            "GWS_CA_BUNDLE": "/run/kagemusha-test-ca.pem",
            "KAGEMUSHA_HTTP_PORT": str(port),
        }
    )
    remote = None
    compose_started = False
    with tempfile.TemporaryDirectory(prefix=".integration-", dir=ROOT) as temporary:
        work = Path(temporary)
        try:
            seed_repository(work)
            bare = work / "remote.git"
            ca = trustme.CA()
            with ca.cert_pem.tempfile(dir=work) as ca_bundle:
                server = GitHttpsServer(bare, ca)
                remote_port, _ = server.start()
                remote = server
                override = work / "compose.test.yaml"
                override.write_text(
                    "services:\n"
                    "  kagemusha:\n"
                    "    volumes:\n"
                    f"      - {ca_bundle}:/run/kagemusha-test-ca.pem:ro\n"
                )
                env["GWS_ALLOWED_GIT_HOSTS"] = f"host.containers.internal:{remote_port}"
                compose = [
                    "podman",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    "compose.yaml",
                    "-f",
                    str(override),
                ]
                if not arguments.no_build:
                    command(
                        [
                            "podman",
                            "build",
                            "-t",
                            "localhost/kagemusha:local",
                            "-f",
                            "Dockerfile",
                            ".",
                        ],
                        env=env,
                    )
                try:
                    compose_started = True
                    command([*compose, "up", "-d"], env=env)
                    wait_healthy(port, project, env)
                    name = container_name(project, env)
                    uid = command(
                        ["podman", "exec", name, "id", "-u"], env=env, capture=True
                    ).stdout.strip()
                    assert uid != "0"
                    command(
                        [
                            "podman",
                            "exec",
                            name,
                            "python",
                            "-c",
                            "import os; assert os.access('/data', os.W_OK)",
                        ],
                        env=env,
                    )
                    command(
                        ["podman", "exec", name, "sh", "-c", "command -v git tar zstd"],
                        env=env,
                    )
                    command(
                        [
                            "podman",
                            "exec",
                            name,
                            "python",
                            "-c",
                            (
                                "from importlib.metadata import version; "
                                "import kagemusha; "
                                "assert kagemusha.__version__ == version('kagemusha')"
                            ),
                        ],
                        env=env,
                    )
                    repository_url = (
                        f"https://host.containers.internal:{remote_port}/remote.git"
                    )
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{port}",
                        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                        trust_env=False,
                        timeout=90,
                    ) as client:
                        initialized = client.post(
                            "/projects/integration",
                            json={
                                "repository": repository_url,
                                "provider": "gitlab",
                                "credential": {"type": "token", "token": GIT_TOKEN},
                            },
                        )
                        assert initialized.status_code == 201, initialized.text
                        copy = client.post(
                            "/projects/integration/copies",
                            json={
                                "ref": "mr:456",
                                "depth": 10,
                                "credential": {"type": "token", "token": GIT_TOKEN},
                            },
                        )
                        assert copy.status_code == 200, copy.text
                        assert copy.headers["content-type"] == "application/zstd"
                        assert int(copy.headers["content-length"]) == len(copy.content)
                        expected = git(
                            "--git-dir",
                            str(bare),
                            "rev-parse",
                            "refs/merge-requests/456/head^{commit}",
                        )
                        assert copy.headers["x-git-commit"] == expected
                        verify_archive(copy.content, work / "extracted", expected)

                    command([*compose, "down"], env=env)
                    command([*compose, "up", "-d"], env=env)
                    wait_healthy(port, project, env)
                    name = container_name(project, env)
                    assert (
                        command(
                            [
                                "podman",
                                "exec",
                                name,
                                "git",
                                "--git-dir=/data/projects/integration.git",
                                "rev-parse",
                                "--is-bare-repository",
                            ],
                            env=env,
                            capture=True,
                        ).stdout.strip()
                        == "true"
                    )
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{port}",
                        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                        trust_env=False,
                        timeout=30,
                    ) as client:
                        duplicate = client.post(
                            "/projects/integration",
                            json={
                                "repository": repository_url,
                                "provider": "gitlab",
                                "credential": {"type": "token", "token": GIT_TOKEN},
                            },
                        )
                        assert duplicate.status_code == 409, duplicate.text
                    print(
                        "Podman integration passed: health, MR archive, portable Git, "
                        "non-root storage, tools, and Named Volume persistence."
                    )
                finally:
                    if compose_started:
                        subprocess.run(
                            [*compose, "down", "-v"],
                            cwd=ROOT,
                            env=env,
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
        finally:
            if remote is not None:
                remote.server.shutdown()
                remote.thread.join(timeout=5)
                remote.server.server_close()


if __name__ == "__main__":
    main()
