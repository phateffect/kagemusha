from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self):
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            await connection.close()

    async def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied_rows = await (
                await connection.execute("SELECT version FROM schema_migrations")
            ).fetchall()
            applied = {row["version"] for row in applied_rows}
            root = resources.files("kagemusha").joinpath("migrations")
            for migration in sorted(root.iterdir(), key=lambda item: item.name):
                if not migration.name.endswith(".sql") or migration.name in applied:
                    continue
                await connection.executescript(migration.read_text())
                await connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.name, utc_now()),
                )
            await connection.commit()

    async def execute(self, sql: str, parameters: Iterable[Any] = ()) -> int:
        async with self.connect() as connection:
            cursor = await connection.execute(sql, tuple(parameters))
            await connection.commit()
            return cursor.rowcount

    async def fetch_one(
        self, sql: str, parameters: Iterable[Any] = ()
    ) -> dict[str, Any] | None:
        async with self.connect() as connection:
            row = await (await connection.execute(sql, tuple(parameters))).fetchone()
            return dict(row) if row is not None else None

    async def fetch_all(
        self, sql: str, parameters: Iterable[Any] = ()
    ) -> list[dict[str, Any]]:
        async with self.connect() as connection:
            rows = await (await connection.execute(sql, tuple(parameters))).fetchall()
            return [dict(row) for row in rows]

    async def integrity_check(self) -> bool:
        row = await self.fetch_one("PRAGMA integrity_check")
        return row is not None and next(iter(row.values())) == "ok"

    async def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = await aiosqlite.connect(self.path)
        target = await aiosqlite.connect(destination)
        try:
            await source.backup(target)
        finally:
            await target.close()
            await source.close()


def json_text(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
