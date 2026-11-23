from __future__ import annotations

import asyncio

import pytest

from kagemusha.errors import ServiceError
from kagemusha.process import ProcessRunner, redact
from kagemusha.refs import resolve_ref, workspace_branch


@pytest.mark.parametrize(
    ("value", "provider", "remote"),
    [
        ("main", "generic", "refs/heads/main"),
        ("feature/login", "github", "refs/heads/feature/login"),
        ("refs/tags/v1", "gitlab", "refs/tags/v1"),
        ("pr:12", "github", "refs/pull/12/head"),
        ("mr:34", "gitlab", "refs/merge-requests/34/head"),
    ],
)
def test_ref_resolution(value, provider, remote):
    assert resolve_ref(value, provider) == remote


@pytest.mark.parametrize(
    "value", ["HEAD~1", "a" * 40, "refs/remotes/origin/main", "../main", "-main"]
)
def test_unsafe_refs_are_rejected(value):
    with pytest.raises(ServiceError):
        resolve_ref(value, "generic")


def test_working_branch_rules():
    assert workspace_branch("main", "refs/heads/main", None) == ("main", True)
    assert workspace_branch("mr:2", "refs/merge-requests/2/head", None) == (
        "kagemusha/mr-2",
        False,
    )
    assert workspace_branch("refs/tags/v1", "refs/tags/v1", None) == (None, False)
    assert workspace_branch("refs/tags/v1", "refs/tags/v1", "agent") == (
        "agent/tag-v1",
        False,
    )


def test_process_timeout_and_redaction(settings):
    async def run():
        runner = ProcessRunner(settings)
        with pytest.raises(ServiceError) as error:
            await runner.run(
                ("sh", "-c", "sleep 30"),
                env={"PATH": "/usr/bin:/bin"},
                timeout=0,
                operation="timeout test",
            )
        assert error.value.code == "GIT_TIMEOUT"

    asyncio.run(run())
    assert redact("secret=value", ("value",)) == "secret=[REDACTED]"
