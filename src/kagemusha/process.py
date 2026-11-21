from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from .config import Settings
from .errors import ErrorCode, ProcessFailure, ServiceError


@contextmanager
def isolated_environment(settings: Settings) -> Generator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="home-", dir=settings.tmp_dir) as home:
        yield {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": home,
            "XDG_CONFIG_HOME": str(Path(home) / "xdg"),
            "LANG": "C",
            "LC_ALL": "C",
        }


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result[-8192:]


class ProcessRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_git)

    async def run(
        self,
        arguments: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: int | None = None,
        secrets: Sequence[str] = (),
        operation: str,
    ) -> str:
        async with self._semaphore:
            try:
                process = await asyncio.create_subprocess_exec(
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=dict(env),
                    start_new_session=True,
                )
            except OSError as error:
                if error.errno == 28:
                    raise ServiceError(
                        ErrorCode.INSUFFICIENT_STORAGE,
                        "Insufficient temporary storage",
                    ) from error
                raise ServiceError(
                    ErrorCode.INTERNAL_ERROR, "Unable to start subprocess"
                ) from error

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=(
                        self.settings.git_timeout_seconds
                        if timeout is None
                        else timeout
                    ),
                )
            except TimeoutError:
                await self._terminate(process)
                raise ServiceError(
                    ErrorCode.GIT_TIMEOUT, f"{operation} timed out"
                ) from None
            except asyncio.CancelledError:
                await self._terminate(process)
                raise
            if process.returncode != 0:
                safe_stderr = redact(stderr.decode("utf-8", "replace"), secrets)
                raise ProcessFailure(operation, process.returncode or 1, safe_stderr)
            return stdout.decode("utf-8", "replace").strip()

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.communicate()
