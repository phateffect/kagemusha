from __future__ import annotations

import asyncio
import os

import pytest

from kagemusha.cache import (
    CacheManager,
    compatibility_key,
    dependency_fingerprint,
    detect_targets,
)
from kagemusha.database import Database
from kagemusha.process import ProcessRunner
from kagemusha.repository import Repository, RepositoryManager
from kagemusha.storage import LocalArtifactStore
from kagemusha.workspace import WorkspaceManager


def test_monorepo_target_detection(tmp_path, settings):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "gradlew").write_text("#!/bin/sh\n")
    (backend / "settings.gradle.kts").write_text('rootProject.name = "backend"\n')
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (frontend / "package.json").write_text(
        '{"packageManager":"pnpm@10.4.1","name":"frontend"}\n'
    )
    nested = frontend / "packages" / "app"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text('{"name":"app"}\n')

    targets = detect_targets(tmp_path)
    assert [(item["relative_path"], item["provider"]) for item in targets] == [
        ("backend", "gradle"),
        ("frontend", "pnpm"),
    ]
    assert compatibility_key("pnpm", frontend, settings) == "pnpm-store-v1:10.4.1"


def test_dependency_fingerprint_ignores_source_only_changes(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (tmp_path / "package.json").write_text('{"name":"demo"}\n')
    target = {"provider": "pnpm", "config": {}}
    first = dependency_fingerprint(target, tmp_path, "pnpm-store-v1:10")
    (tmp_path / "src.ts").write_text("export const changed = true\n")
    assert dependency_fingerprint(target, tmp_path, "pnpm-store-v1:10") == first
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.1'\n")
    assert dependency_fingerprint(target, tmp_path, "pnpm-store-v1:10") != first


def test_gradle_candidate_is_offline_validated_before_publication(
    settings, tmp_path, monkeypatch
):
    async def run():
        settings.ensure_directories()
        database = Database(settings.database_path)
        await database.migrate()
        runner = ProcessRunner(settings)
        repositories = RepositoryManager(settings, runner)
        manager = CacheManager(
            database,
            settings,
            runner,
            repositories,
            WorkspaceManager(settings, runner, repositories),
        )
        tools = tmp_path / "tools"
        tools.mkdir()
        mise = tools / "mise"
        mise.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = install ]; then exit 0; fi\n'
            'if [ "$1" = exec ]; then shift 2; exec "$@"; fi\n'
            "exit 1\n"
        )
        mise.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")

        project = tmp_path / "project"
        project.mkdir()
        wrapper = project / "gradlew"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if printf '%s\\n' \"$@\" | grep -q -- --offline; then\n"
            '  test -f "$GRADLE_RO_DEP_CACHE/modules-2/files-2.1/example/artifact"\n'
            "else\n"
            '  mkdir -p "$GRADLE_USER_HOME/caches/modules-2/files-2.1/example"\n'
            '  echo cached > "$GRADLE_USER_HOME/caches/modules-2/files-2.1/example/artifact"\n'
            "fi\n"
        )
        wrapper.chmod(0o755)
        (project / "settings.gradle.kts").write_text('rootProject.name = "test"\n')
        now = "2026-01-01T00:00:00+00:00"
        await database.execute(
            "INSERT INTO repositories(id,name,remote_url,git_provider,default_ref,status,created_at,updated_at) "
            "VALUES ('repo','Repo','https://example.com/repo.git','generic','main','ready',?,?)",
            (now, now),
        )
        await database.execute(
            "INSERT INTO seed_targets(id,repository_id,relative_path,provider,enabled,config_json,created_at,updated_at) "
            "VALUES ('target','repo','.','gradle',1,'{\"tasks\":[\"classes\"]}',?,?)",
            (now, now),
        )
        group = await manager._ensure_group(
            "gradle", "gradle-modules-v1:8", "linux-test"
        )
        member = {
            "repository": Repository(
                id="repo",
                name="Repo",
                remote_url="https://example.com/repo.git",
                git_provider="generic",
                default_ref="main",
            ),
            "target": {
                "id": "target",
                "provider": "gradle",
                "config": {"tasks": ["classes"]},
            },
            "root": project,
            "commit": "a" * 40,
            "fingerprint": "fingerprint",
        }
        snapshot_id = await manager._seed_group(group, [member], [member], "full")
        snapshot = await database.fetch_one(
            "SELECT * FROM cache_snapshots WHERE id=?", (snapshot_id,)
        )
        assert snapshot is not None
        assert snapshot["status"] == "published"
        assert await manager.verify_snapshot(snapshot_id)
        active = await database.fetch_one(
            "SELECT * FROM cache_groups WHERE id=?", (group["id"],)
        )
        assert active is not None
        assert active["active_snapshot_id"] == snapshot_id

    asyncio.run(run())


