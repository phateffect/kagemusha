from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import anyio

from .database import Database, json_text, utc_now
from .errors import ErrorCode, ProcessFailure, ServiceError
from .locks import file_lock
from .process import ProcessRunner
from .repository import Repository, RepositoryManager
from .storage import LocalArtifactStore
from .workspace import WorkspaceManager, cleanup_workspace

_GRADLE_VERSION = re.compile(r"gradle-([0-9][0-9A-Za-z.-]*)-(?:bin|all)\.zip")


class CacheManager:
    def __init__(
        self,
        database: Database,
        settings,
        runner: ProcessRunner,
        repositories: RepositoryManager,
        workspaces: WorkspaceManager,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runner = runner
        self.repositories = repositories
        self.workspaces = workspaces
        self.storage = LocalArtifactStore(settings.cache_snapshots_dir)

    async def discover(
        self, repository: Repository, token: str | None
    ) -> list[dict[str, Any]]:
        root, source, _commit = await self._source(repository, token)
        try:
            detected = detect_targets(source)
            now = utc_now()
            for target in detected:
                target_id = _target_id(
                    repository.id, target["relative_path"], target["provider"]
                )
                await self.database.execute(
                    "INSERT INTO seed_targets(id, repository_id, relative_path, provider, "
                    "enabled, config_json, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(repository_id, relative_path, provider) DO UPDATE SET "
                    "updated_at=excluded.updated_at",
                    (
                        target_id,
                        repository.id,
                        target["relative_path"],
                        target["provider"],
                        json_text(target["config"]),
                        now,
                        now,
                    ),
                )
            return await self.targets(repository.id)
        finally:
            await cleanup_workspace(root)

    async def targets(self, repository_id: str) -> list[dict[str, Any]]:
        rows = await self.database.fetch_all(
            "SELECT * FROM seed_targets WHERE repository_id=? ORDER BY relative_path, provider",
            (repository_id,),
        )
        for row in rows:
            row["enabled"] = bool(row["enabled"])
            row["config"] = json.loads(row.pop("config_json"))
        return rows

    async def seed(
        self,
        repositories: list[Repository],
        credentials: dict[str, str | None],
        *,
        providers: set[str] | None,
        mode: str,
        requested_group: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sources: dict[str, tuple[Path, Path, str]] = {}
        try:
            selected: list[tuple[Repository, dict[str, Any], Path, str]] = []
            for repository in repositories:
                token = credentials.get(repository.id)
                root, source, commit = await self._source(repository, token)
                sources[repository.id] = (root, source, commit)
                targets = await self.targets(repository.id)
                if not targets:
                    await self.discover(repository, token)
                    targets = await self.targets(repository.id)
                for target in targets:
                    if target["enabled"] and (
                        providers is None or target["provider"] in providers
                    ):
                        selected.append((repository, target, source, commit))

            grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(
                list
            )
            for repository, target, source, commit in selected:
                target_root = _safe_target_path(source, target["relative_path"])
                compatibility = compatibility_key(
                    target["provider"], target_root, self.settings
                )
                platform_key = f"linux-{platform.machine().lower()}"
                fingerprint = dependency_fingerprint(target, target_root, compatibility)
                grouped[(target["provider"], compatibility, platform_key)].append(
                    {
                        "repository": repository,
                        "target": target,
                        "root": target_root,
                        "commit": commit,
                        "fingerprint": fingerprint,
                    }
                )

            published: list[str] = []
            skipped: list[str] = []
            for (provider, compatibility, platform_key), members in grouped.items():
                group = await self._ensure_group(provider, compatibility, platform_key)
                if requested_group is not None:
                    requested_provider = requested_group["provider"]
                    matches = group["id"] == requested_group["id"] or (
                        requested_provider == "gradle-wrapper"
                        and provider == "gradle"
                        and compatibility == requested_group["compatibility_key"]
                        and platform_key == requested_group["platform"]
                    )
                    if not matches:
                        continue
                changed = []
                for member in members:
                    state = await self.database.fetch_one(
                        "SELECT * FROM cache_seed_states WHERE target_id=? AND cache_group_id=?",
                        (member["target"]["id"], group["id"]),
                    )
                    if (
                        mode == "incremental"
                        and state
                        and state["fingerprint"] == member["fingerprint"]
                    ):
                        skipped.append(member["target"]["id"])
                    else:
                        changed.append(member)
                mutable_ready = (
                    True if mode == "full" else await self._ensure_mutable_cache(group)
                )
                if not changed and group["active_snapshot_id"] and mutable_ready:
                    continue
                if not mutable_ready:
                    changed = list(members)
                snapshot_id = await self._seed_group(
                    group, members, changed if mode != "full" else members, mode
                )
                published.append(snapshot_id)
                if provider == "gradle":
                    wrapper_snapshot = await self._publish_gradle_wrapper(
                        group, members
                    )
                    if wrapper_snapshot is not None:
                        published.append(wrapper_snapshot)
            return {"published_snapshot_ids": published, "skipped_target_ids": skipped}
        finally:
            for root, _source_path, _commit in sources.values():
                await cleanup_workspace(root)

    async def _ensure_mutable_cache(self, group: dict[str, Any]) -> bool:
        source = _snapshot_source(
            group["provider"], self.settings.cache_work_dir / group["id"]
        )
        if source.exists():
            return True
        snapshot_id = group.get("active_snapshot_id")
        if not snapshot_id:
            return False
        snapshot = await self.database.fetch_one(
            "SELECT * FROM cache_snapshots WHERE id=? AND status='published'",
            (snapshot_id,),
        )
        if snapshot is None or not await self.storage.verify(
            Path(snapshot["artifact_path"]), snapshot["artifact_sha256"]
        ):
            return False
        group_work = self.settings.cache_work_dir / group["id"]
        if group["provider"] == "gradle":
            destination = group_work / "caches"
        else:
            destination = group_work.parent
            if group_work.exists():
                await anyio.to_thread.run_sync(shutil.rmtree, group_work, True)
        destination.mkdir(parents=True, exist_ok=True)
        await self.runner.run(
            (
                "tar",
                "--zstd",
                "-xf",
                snapshot["artifact_path"],
                "-C",
                str(destination),
            ),
            env=self._tool_environment(destination),
            timeout=self.settings.seed_timeout_seconds,
            operation="restore mutable cache from snapshot",
        )
        return source.exists()

    async def verify_snapshot(self, snapshot_id: str) -> bool:
        row = await self.database.fetch_one(
            "SELECT * FROM cache_snapshots WHERE id=?", (snapshot_id,)
        )
        if row is None:
            raise KeyError(snapshot_id)
        return await self.storage.verify(
            Path(row["artifact_path"]), row["artifact_sha256"]
        )

    async def gc(self) -> dict[str, int]:
        removed = 0
        groups = await self.database.fetch_all("SELECT * FROM cache_groups")
        for group in groups:
            snapshots = await self.database.fetch_all(
                "SELECT * FROM cache_snapshots WHERE cache_group_id=? AND status='published' "
                "ORDER BY published_at DESC",
                (group["id"],),
            )
            keep = {group["active_snapshot_id"]}
            previous = [
                row["id"]
                for row in snapshots
                if row["id"] != group["active_snapshot_id"]
            ]
            keep.update(previous[: self.settings.snapshot_retention_count])
            keep.update(row["id"] for row in snapshots if row["pinned"])
            keep.update(row["id"] for row in snapshots if row["download_count"] > 0)
            for row in snapshots:
                if row["id"] in keep:
                    continue
                Path(row["artifact_path"]).unlink(missing_ok=True)
                Path(row["manifest_path"]).unlink(missing_ok=True)
                await self.database.execute(
                    "DELETE FROM cache_snapshots WHERE id=?", (row["id"],)
                )
                removed += 1
        return {"removed_snapshots": removed}

    async def recover_publications(self) -> dict[str, int]:
        failed = 0
        referenced: set[Path] = set()
        rows = await self.database.fetch_all("SELECT * FROM cache_snapshots")
        for row in rows:
            archive = Path(row["artifact_path"])
            manifest = Path(row["manifest_path"])
            valid = (
                row["status"] == "published"
                and manifest.is_file()
                and await self.storage.verify(archive, row["artifact_sha256"])
            )
            if valid:
                referenced.update((archive, manifest))
                continue
            await self.database.execute(
                "UPDATE cache_snapshots SET status='failed' WHERE id=?", (row["id"],)
            )
            await self.database.execute(
                "UPDATE cache_groups SET active_snapshot_id=NULL "
                "WHERE active_snapshot_id=?",
                (row["id"],),
            )
            failed += 1
        removed = await anyio.to_thread.run_sync(self.storage.recover, referenced)
        for staging in self.settings.cache_staging_dir.iterdir():
            if staging.is_dir():
                await anyio.to_thread.run_sync(shutil.rmtree, staging, True)
            else:
                staging.unlink(missing_ok=True)
        return {"failed_snapshots": failed, "removed_orphan_files": removed}

    async def _source(
        self, repository: Repository, token: str | None
    ) -> tuple[Path, Path, str]:
        from .refs import resolve_ref, workspace_branch

        remote_ref = resolve_ref(repository.default_ref, repository.git_provider)
        branch, tracks = workspace_branch(repository.default_ref, remote_ref, None)
        result = await self.workspaces.create(
            repository,
            remote_ref,
            branch,
            tracks,
            token,
            1,
            produce_archive=False,
        )
        return result.root, result.root / "workspace", result.commit

    async def _ensure_group(
        self, provider: str, compatibility: str, platform_key: str
    ) -> dict[str, Any]:
        group_id = (
            "group-"
            + hashlib.sha256(
                f"{provider}\0{compatibility}\0{platform_key}".encode()
            ).hexdigest()[:24]
        )
        await self.database.execute(
            "INSERT INTO cache_groups(id, provider, compatibility_key, platform, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, compatibility_key, platform) DO NOTHING",
            (group_id, provider, compatibility, platform_key, utc_now()),
        )
        row = await self.database.fetch_one(
            "SELECT * FROM cache_groups WHERE id=?", (group_id,)
        )
        assert row is not None
        return row

    async def _seed_group(
        self,
        group: dict[str, Any],
        members: list[dict[str, Any]],
        changed: list[dict[str, Any]],
        mode: str,
    ) -> str:
        async with file_lock(self.settings.locks_dir / f"cache-{group['id']}.lock"):
            return await self._seed_group_locked(group, members, changed, mode)

    async def _seed_group_locked(
        self,
        group: dict[str, Any],
        members: list[dict[str, Any]],
        changed: list[dict[str, Any]],
        mode: str,
    ) -> str:
        group_work = self.settings.cache_work_dir / group["id"]
        if mode == "full" and group_work.exists():
            await anyio.to_thread.run_sync(shutil.rmtree, group_work, True)
        group_work.mkdir(parents=True, exist_ok=True)
        for member in changed:
            if group["provider"] == "gradle":
                await self._seed_gradle(member, group_work)
            else:
                await self._seed_pnpm(member, group_work)
        source = _snapshot_source(group["provider"], group_work)
        if not source.exists():
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR, "Cache provider produced no store"
            )
        staging = self.settings.cache_staging_dir / f"snapshot-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        archive = staging / "cache.tar.zst"
        try:
            await self._archive(source, archive)
            manifest = {
                "provider": group["provider"],
                "cache_group_id": group["id"],
                "platform": group["platform"],
                "compatibility_key": group["compatibility_key"],
                "created_at": utc_now(),
                "tool_versions": await self._tool_versions(group["provider"]),
                "source_targets": [
                    {
                        "target_id": member["target"]["id"],
                        "repository_id": member["repository"].id,
                        "commit_sha": member["commit"],
                        "fingerprint": member["fingerprint"],
                    }
                    for member in members
                ],
                "validation_result": await self._validate_candidate(
                    group, members, archive, staging
                ),
            }
            (
                snapshot_id,
                artifact,
                manifest_path,
                digest,
                size,
            ) = await self.storage.publish(group["id"], archive, manifest)
            await self._publish_metadata(
                group, snapshot_id, artifact, manifest_path, digest, size, members
            )
            return snapshot_id
        finally:
            await anyio.to_thread.run_sync(shutil.rmtree, staging, True)

    async def _publish_gradle_wrapper(
        self, dependency_group: dict[str, Any], members: list[dict[str, Any]]
    ) -> str | None:
        source = (
            self.settings.cache_work_dir / dependency_group["id"] / "wrapper" / "dists"
        )
        if not source.is_dir():
            return None
        wrapper_group = await self._ensure_group(
            "gradle-wrapper",
            dependency_group["compatibility_key"],
            dependency_group["platform"],
        )
        async with file_lock(
            self.settings.locks_dir / f"cache-{wrapper_group['id']}.lock"
        ):
            return await self._publish_gradle_wrapper_locked(
                dependency_group, wrapper_group, members, source
            )

    async def _publish_gradle_wrapper_locked(
        self,
        dependency_group: dict[str, Any],
        wrapper_group: dict[str, Any],
        members: list[dict[str, Any]],
        source: Path,
    ) -> str:
        staging = self.settings.cache_staging_dir / f"wrapper-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        archive = staging / "wrapper.tar.zst"
        try:
            await self._archive(source, archive)
            manifest_data = {
                "provider": "gradle-wrapper",
                "cache_group_id": wrapper_group["id"],
                "platform": wrapper_group["platform"],
                "compatibility_key": wrapper_group["compatibility_key"],
                "created_at": utc_now(),
                "tool_versions": {"gradle": dependency_group["compatibility_key"]},
                "source_targets": [
                    {
                        "target_id": member["target"]["id"],
                        "repository_id": member["repository"].id,
                        "commit_sha": member["commit"],
                        "fingerprint": member["fingerprint"],
                    }
                    for member in members
                ],
                "validation_result": {
                    "status": "passed",
                    "detail": "wrapper distribution used during dependency validation",
                },
            }
            (
                snapshot_id,
                artifact,
                manifest_path,
                digest,
                size,
            ) = await self.storage.publish(wrapper_group["id"], archive, manifest_data)
            await self._publish_metadata(
                wrapper_group,
                snapshot_id,
                artifact,
                manifest_path,
                digest,
                size,
                members,
            )
            return snapshot_id
        finally:
            await anyio.to_thread.run_sync(shutil.rmtree, staging, True)

    async def _validate_candidate(
        self,
        group: dict[str, Any],
        members: list[dict[str, Any]],
        archive: Path,
        staging: Path,
    ) -> dict[str, Any]:
        restored = staging / "restored"
        restored.mkdir()
        await self.runner.run(
            ("tar", "--zstd", "-xf", str(archive), "-C", str(restored)),
            env=self._tool_environment(restored),
            timeout=self.settings.seed_timeout_seconds,
            operation="restore candidate cache snapshot",
        )
        restored_store = next(restored.iterdir(), None)
        if restored_store is None or not restored_store.is_dir():
            raise ServiceError(ErrorCode.INTERNAL_ERROR, "Cache snapshot is empty")
        validated: list[str] = []
        for member in members:
            validation_root = staging / "validation" / member["target"]["id"]
            await anyio.to_thread.run_sync(
                shutil.copytree,
                member["root"],
                validation_root,
                False,
                _validation_ignore,
            )
            await self._mise_install(validation_root)
            if group["provider"] == "gradle":
                writable_home = staging / "gradle-home" / member["target"]["id"]
                wrapper_source = (
                    self.settings.cache_work_dir / group["id"] / "wrapper" / "dists"
                )
                if wrapper_source.is_dir():
                    await anyio.to_thread.run_sync(
                        shutil.copytree,
                        wrapper_source,
                        writable_home / "wrapper" / "dists",
                        True,
                    )
                env = self._tool_environment(restored_store)
                env.update(
                    {
                        "GRADLE_USER_HOME": str(writable_home),
                        "GRADLE_RO_DEP_CACHE": str(restored),
                    }
                )
                tasks = member["target"]["config"].get(
                    "tasks", ["classes", "testClasses"]
                )
                await self.runner.run(
                    (
                        "mise",
                        "exec",
                        "--",
                        str(validation_root / "gradlew"),
                        *tasks,
                        "--offline",
                        "--no-daemon",
                    ),
                    env=env,
                    cwd=validation_root,
                    timeout=self.settings.seed_timeout_seconds,
                    operation="validate Gradle cache snapshot offline",
                )
            else:
                env = self._tool_environment(restored_store)
                await self.runner.run(
                    (
                        "mise",
                        "exec",
                        "--",
                        "pnpm",
                        "install",
                        "--offline",
                        "--frozen-lockfile",
                        "--store-dir",
                        str(restored_store),
                    ),
                    env=env,
                    cwd=validation_root,
                    timeout=self.settings.seed_timeout_seconds,
                    operation="validate pnpm cache snapshot offline",
                )
            validated.append(member["target"]["id"])
        return {"status": "passed", "target_ids": validated}

    async def _seed_gradle(self, member: dict[str, Any], store: Path) -> None:
        root: Path = member["root"]
        wrapper = root / "gradlew"
        if not wrapper.is_file():
            raise ServiceError(ErrorCode.INVALID_REQUEST, "Gradle wrapper is required")
        tasks = member["target"]["config"].get("tasks", ["classes", "testClasses"])
        await self._mise_install(root)
        env = self._tool_environment(store)
        env["GRADLE_USER_HOME"] = str(store)
        await self.runner.run(
            ("mise", "exec", "--", str(wrapper), *tasks, "--no-daemon"),
            env=env,
            timeout=self.settings.seed_timeout_seconds,
            cwd=root,
            operation="seed Gradle dependencies",
        )

    async def _seed_pnpm(self, member: dict[str, Any], store: Path) -> None:
        root: Path = member["root"]
        await self._mise_install(root)
        env = self._tool_environment(store)
        await self.runner.run(
            (
                "mise",
                "exec",
                "--",
                "pnpm",
                "fetch",
                "--frozen-lockfile",
                "--store-dir",
                str(store),
            ),
            env=env,
            timeout=self.settings.seed_timeout_seconds,
            cwd=root,
            operation="seed pnpm store",
        )

    async def _mise_install(self, root: Path) -> None:
        env = self._tool_environment(self.settings.cache_work_dir)
        await self.runner.run(
            ("mise", "install", "--yes"),
            env=env,
            timeout=self.settings.seed_timeout_seconds,
            cwd=root,
            operation="install project toolchain",
        )

    def _tool_environment(self, store: Path) -> dict[str, str]:
        home = self.settings.jobs_dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "LANG": "C",
            "LC_ALL": "C",
        }
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ):
            if value := os.environ.get(name):
                env[name] = value
        env.update(
            {
                "MISE_DATA_DIR": str(self.settings.mise_data_dir),
                "MISE_YES": "1",
                "MISE_JAVA_VERSION": self.settings.default_java_version,
                "MISE_NODE_VERSION": self.settings.default_node_version,
                "MISE_PNPM_VERSION": self.settings.default_pnpm_version,
                "KAGEMUSHA_CACHE_STORE": str(store),
            }
        )
        return env

    async def _archive(self, source: Path, destination: Path) -> None:
        env = self._tool_environment(source)
        await self.runner.run(
            (
                "tar",
                "-C",
                str(source.parent),
                "--use-compress-program",
                f"zstd -T{self.settings.zstd_threads} -{self.settings.zstd_level}",
                "-cf",
                str(destination),
                source.name,
            ),
            env=env,
            timeout=self.settings.seed_timeout_seconds,
            operation="create cache snapshot",
        )

    async def _tool_versions(self, provider: str) -> dict[str, str]:
        env = self._tool_environment(self.settings.cache_work_dir)
        command = "gradle" if provider == "gradle" else "pnpm"
        try:
            version = await self.runner.run(
                ("mise", "exec", "--", command, "--version"),
                env=env,
                timeout=60,
                operation=f"resolve {provider} version",
            )
        except ProcessFailure, ServiceError:
            version = "project wrapper" if provider == "gradle" else "unknown"
        return {provider: version.splitlines()[0] if version else "unknown"}

    async def _publish_metadata(
        self,
        group: dict[str, Any],
        snapshot_id: str,
        artifact: Path,
        manifest: Path,
        digest: str,
        size: int,
        members: list[dict[str, Any]],
    ) -> None:
        published_at = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "INSERT INTO cache_snapshots(id, cache_group_id, status, "
                "artifact_path, artifact_sha256, artifact_size, manifest_path, created_at, published_at) "
                "VALUES (?, ?, 'published', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status='published', "
                "artifact_path=excluded.artifact_path, "
                "artifact_sha256=excluded.artifact_sha256, "
                "artifact_size=excluded.artifact_size, "
                "manifest_path=excluded.manifest_path, "
                "published_at=excluded.published_at",
                (
                    snapshot_id,
                    group["id"],
                    str(artifact),
                    digest,
                    size,
                    str(manifest),
                    published_at,
                    published_at,
                ),
            )
            for member in members:
                await connection.execute(
                    "INSERT OR REPLACE INTO cache_snapshot_targets"
                    "(snapshot_id, target_id, commit_sha, fingerprint) VALUES (?, ?, ?, ?)",
                    (
                        snapshot_id,
                        member["target"]["id"],
                        member["commit"],
                        member["fingerprint"],
                    ),
                )
                await connection.execute(
                    "INSERT OR REPLACE INTO cache_seed_states"
                    "(target_id, cache_group_id, commit_sha, fingerprint, snapshot_id, seeded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        member["target"]["id"],
                        group["id"],
                        member["commit"],
                        member["fingerprint"],
                        snapshot_id,
                        published_at,
                    ),
                )
            await connection.execute(
                "UPDATE cache_groups SET active_snapshot_id=? WHERE id=?",
                (snapshot_id, group["id"]),
            )
            await connection.commit()


