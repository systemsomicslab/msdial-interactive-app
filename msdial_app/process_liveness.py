"""Whether a recorded process is still running, read without ever signalling it.

WHY THIS IS NOT os.kill. On Windows os.kill(pid, 0) is not a probe: signal 0 is CTRL_C_EVENT, and
os.kill with any value other than the two console events terminates the process. A record that names
its owner - a download lease, a store lock, the campaign runner's lock - is judged by reading the
process through OpenProcess (or psutil) instead, and by its creation time, because Windows reuses
process ids: a live process with a different creation time is a different process, and the recorded
owner is gone.

One helper, so every such record answers the question the same way. The accession download store
(download_store.py on feat/download-store-module) carries the same two functions under the same names;
whichever lands second should import these rather than keep a copy.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_WAIT_OBJECT_0 = 0x0
_WAIT_TIMEOUT = 0x102
_STILL_ACTIVE = 259
_FILETIME_UNIX_EPOCH_SECONDS = 11_644_473_600
# Two readings of one process's creation time can differ by rounding; a reused id starts later.
CREATION_TIME_TOLERANCE_SECONDS = 1.0


def process_is_alive(pid: Any, created_at: float | None = None) -> bool | None:
    """Return True, False, or None when liveness cannot be read. Never signals the process.

    ``created_at`` is the holder's creation time as recorded by process_created_at. A live process
    whose creation time differs is a different process, and the holder is dead. None means "cannot
    tell", and a caller must treat it as possibly alive.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    alive, created = _probe_process(pid)
    if alive and created_at is not None and created is not None:
        try:
            if abs(float(created) - float(created_at)) > CREATION_TIME_TOLERANCE_SECONDS:
                return False
        except (TypeError, ValueError):
            return alive
    return alive


def process_created_at(pid: int | None = None) -> float | None:
    """Creation time of a process (default: this one) in Unix seconds, or None if unreadable."""
    alive, created = _probe_process(os.getpid() if pid is None else int(pid))
    return created if alive else None


def _probe_process(pid: int) -> tuple[bool | None, float | None]:
    if os.name == "nt":
        alive, created = _windows_process_probe(pid)
        if alive is not None:
            return alive, created
        return _psutil_process_probe(pid)
    alive, created = _psutil_process_probe(pid)
    if alive is not None:
        return alive, created
    return _proc_process_probe(pid)


def _windows_process_probe(pid: int) -> tuple[bool | None, float | None]:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - ctypes is part of every CPython on Windows
        return None, None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    # A waited-for child keeps its process object alive while any handle to it is open, so
    # OpenProcess alone succeeds for a process that has already exited. The wait (or, without
    # SYNCHRONIZE, the exit code) is what says whether it is still running.
    synchronize = True
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return False, None
        if error != _ERROR_ACCESS_DENIED:
            return None, None
        synchronize = False
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Access denied for even the least right: the process exists but is not ours to read.
            return (True if ctypes.get_last_error() == _ERROR_ACCESS_DENIED else None), None
    try:
        if synchronize:
            state = kernel32.WaitForSingleObject(handle, 0)
            if state == _WAIT_OBJECT_0:
                return False, None
            if state != _WAIT_TIMEOUT:
                return None, None
        else:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None, None
            if code.value != _STILL_ACTIVE:
                return False, None
        times = [wintypes.FILETIME() for _ in range(4)]
        created = None
        if kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            created = ticks / 10_000_000 - _FILETIME_UNIX_EPOCH_SECONDS
        return True, created
    finally:
        kernel32.CloseHandle(handle)


def _psutil_process_probe(pid: int) -> tuple[bool | None, float | None]:
    try:
        import psutil
    except ImportError:
        return None, None
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False, None
        return True, process.create_time()
    except psutil.NoSuchProcess:  # includes ZombieProcess
        return False, None
    except psutil.AccessDenied:
        return True, None
    except psutil.Error:
        return None, None


def _proc_process_probe(pid: int) -> tuple[bool | None, float | None]:
    if not Path("/proc/self").exists():
        return None, None
    return Path(f"/proc/{pid}").exists(), None