def test_pnpm_candidate_is_offline_validated_and_publication_is_stable(
    settings, tmp_path, monkeypatch
):
    async def run():
        settings.ensure_directories()
        database = Database(settings.database_path)
        await database.migrate()
        runner = ProcessRunner(settings)
        repositories = RepositoryManager(settings, runner)
        manager = CacheManager(
            database,
            settings,
            runner,
            repositories,
            WorkspaceManager(settings, runner, repositories),
        )
        tools = tmp_path / "pnpm-tools"
        tools.mkdir()
        mise = tools / "mise"
        mise.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = install ]; then exit 0; fi\n'
            'if [ "$1" != exec ]; then exit 1; fi\n'
            "shift 2\n"
            'if [ "$1" = pnpm ] && [ "$2" = --version ]; then echo 10.4.1; exit 0; fi\n'
            'command="$2"\n'
            "shift 2\n"
            'store=""\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = --store-dir ]; then store="$2"; shift 2; else shift; fi\n'
            "done\n"
            'if [ "$command" = fetch ]; then mkdir -p "$store/v10/files"; echo cached > "$store/v10/files/pkg"; exit 0; fi\n'
            'if [ "$command" = install ]; then test -f "$store/v10/files/pkg"; exit; fi\n'
            "exit 1\n"
        )
        mise.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")

        project = tmp_path / "pnpm-project"
        project.mkdir()
        (project / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (project / "package.json").write_text(
            '{"name":"demo","packageManager":"pnpm@10.4.1"}\n'
        )
        now = "2026-01-01T00:00:00+00:00"
        await database.execute(
            "INSERT INTO repositories(id,name,remote_url,git_provider,default_ref,status,created_at,updated_at) "
            "VALUES ('repo','Repo','https://example.com/repo.git','generic','main','ready',?,?)",
            (now, now),
        )
        await database.execute(
            "INSERT INTO seed_targets(id,repository_id,relative_path,provider,enabled,config_json,created_at,updated_at) "
            "VALUES ('target','repo','.','pnpm',1,'{}',?,?)",
            (now, now),
        )
        group = await manager._ensure_group(
            "pnpm", "pnpm-store-v1:10.4.1", "linux-test"
        )
        member = {
            "repository": Repository(
                id="repo",
                name="Repo",
                remote_url="https://example.com/repo.git",
                git_provider="generic",
                default_ref="main",
            ),
            "target": {"id": "target", "provider": "pnpm", "config": {}},
            "root": project,
            "commit": "b" * 40,
            "fingerprint": "fingerprint",
        }
        first = await manager._seed_group(group, [member], [member], "full")
        refreshed = await database.fetch_one(
            "SELECT * FROM cache_groups WHERE id=?", (group["id"],)
        )
        assert refreshed is not None
        active_before_failure = refreshed["active_snapshot_id"]

        async def reject_candidate(*_args, **_kwargs):
            raise RuntimeError("offline validation failed")

        monkeypatch.setattr(manager, "_validate_candidate", reject_candidate)
        with pytest.raises(RuntimeError, match="offline validation failed"):
            await manager._seed_group(refreshed, [member], [member], "force")
        after_failure = await database.fetch_one(
            "SELECT active_snapshot_id FROM cache_groups WHERE id=?", (group["id"],)
        )
        assert after_failure is not None
        assert after_failure["active_snapshot_id"] == active_before_failure
        assert await manager.verify_snapshot(first)

    asyncio.run(run())


def test_incremental_seed_runs_only_changed_targets(settings, tmp_path, monkeypatch):
    async def run():
        settings.ensure_directories()
        database = Database(settings.database_path)
        await database.migrate()
        runner = ProcessRunner(settings)
        repositories = RepositoryManager(settings, runner)
        manager = CacheManager(
            database,
            settings,
            runner,
            repositories,
            WorkspaceManager(settings, runner, repositories),
        )
        checkout = tmp_path / "checkout"
        source_root = checkout / "workspace"
        for name in ("a", "b"):
            target_root = source_root / name
            target_root.mkdir(parents=True)
            (target_root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
            (target_root / "package.json").write_text(
                f'{{"name":"{name}","packageManager":"pnpm@10"}}\n'
            )
        now = "2026-01-01T00:00:00+00:00"
        await database.execute(
            "INSERT INTO repositories(id,name,remote_url,git_provider,default_ref,status,created_at,updated_at) "
            "VALUES ('repo','Repo','https://example.com/repo.git','generic','main','ready',?,?)",
            (now, now),
        )
        for name in ("a", "b"):
            await database.execute(
                "INSERT INTO seed_targets(id,repository_id,relative_path,provider,enabled,config_json,created_at,updated_at) "
                "VALUES (?, 'repo', ?, 'pnpm', 1, '{}', ?, ?)",
                (f"target-{name}", name, now, now),
            )
        group = await manager._ensure_group("pnpm", "pnpm-store-v1:10", "linux-arm64")
        await database.execute(
            "INSERT INTO cache_snapshots(id,cache_group_id,status,artifact_path,artifact_sha256,artifact_size,manifest_path,created_at,published_at) "
            "VALUES ('snapshot-old', ?, 'published', '/unused/archive', 'digest', 1, '/unused/manifest', ?, ?)",
            (group["id"], now, now),
        )
        await database.execute(
            "UPDATE cache_groups SET active_snapshot_id='snapshot-old' WHERE id=?",
            (group["id"],),
        )
        fingerprint_a = dependency_fingerprint(
            {"id": "target-a", "provider": "pnpm", "config": {}},
            source_root / "a",
            "pnpm-store-v1:10",
        )
        await database.execute(
            "INSERT INTO cache_seed_states(target_id,cache_group_id,commit_sha,fingerprint,snapshot_id,seeded_at) "
            "VALUES ('target-a', ?, ?, ?, 'snapshot-old', ?)",
            (group["id"], "a" * 40, fingerprint_a, now),
        )

        async def source(_repository, _token):
            return checkout, source_root, "a" * 40

        async def mutable(_group):
            return True

        changed: list[str] = []

        async def seed_group(_group, _members, changed_members, _mode):
            changed.extend(member["target"]["id"] for member in changed_members)
            return "snapshot-new"

        monkeypatch.setattr(manager, "_source", source)
        monkeypatch.setattr(manager, "_ensure_mutable_cache", mutable)
        monkeypatch.setattr(manager, "_seed_group", seed_group)
        monkeypatch.setattr("kagemusha.cache.platform.machine", lambda: "arm64")
        result = await manager.seed(
            [
                Repository(
                    id="repo",
                    name="Repo",
                    remote_url="https://example.com/repo.git",
                    git_provider="generic",
                    default_ref="main",
                )
            ],
            {"repo": None},
            providers={"pnpm"},
            mode="incremental",
        )
        assert result["skipped_target_ids"] == ["target-a"]
        assert changed == ["target-b"]

    asyncio.run(run())


def test_artifact_recovery_removes_interrupted_publications(tmp_path):
    async def run():
        store = LocalArtifactStore(tmp_path / "snapshots")
        candidate = tmp_path / "candidate.tar.zst"
        candidate.write_bytes(b"cache")
        first, archive, manifest, _, _ = await store.publish(
            "group", candidate, {"provider": "pnpm", "created_at": "one"}
        )
        second, _, _, _, _ = await store.publish(
            "group", candidate, {"provider": "pnpm", "created_at": "two"}
        )
        assert first == second
        orphan = archive.parent / "orphan.tar.zst"
        orphan.write_bytes(b"orphan")
        temporary = archive.parent / ".partial"
        temporary.write_bytes(b"partial")
        assert store.recover({archive, manifest}) == 2
        assert archive.is_file() and manifest.is_file()
        assert not orphan.exists() and not temporary.exists()

    asyncio.run(run())
