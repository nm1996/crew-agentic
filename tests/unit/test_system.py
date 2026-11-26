"""Portable process-tree termination and file locking."""

import multiprocessing
import subprocess
import sys
import time
from pathlib import Path

import psutil

from crew.system.locking import FileLock
from crew.system.processes import (
    is_same_process,
    kill_tree,
    process_started_at,
    spawn_kwargs,
    terminate_tree,
)

SPAWN_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def test_kill_tree_stops_grandchildren():
    proc = subprocess.Popen(
        [sys.executable, "-c", SPAWN_GRANDCHILD],
        stdout=subprocess.PIPE,
        text=True,
        **spawn_kwargs(),
    )
    grandchild = int(proc.stdout.readline())
    assert kill_tree(proc.pid, grace=2)
    proc.wait(timeout=10)
    deadline = time.monotonic() + 10
    while psutil.pid_exists(grandchild) and time.monotonic() < deadline:
        if psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE:
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(grandchild) or (
        psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE
    )


async def test_terminate_tree_after_normal_exit_is_safe():
    import asyncio

    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", **spawn_kwargs())
    await proc.wait()
    assert await terminate_tree(proc, grace=1)


def test_process_identity_detects_pid_reuse():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = process_started_at(proc.pid)
        assert started is not None
        assert is_same_process(proc.pid, started, "python")
        assert not is_same_process(proc.pid, started - 100, "python")  # different start time
        assert not is_same_process(proc.pid, started, "claude")  # different program
    finally:
        proc.kill()
        proc.wait()


def _hold_lock(path: str, ready, release) -> None:
    lock = FileLock(Path(path))
    assert lock.try_acquire()
    ready.set()
    release.wait(10)
    lock.release()


def test_file_lock_is_exclusive_across_processes(tmp_path: Path):
    path = tmp_path / "run.lock"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(path), ready, release))
    holder.start()
    try:
        assert ready.wait(20)
        assert not FileLock(path).try_acquire()
    finally:
        release.set()
        holder.join(20)
    lock = FileLock(path)
    assert lock.try_acquire()  # released when the holder exited
    lock.release()


def test_file_lock_released_when_holder_dies(tmp_path: Path):
    path = tmp_path / "run.lock"
    code = (
        "import sys, time; from pathlib import Path; from crew.system.locking import FileLock;"
        f"l = FileLock(Path({str(path)!r})); assert l.try_acquire(); print('locked', flush=True);"
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    assert not FileLock(path).try_acquire()
    proc.kill()
    proc.wait()
    lock = FileLock(path)
    assert lock.try_acquire()
    lock.release()
