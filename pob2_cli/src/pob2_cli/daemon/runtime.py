"""PoE2 daemon runtime paths, slot slug validation, PID liveness, redaction, and stderr alerts."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from ..compat import ensure_utf8
from ..errors import BootstrapMissingError as BootstrapMissingError
from ..game_profile import (
    DAEMON_BOOT_ERROR_FILENAME,
    DAEMON_LOCK_FILENAME,
    DAEMON_LOG_FILENAME,
    DAEMON_STATE_FILENAME,
    DEFAULT_DAEMON_HOST,
    DEFAULT_DAEMON_IDLE_SEC,
    ENV_DAEMON_HOST,
    ENV_DAEMON_IDLE_SEC,
    ENV_DAEMON_PORT,
    ENV_LANG,
    GAME_ID,
    TOKEN_HEADER,
    env_str,
    is_offline,
)
from ..os_util import _CREATE_BREAKAWAY_FROM_JOB as _CREATE_BREAKAWAY_FROM_JOB
from ..os_util import _ERROR_ACCESS_DENIED as _ERROR_ACCESS_DENIED
from ..os_util import _get_kernel32 as _get_kernel32
from ..os_util import _is_pid_alive as _is_pid_alive
from ..updater import format_stderr_update_alert, get_runtime_dir, load_update_state

FOREIGN_TOKEN_HEADER = "X-PoB-Token"
REF_IDLE_TIMEOUT_SEC = 300.0
GLOBAL_IDLE_TIMEOUT_SEC = float(DEFAULT_DAEMON_IDLE_SEC)
UPDATE_RECHECK_INTERVAL_SEC = 1800.0

_SLOT_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")
_SECRET_RE = re.compile(r"(?i)(POESESSID|Authorization|Bearer|Cookie|X-PoB2?-Token)([=:\s]+)([^\s;,\"']+)")

_stderr_alert_emitted = False
_stderr_alert_checked_at: float | None = None


def redact_secrets(text: str) -> str:
    """Redact common auth tokens and cookies from daemon error messages."""
    try:
        from ..guardrails import redact_secrets as _gr_redact  # type: ignore[import-not-found]

        return str(_gr_redact(text))
    except ImportError:
        pass
    return _SECRET_RE.sub(r"\1\2[REDACTED]", str(text or ""))


def normalize_slot_name(slot: str | None) -> str:
    """Validate and normalize a named PoB2 worker slot slug."""
    if slot is None:
        return "main"
    cleaned = str(slot).strip()
    if not cleaned:
        return "main"
    if cleaned in (".", "..") or not _SLOT_NAME_RE.match(cleaned):
        raise ValueError(
            f"Invalid slot name {slot!r}: must match ^[a-zA-Z0-9._-]{{1,48}}$ and not be '.' or '..'."
        )
    return cleaned.lower()


def _get_runtime_dir(runtime_dir: Path | None = None) -> Path:
    return get_runtime_dir(runtime_dir)


def resolve_daemon_host(host: str | None = None) -> str:
    return (host or env_str(ENV_DAEMON_HOST) or DEFAULT_DAEMON_HOST).strip() or DEFAULT_DAEMON_HOST


def resolve_daemon_port(port: int | None = None) -> int:
    if port is not None:
        return int(port)
    raw = env_str(ENV_DAEMON_PORT)
    if raw:
        try:
            val = int(raw)
            if 0 <= val <= 65535:
                return val
        except ValueError:
            pass
    return 0


def resolve_global_idle_sec() -> float:
    raw = env_str(ENV_DAEMON_IDLE_SEC)
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return GLOBAL_IDLE_TIMEOUT_SEC


def _build_daemon_spawn_cmd(
    runtime_dir: Path | None = None,
    *,
    pob_dir: Path | None = None,
    data_dir: Path | None = None,
    port: int | None = None,
) -> list[str]:
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_daemon"]
    else:
        cmd = [sys.executable, "-m", "pob2_cli.daemon"]
    if runtime_dir is not None:
        cmd.extend(["--runtime-dir", str(runtime_dir)])
    if pob_dir is not None:
        cmd.extend(["--pob-dir", str(pob_dir)])
    if data_dir is not None:
        cmd.extend(["--data-dir", str(data_dir)])
    if port is not None and int(port) > 0:
        cmd.extend(["--port", str(int(port))])
    return cmd


def _daemon_state_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / DAEMON_STATE_FILENAME


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write ``payload`` with owner-only permissions (0600 on POSIX; safe replace on Windows)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".{os.getpid()}.tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def same_slot_error(action: str, slot: str) -> str:
    """Actionable error message refusing a dual-slot comparison of a slot against itself."""
    return (
        f"Cannot {action} build slot '{slot}' against itself: the reference slot must differ "
        "from the target slot. Pass a different `--ref-target` or a separate PoE2 build source."
    )


def _daemon_lock_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / DAEMON_LOCK_FILENAME


def _daemon_log_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / DAEMON_LOG_FILENAME


def _daemon_boot_error_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / DAEMON_BOOT_ERROR_FILENAME


def _is_offline_env() -> bool:
    return is_offline()


def reset_stderr_alert_flag() -> None:
    global _stderr_alert_emitted, _stderr_alert_checked_at
    _stderr_alert_emitted = False
    _stderr_alert_checked_at = None


def emit_stderr_update_alert(state: dict[str, Any] | None, *, force: bool = False) -> bool:
    global _stderr_alert_emitted, _stderr_alert_checked_at
    checked_at_raw = state.get("checkedAt") if isinstance(state, dict) else None
    checked_at = float(checked_at_raw) if isinstance(checked_at_raw, (int, float)) else None
    if _stderr_alert_emitted and not force:
        if checked_at is None or checked_at == _stderr_alert_checked_at:
            return False
    alert = format_stderr_update_alert(state)
    if not alert:
        return False
    ensure_utf8(sys.stderr)
    sys.stderr.write(alert + "\n")
    sys.stderr.flush()
    _stderr_alert_emitted = True
    if checked_at is not None:
        _stderr_alert_checked_at = checked_at
    return True


def _maybe_emit_degraded_alert(result: Any, rt_dir: Path) -> None:
    import pob2_cli.daemon as _daemon_pkg

    if not isinstance(result, dict):
        return
    upd_status = result.get("updateStatus")
    if isinstance(upd_status, dict) and str(upd_status.get("state") or "").startswith("degraded_"):
        _daemon_pkg.emit_stderr_update_alert(upd_status)
        return
    if str(result.get("state") or "").startswith("degraded_"):
        _daemon_pkg.emit_stderr_update_alert(result)
        return
    warn = result.get("updateWarning")
    if isinstance(warn, str) and warn.strip():
        disk_state = load_update_state(rt_dir)
        if isinstance(disk_state, dict) and str(disk_state.get("state") or "").startswith("degraded_"):
            _daemon_pkg.emit_stderr_update_alert(disk_state)
        else:
            _daemon_pkg.emit_stderr_update_alert({"state": "degraded_network_error", "userHint": warn})


__all__ = [
    "DAEMON_BOOT_ERROR_FILENAME",
    "DAEMON_LOCK_FILENAME",
    "DAEMON_LOG_FILENAME",
    "DAEMON_STATE_FILENAME",
    "ENV_LANG",
    "FOREIGN_TOKEN_HEADER",
    "GAME_ID",
    "GLOBAL_IDLE_TIMEOUT_SEC",
    "REF_IDLE_TIMEOUT_SEC",
    "TOKEN_HEADER",
    "UPDATE_RECHECK_INTERVAL_SEC",
    "_CREATE_BREAKAWAY_FROM_JOB",
    "_ERROR_ACCESS_DENIED",
    "BootstrapMissingError",
    "_build_daemon_spawn_cmd",
    "_daemon_boot_error_file",
    "_daemon_lock_file",
    "_daemon_log_file",
    "_daemon_state_file",
    "_get_kernel32",
    "_get_runtime_dir",
    "_is_offline_env",
    "_is_pid_alive",
    "_maybe_emit_degraded_alert",
    "emit_stderr_update_alert",
    "normalize_slot_name",
    "redact_secrets",
    "reset_stderr_alert_flag",
    "resolve_daemon_host",
    "resolve_daemon_port",
    "resolve_global_idle_sec",
    "same_slot_error",
    "write_private_json",
]
