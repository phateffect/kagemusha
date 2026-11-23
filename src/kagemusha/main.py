from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn

from .config import Settings
from .database import Database
from .jobs import JobQueue
from .storage import LocalArtifactStore
from .worker import Worker


def main() -> None:
    parser = argparse.ArgumentParser(prog="kagemusha")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    subparsers.add_parser("worker-health")
    db = subparsers.add_parser("db")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate")
    backup = db_commands.add_parser("backup")
    backup.add_argument("destination", nargs="?")
    cache = subparsers.add_parser("cache")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_commands.add_parser("verify")
    cache_commands.add_parser("gc")
    arguments = parser.parse_args()
    settings = Settings()

    if arguments.command == "serve":
        uvicorn.run(
            "kagemusha.asgi:app",
            host=arguments.host,
            port=arguments.port,
            workers=1,
            access_log=False,
        )
    elif arguments.command == "worker":
        asyncio.run(Worker(settings).run(once=arguments.once))
    elif arguments.command == "worker-health":
        _worker_health(settings)
    elif arguments.command == "db":
        asyncio.run(_database_command(settings, arguments.db_command, arguments))
    elif arguments.command == "cache":
        asyncio.run(_cache_command(settings, arguments.cache_command))


async def _database_command(settings: Settings, command: str, arguments: Any) -> None:
    settings.ensure_directories()
    database = Database(settings.database_path)
    await database.migrate()
    if command == "backup":
        destination = (
            Path(arguments.destination)
            if arguments.destination
            else settings.backups_dir
            / f"kagemusha-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
        )
        await database.backup(destination)
        print(destination)


async def _cache_command(settings: Settings, command: str) -> None:
    database = Database(settings.database_path)
    await database.migrate()
    if command == "gc":
        job = await JobQueue(database, settings.job_lease_seconds).enqueue(
            "cache.gc", {}
        )
        print(job["id"])
        return
    store = LocalArtifactStore(settings.cache_snapshots_dir)
    snapshots = await database.fetch_all(
        "SELECT id, artifact_path, artifact_sha256 FROM cache_snapshots "
        "WHERE status='published'"
    )
    failed = [
        snapshot["id"]
        for snapshot in snapshots
        if not await store.verify(
            Path(snapshot["artifact_path"]), snapshot["artifact_sha256"]
        )
    ]
    if failed:
        print("Invalid snapshots: " + ", ".join(failed), file=sys.stderr)
        raise SystemExit(1)
    print(f"Verified {len(snapshots)} snapshots")


def _worker_health(settings: Settings) -> None:
    path = settings.jobs_dir / "worker-health"
    try:
        timestamp = datetime.fromisoformat(path.read_text().strip())
    except OSError, ValueError:
        raise SystemExit(1) from None
    age = (datetime.now(UTC) - timestamp).total_seconds()
    if age > max(30, settings.worker_poll_seconds * 5):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
