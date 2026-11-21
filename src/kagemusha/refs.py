from __future__ import annotations

import re

from .errors import invalid_ref

_SPECIAL_REF = re.compile(r"^(pr|mr):([1-9][0-9]{0,18})$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9._/@-]+$")
_COMMIT_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


def resolve_ref(value: str, provider: str) -> str:
    special = _SPECIAL_REF.fullmatch(value)
    if special:
        kind, number = special.groups()
        if kind == "pr" and provider != "github":
            raise invalid_ref("GitHub PR refs require the github provider")
        if kind == "mr" and provider != "gitlab":
            raise invalid_ref("GitLab MR refs require the gitlab provider")
        remote = (
            f"refs/pull/{number}/head"
            if kind == "pr"
            else f"refs/merge-requests/{number}/head"
        )
        return remote

    if value.startswith(("pr:", "mr:")):
        raise invalid_ref()
    if value.startswith("refs/"):
        if not value.startswith(("refs/heads/", "refs/tags/")):
            raise invalid_ref("Only branch and tag refs are supported")
        remote = value
    else:
        remote = f"refs/heads/{value}"

    if (
        len(remote) > 255
        or not _SAFE_REF.fullmatch(remote)
        or _COMMIT_SHA.fullmatch(value)
    ):
        raise invalid_ref()
    if (
        remote.endswith(("/", ".", ".lock"))
        or "//" in remote
        or ".." in remote
        or "@{" in remote
        or remote == "@"
    ):
        raise invalid_ref()
    components = remote.split("/")
    if any(
        not component or component.startswith(".") or component.endswith(".lock")
        for component in components
    ):
        raise invalid_ref()
    if not value.startswith("refs/") and value.startswith("-"):
        raise invalid_ref()
    return remote
