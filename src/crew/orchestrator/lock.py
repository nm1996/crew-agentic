"""Per-run driver lock.

Exactly one CREW process may drive a run at a time. The underlying OS lock is
released by the kernel when the holder dies, so a killed process never leaves
a stale lock (see :mod:`crew.system.locking`).
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from crew.domain.errors import RunLockedError
from crew.system.locking import FileLock


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = FileLock(path)

    def try_acquire(self) -> bool:
        return self._lock.try_acquire()

    def release(self) -> None:
        self._lock.release()

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
