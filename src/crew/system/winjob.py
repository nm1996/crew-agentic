"""Windows Job Objects: kill a whole process tree, even after its root exited.

Every agent / verification process is assigned to its own job created with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``:

* ``terminate(pid)`` ends every process in the job, including grandchildren
  whose parent already exited (which psutil tree walking cannot find);
* if CREW itself dies, Windows closes the job handle and kills the tree.

Processes the child starts before it is assigned (a few milliseconds after
spawn) are not in the job; they are still covered by psutil tree termination.
On non-Windows platforms every function is a no-op.
"""

from __future__ import annotations

import os
import threading

_jobs: dict[int, int] = {}  # pid -> job handle
_lock = threading.Lock()

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _create_job() -> int | None:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(job)
            return None
        return job

    def attach(pid: int) -> bool:
        """Put ``pid`` into a new kill-on-close job. Returns False if that failed."""
        job = _create_job()
        if job is None:
            return False
        process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            _kernel32.CloseHandle(job)
            return False
        try:
            if not _kernel32.AssignProcessToJobObject(job, process):
                _kernel32.CloseHandle(job)
                return False
        finally:
            _kernel32.CloseHandle(process)
        with _lock:
            _jobs[pid] = job
        return True

    def terminate(pid: int) -> bool:
        """Kill every process in ``pid``'s job and release it. False if there is no job."""
        with _lock:
            job = _jobs.pop(pid, None)
        if job is None:
            return False
        _kernel32.TerminateJobObject(job, 1)
        _kernel32.CloseHandle(job)
        return True

else:

    def attach(pid: int) -> bool:
        return False

    def terminate(pid: int) -> bool:
        return False


def has_job(pid: int) -> bool:
    with _lock:
        return pid in _jobs
