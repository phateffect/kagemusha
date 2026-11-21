from __future__ import annotations

import asyncio
import fcntl
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path


@asynccontextmanager
async def file_lock(path: Path) -> AsyncGenerator[None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    locked = False
    try:
        while not locked:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
