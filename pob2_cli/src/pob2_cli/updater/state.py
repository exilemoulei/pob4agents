"""PoB2 local version detection, update_state persistence, alerts, and headless engine gate probe."""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..game_profile import stamp_game
from ..guardrails import redact_secrets, redact_url_credentials
from ..json_io import atomic_write_json, read_json_dict
from ..os_util import win_hidden_subprocess_kwargs
from .constants import (
    OK_STATES,
    POBTOOLS_META_FILENAME,
    STATE_BOOTSTRAP_MISSING,
    STATE_DEGRADED_NETWORK,
    STATE_DEGRADED_ROLLBACK,
    UPDATE_STATE_FILENAME,
)
from .paths import get_runtime_dir, resolve_data_root, resolve_game_dir


def _pkg() -> Any:
    return sys.modules["pob2_cli.updater"]


def detect_local_versions(
    engine_dir: Path | None,
    runtime_dir: Path | None = None,
    *,
    data_dir: Path | None = None,
) -> dict[str, str | None]:
    pob_ver: str | None = None
    pobtools_ver: str | None = None
    pobtools_tag: str | None = None
    data_ver: str | None = None

    if engine_dir is not None:
        eng = Path(engine_dir)
        manifest_path = resolve_game_dir(eng) / "manifest.xml"
        if manifest_path.is_file():
            try:
                import xml.etree.ElementTree as ET

                root = ET.parse(manifest_path).getroot()
                v = root.find("Version")
                if v is not None and v.attrib.get("number"):
                    pob_ver = v.attrib["number"].strip()
            except (ET.ParseError, OSError, ValueError):
                pass

        trans_path = resolve_data_root(eng, data_dir=data_dir) / "translations_version.json"
        tdata = read_json_dict(trans_path)
        if tdata and tdata.get("dataVersion"):
            data_ver = str(tdata["dataVersion"]).strip()

        mdata = read_json_dict(eng / POBTOOLS_META_FILENAME)
        if mdata:
            pobtools_ver = mdata.get("pobToolsVersion") or pobtools_ver
            pobtools_tag = mdata.get("pobToolsTag") or pobtools_tag
            data_ver = data_ver or mdata.get("dataVersion")

    prev = load_update_state(runtime_dir)
    if prev and (engine_dir is None or str(prev.get("engineDir") or "") == str(engine_dir)):
        pob_ver = pob_ver or prev.get("localPobVersion")
        pobtools_ver = pobtools_ver or prev.get("pobToolsVersion")
        pobtools_tag = pobtools_tag or prev.get("pobToolsTag")
        data_ver = data_ver or prev.get("dataVersion")

    return {
        "localPobVersion": pob_ver,
        "pobToolsVersion": pobtools_ver,
        "pobToolsTag": pobtools_tag,
        "dataVersion": data_ver,
    }


def _write_pobtools_meta(
    engine_dir: Path,
    *,
    pobtools_version: str | None,
    pobtools_tag: str | None,
    data_version: str | None,
) -> None:
    atomic_write_json(
        Path(engine_dir) / POBTOOLS_META_FILENAME,
        {
            "pobToolsVersion": pobtools_version,
            "pobToolsTag": pobtools_tag,
            "dataVersion": data_version,
            "updatedAt": time.time(),
        },
    )


def get_update_state_path(runtime_dir: Path | None = None) -> Path:
    return get_runtime_dir(runtime_dir) / UPDATE_STATE_FILENAME


def load_update_state(runtime_dir: Path | None = None) -> dict[str, Any]:
    return read_json_dict(get_update_state_path(runtime_dir)) or {}


def save_update_state(state: dict[str, Any], runtime_dir: Path | None = None) -> Path:
    path = get_update_state_path(runtime_dir)
    atomic_write_json(path, stamp_game(state))
    return path


def _build_user_hint(
    state_name: str,
    local_pob_version: str | None = None,
    network_error: str | None = None,
    *,
    rolled_back: bool = True,
) -> str | None:
    if state_name in OK_STATES:
        return None
    ver = local_pob_version or "unknown"
    safe_err = redact_secrets(redact_url_credentials(network_error)) if network_error else ""
    err = f" ({safe_err})" if safe_err else ""
    if state_name == STATE_DEGRADED_NETWORK:
        return (
            f"GitHub update check failed for PoB2 (local {ver}{err}). "
            "Set POB2_PROXY / HTTPS_PROXY or pass POB2_ENGINE_DIR to an existing PoE2 engine."
        )
    if state_name == STATE_BOOTSTRAP_MISSING:
        return (
            f"No runnable PoB2 slim engine found{err}. "
            "Run `pob2 bootstrap` or point POB2_ENGINE_DIR / POB2_DATA_DIR to a PoE2 installation."
        )
    if state_name == STATE_DEGRADED_ROLLBACK:
        tail = f"rolled back to {ver}" if rolled_back else "rollback incomplete; run `pob2 stop`"
        return f"PoB2 engine gate probe failed{err}; {tail}."
    return f"PoB2 updater state={state_name} (local {ver}{err})."


