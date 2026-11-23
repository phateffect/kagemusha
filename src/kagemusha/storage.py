from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import anyio


class LocalArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def snapshot_paths(self, group: str, snapshot_id: str) -> tuple[Path, Path]:
        directory = self.root / group
        return directory / f"{snapshot_id}.tar.zst", directory / f"{snapshot_id}.json"

    async def publish(
        self,
        group: str,
        candidate_archive: Path,
        manifest: dict[str, Any],
    ) -> tuple[str, Path, Path, str, int]:
        archive_sha256 = await anyio.to_thread.run_sync(_sha256, candidate_archive)
        identity_manifest = {
            key: value
            for key, value in manifest.items()
            if key not in {"created_at", "published_at"}
        }
        identity = hashlib.sha256(
            json.dumps(
                {**identity_manifest, "archive_sha256": archive_sha256},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        snapshot_id = f"snapshot-{identity[:32]}"
        archive_path, manifest_path = self.snapshot_paths(group, snapshot_id)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if archive_path.is_file() and manifest_path.is_file():
            existing_digest = await anyio.to_thread.run_sync(_sha256, archive_path)
            if existing_digest == archive_sha256:
                return (
                    snapshot_id,
                    archive_path,
                    manifest_path,
                    existing_digest,
                    archive_path.stat().st_size,
                )
        manifest = {
            **manifest,
            "snapshot_id": snapshot_id,
            "archive_sha256": archive_sha256,
            "archive_size": candidate_archive.stat().st_size,
        }
        temporary_archive = archive_path.with_name(
            f".{archive_path.name}.{uuid.uuid4().hex}"
        )
        temporary_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{uuid.uuid4().hex}"
        )
        try:
            await anyio.to_thread.run_sync(
                shutil.copyfile, candidate_archive, temporary_archive
            )
            temporary_manifest.write_text(
                json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary_archive, archive_path)
            os.replace(temporary_manifest, manifest_path)
        finally:
            temporary_archive.unlink(missing_ok=True)
            temporary_manifest.unlink(missing_ok=True)
        return (
            snapshot_id,
            archive_path,
            manifest_path,
            archive_sha256,
            archive_path.stat().st_size,
        )

    async def verify(self, archive: Path, expected_sha256: str) -> bool:
        return (
            archive.is_file()
            and await anyio.to_thread.run_sync(_sha256, archive) == expected_sha256
        )

    def recover(self, referenced: set[Path]) -> int:
        removed = 0
        if not self.root.exists():
            return removed
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if path.name.startswith(".") or path not in referenced:
                path.unlink(missing_ok=True)
                removed += 1
        return removed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
