from __future__ import annotations

import os
import shutil
from pathlib import Path

import anyio

from .config import Settings
from .errors import ErrorCode, ProcessFailure, ServiceError
from .process import ProcessRunner, isolated_environment


def _directory_size(path: Path) -> int:
    total = 0
    for root, directories, files in os.walk(path):
        directories[:] = [
            name for name in directories if not (Path(root) / name).is_symlink()
        ]
        for name in files:
            item = Path(root) / name
            try:
                total += item.lstat().st_size
            except FileNotFoundError:
                pass
    return total


def _check_temp_storage(settings: Settings, path: Path) -> None:
    used = _directory_size(path)
    if used >= settings.temp_budget_bytes:
        raise ServiceError(
            ErrorCode.INSUFFICIENT_STORAGE, "Temporary storage budget exceeded"
        )
    if shutil.disk_usage(settings.tmp_dir).free < 16 * 1024**2:
        raise ServiceError(
            ErrorCode.INSUFFICIENT_STORAGE, "Insufficient temporary storage"
        )


async def ensure_temp_capacity(settings: Settings, path: Path | None = None) -> None:
    await anyio.to_thread.run_sync(
        _check_temp_storage,
        settings,
        path or settings.tmp_dir,
    )


async def create_archive(
    settings: Settings, runner: ProcessRunner, root: Path
) -> tuple[Path, int]:
    await ensure_temp_capacity(settings)
    await ensure_temp_capacity(settings, root)
    archive = root / "workspace.tar.zst"
    with isolated_environment(settings) as env:
        try:
            await runner.run(
                (
                    "tar",
                    "-C",
                    str(root),
                    "--use-compress-program",
                    f"zstd -T{settings.zstd_threads} -{settings.zstd_level}",
                    "-cf",
                    str(archive),
                    "workspace",
                ),
                env=env,
                operation="create tar.zst archive",
            )
        except ProcessFailure as error:
            if "no space left on device" in error.stderr.lower():
                raise ServiceError(
                    ErrorCode.INSUFFICIENT_STORAGE, "Insufficient archive storage"
                ) from error
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR, "Archive operation failed"
            ) from error
    size = archive.stat().st_size
    if size > settings.max_archive_bytes:
        raise ServiceError(ErrorCode.ARCHIVE_TOO_LARGE)
    await ensure_temp_capacity(settings, root)
    return archive, size
