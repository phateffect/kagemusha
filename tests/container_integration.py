from __future__ import annotations

import argparse
import json
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


def service_container(compose: list[str], service: str, env: dict[str, str]) -> str:
    result = command([*compose, "ps", "-q", service], env=env, capture=True)
    container = result.stdout.strip()
    if not container:
        raise AssertionError(f"No container found for {service}")
    return container


def wait_healthy(
    port: int, compose: list[str], env: dict[str, str], timeout: int = 120
) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/health/ready",
                trust_env=False,
                timeout=3,
            )
            health = []
            for service in ("api", "worker"):
                container = service_container(compose, service, env)
                state = command(
                    [
                        "podman",
                        "inspect",
                        "--format",
                        "{{.State.Health.Status}}",
                        container,
                    ],
                    env=env,
                    capture=True,
                ).stdout.strip()
                health.append(state)
            if response.status_code == 200 and health == ["healthy", "healthy"]:
                return
            last_error = f"HTTP {response.status_code}, health {health}"
        except (OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
            last_error = str(error)
        time.sleep(1)
    raise AssertionError(f"Services did not become healthy: {last_error}")


def wait_job(client: httpx.Client, job_id: str, timeout: int = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            if job["status"] != "succeeded":
                raise AssertionError(f"Job failed: {job}")
            return job
        time.sleep(0.25)
    raise AssertionError(f"Job {job_id} did not finish")


def verify_archive(archive: bytes, destination: Path, expected_commit: str) -> None:
    workspace = extract_workspace(archive, destination)
    assert git("rev-parse", "HEAD", cwd=workspace) == expected_commit
    assert git("branch", "--show-current", cwd=workspace) == "kagemusha/mr-456"
    assert git("status", "--porcelain", cwd=workspace) == ""
    assert int(git("rev-list", "--count", "HEAD", cwd=workspace)) == 10
    assert git("rev-parse", "--is-shallow-repository", cwd=workspace) == "true"
    assert not (workspace / ".git" / "objects" / "info" / "alternates").exists()
    git("switch", "--quiet", "-c", "agent/container-test", cwd=workspace)
    git("config", "user.name", "Container Test", cwd=workspace)
    git("config", "user.email", "container@example.invalid", cwd=workspace)
    (workspace / "container.txt").write_text("portable workspace\n")
    git("add", "container.txt", cwd=workspace)
    git("commit", "--quiet", "-m", "Container workspace commit", cwd=workspace)
    for path in workspace.rglob("*"):
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            assert GIT_TOKEN.encode() not in content, path
            assert SERVICE_TOKEN.encode() not in content, path
            assert b"/data/git/mirrors" not in content, path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument(
        "--build-arg",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="forward an explicit build argument to podman build",
    )
    arguments = parser.parse_args()
    project = f"kagemusha-test-{uuid.uuid4().hex[:12]}"
    env = os.environ.copy()
    port = free_port()
    env.update(
        {
            "KAGEMUSHA_API_TOKEN": SERVICE_TOKEN,
            "KAGEMUSHA_CA_BUNDLE": "/run/kagemusha-test-ca.pem",
            "KAGEMUSHA_HTTP_PORT": str(port),
            "KAGEMUSHA_GIT_CREDENTIALS": json.dumps({"integration-reader": GIT_TOKEN}),
        }
    )
    with tempfile.TemporaryDirectory(prefix=".integration-", dir=ROOT) as temporary:
        work = Path(temporary)
        seed_repository(work)
        bare = work / "remote.git"
        ca = trustme.CA()
        with ca.cert_pem.tempfile(dir=work) as ca_bundle:
            server = GitHttpsServer(bare, ca)
            remote_port, _ = server.start()
            override = work / "compose.test.yaml"
            override.write_text(
                "services:\n"
                "  api:\n"
                "    volumes:\n"
                f"      - {ca_bundle}:/run/kagemusha-test-ca.pem:ro\n"
                "  worker:\n"
                "    volumes:\n"
                f"      - {ca_bundle}:/run/kagemusha-test-ca.pem:ro\n"
            )
            env["KAGEMUSHA_ALLOWED_GIT_HOSTS"] = (
                f"host.containers.internal:{remote_port}"
            )
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
                build_arguments = [
                    item
                    for value in arguments.build_arg
                    for item in ("--build-arg", value)
                ]
                command(
                    [
                        "podman",
                        "build",
                        *build_arguments,
                        "-t",
                        "localhost/kagemusha:local",
                        "-f",
                        "Dockerfile",
                        ".",
                    ],
                    env=env,
                )
            try:
                command([*compose, "up", "-d"], env=env)
                wait_healthy(port, compose, env)
                for service in ("api", "worker"):
                    container = service_container(compose, service, env)
                    uid = command(
                        ["podman", "exec", container, "id", "-u"],
                        env=env,
                        capture=True,
                    ).stdout.strip()
                    assert uid != "0"
                worker = service_container(compose, "worker", env)
                command(
                    [
                        "podman",
                        "exec",
                        worker,
                        "sh",
                        "-c",
                        'for tool in git git-lfs tar zstd mise; do command -v "$tool"; done',
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
                    registered = client.put(
                        "/repositories/integration",
                        json={
                            "name": "Integration",
                            "remote_url": repository_url,
                            "git_provider": "gitlab",
                            "default_ref": "main",
                            "credential_ref": "integration-reader",
                        },
                    )
                    assert registered.status_code == 200, registered.text
                    fetch = client.post("/repositories/integration/fetch")
                    assert fetch.status_code == 202, fetch.text
                    wait_job(client, fetch.json()["id"])
                    created = client.post(
                        "/repositories/integration/copies",
                        json={"ref": "mr:456", "depth": 10, "mode": "git"},
                    )
                    assert created.status_code == 202, created.text
                    wait_job(client, created.json()["job"]["id"])
                    copy_id = created.json()["copy"]["id"]
                    download = client.get(f"/copies/{copy_id}/download")
                    assert download.status_code == 200, download.text
                    expected = git(
                        "--git-dir",
                        str(bare),
                        "rev-parse",
                        "refs/merge-requests/456/head^{commit}",
                    )
                    verify_archive(download.content, work / "extracted", expected)

                command([*compose, "down"], env=env)
                command([*compose, "up", "-d"], env=env)
                wait_healthy(port, compose, env)
                worker = service_container(compose, "worker", env)
                assert (
                    command(
                        [
                            "podman",
                            "exec",
                            worker,
                            "git",
                            "--git-dir=/data/git/mirrors/integration.git",
                            "rev-parse",
                            "--is-bare-repository",
                        ],
                        env=env,
                        capture=True,
                    ).stdout.strip()
                    == "true"
                )
                print(
                    "Podman integration passed: API/Worker health, durable jobs, "
                    "portable Git copy, non-root tools, and volume persistence."
                )
            finally:
                subprocess.run([*compose, "down", "-v"], cwd=ROOT, env=env, check=False)
                server.server.shutdown()
                server.thread.join(timeout=5)
                server.server.server_close()


if __name__ == "__main__":
    main()
