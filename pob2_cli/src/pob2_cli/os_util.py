"""OS process liveness, stale lockfile detection, stale-lock reclaim, and hidden subprocess launch (L0).

Single canonical implementations shared by updater, bridge, daemon, and game_data.
All lock/liveness helpers are best-effort and fail safe: they never raise on
malformed lock payloads or unsupported platforms.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_kernel32: Any = None


def _get_kernel32() -> Any:
    """Cached kernel32 handle with OpenProcess/GetExitCodeProcess/CloseHandle bound."""
    global _kernel32
    if _kernel32 is None:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.GetExitCodeProcess.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        _kernel32 = k32
    return _kernel32


def _is_pid_alive(pid: int) -> bool:
    """Fast (<1ms) OS-native process liveness probe to avoid stale loopback timeouts.

    Rejects non-int/bool/non-positive PIDs; short-circuits for the current process;
    on Windows treats ERROR_ACCESS_DENIED (and unexpected ctypes failures) as alive
    so callers never break a live lock; on POSIX uses ``os.kill(pid, 0)``.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        if pid > 0xFFFFFFFF:
            return False
        try:
            import ctypes

            k32 = _get_kernel32()
            handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if not handle:
                err = ctypes.get_last_error()
                return err == _ERROR_ACCESS_DENIED
            try:
                exit_code = ctypes.c_uint32(0)
                if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return int(exit_code.value) == _STILL_ACTIVE
            finally:
                k32.CloseHandle(handle)
        except (OSError, AttributeError, ValueError):
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def _is_lockfile_stale(lock_path: Path, *, stale_sec: float) -> bool:
    """True when a JSON lockfile exceeds ``stale_sec`` (mtime or payload ``ts``) or its owner PID is dead."""
    try:
        st = lock_path.stat()
    except (OSError, OverflowError):
        return False
    if (time.time() - st.st_mtime) > stale_sec:
        return True
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            ts = payload.get("ts")
            if isinstance(ts, (int, float)) and (time.time() - float(ts)) > stale_sec:
                return True
            pid = int(payload.get("pid") or 0)
            if pid > 0 and pid != os.getpid() and not _is_pid_alive(pid):
                return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def _try_break_stale_lock(lock_path: Path) -> bool:
    """Atomically claim a stale lockfile via rename before unlinking so peers never delete a fresh lock."""
    trash = (
        lock_path.parent
        / f".{lock_path.name}.stale.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    )
    try:
        os.replace(lock_path, trash)
    except (OSError, OverflowError):
        return False
    try:
        trash.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def win_hidden_startupinfo(sub: Any = subprocess, *, windows: bool | None = None) -> Any:
    """Return a Windows STARTUPINFO configured with STARTF_USESHOWWINDOW + SW_HIDE, or None off Windows."""
    is_win = sys.platform == "win32" if windows is None else windows
    if not is_win:
        return None
    si_cls = getattr(sub, "STARTUPINFO", None)
    if not callable(si_cls):
        return None
    si = si_cls()
    si.dwFlags |= getattr(sub, "STARTF_USESHOWWINDOW", 0)
    si.wShowWindow = getattr(sub, "SW_HIDE", 0)
    return si


def win_hidden_subprocess_kwargs(sub: Any = subprocess, *, windows: bool | None = None) -> dict[str, Any]:
    """Return Windows kwargs (``creationflags=CREATE_NO_WINDOW`` + hidden ``startupinfo``) for child spawns.

    Never combines ``CREATE_NO_WINDOW`` with detached-console creation flags, because Win32
    ``CreateProcess`` ignores ``CREATE_NO_WINDOW`` in that case and flashes visible black windows.
    """
    is_win = sys.platform == "win32" if windows is None else windows
    if not is_win:
        return {}
    kwargs: dict[str, Any] = {}
    no_window = getattr(sub, "CREATE_NO_WINDOW", 0)
    if no_window:
        kwargs["creationflags"] = no_window
    si = win_hidden_startupinfo(sub, windows=True)
    if si is not None:
        kwargs["startupinfo"] = si
    return kwargs


def spawn_detached_process(
    cmd: Sequence[str],
    *,
    sub: Any = subprocess,
    windows: bool | None = None,
    **popen_kwargs: Any,
) -> Any:
    """Spawn a background process detached from parent exit / Ctrl+C without flashing a console window.

    On Windows, uses ``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`` with hidden ``STARTUPINFO``
    and ``close_fds=True``, trying ``CREATE_BREAKAWAY_FROM_JOB`` first and falling back without it
    on ``PermissionError`` or ``WinError 5``. On POSIX, sets ``start_new_session=True``.
    """
    kwargs: dict[str, Any] = dict(popen_kwargs)
    kwargs.setdefault("close_fds", True)
    is_win = sys.platform == "win32" if windows is None else windows
    if is_win:
        base_flags = getattr(sub, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(sub, "CREATE_NO_WINDOW", 0)
        if "startupinfo" not in kwargs:
            si = win_hidden_startupinfo(sub, windows=True)
            if si is not None:
                kwargs["startupinfo"] = si
        try:
            return sub.Popen(cmd, creationflags=base_flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError as exc:
            if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
                raise
            return sub.Popen(cmd, creationflags=base_flags, **kwargs)
    kwargs.setdefault("start_new_session", True)
    return sub.Popen(cmd, **kwargs)


is_pid_alive = _is_pid_alive
is_lockfile_stale = _is_lockfile_stale
try_break_stale_lock = _try_break_stale_lock
