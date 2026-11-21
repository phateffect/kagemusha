from __future__ import annotations

import json
import os
import time

from fastapi.testclient import TestClient

from kagemusha.app import create_app

from .conftest import initialize
from .helpers import git


def test_startup_recovers_partial_projects_and_stale_temp_resources(
    settings, remote, auth
):
    with TestClient(create_app(settings)) as client:
        assert initialize(client, remote, auth).status_code == 201
        cache = settings.projects_dir / "project-a.git"
        commit = git("--git-dir", str(cache), "rev-parse", "main")
        git("--git-dir", str(cache), "update-ref", "refs/gws/snapshots/stale", commit)
        git("--git-dir", str(cache), "update-ref", "refs/gws/snapshots/recent", commit)
        stale_ref = cache / "refs" / "gws" / "snapshots" / "stale"
        stale_temp = settings.tmp_dir / "copy-stale"
        recent_temp = settings.tmp_dir / "copy-recent"
        stale_temp.mkdir()
        recent_temp.mkdir()
        old = time.time() - settings.temp_ttl_seconds - 60
        os.utime(stale_ref, (old, old))
        os.utime(stale_temp, (old, old))
        orphan = settings.projects_dir / "orphan.git"
        git("init", "--quiet", "--bare", str(orphan))
        (settings.metadata_dir / "missing-repo.json").write_text(
            json.dumps({"id": "missing-repo"})
        )

    with TestClient(create_app(settings)):
        assert not (settings.projects_dir / "orphan.git").exists()
        assert not (settings.metadata_dir / "missing-repo.json").exists()
        assert not stale_temp.exists()
        assert recent_temp.exists()
        refs = git("--git-dir", str(cache), "for-each-ref", "refs/gws/snapshots")
        assert "snapshots/stale" not in refs
        assert "snapshots/recent" in refs
