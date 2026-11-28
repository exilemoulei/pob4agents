"""Tests for PoE2 daemon isolation, read cache, cross-game token guard, and PoE1/PoE2 coexistence."""

from __future__ import annotations

import http.client
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from pob2_cli import daemon, updater
from pob2_cli.bridge_protocol import encode_xml_share_code
from pob2_cli.daemon.read_cache import LOOP_GUARD_WARNING, SlotReadCache
from pob2_cli.daemon.server import make_handler
from pob2_cli.errors import CrossGameHallucinationError
from pob2_cli.game_profile import (
    DAEMON_LOCK_FILENAME,
    DAEMON_LOG_FILENAME,
    DAEMON_STATE_FILENAME,
    GAME_ID,
    TOKEN_HEADER,
)
from pob2_cli.locale_cfg import get_active_lang
from pob2_cli.snapshot import SlotHistory

_POE2_DAEMON_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="85" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <ItemSet id="1" useSecondWeaponSet="false"/>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1"/>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1"/>
  </Config>
</PathOfBuilding2>
"""


class _FakeWorker:
    def __init__(self, slot_name: str = "main", runtime_dir: Path | None = None) -> None:
        self.slot_name = slot_name
        self.runtime_dir = runtime_dir
        self.pob_root = Path(".")
        self.lock = threading.RLock()
        self.caps = {"cowSnapshot": True, "winLuaHost": True, "game": "poe2"}
        self.hello_info = {"protocol": 1, "bridge": "test"}
        self.locale = "zh-CN"
        self.last_good_code: str | None = None
        self.generation = 1
        self.last_active_ts = time.time()
        self.restore_failed = None
        self._alive = True
        self.calls: list[str] = []

    def is_alive(self) -> bool:
        return self._alive

    def ensure_alive(self) -> None:
        self._alive = True

    def start(self) -> None:
        self._alive = True

    def stop(self) -> None:
        self._alive = False

    def abort(self) -> None:
        self._alive = False

    def set_locale(self, lang: str) -> dict[str, Any]:
        self.locale = lang
        return {"ok": True, "lang": lang}

    def get_engine_diagnostics(self) -> dict[str, Any]:
        return {"ok": True, "game": "poe2", "engineWarnings": []}

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append(method)
        if method == "get_build_info":
            return {"level": 85, "classId": 10, "className": "Monk", "points": {"used": 1, "usedMax": 120}}
        if method == "get_stats":
            return {"stats": {"Life": 1500, "Spirit": 100, "CombinedDPS": 12000}, "count": 3}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "get_build_header":
            return {"level": 85}
        if method == "export_code":
            return {"code": "FAKE_POE2_CODE"}
        return {"ok": True}


def _make_mock_poe1_server(poe1_token: str) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Minimal simulated PoE1 daemon speaking X-PoB-Token & returning game='poe1'."""

    class _Poe1Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            tok = self.headers.get("X-PoB-Token", "")
            if self.headers.get("X-PoB2-Token") and not tok:
                body = json.dumps({"ok": False, "game": "poe1", "errorKind": "cross_game_hallucination"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not secrets.compare_digest(tok, poe1_token):
                body = json.dumps({"ok": False, "error": "Forbidden"}).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            length = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(length)
            body = json.dumps({"ok": True, "result": {"game": "poe1", "pid": os.getpid()}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Poe1Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _post_raw(port: int, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        conn.request("POST", "/rpc", body=body, headers={"Content-Type": "application/json", **headers})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw)
    finally:
        conn.close()


def test_read_cache_rev_lang_invalidation_and_loop_guard() -> None:
    cache = SlotReadCache(max_entries=16)
    cache.store("main", rev=1, lang="zh-CN", action="summary", params={}, payload={"life": 1000})
    hit1 = cache.lookup("main", rev=1, lang="zh-CN", action="summary", params={})
    assert hit1 is not None and hit1["cached"] is True and "loopGuardWarning" not in hit1
    hit2 = cache.lookup("main", rev=1, lang="zh-CN", action="summary", params={})
    assert hit2 is not None and hit2["loopGuardWarning"] == LOOP_GUARD_WARNING
    assert cache.lookup("main", rev=1, lang="en", action="summary", params={}) is None
    assert cache.lookup("main", rev=2, lang="zh-CN", action="summary", params={}) is None


def test_foreign_poe1_state_file_blocked_non_destructively(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    rt.mkdir()
    state_file = rt / DAEMON_STATE_FILENAME
    state_file.write_text(json.dumps({"game": "poe1", "pid": os.getpid(), "port": 9999, "token": "x"}), encoding="utf-8")
    with pytest.raises(CrossGameHallucinationError) as exc_info:
        daemon.stop_daemon(runtime_dir=rt)
    assert exc_info.value.rule_id == "DAEMON_STATE_GAME_MISMATCH"
    assert state_file.exists(), "Foreign PoE1 state file must not be deleted."


def test_concurrent_poe1_and_poe2_daemons_zero_port_env_dir_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poe1_rt = tmp_path / "poe1_rt"
    poe2_rt = tmp_path / "poe2_rt"
    poe1_rt.mkdir()
    poe2_rt.mkdir()

    # Conflicting PoE1 env vars MUST NEVER leak into PoE2 daemon/profile resolution
    monkeypatch.setenv("POB_RUNTIME_DIR", str(poe1_rt))
    monkeypatch.setenv("POB_LANG", "en")
    monkeypatch.setenv("POB_OFFLINE", "0")
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(poe2_rt))
    monkeypatch.setenv("POB2_LANG", "zh-CN")
    monkeypatch.setenv("POB2_OFFLINE", "1")

    assert daemon._get_runtime_dir() == poe2_rt
    assert get_active_lang()[0] == "zh-CN"
    assert daemon._is_offline_env() is True

    # Start simulated PoE1 daemon on loopback:0 + real PoE2 DaemonManager HTTP server on loopback:0 simultaneously
    poe1_token = secrets.token_hex(16)
    poe2_token = secrets.token_hex(16)
    poe1_srv, _ = _make_mock_poe1_server(poe1_token)
    poe1_port = poe1_srv.server_address[1]
    (poe1_rt / "daemon.json").write_text(
        json.dumps({"game": "poe1", "pid": os.getpid(), "port": poe1_port, "token": poe1_token}), encoding="utf-8"
    )

    fake_w = _FakeWorker("main", runtime_dir=poe2_rt)
    mgr = daemon.DaemonManager(pob_dir=tmp_path, token=poe2_token, runtime_dir=poe2_rt, offline=True, skip_update_check=True)
    SlotHistory("main", poe2_rt, persist=True).bind(fake_w)
    mgr.main_worker = fake_w
    poe2_srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(mgr))
    mgr.server = poe2_srv
    poe2_port = poe2_srv.server_address[1]
    t2 = threading.Thread(target=poe2_srv.serve_forever, daemon=True)
    t2.start()

    poe2_state = poe2_rt / DAEMON_STATE_FILENAME
    poe2_state.write_text(
        json.dumps({"game": GAME_ID, "pid": os.getpid(), "port": poe2_port, "token": poe2_token}), encoding="utf-8"
    )

    try:
        assert poe1_port != poe2_port
        assert (poe1_rt / "daemon.json").is_file()
        assert poe2_state.is_file()
        assert DAEMON_STATE_FILENAME == "daemon_poe2.json"
        assert DAEMON_LOCK_FILENAME == "daemon_poe2.lock"
        assert DAEMON_LOG_FILENAME == "daemon_poe2.log"

        # 1. PoE2 rejects missing/invalid token (403) and PoE1 X-PoB-Token (400 CrossGameHallucinationError)
        st403, _ = _post_raw(poe2_port, {}, {"action": "status"})
        assert st403 == 403
        st400, body400 = _post_raw(poe2_port, {"X-PoB-Token": poe1_token}, {"action": "status"})
        assert st400 == 400 and body400["errorKind"] == "cross_game_hallucination" and body400["detectedGame"] == "poe1"

        # 2. PoE1 rejects PoE2 X-PoB2-Token cleanly too
        st_p1_400, _ = _post_raw(poe1_port, {TOKEN_HEADER: poe2_token}, {"action": "status"})
        assert st_p1_400 == 400

        # 3. Both daemons serve concurrently via their own tokens & headers with zero conflict
        p1_st, p1_body = _post_raw(poe1_port, {"X-PoB-Token": poe1_token}, {"action": "status"})
        assert p1_st == 200 and p1_body["result"]["game"] == "poe1"

        p2_status = daemon.call_daemon("status", auto_start=False, runtime_dir=poe2_rt, emit_cold_start_alert=False)
        assert p2_status["game"] == "poe2"
        assert p2_status["lang"] == "zh-CN"

        # 4. Unloaded slot rejects summary until new_build/load succeeds; read cache then serves repeated summary
        with pytest.raises(daemon.DaemonRequestError, match="has no build loaded"):
            daemon.call_daemon("summary", auto_start=False, runtime_dir=poe2_rt, emit_cold_start_alert=False)
        daemon.call_daemon("new_build", auto_start=False, runtime_dir=poe2_rt, emit_cold_start_alert=False)
        calls_before = len(fake_w.calls)
        s1 = daemon.call_daemon("summary", auto_start=False, runtime_dir=poe2_rt, emit_cold_start_alert=False)
        calls_after_1 = len(fake_w.calls)
        s2 = daemon.call_daemon("summary", auto_start=False, runtime_dir=poe2_rt, emit_cold_start_alert=False)
        assert s1["game"] == "poe2" and not s1.get("cached")
        assert s2["cached"] is True and len(fake_w.calls) == calls_after_1 and calls_after_1 > calls_before

        # 5. Stopping PoE2 daemon does NOT affect PoE1 daemon or PoE1 state file
        stop_res = daemon.stop_daemon(runtime_dir=poe2_rt)
        assert stop_res.get("stopped") is True
        assert not poe2_state.exists()
        assert (poe1_rt / "daemon.json").exists()
        p1_st_after, _ = _post_raw(poe1_port, {"X-PoB-Token": poe1_token}, {"action": "status"})
        assert p1_st_after == 200
    finally:
        poe2_srv.shutdown()
        poe2_srv.server_close()
        poe1_srv.shutdown()
        poe1_srv.server_close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 daemon end-to-end test")
def test_live_poe2_daemon_spawn_cache_and_coexistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 daemon E2E test.")

    poe1_rt = tmp_path / "poe1_live_rt"
    poe2_rt = tmp_path / "poe2_live_rt"
    poe1_rt.mkdir()
    poe2_rt.mkdir()

    poe1_token = secrets.token_hex(16)
    poe1_srv, _ = _make_mock_poe1_server(poe1_token)
    poe1_port = poe1_srv.server_address[1]
    (poe1_rt / "daemon.json").write_text(
        json.dumps({"game": "poe1", "pid": os.getpid(), "port": poe1_port, "token": poe1_token}), encoding="utf-8"
    )

    monkeypatch.setenv("POB_RUNTIME_DIR", str(poe1_rt))
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(poe2_rt))
    monkeypatch.setenv("POB2_ENGINE_DIR", str(eng))
    monkeypatch.setenv("POB2_OFFLINE", "1")
    monkeypatch.setenv("POB2_LANG", "zh-CN")

    try:
        st = daemon.call_daemon("status", runtime_dir=poe2_rt, emit_cold_start_alert=False)
        assert st["game"] == "poe2"
        assert st["caps"].get("cowSnapshot") is True
        assert st["caps"].get("nodePowerKernel") is True
        assert st["caps"].get("agentProbes") is True

        state_disk = daemon.read_daemon_state(poe2_rt)
        assert state_disk is not None
        assert state_disk["game"] == "poe2"
        assert state_disk["port"] != poe1_port
        assert (poe2_rt / DAEMON_STATE_FILENAME).is_file()

        code = encode_xml_share_code(_POE2_DAEMON_XML)
        loaded = daemon.call_daemon("load", {"xml_code": code}, runtime_dir=poe2_rt, emit_cold_start_alert=False)
        assert loaded["game"] == "poe2" and loaded["loaded"] is True

        sum1 = daemon.call_daemon("summary", runtime_dir=poe2_rt, emit_cold_start_alert=False)
        sum2 = daemon.call_daemon("summary", runtime_dir=poe2_rt, emit_cold_start_alert=False)
        assert sum1["build"]["className"] == "Monk" and not sum1.get("cached")
        assert sum2.get("cached") is True

        # Switch language -> invalidates read cache cleanly
        lang_res = daemon.call_daemon("lang", {"lang": "en"}, runtime_dir=poe2_rt, emit_cold_start_alert=False)
        assert lang_res["effectiveLang"] == "en"
        sum3 = daemon.call_daemon("summary", runtime_dir=poe2_rt, emit_cold_start_alert=False)
        assert not sum3.get("cached")

        # PoE1 mock daemon still serves cleanly on its own port
        p1_st, p1_body = _post_raw(poe1_port, {"X-PoB-Token": poe1_token}, {"action": "status"})
        assert p1_st == 200 and p1_body["result"]["game"] == "poe1"
    finally:
        daemon.stop_daemon(runtime_dir=poe2_rt)
        poe1_srv.shutdown()
        poe1_srv.server_close()
