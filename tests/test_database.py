from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from kagemusha.database import Database
from kagemusha.jobs import JobQueue


def test_migrations_integrity_and_backup(settings, tmp_path):
    async def run():
        settings.ensure_directories()
        database = Database(settings.database_path)
        await database.migrate()
        await database.migrate()
        assert await database.integrity_check()
        backup = tmp_path / "backup.db"
        await database.backup(backup)
        assert backup.is_file()
        assert await Database(backup).integrity_check()

    asyncio.run(run())


def test_atomic_job_claim_and_expired_lease_recovery(settings):
    async def run():
        settings.ensure_directories()
        database = Database(settings.database_path)
        await database.migrate()
        queue = JobQueue(database, 30)
        job = await queue.enqueue("repository.fetch", {"repository_id": "a"})
        first, second = await asyncio.gather(
            queue.claim("worker-a"), queue.claim("worker-b")
        )
        claimed = [item for item in (first, second) if item is not None]
        assert len(claimed) == 1
        await database.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job["id"]),
        )
        recovered = await queue.claim("worker-c")
        assert recovered is not None
        assert recovered["id"] == job["id"]
        assert recovered["attempt"] == 2
        assert recovered["error"] is None
        await queue.succeed(job["id"], {"ok": True})
        assert (await queue.get(job["id"]))["error"] is None

    asyncio.run(run())


def test_job_cancellation_is_durable(settings):
    async def run():
        settings.ensure_directories()
        database = Database(settings.database_path)
        await database.migrate()
        queue = JobQueue(database, 30)
        job = await queue.enqueue("cache.seed", {})
        assert await queue.cancel(job["id"])
        assert (await queue.get(job["id"]))["status"] == "cancelled"
        assert await queue.claim("worker") is None

    asyncio.run(run())
