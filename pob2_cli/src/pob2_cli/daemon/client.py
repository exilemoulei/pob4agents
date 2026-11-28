"""Loopback HTTP client helpers, auto-start locking, stop_daemon, and CLI entrypoint for PoB2 daemon."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..errors import (
    CrossGameHallucinationError,
    DaemonRequestError,
    InternalBugError,
    LegacyPatchTrapError,
)
from ..json_io import read_json_dict
from ..os_util import spawn_detached_process
from ..updater import STATE_BOOTSTRAP_MISSING, load_update_state
from .runtime import (
    GAME_ID,
    TOKEN_HEADER,
    BootstrapMissingError,
    _build_daemon_spawn_cmd,
    _daemon_boot_error_file,
    _daemon_lock_file,
    _daemon_log_file,
    _daemon_state_file,
    _get_runtime_dir,
    _is_offline_env,
    _is_pid_alive,
    _maybe_emit_degraded_alert,
    resolve_daemon_host,
)


def read_daemon_state(runtime_dir: Path | None = None) -> dict[str, Any] | None:
    return read_json_dict(_daemon_state_file(runtime_dir))


def _read_boot_error(runtime_dir: Path | None = None) -> dict[str, Any] | None:
    return read_json_dict(_daemon_boot_error_file(runtime_dir))


def _http_post_rpc(
    port: int,
    token: str,
    payload: dict[str, Any],
    *,
    host: str = "127.0.0.1",
    timeout: float = 150.0,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request(
            "POST",
            "/rpc",
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
                TOKEN_HEADER: token,
            },
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        status = resp.status
    finally:
        conn.close()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        if status in (403, 404):
            return status, {"ok": False, "error": f"HTTP {status}: {raw[:200]}"}
        raise RuntimeError(f"Invalid JSON response from PoB2 daemon (HTTP {status}): {raw[:200]}") from exc
    if not isinstance(data, dict):
        if status in (403, 404):
            return status, {"ok": False, "error": f"HTTP {status}"}
        raise RuntimeError(f"Unexpected response payload from PoB2 daemon (HTTP {status}).")
    return status, data


def _parse_daemon_endpoint(state: dict[str, Any]) -> tuple[str, int, str]:
    raw_port = state["port"]
    if isinstance(raw_port, bool):
        raise TypeError("Invalid boolean port in daemon_poe2.json")
    port = int(raw_port)
    if not (1 <= port <= 65535):
        raise ValueError(f"Out-of-range port in daemon_poe2.json: {port}")
    raw_token = state["token"]
    if raw_token is None:
        raise TypeError("Missing token in daemon_poe2.json")
    host = str(state.get("host") or resolve_daemon_host())
    return host, port, str(raw_token)


def _guard_foreign_daemon_state(state: dict[str, Any], state_file: Path, rt_dir: Path) -> None:
    """Non-destructively block when state file explicitly belongs to ``poe1``."""
    recorded_game = state.get("game")
    if recorded_game is None:
        return
    if str(recorded_game).strip().lower() == "poe1":
        raise CrossGameHallucinationError(
            game=GAME_ID,
            detected_game="poe1",
            trigger=f"{state_file} (game={recorded_game!r})",
            error=f"Runtime directory `{rt_dir}` belongs to a `poe1` daemon, not `{GAME_ID}`.",
            remediation="Use separate runtime directories (`POB2_RUNTIME_DIR` vs PoE1 runtime dir).",
            equivalent_command="pob status",
            rule_id="DAEMON_STATE_GAME_MISMATCH",
        )


def _probe_daemon(state: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any] | None:
    import pob2_cli.daemon as _daemon_pkg

    pid = state.get("pid")
    if isinstance(pid, int) and not _is_pid_alive(pid):
        return None
    try:
        host, port, token = _parse_daemon_endpoint(state)
        status_code, data = _daemon_pkg._http_post_rpc(
            port,
            token,
            {"action": "status", "params": {"skip_periodic_check": True}},
            host=host,
            timeout=timeout,
        )
        if status_code == 400 and data.get("errorKind") == "cross_game_hallucination":
            raise CrossGameHallucinationError.from_dict(data, default_game=GAME_ID)
        if status_code == 200 and data.get("ok"):
            res = data.get("result")
            if isinstance(res, dict) and str(res.get("game") or "").strip().lower() == "poe1":
                raise CrossGameHallucinationError(
                    game=GAME_ID,
                    detected_game="poe1",
                    trigger=f"status.game={res.get('game')!r}",
                    error="Connected daemon reports `game='poe1'`, not `poe2`.",
                    remediation="Use separate runtime directories (`POB2_RUNTIME_DIR`).",
                    equivalent_command="pob status",
                    rule_id="DAEMON_STATUS_GAME_MISMATCH",
                )
            return res
    except (OSError, http.client.HTTPException, TimeoutError, KeyError, TypeError, ValueError):
        return None
    return None


def stop_daemon(runtime_dir: Path | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    """Stop the running local PoB2 daemon cleanly if alive; unlink stale state file if dead."""
    import pob2_cli.daemon as _daemon_pkg

    rt_dir = _get_runtime_dir(runtime_dir)
    state_file = _daemon_state_file(rt_dir)
    state = _daemon_pkg.read_daemon_state(rt_dir)
    if state is None:
        return {"game": GAME_ID, "stopped": False, "alreadyStopped": True}

    _guard_foreign_daemon_state(state, state_file, rt_dir)
    pid = state.get("pid")
    if isinstance(pid, int) and not _is_pid_alive(pid):
        try:
            state_file.unlink()
        except OSError:
            pass
        return {"game": GAME_ID, "stopped": False, "staleCleaned": True}

    try:
        host, port, token = _parse_daemon_endpoint(state)
        status_code, data = _daemon_pkg._http_post_rpc(
            port, token, {"action": "stop", "params": {}}, host=host, timeout=timeout
        )
        if status_code == 400 and data.get("errorKind") == "cross_game_hallucination":
            raise CrossGameHallucinationError.from_dict(data, default_game=GAME_ID)
        if status_code in (403, 404):
            try:
                state_file.unlink()
            except OSError:
                pass
            return {"game": GAME_ID, "stopped": False, "staleCleaned": True}
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Daemon stop error")
        return data.get("result") or {"game": GAME_ID, "stopped": True}
    except (OSError, http.client.HTTPException, TimeoutError, KeyError, TypeError, ValueError):
        try:
            state_file.unlink()
        except OSError:
            pass
        return {"game": GAME_ID, "stopped": False, "staleCleaned": True}


def _daemon_child_env() -> dict[str, str]:
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        src_root = str(Path(__file__).resolve().parents[2])
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{src_root}{os.pathsep}{existing}" if existing else src_root
    return env


def _spawn_daemon_process(cmd: list[str], rt_dir: Path, log_fh: Any) -> subprocess.Popen[Any]:
    import pob2_cli.daemon as _daemon_pkg

    sub = _daemon_pkg.subprocess
    return spawn_detached_process(
        cmd,
        sub=sub,
        cwd=str(rt_dir),
        env=_daemon_child_env(),
        stdin=sub.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        close_fds=True,
    )


def _acquire_start_lock(lock_file: Path) -> bool:
    if lock_file.exists():
        try:
            lock_age = time.time() - lock_file.stat().st_mtime
            stale = lock_age > 70.0
            if not stale:
                raw_pid = lock_file.read_text(encoding="ascii", errors="ignore").strip()
                if raw_pid.isdigit() and not _is_pid_alive(int(raw_pid)):
                    stale = True
            if stale:
                lock_file.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except OSError:
        return False


def _ensure_daemon(
    rt_dir: Path,
    *,
    runtime_dir_arg: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    import pob2_cli.daemon as _daemon_pkg

    lock_file = _daemon_lock_file(rt_dir)
    boot_err_file = _daemon_boot_error_file(rt_dir)
    rt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = _daemon_log_file(rt_dir)
    acquired_lock = _acquire_start_lock(lock_file)

    spawned_proc: subprocess.Popen[Any] | None = None
    ping_status: dict[str, Any] | None = None
    final_state: dict[str, Any] | None = None

    try:
        if acquired_lock:
            try:
                if boot_err_file.exists():
                    boot_err_file.unlink()
            except OSError:
                pass
            cmd = _build_daemon_spawn_cmd(rt_dir if runtime_dir_arg is not None else None)
            with open(log_path, "a", encoding="utf-8") as log_fh:
                spawned_proc = _spawn_daemon_process(cmd, rt_dir, log_fh)

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            boot_err = _read_boot_error(rt_dir)
            if boot_err is not None:
                err_msg = str(boot_err.get("error") or "PoB2 daemon failed to start.")
                if boot_err.get("state") == STATE_BOOTSTRAP_MISSING:
                    raise BootstrapMissingError(err_msg, update_status=boot_err.get("updateStatus"))
                raise RuntimeError(err_msg)

            st = _daemon_pkg.read_daemon_state(rt_dir)
            if st is not None:
                probed = _probe_daemon(st, timeout=5.0)
                if probed is not None:
                    ping_status = probed
                    final_state = st
                    break

            if spawned_proc is not None and spawned_proc.poll() is not None:
                boot_err = _read_boot_error(rt_dir)
                if boot_err is not None:
                    err_msg = str(boot_err.get("error") or "PoB2 daemon exited during startup.")
                    if boot_err.get("state") == STATE_BOOTSTRAP_MISSING:
                        raise BootstrapMissingError(err_msg, update_status=boot_err.get("updateStatus"))
                    raise RuntimeError(err_msg)
                raise RuntimeError(
                    f"PoB2 daemon exited immediately with code {spawned_proc.returncode}. Check {log_path}."
                )
            time.sleep(0.05)
        else:
            tail = ""
            try:
                if log_path.is_file():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if lines:
                        tail = " | log tail: " + " ; ".join(lines[-8:])
            except OSError:
                pass
            raise RuntimeError(f"Timed out waiting for PoB2 daemon to start. Check {log_path}.{tail}")
    finally:
        if acquired_lock:
            try:
                lock_file.unlink()
            except OSError:
                pass

    final_state = final_state or _daemon_pkg.read_daemon_state(rt_dir)
    if not final_state:
        raise RuntimeError("PoB2 daemon state file disappeared after startup.")
    return final_state, ping_status


def _rpc_result(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("ok"):
        if data.get("errorKind") == "cross_game_hallucination":
            raise CrossGameHallucinationError.from_dict(data, default_game=GAME_ID)
        if data.get("errorKind") == "legacy_patch_trap":
            raise LegacyPatchTrapError.from_dict(data)
        message = data.get("error") or "PoB2 daemon error"
        if data.get("errorKind") == "internal_error":
            raise InternalBugError(message)
        if data.get("errorKind") == "request":
            raise DaemonRequestError(message)
        raise RuntimeError(message)
    if "result" not in data:
        raise RuntimeError("Malformed PoB2 daemon RPC response: missing 'result'.")
    return data["result"]


def call_daemon(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
    runtime_dir: Path | None = None,
    emit_cold_start_alert: bool = True,
) -> dict[str, Any]:
    """Send an authenticated X-PoB2-Token RPC to the local PoB2 daemon, auto-starting if needed."""
    import pob2_cli.daemon as _daemon_pkg

    if action in ("status", "load") and _is_offline_env():
        params = {**(params or {}), "skip_periodic_check": True}

    rt_dir = _get_runtime_dir(runtime_dir)
    state_file = _daemon_state_file(rt_dir)
    state = _daemon_pkg.read_daemon_state(rt_dir)
    if state is not None:
        _guard_foreign_daemon_state(state, state_file, rt_dir)
        pid = state.get("pid")
        if isinstance(pid, int) and not _is_pid_alive(pid):
            state_file.unlink(missing_ok=True)
            state = None
        else:
            pid_alive = isinstance(pid, int) and not isinstance(pid, bool) and _is_pid_alive(pid)
            try:
                host, port, token = _parse_daemon_endpoint(state)
                status_code, data = _daemon_pkg._http_post_rpc(
                    port, token, {"action": action, "params": params or {}}, host=host, timeout=timeout
                )
            except TimeoutError:
                if pid_alive:
                    raise
                state_file.unlink(missing_ok=True)
                state = None
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                if pid_alive and not isinstance(exc, ConnectionRefusedError):
                    raise RuntimeError(
                        f"The PoB2 daemon (pid {pid}) did not answer '{action}' cleanly ({exc!r})."
                    ) from exc
                state_file.unlink(missing_ok=True)
                state = None
            except (KeyError, TypeError, ValueError):
                state_file.unlink(missing_ok=True)
                state = None
            else:
                if status_code in (403, 404):
                    state_file.unlink(missing_ok=True)
                    state = None
                else:
                    result = _rpc_result(data)
                    if emit_cold_start_alert:
                        _maybe_emit_degraded_alert(result, rt_dir)
                    return result

    if not auto_start:
        raise RuntimeError("PoB2 daemon is not running.")

    st, ping_status = _daemon_pkg._ensure_daemon(rt_dir, runtime_dir_arg=runtime_dir)
    if emit_cold_start_alert:
        upd = (ping_status or {}).get("updateStatus") or load_update_state(rt_dir)
        if str((upd or {}).get("state") or "").startswith("degraded_"):
            _daemon_pkg.emit_stderr_update_alert(upd)
    if action == "status" and ping_status is not None:
        return ping_status

    host, port, token = _parse_daemon_endpoint(st)
    _, data = _daemon_pkg._http_post_rpc(
        port, token, {"action": action, "params": params or {}}, host=host, timeout=timeout
    )
    result = _rpc_result(data)
    if emit_cold_start_alert:
        _maybe_emit_degraded_alert(result, rt_dir)
    return result


def main() -> None:
    import argparse

    import pob2_cli.daemon as _daemon_pkg

    parser = argparse.ArgumentParser(description="Headless PoB2 local HTTP daemon")
    parser.add_argument("--pob-dir", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--proxy", type=str, default=None)
    parser.add_argument("--mirror", type=str, default=None)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--skip-update-check", action="store_true")
    args = parser.parse_args()
    runner = _daemon_pkg.run_daemon_forever if args.foreground else _daemon_pkg.run_daemon_server
    runner(
        pob_dir=args.pob_dir,
        data_dir=args.data_dir,
        port=args.port,
        runtime_dir=args.runtime_dir,
        offline=True if args.offline else None,
        proxy=args.proxy,
        mirror=args.mirror,
        skip_update_check=bool(args.skip_update_check),
    )
