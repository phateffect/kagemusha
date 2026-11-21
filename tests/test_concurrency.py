from __future__ import annotations

import asyncio
import concurrent.futures
import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

from kagemusha.errors import ProcessFailure, ServiceError
from kagemusha.locks import file_lock
from kagemusha.process import ProcessRunner
from kagemusha.refs import resolve_ref
from kagemusha.repository import RepositoryManager
from kagemusha.workspace import WorkspaceManager

from .conftest import initialize
from .helpers import git


def test_concurrent_copies_share_cache_but_not_snapshots(client, remote, auth):
    assert initialize(client, remote, auth).status_code == 201
    remote.controller.delay_seconds = 0.05

    def copy(token: str):
        return client.post(
            "/projects/project-a/copies",
            headers=auth,
            json={
                "ref": "main",
                "depth": 10,
                "credential": {"type": "token", "token": token},
            },
        )

    tokens = ["second-token", "third-token"] * 3
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(copy, tokens))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.headers["x-git-commit"] for response in responses}) == 1
    assert {"second-token", "third-token"}.issubset(set(remote.used_tokens))
    cache = client.app.state.settings.projects_dir / "project-a.git"
    assert git("--git-dir", str(cache), "for-each-ref", "refs/gws/snapshots") == ""
    assert not list(client.app.state.settings.tmp_dir.glob("copy-*"))


def test_different_projects_can_copy_concurrently(client, remote, auth, credential):
    for project_id in ("project-a", "project-b"):
        assert (
            initialize(client, remote, auth, project_id=project_id).status_code == 201
        )

    def copy(project_id: str):
        return client.post(
            f"/projects/{project_id}/copies",
            headers=auth,
            json={"ref": "main", "depth": 1, **credential},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(copy, ("project-a", "project-b")))
    assert all(response.status_code == 200 for response in responses)


async def test_subprocess_cancellation_kills_process_group(settings):
    settings.ensure_directories()
    runner = ProcessRunner(settings)
    pid_file = settings.tmp_dir / "child.pid"
    script = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    )
    task = asyncio.create_task(
        runner.run(
            (sys.executable, "-c", script, str(pid_file)),
            env=os.environ,
            operation="cancellation test",
        )
    )
    while not pid_file.exists():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Cancelled subprocess was not terminated")


async def test_subprocess_timeout(settings):
    settings.ensure_directories()
    runner = ProcessRunner(settings)
    started = time.monotonic()
    with pytest.raises(ServiceError) as error:
        await runner.run(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            env=os.environ,
            timeout=1,
            operation="timeout test",
        )
    assert error.value.code == "GIT_TIMEOUT"
    assert time.monotonic() - started < 3


async def test_explicit_zero_subprocess_timeout_is_not_replaced(settings):
    settings.ensure_directories()
    runner = ProcessRunner(settings)
    with pytest.raises(ServiceError) as error:
        await runner.run(
            (sys.executable, "-c", "print('done')"),
            env=os.environ,
            timeout=0,
            operation="zero timeout test",
        )
    assert error.value.code == "GIT_TIMEOUT"


async def test_cancel_remote_fetch_cleans_workspace_and_snapshot(
    settings, remote, monkeypatch
):
    settings.ensure_directories()
    runner = ProcessRunner(settings)
    repositories = RepositoryManager(settings, runner)
    workspaces = WorkspaceManager(settings, runner, repositories)
    project = await repositories.create(
        "project-a", remote.url, "gitlab", "test-git-token"
    )
    started = asyncio.Event()
    import kagemusha.repository as repository_module

    original = repository_module.run_git

    async def slow_fetch(*args, **kwargs):
        if kwargs.get("operation") == "git fetch remote ref":
            started.set()
            await asyncio.sleep(60)
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository_module, "run_git", slow_fetch)
    task = asyncio.create_task(
        workspaces.create(project, resolve_ref("main", "gitlab"), "test-git-token", 10)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(settings.tmp_dir.glob("copy-*"))
    assert (
        git(
            "--git-dir",
            str(repositories.bare_path(project.id)),
            "for-each-ref",
            "refs/gws/snapshots",
        )
        == ""
    )


async def test_cancel_archive_cleans_workspace_and_snapshot(
    settings, remote, monkeypatch
):
    settings.ensure_directories()
    runner = ProcessRunner(settings)
    repositories = RepositoryManager(settings, runner)
    workspaces = WorkspaceManager(settings, runner, repositories)
    project = await repositories.create(
        "project-a", remote.url, "gitlab", "test-git-token"
    )
    started = asyncio.Event()
    import kagemusha.workspace as workspace_module

    async def slow_archive(*args, **kwargs):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(workspace_module, "create_archive", slow_archive)
    task = asyncio.create_task(
        workspaces.create(project, resolve_ref("main", "gitlab"), "test-git-token", 10)
    )
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(settings.tmp_dir.glob("copy-*"))
    assert (
        git(
            "--git-dir",
            str(repositories.bare_path(project.id)),
            "for-each-ref",
            "refs/gws/snapshots",
        )
        == ""
    )


def test_archive_failure_cleans_resources(
    client, remote, auth, credential, monkeypatch
):
    assert initialize(client, remote, auth).status_code == 201
    original = ProcessRunner.run

    async def fail_tar(self, arguments, **kwargs):
        if arguments[0] == "tar":
            raise ProcessFailure("create archive", 2, "simulated tar failure")
        return await original(self, arguments, **kwargs)

    monkeypatch.setattr(ProcessRunner, "run", fail_tar)
    response = client.post(
        "/projects/project-a/copies", headers=auth, json={"ref": "main", **credential}
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    settings = client.app.state.settings
    assert not list(settings.tmp_dir.glob("copy-*"))
    cache = settings.projects_dir / "project-a.git"
    assert git("--git-dir", str(cache), "for-each-ref", "refs/gws/snapshots") == ""


def test_download_disconnect_runs_cleanup(client, remote, auth, credential):
    assert initialize(client, remote, auth).status_code == 201
    with client.stream(
        "POST",
        "/projects/project-a/copies",
        headers=auth,
        json={"ref": "main", "depth": 1, **credential},
    ) as response:
        assert response.status_code == 200
        next(response.iter_bytes())
    settings = client.app.state.settings
    for _ in range(100):
        if not list(settings.tmp_dir.glob("copy-*")):
            break
        time.sleep(0.01)
    assert not list(settings.tmp_dir.glob("copy-*"))


def _process_lock_worker(path: str, acquired) -> None:
    async def acquire() -> None:
        async with file_lock(Path(path)):
            acquired.set()
            await asyncio.sleep(0.05)

    asyncio.run(acquire())


async def test_file_lock_coordinates_separate_processes(settings):
    settings.ensure_directories()
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    process = context.Process(
        target=_process_lock_worker,
        args=(str(settings.locks_dir / "shared.lock"), acquired),
    )
    try:
        async with file_lock(settings.locks_dir / "shared.lock"):
            process.start()
            await asyncio.sleep(0.2)
            assert not acquired.is_set()
        assert await asyncio.to_thread(acquired.wait, 3)
        await asyncio.to_thread(process.join, 3)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=3)
