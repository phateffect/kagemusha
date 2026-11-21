from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import trustme
from fastapi.testclient import TestClient

from kagemusha.app import create_app
from kagemusha.config import Settings

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
        allowed_git_hosts=remote.allowed_hosts,
        ca_bundle=remote.ca_bundle,
        git_timeout_seconds=30,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": "Bearer service-test-token"}


@pytest.fixture
def credential() -> dict[str, dict[str, str]]:
    return {"credential": {"type": "token", "token": "test-git-token"}}


def initialize(
    client: TestClient,
    remote: GitRemote,
    auth: dict[str, str],
    provider: str = "gitlab",
    project_id: str = "project-a",
):
    return client.post(
        f"/projects/{project_id}",
        headers=auth,
        json={
            "repository": remote.url,
            "provider": provider,
            "credential": {"type": "token", "token": "test-git-token"},
        },
    )