def detect_targets(root: Path) -> list[dict[str, Any]]:
    targets: dict[tuple[str, str], dict[str, Any]] = {}
    ignored = {".git", "node_modules", "build", ".gradle"}
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in ignored]
        path = Path(current)
        relative = path.relative_to(root).as_posix() or "."
        names = set(files)
        if "gradlew" in names and (
            "settings.gradle" in names
            or "settings.gradle.kts" in names
            or (path / "gradle/wrapper/gradle-wrapper.properties").is_file()
        ):
            targets[(relative, "gradle")] = {
                "relative_path": relative,
                "provider": "gradle",
                "config": {"tasks": ["classes", "testClasses"]},
            }
        if "pnpm-lock.yaml" in names:
            targets[(relative, "pnpm")] = {
                "relative_path": relative,
                "provider": "pnpm",
                "config": {},
            }
    return sorted(
        targets.values(), key=lambda item: (item["relative_path"], item["provider"])
    )


def compatibility_key(provider: str, root: Path, settings) -> str:
    if provider == "gradle":
        wrapper = root / "gradle/wrapper/gradle-wrapper.properties"
        text = wrapper.read_text(errors="replace") if wrapper.is_file() else ""
        match = _GRADLE_VERSION.search(text)
        version = match.group(1) if match else "unknown"
        return f"gradle-modules-v1:{version.split('.')[0]}"
    package = root / "package.json"
    version = settings.default_pnpm_version
    if package.is_file():
        try:
            manager = json.loads(package.read_text()).get("packageManager", "")
            if manager.startswith("pnpm@"):
                version = manager.removeprefix("pnpm@")
        except json.JSONDecodeError, OSError:
            pass
    return f"pnpm-store-v1:{version}"


