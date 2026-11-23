from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import trustme
from fastapi.testclient import TestClient

from kagemusha.app import create_app
from kagemusha.config import Settings
from kagemusha.worker import Worker

from .git_server import GitHttpsServer, GitRemote
from .helpers import seed_repository


@pytest.fixture(scope="session")
def seed_bare(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("seed")
    seed_repository(path)
    return path / "remote.git"


@pytest.fixture(scope="session")
def certificate_authority() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def remote(
    seed_bare: Path, certificate_authority: trustme.CA, tmp_path: Path
) -> GitRemote:
    bare = tmp_path / "remote.git"
    shutil.copytree(seed_bare, bare)
    server = GitHttpsServer(bare, certificate_authority)
    with certificate_authority.cert_pem.tempfile() as ca_bundle:
        port, thread = server.start()
        resource = GitRemote(
            bare=bare,
            port=port,
            ca_bundle=Path(ca_bundle),
            server=server.server,
            thread=thread,
            controller=server,
            used_tokens=server.used_tokens,
        )
        yield resource
        resource.stop()


@pytest.fixture
def settings(remote: GitRemote, tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        api_token="service-test-token",
        git_credentials={"test-reader": "test-git-token"},
        allowed_git_hosts=remote.allowed_hosts,
        ca_bundle=remote.ca_bundle,
        git_timeout_seconds=30,
        worker_poll_seconds=0.01,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": "Bearer service-test-token"}


def register_repository(
    client: TestClient,
    remote: GitRemote,
    auth: dict[str, str],
    *,
    provider: str = "gitlab",
    repository_id: str = "repository-a",
):
    return client.put(
        f"/repositories/{repository_id}",
        headers=auth,
        json={
            "name": "Test Repository",
            "remote_url": remote.url,
            "git_provider": provider,
            "default_ref": "main",
            "credential_ref": "test-reader",
        },
    )


def run_worker(settings: Settings, count: int = 1) -> None:
    async def run() -> None:
        worker = Worker(settings)
        for _ in range(count):
            await worker.run(once=True)

    asyncio.run(run())
