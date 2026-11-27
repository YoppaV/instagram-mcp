"""Cross-process navigation lock (``<state>/nav.lock``).

Each Claude Code session starts its own stdio server. Without this lock two
processes could drive Instagram at the same time with the same account (two
Chromium, double the rate). It is an OS advisory lock on an open file —
``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows — so it is released
automatically if the process dies. Acquisition polls with a non-blocking try
so it stays cancellable from asyncio. It does not cross machines (D14).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import IO, Any

POLL_S = 0.2

try:  # POSIX
    import fcntl

    def _try_lock(fh: IO[Any]) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, PermissionError):
            return False

    def _unlock(fh: IO[Any]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

except ImportError:  # pragma: no cover - Windows
    import msvcrt

    def _try_lock(fh: IO[Any]) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh: IO[Any]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


class FileLock:
    """Exclusive lock on ``path``. Not re-entrant; one holder per instance."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[Any] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def try_acquire(self) -> bool:
        if self._fh is not None:
            raise RuntimeError("FileLock is not re-entrant")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":  # msvcrt locks a byte range: make sure it exists
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"\0")
                    fh.flush()
            if _try_lock(fh):
                self._fh = fh
                return True
        except BaseException:
            fh.close()
            raise
        fh.close()
        return False

    async def acquire(self, timeout_s: float) -> bool:
        """Poll until acquired or ``timeout_s`` elapsed. False on timeout."""
        deadline = time.monotonic() + timeout_s
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(POLL_S)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        except OSError:
            pass
        finally:
            fh.close()