def dependency_fingerprint(
    target: dict[str, Any], root: Path, compatibility: str
) -> str:
    digest = hashlib.sha256()
    digest.update(json_text(target["config"]).encode())
    digest.update(compatibility.encode())
    patterns = (
        {
            "settings.gradle",
            "settings.gradle.kts",
            "build.gradle",
            "build.gradle.kts",
            "gradle.properties",
            "libs.versions.toml",
            "gradle-wrapper.properties",
            "mise.toml",
            ".mise.toml",
            ".tool-versions",
        }
        if target["provider"] == "gradle"
        else {
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "package.json",
            ".npmrc",
            "mise.toml",
            ".mise.toml",
            ".tool-versions",
        }
    )
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name in patterns:
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _safe_target_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_dir():
        raise ServiceError(ErrorCode.INVALID_REQUEST, "Invalid seed target path")
    return path


def _target_id(repository_id: str, relative: str, provider: str) -> str:
    digest = hashlib.sha256(
        f"{repository_id}\0{relative}\0{provider}".encode()
    ).hexdigest()
    return f"target-{digest[:24]}"


def _snapshot_source(provider: str, store: Path) -> Path:
    if provider == "gradle":
        return store / "caches" / "modules-2"
    return store


def _validation_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = {".git", ".gradle", "build", "node_modules"}
    return ignored.intersection(names)
