"""Portable child-process lifecycle: spawning, tree termination and identity.

* POSIX: children start in their own session/process group, so a whole agent
  tree (``claude`` plus its tool subprocesses) can be signalled at once.
* Windows: children start in a new process group without a console window and
  are assigned to a kill-on-close Job Object (:mod:`crew.system.winjob`), so the
  whole tree dies with the job, even grandchildren whose parent already exited.

Termination always walks the process tree with psutil and, on POSIX, also
signals the process group, so grandchildren that outlive their parent are
caught too.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any

import psutil

from crew.system import winjob

IS_WINDOWS = os.name == "nt"
DEFAULT_GRACE_SECONDS = 5.0


def spawn_kwargs() -> dict[str, Any]:
    """Keyword arguments for ``create_subprocess_exec`` that isolate the child tree."""
    if IS_WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        return {"creationflags": flags}
    return {"start_new_session": True}


def _collect_tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    try:
        children = root.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    return [*children, root]


def _signal_group(pid: int, sig: int) -> None:
    if IS_WINDOWS:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def track_child(pid: int) -> None:
    """Register a freshly spawned child for whole-tree termination (Windows Job Object)."""
    winjob.attach(pid)


def kill_tree(pid: int, grace: float = DEFAULT_GRACE_SECONDS) -> bool:
    """Terminate ``pid`` and all of its descendants; escalate to kill after ``grace``.

    Blocking. Returns ``True`` when nothing from the tree is left alive.
    """
    procs = _collect_tree(pid)
    winjob.terminate(pid)  # Windows: ends the whole job at once, if the child had one
    _signal_group(pid, signal.SIGTERM)
    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=grace)
    if alive or not IS_WINDOWS:
        _signal_group(pid, signal.SIGKILL if not IS_WINDOWS else signal.SIGTERM)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
    _, alive = psutil.wait_procs(alive, timeout=grace)
    return not alive


async def terminate_tree(
    proc: asyncio.subprocess.Process, grace: float = DEFAULT_GRACE_SECONDS
) -> bool:
    """Stop an asyncio child and its descendants.

    Safe to call after the child exited normally: leftover grandchildren (for
    example a dev server an agent started) are still cleaned up.
    """
    stopped = await asyncio.to_thread(kill_tree, proc.pid, grace)
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except TimeoutError:
            return False
    return stopped


def process_started_at(pid: int) -> float | None:
    """Creation time of ``pid`` (seconds since epoch), used to detect PID reuse."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def is_same_process(pid: int, started_at: float | None, name_hint: str) -> bool:
    """True if ``pid`` is still the process CREW started (not a reused PID)."""
    try:
        proc = psutil.Process(pid)
        if started_at is not None and abs(proc.create_time() - started_at) > 1.0:
            return False
        cmdline = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return name_hint.casefold() in cmdline.casefold()


def install_termination_handlers() -> None:
    """Treat SIGTERM / SIGHUP (POSIX) and SIGBREAK (Windows) like Ctrl-C.

    ``KeyboardInterrupt`` unwinds through asyncio, which cancels running tasks;
    their cancellation handlers terminate agent process trees before exit.
    """
    names = ("SIGTERM", "SIGBREAK") if IS_WINDOWS else ("SIGTERM", "SIGHUP")
    for name in names:
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):  # not in main thread
                signal.signal(sig, signal.default_int_handler)
