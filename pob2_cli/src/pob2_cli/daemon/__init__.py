"""Local HTTP daemon managing persistent named headless PoB2 workers with per-slot locking."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from ..errors import CrossGameHallucinationError, DaemonRequestError, LegacyPatchTrapError
from .runtime import (
    _CREATE_BREAKAWAY_FROM_JOB,
    DAEMON_BOOT_ERROR_FILENAME,
    DAEMON_LOCK_FILENAME,
    DAEMON_LOG_FILENAME,
    DAEMON_STATE_FILENAME,
    FOREIGN_TOKEN_HEADER,
    GAME_ID,
    GLOBAL_IDLE_TIMEOUT_SEC,
    REF_IDLE_TIMEOUT_SEC,
    TOKEN_HEADER,
    UPDATE_RECHECK_INTERVAL_SEC,
    BootstrapMissingError,
    _build_daemon_spawn_cmd,
    _daemon_boot_error_file,
    _daemon_lock_file,
    _daemon_log_file,
    _daemon_state_file,
    _get_kernel32,
    _get_runtime_dir,
    _is_offline_env,
    _is_pid_alive,
    _maybe_emit_degraded_alert,
    emit_stderr_update_alert,
    normalize_slot_name,
    redact_secrets,
    reset_stderr_alert_flag,
)

_CLIENT_EXPORTS: frozenset[str] = frozenset(
    {
        "_ensure_daemon",
        "_guard_foreign_daemon_state",
        "_http_post_rpc",
        "_parse_daemon_endpoint",
        "_probe_daemon",
        "_read_boot_error",
        "_rpc_result",
        "_spawn_daemon_process",
        "call_daemon",
        "main",
        "read_daemon_state",
        "stop_daemon",
    }
)
_READ_CACHE_EXPORTS: frozenset[str] = frozenset({"LOOP_GUARD_WARNING", "READ_CACHE_ACTIONS", "SlotReadCache"})
_RPC_HANDLER_EXPORTS: frozenset[str] = frozenset({"dispatch_slot_action", "register_slot_handler"})
_SERVER_EXPORTS: frozenset[str] = frozenset({"_serve_daemon", "make_handler", "run_daemon_forever", "run_daemon_server"})


def __getattr__(name: str) -> Any:
    if name in _CLIENT_EXPORTS:
        from . import client as _mod
    elif name in _READ_CACHE_EXPORTS:
        from . import read_cache as _mod
    elif name in _RPC_HANDLER_EXPORTS:
        from . import rpc_handlers as _mod
    elif name in _SERVER_EXPORTS:
        from . import server as _mod
    elif name == "DaemonManager":
        from . import slots as _mod
    elif name in ("BridgeError", "BridgeWorker"):
        from .. import bridge_client as _mod
    elif name == "check_and_apply_updates":
        from .. import updater as _mod
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    val = getattr(_mod, name)
    globals()[name] = val
    return val


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "DAEMON_BOOT_ERROR_FILENAME",
    "DAEMON_LOCK_FILENAME",
    "DAEMON_LOG_FILENAME",
    "DAEMON_STATE_FILENAME",
    "FOREIGN_TOKEN_HEADER",
    "GAME_ID",
    "GLOBAL_IDLE_TIMEOUT_SEC",
    "LOOP_GUARD_WARNING",
    "READ_CACHE_ACTIONS",
    "REF_IDLE_TIMEOUT_SEC",
    "TOKEN_HEADER",
    "UPDATE_RECHECK_INTERVAL_SEC",
    "_CREATE_BREAKAWAY_FROM_JOB",
    "BootstrapMissingError",
    "BridgeError",
    "BridgeWorker",
    "CrossGameHallucinationError",
    "DaemonManager",
    "DaemonRequestError",
    "LegacyPatchTrapError",
    "SlotReadCache",
    "_build_daemon_spawn_cmd",
    "_daemon_boot_error_file",
    "_daemon_lock_file",
    "_daemon_log_file",
    "_daemon_state_file",
    "_ensure_daemon",
    "_get_kernel32",
    "_get_runtime_dir",
    "_guard_foreign_daemon_state",
    "_http_post_rpc",
    "_is_offline_env",
    "_is_pid_alive",
    "_maybe_emit_degraded_alert",
    "_parse_daemon_endpoint",
    "_probe_daemon",
    "_read_boot_error",
    "_rpc_result",
    "_serve_daemon",
    "_spawn_daemon_process",
    "call_daemon",
    "check_and_apply_updates",
    "dispatch_slot_action",
    "emit_stderr_update_alert",
    "main",
    "make_handler",
    "normalize_slot_name",
    "read_daemon_state",
    "redact_secrets",
    "register_slot_handler",
    "reset_stderr_alert_flag",
    "run_daemon_forever",
    "run_daemon_server",
    "stop_daemon",
    "subprocess",
    "sys",
]