def get_update_warning(state: dict[str, Any] | None) -> str | None:
    if not state:
        return None
    st = str(state.get("state") or "")
    if not st or st in OK_STATES:
        return None
    hint = state.get("userHint")
    if isinstance(hint, str) and hint.strip():
        return hint.strip()
    return _build_user_hint(
        st,
        local_pob_version=state.get("localPobVersion"),
        network_error=state.get("networkError"),
        rolled_back=bool(state.get("rolledBack")),
    )


def format_stderr_update_alert(state: dict[str, Any] | None) -> str | None:
    warning = get_update_warning(state)
    if not warning or not state:
        return None
    return f"[pob2 | {state.get('state') or 'unknown'} | PoB2 {state.get('localPobVersion') or 'unknown'}] {warning}"


def probe_engine_gate(
    engine_dir: Path,
    timeout_sec: float = 25.0,
    *,
    data_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Launch native PoB2 slim host and verify gate_result(ok) + hello + version caps."""
    eng = Path(engine_dir)
    game_dir = resolve_game_dir(eng)
    try:
        cmd, extra_env = _pkg().build_engine_command(eng, data_dir=data_dir, runtime_dir=runtime_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "stderrTail": []}

    out_queue: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
    stderr_tail: collections.deque[str] = collections.deque(maxlen=60)
    proc_env = os.environ.copy()
    proc_env.update(extra_env)

    proc = subprocess.Popen(
        cmd,
        cwd=str(game_dir),
        env=proc_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **win_hidden_subprocess_kwargs(subprocess),
    )

    def _read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out_queue.put(obj)
        except Exception as exc:
            out_queue.put(exc)
        finally:
            out_queue.put(None)

    def _read_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                s = line.rstrip("\r\n")
                if s:
                    stderr_tail.append(s)
        except (OSError, ValueError):
            pass

    threading.Thread(target=_read_stdout, name="pob2-gate-stdout", daemon=True).start()
    threading.Thread(target=_read_stderr, name="pob2-gate-stderr", daemon=True).start()

    gate_data: dict[str, Any] | None = None
    hello_data: dict[str, Any] | None = None
    ver_data: dict[str, Any] = {}
    deadline = time.monotonic() + timeout_sec

    try:
        while gate_data is None or hello_data is None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                return {
                    "ok": False,
                    "error": f"Timed out after {timeout_sec:.1f}s waiting for gate_result/hello",
                    "gate": gate_data,
                    "hello": hello_data,
                    "stderrTail": list(stderr_tail)[-15:],
                }
            try:
                msg = out_queue.get(timeout=rem)
            except queue.Empty:
                return {
                    "ok": False,
                    "error": f"Timed out after {timeout_sec:.1f}s waiting for gate_result/hello",
                    "stderrTail": list(stderr_tail)[-15:],
                }
            if msg is None or isinstance(msg, Exception):
                return {
                    "ok": False,
                    "error": f"PoB2 engine exited before handshake completed: {msg}",
                    "gate": gate_data,
                    "hello": hello_data,
                    "stderrTail": list(stderr_tail)[-15:],
                }
            event = msg.get("event")
            data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
            if event == "gate_result":
                gate_data = data
                if not data.get("ok"):
                    return {
                        "ok": False,
                        "error": f"gate_result failed: {data.get('failed')}",
                        "failed": data.get("failed"),
                        "gate": gate_data,
                        "stderrTail": list(stderr_tail)[-15:],
                    }
            elif event == "hello":
                hello_data = data

        rem = max(0.0, deadline - time.monotonic())
        if proc.stdin is not None and rem > 0.5:
            try:
                proc.stdin.write(json.dumps({"id": 1, "method": "version", "params": {}}) + "\n")
                proc.stdin.flush()
                ver_deadline = time.monotonic() + min(5.0, rem)
                while time.monotonic() < ver_deadline:
                    msg = out_queue.get(timeout=max(0.1, ver_deadline - time.monotonic()))
                    if msg is None or isinstance(msg, Exception):
                        break
                    if msg.get("id") == 1 and isinstance(msg.get("result"), dict):
                        ver_data = msg["result"]
                        break
            except (OSError, ValueError, queue.Empty):
                pass

        return {
            "ok": True,
            "game": "poe2",
            "gate": gate_data,
            "hello": hello_data,
            "pobVersion": ver_data.get("pobVersion") or hello_data.get("pobVersion"),
            "bridgeVersion": hello_data.get("bridge"),
            "caps": ver_data.get("caps") or {},
            "stderrTail": list(stderr_tail)[-10:],
        }
    finally:
        try:
            proc.kill()
            proc.wait(timeout=3.0)
        except (OSError, subprocess.SubprocessError):
            pass


def _save_state_snapshot(
    state_name: str,
    *,
    engine_dir: Path,
    rt_dir: Path,
    local_pob_ver: str | None = None,
    remote_pob_ver: str | None = None,
    err: str | None = None,
    rolled_back: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_err = redact_secrets(redact_url_credentials(err)) if err else None
    state: dict[str, Any] = {
        "state": state_name,
        "localPobVersion": local_pob_ver,
        "remotePobVersion": remote_pob_ver,
        "checkedAt": time.time(),
        "engineDir": str(engine_dir),
        "networkError": clean_err,
        "userHint": _build_user_hint(state_name, local_pob_ver, clean_err, rolled_back=rolled_back),
    }
    if rolled_back:
        state["rolledBack"] = True
    if extra:
        state.update(extra)
    save_update_state(state, rt_dir)
    return state
