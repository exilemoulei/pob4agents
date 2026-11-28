"""Loopback HTTP server, X-PoB2-Token authenticated RPC handler, and server entrypoints."""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeError, BridgeRejectedError, is_debug_enabled, sanitize_bridge_error
from ..errors import CrossGameHallucinationError, DaemonRequestError, LegacyPatchTrapError
from .runtime import (
    FOREIGN_TOKEN_HEADER,
    GAME_ID,
    TOKEN_HEADER,
    BootstrapMissingError,
    _daemon_boot_error_file,
    _daemon_state_file,
    _get_runtime_dir,
    redact_secrets,
    resolve_daemon_host,
    resolve_daemon_port,
    write_private_json,
)
from .slots import DaemonManager

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler


def make_handler(manager: DaemonManager) -> type[BaseHTTPRequestHandler]:
    import traceback
    from http.server import BaseHTTPRequestHandler

    class RpcHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def do_POST(self) -> None:
            token = self.headers.get(TOKEN_HEADER, "")
            foreign_token = self.headers.get(FOREIGN_TOKEN_HEADER, "")
            if foreign_token and not token:
                exc = CrossGameHallucinationError(
                    game=GAME_ID,
                    detected_game="poe1",
                    trigger=f"Header {FOREIGN_TOKEN_HEADER}",
                    error=f"Received PoE1 auth header `{FOREIGN_TOKEN_HEADER}` on a PoE2 (`pob2`) daemon.",
                    remediation=(
                        f"Send `{TOKEN_HEADER}` to the PoE2 daemon, or target the PoE1 daemon (`pob`) "
                        "using separate runtime directories."
                    ),
                    equivalent_command="pob status",
                    rule_id="DAEMON_CROSS_GAME_TOKEN_HEADER",
                )
                self._send_json(400, {**exc.to_dict(), "type": "CrossGameHallucinationError"})
                return
            if not secrets.compare_digest(token, manager.token):
                self._send_json(403, {"ok": False, "game": GAME_ID, "error": f"Forbidden: invalid or missing {TOKEN_HEADER}"})
                return

            if self.path != "/rpc":
                self._send_json(404, {"ok": False, "game": GAME_ID, "error": "Not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError("negative Content-Length")
            except ValueError:
                self._send_json(
                    400,
                    {"ok": False, "game": GAME_ID, "error": "Invalid Content-Length header", "type": "ValueError", "errorKind": "request"},
                )
                return

            raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                req = json.loads(raw_body) if raw_body else {}
                if not isinstance(req, dict):
                    raise ValueError("RPC request payload must be a JSON object.")
                action = req.get("action")
                params = req.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("RPC 'params' must be a JSON object.")
                result = manager.dispatch(str(action or ""), params)
                self._send_json(200, {"ok": True, "game": GAME_ID, "result": result})
            except CrossGameHallucinationError as exc:
                self._send_json(400, {**exc.to_dict(), "type": "CrossGameHallucinationError"})
            except LegacyPatchTrapError as exc:
                self._send_json(400, {**exc.to_dict(), "type": "LegacyPatchTrapError"})
            except (DaemonRequestError, ValueError) as exc:
                self._send_json(
                    400,
                    {"ok": False, "game": GAME_ID, "error": _error_text(exc), "type": type(exc).__name__, "errorKind": "request"},
                )
            except BridgeError as exc:
                kind = exc.kind if isinstance(exc, BridgeRejectedError) else "daemon"
                payload: dict[str, Any] = {
                    "ok": False,
                    "game": GAME_ID,
                    "error": _error_text(exc, sanitize_bridge=True),
                    "type": type(exc).__name__,
                    "errorKind": kind,
                }
                if isinstance(exc, BridgeRejectedError) and exc.code:
                    payload["engineCode"] = exc.code
                self._send_json(400, payload)
            except (AttributeError, TypeError, KeyError) as exc:
                err_payload: dict[str, Any] = {
                    "ok": False,
                    "game": GAME_ID,
                    "error": _error_text(exc, sanitize_bridge=True),
                    "type": exc.__class__.__name__,
                    "errorKind": "internal_error",
                    "code": "POE2-INTERNAL-ERROR",
                }
                if is_debug_enabled():
                    err_payload["trace"] = redact_secrets(traceback.format_exc(limit=4))
                self._send_json(400, err_payload)
            except Exception as exc:
                err_payload = {
                    "ok": False,
                    "game": GAME_ID,
                    "error": _error_text(exc, sanitize_bridge=True),
                    "type": exc.__class__.__name__,
                    "errorKind": "daemon",
                }
                if is_debug_enabled():
                    err_payload["trace"] = redact_secrets(traceback.format_exc(limit=4))
                self._send_json(400, err_payload)

    return RpcHandler


def _error_text(exc: BaseException, *, sanitize_bridge: bool = False) -> str:
    """Format an exception and any attached ``__notes__`` with secret redaction."""
    head = sanitize_bridge_error(str(exc)) if sanitize_bridge else str(exc)
    notes = [str(n) for n in getattr(exc, "__notes__", ()) if str(n).strip()]
    return redact_secrets("\n".join([head, *notes]))


def _serve_daemon(
    pob_dir: Path | None = None,
    port: int | None = None,
    *,
    data_dir: Path | None = None,
    host: str | None = None,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
    disable_global_idle: bool = False,
    announce_foreground: bool = False,
) -> None:
    """Start the local PoE2 daemon HTTP server, write ``daemon_poe2.json``, and serve until stopped."""
    import pob2_cli.daemon as _daemon_pkg

    from .client import _guard_foreign_daemon_state

    rt_dir = _get_runtime_dir(runtime_dir)
    rt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    boot_err_file = _daemon_boot_error_file(rt_dir)
    state_file = _daemon_state_file(rt_dir)
    existing_state = _daemon_pkg.read_daemon_state(rt_dir)
    if existing_state is not None:
        _guard_foreign_daemon_state(existing_state, state_file, rt_dir)

    try:
        if boot_err_file.exists():
            boot_err_file.unlink()
    except OSError:
        pass

    token = secrets.token_hex(32)
    manager = DaemonManager(
        pob_dir=pob_dir,
        token=token,
        data_dir=data_dir,
        runtime_dir=rt_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=disable_global_idle,
    )
    try:
        manager.start()
    except Exception as exc:
        upd_status: dict[str, Any] | None = None
        if isinstance(exc, BootstrapMissingError) and isinstance(exc.update_status, dict):
            upd_status = dict(exc.update_status)
            if isinstance(upd_status.get("networkError"), str):
                upd_status["networkError"] = redact_secrets(upd_status["networkError"])
        err_payload = {
            "game": GAME_ID,
            "error": redact_secrets(str(exc)),
            "type": exc.__class__.__name__,
            "state": exc.update_status.get("state") if isinstance(exc, BootstrapMissingError) else "boot_error",
            "updateStatus": upd_status,
            "ts": time.time(),
        }
        try:
            write_private_json(boot_err_file, err_payload)
        except OSError:
            pass
        raise

    import socketserver
    from http.server import ThreadingHTTPServer

    class _LoopbackHTTPServer(ThreadingHTTPServer):
        def server_bind(self) -> None:
            socketserver.TCPServer.server_bind(self)
            bound_host, bound_port = self.server_address[:2]
            self.server_name = str(bound_host)
            self.server_port = bound_port

    bind_host = resolve_daemon_host(host)
    bind_port = resolve_daemon_port(port)
    server = _LoopbackHTTPServer((bind_host, bind_port), make_handler(manager))
    manager.server = server
    actual_port = server.server_address[1]

    state_payload = {
        "game": GAME_ID,
        "pid": os.getpid(),
        "host": bind_host,
        "port": actual_port,
        "token": token,
        "started_at": manager.started_at,
        "pob_dir": str(manager.pob_dir),
    }
    write_private_json(state_file, state_payload)

    if announce_foreground:
        sys.stderr.write(
            f"pob2 daemon listening on {bind_host}:{actual_port} (pid={os.getpid()}, runtime={rt_dir})\n"
        )
        sys.stderr.flush()

    def _watchdog() -> None:
        while not manager._stopping:
            time.sleep(15.0)
            if manager.check_idle():
                manager.shutdown_all()
                break

    threading.Thread(target=_watchdog, name="pob2-daemon-watchdog", daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown_all()


def run_daemon_server(
    pob_dir: Path | None = None,
    port: int | None = None,
    *,
    data_dir: Path | None = None,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
) -> None:
    """Start the background PoE2 daemon server (auto-exits after global idle)."""
    _serve_daemon(
        pob_dir=pob_dir,
        port=port,
        data_dir=data_dir,
        runtime_dir=runtime_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=False,
        announce_foreground=False,
    )


def run_daemon_forever(
    pob_dir: Path | None = None,
    port: int | None = None,
    *,
    data_dir: Path | None = None,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
) -> None:
    """Run the PoE2 daemon persistently in the foreground."""
    _serve_daemon(
        pob_dir=pob_dir,
        port=port,
        data_dir=data_dir,
        runtime_dir=runtime_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=True,
        announce_foreground=True,
    )
