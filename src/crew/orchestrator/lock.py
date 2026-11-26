"""Per-run driver lock.

Exactly one CREW process may drive a run at a time. ``flock`` is released by the
kernel when the holder dies, so a killed process never leaves a stale lock.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

from crew.domain.errors import RunLockedError


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> RunLock:
        if not self.try_acquire():
            raise RunLockedError(
                "Another CREW process is currently driving this run.",
                hint="Wait for it to finish, or use `crew stop <run-id>` to request cancellation.",
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
