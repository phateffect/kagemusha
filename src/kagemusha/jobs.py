from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .database import Database, json_text, utc_now


class JobQueue:
    def __init__(self, database: Database, lease_seconds: int) -> None:
        self.database = database
        self.lease_seconds = lease_seconds

    async def enqueue(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = f"job-{uuid.uuid4().hex}"
        created_at = utc_now()
        await self.database.execute(
            "INSERT INTO jobs(id, type, status, payload_json, created_at) "
            "VALUES (?, ?, 'queued', ?, ?)",
            (job_id, job_type, json_text(payload), created_at),
        )
        return await self.get(job_id)

    async def get(self, job_id: str) -> dict[str, Any]:
        row = await self.database.fetch_one(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        )
        if row is None:
            raise KeyError(job_id)
        return _decode_job(row)

    async def list_jobs(
        self,
        *,
        job_type: str | None = None,
        job_type_prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if job_type is not None and job_type_prefix is not None:
            raise ValueError("Use either job_type or job_type_prefix")
        if job_type is not None:
            rows = await self.database.fetch_all(
                "SELECT * FROM jobs WHERE type = ? ORDER BY created_at DESC "
                "LIMIT ? OFFSET ?",
                (job_type, limit, offset),
            )
        elif job_type_prefix is not None:
            rows = await self.database.fetch_all(
                "SELECT * FROM jobs WHERE type LIKE ? ORDER BY created_at DESC "
                "LIMIT ? OFFSET ?",
                (f"{job_type_prefix}%", limit, offset),
            )
        else:
            rows = await self.database.fetch_all(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [_decode_job(row) for row in rows]

    async def claim(self, worker_id: str) -> dict[str, Any] | None:
        now = utc_now()
        lease = (datetime.now(UTC) + timedelta(seconds=self.lease_seconds)).isoformat()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "UPDATE jobs SET status='queued', worker_id=NULL, lease_expires_at=NULL, "
                "error='Worker lease expired' "
                "WHERE status='running' AND lease_expires_at < ?",
                (now,),
            )
            row = await (
                await connection.execute(
                    "SELECT * FROM jobs WHERE status='queued' "
                    "ORDER BY created_at, id LIMIT 1"
                )
            ).fetchone()
            if row is None:
                await connection.commit()
                return None
            updated = await connection.execute(
                "UPDATE jobs SET status='running', worker_id=?, lease_expires_at=?, "
                "attempt=attempt+1, started_at=COALESCE(started_at, ?), error=NULL "
                "WHERE id=? AND status='queued'",
                (worker_id, lease, now, row["id"]),
            )
            if updated.rowcount != 1:
                await connection.rollback()
                return None
            claimed = await (
                await connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],))
            ).fetchone()
            await connection.commit()
            return _decode_job(dict(claimed))

    async def heartbeat(self, job_id: str, worker_id: str) -> bool:
        lease = (datetime.now(UTC) + timedelta(seconds=self.lease_seconds)).isoformat()
        return (
            await self.database.execute(
                "UPDATE jobs SET lease_expires_at=? WHERE id=? AND worker_id=? "
                "AND status='running'",
                (lease, job_id, worker_id),
            )
            == 1
        )

    async def succeed(self, job_id: str, result: dict[str, Any]) -> None:
        await self.database.execute(
            "UPDATE jobs SET status='succeeded', result_json=?, finished_at=?, "
            "lease_expires_at=NULL, error=NULL WHERE id=? AND status='running'",
            (json_text(result), utc_now(), job_id),
        )

    async def fail(self, job_id: str, error: str) -> None:
        await self.database.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=?, "
            "lease_expires_at=NULL WHERE id=? AND status='running'",
            (error[-8192:], utc_now(), job_id),
        )

    async def cancel(self, job_id: str) -> bool:
        return (
            await self.database.execute(
                "UPDATE jobs SET status='cancelled', finished_at=?, lease_expires_at=NULL "
                "WHERE id=? AND status IN ('queued', 'running')",
                (utc_now(), job_id),
            )
            == 1
        )


def _decode_job(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    raw_result = result.pop("result_json")
    result["result"] = json.loads(raw_result) if raw_result else None
    return result
