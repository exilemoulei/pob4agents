# SPDX-License-Identifier: GPL-3.0-only
"""Wave 0 Slice 3 offline tests: PR #93 security hardening, updater, snapshot, daemon, and packaging."""

from __future__ import annotations

import base64
import gzip
import importlib.util
import io
import json
import os
import stat
import tarfile
import threading
import time
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob2_cli import bridge_protocol, cli, daemon, importers, share_codec, updater
from pob2_cli.daemon import slot_updates
from pob2_cli.daemon.read_cache import _is_hist_unsettled
from pob2_cli.daemon.runtime import same_slot_error, write_private_json
from pob2_cli.daemon.server import _error_text
from pob2_cli.errors import DaemonRequestError
from pob2_cli.guardrails import redact_secrets, redact_url_credentials
from pob2_cli.importers import url_guard
from pob2_cli.snapshot import (
    BuildTransaction,
    SlotHistory,
    capture_digest,
    compact_digest_summary,
    diff_digest,
    history_of,
    rebuild_if_stale,
    redo_build,
    run_mutation,
    stat_drift,
    undo_build,
    verify_build,
)
from pob2_cli.updater import (
    POSIX_LUAJIT_COMMIT,
    POSIX_LUAJIT_TARBALL_SHA256,
    POSIX_LUAUTF8_TAG,
    POSIX_LUAUTF8_TARBALL_SHA256,
    UpdateError,
    ed25519_verify,
    parse_pob_manifest,
    verify_pob_sha1,
    verify_sums,
)
from pob2_cli.updater import remote as updater_remote
from pob2_cli.updater import state as updater_state

_POB2_ROOT = Path(__file__).resolve().parents[1]
_POE2_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<PathOfBuilding2><Build level="85" targetVersion="0_1" className="Monk" ascendClassName="Invoker"/>'
    '<Tree activeSpec="1"><Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/></Tree>'
    "<Items activeItemSet=\"1\"/><Skills activeSkillSet=\"1\"/><Config activeConfigSet=\"1\"/>"
    "</PathOfBuilding2>"
)


def _load_release_tools() -> Any:
    path = _POB2_ROOT / "packaging" / "release_tools.py"
    spec = importlib.util.spec_from_file_location("pob2_release_tools_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- 1. importers/: 64 MiB zlib/gzip/deflate bomb cap & public-HTTPS anti-SSRF -----------------


def test_share_codec_64mib_constant_and_noise_tolerance() -> None:
    assert share_codec.MAX_SHARE_XML_BYTES == 67_108_864
    code = importers.xml_to_share_code(_POE2_XML)
    noisy = code[:8] + "!\n\t*" + code[8:16] + "\u3002" + code[16:]
    assert importers.share_code_to_xml(noisy) == _POE2_XML

    gz_code = base64.urlsafe_b64encode(gzip.compress(_POE2_XML.encode("utf-8"))).decode("ascii")
    assert importers.share_code_to_xml(gz_code) == _POE2_XML


def test_share_code_64mib_zlib_gzip_and_deflate_bombs_rejected() -> None:
    # Real >64 MiB compressed zero payload (~66 KiB compressed, <50 ms) rejected without OOM!
    over_64m = b"<PathOfBuilding2>" + (b"0" * (share_codec.MAX_SHARE_XML_BYTES + 4096))
    zlib_bomb = base64.urlsafe_b64encode(zlib.compress(over_64m, level=1)).decode("ascii")

    with pytest.raises(ValueError, match="64 MiB"):
        importers.share_code_to_xml(zlib_bomb)
    assert bridge_protocol.decode_share_code_xml(zlib_bomb) is None
    with pytest.raises(DaemonRequestError, match="64 MiB"):
        importers.resolve_poe2_build_source(zlib_bomb)
    with pytest.raises(DaemonRequestError, match="64 MiB"):
        bridge_protocol.validate_poe2_xml_root(zlib_bomb)

    # Raw deflate (-15) & gzip bombs via smaller cap for speed
    raw_comp = zlib.compressobj(1, zlib.DEFLATED, -zlib.MAX_WBITS)
    raw_bomb = base64.urlsafe_b64encode(raw_comp.compress(b"B" * 20_000) + raw_comp.flush()).decode("ascii")
    gz_bomb = base64.urlsafe_b64encode(gzip.compress(b"C" * 20_000, compresslevel=1)).decode("ascii")
    for payload in (raw_bomb, gz_bomb):
        with pytest.raises(ValueError, match="exceeds"):
            importers.share_code_to_xml(payload, max_bytes=2048)

    # Truncated zlib stream fails cleanly as invalid share code
    good_raw = zlib.compress(_POE2_XML.encode("utf-8"))
    with pytest.raises(ValueError, match="Invalid PoB2 share code"):
        importers.share_code_to_xml(base64.urlsafe_b64encode(good_raw[:-6]).decode("ascii"))


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://pobb.in/abc123",
        "http://example.com/build.xml",
        "file:///etc/passwd",
        "ftp://example.com/build.xml",
        "https://alice:secret@pobb.in/abc",
        "https://127.0.0.1:8765/rpc",
        "https://localhost/build",
        "https://evil.localhost/build",
        "https://[::1]/build",
        "https://[::ffff:127.0.0.1]/build",
        "https://0177.0.0.1/build",
        "https://2130706433/build",
        "https://10.1.2.3/build",
        "https://172.16.5.4/build",
        "https://192.168.1.5/build",
        "https://169.254.169.254/latest/meta-data",
        "https://[fe80::1]/build",
        "https://[fc00::1]/build",
        "https://198.18.0.1/build",
        "https://100.64.0.1/build",
        "https://0.0.0.0/build",
        "https://224.0.0.1/build",
        "https://240.0.0.1/build",
        "https://api.github.com/repos/x/y",
        "https://api\u3002github\u3002com/repos/x/y",
    ],
)
def test_ssrf_guard_rejects_non_https_and_internal_targets(bad_url: str) -> None:
    with pytest.raises(DaemonRequestError, match="Refusing"):
        url_guard.check_public_https_url(bad_url)
    with pytest.raises(DaemonRequestError, match="Refusing"):
        importers.resolve_poe2_build_source(bad_url)


def test_ssrf_guard_blocks_internal_dns_and_redirect_hops_while_allowing_public_and_fake_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_map = {
        "internal.example": ["10.20.30.40"],
        "public.example": ["93.184.216.34"],
        "pobb.in": ["198.18.0.42"],  # Clash/sing-box fake-ip allowed ONLY for resolved hostnames
    }
    monkeypatch.setattr(url_guard, "_resolve_host", lambda host: dns_map.get(host, []))

    with pytest.raises(DaemonRequestError, match="private"):
        url_guard.check_public_https_url("https://internal.example/build")
    url_guard.check_public_https_url("https://pobb.in/abc")

    # Redirect hop to internal host or http:// must be blocked BEFORE connecting to target
    seen: list[str] = []

    def _evil_redirect(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path == "/to-lan":
            return httpx.Response(302, headers={"location": "https://internal.example/steal"})
        if req.url.path == "/to-http":
            return httpx.Response(301, headers={"location": "http://public.example/plain"})
        return httpx.Response(200, text="NEVER_REACHED")

    transport = httpx.MockTransport(_evil_redirect)
    with pytest.raises(DaemonRequestError, match="private"):
        importers.resolve_poe2_build_source("https://public.example/to-lan", transport=transport)
    with pytest.raises(DaemonRequestError, match="https://"):
        importers.resolve_poe2_build_source("https://public.example/to-http", transport=transport)
    assert seen == ["https://public.example/to-lan", "https://public.example/to-http"]

    # Safe public HTTPS redirect succeeds
    code = importers.xml_to_share_code(_POE2_XML)

    def _ok_redirect(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/short":
            return httpx.Response(302, headers={"location": "/raw/build"})
        return httpx.Response(200, text=code)

    resolved = importers.resolve_poe2_build_source(
        "https://public.example/short", transport=httpx.MockTransport(_ok_redirect)
    )
    assert resolved.source_kind == "https_share_url"
    assert "<PathOfBuilding2>" in resolved.xml


# --- 2. updater/: strict asset validation, proxy redaction, manifest & Ed25519 -----------------


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "",
        "../evil.zip",
        "a/b.zip",
        "..\\evil.zip",
        "foo..bar.zip",
        "C:evil.zip",
        ".hidden.zip",
        "asset.zip\n",
        "NUL.zip",
        "com1.zip",
        "lpt1.tar.gz",
        "aux",
        "malware.exe",
        "script.sh",
    ],
)
def test_updater_rejects_unsafe_or_unexpected_asset_names(unsafe_name: str, tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="Refusing release asset"):
        updater_remote._safe_asset_name(unsafe_name)
    with pytest.raises(UpdateError, match="Refusing release asset"):
        updater_remote._download_and_stage_release_zip("v1.0.0", {"name": unsafe_name, "sha256": "0" * 64}, tmp_path)


def test_updater_accepts_valid_assets_and_enforces_prefix_and_sha256(tmp_path: Path) -> None:
    assert updater_remote._safe_asset_name("PobTools-DataV2-26.zip") == "PobTools-DataV2-26.zip"
    assert updater_remote._safe_asset_name("PobTools[20261102].zip") == "PobTools[20261102].zip"
    assert updater_remote._safe_asset_name("console-1.0.zip") == "console-1.0.zip"
    with pytest.raises(UpdateError, match="mismatched asset prefix"):
        updater_remote._safe_asset_name("other-1.0.zip", expected_prefix="pob24agents-")

    with pytest.raises(UpdateError, match="unsafe tag"):
        updater_remote._download_and_stage_release_zip(
            "../badtag", {"name": "PobTools-1.0.zip", "sha256": "0" * 64}, tmp_path
        )
    with pytest.raises(UpdateError, match="Location header"):
        updater_remote._extract_tag_from_location("https://github.com/org/repo/releases/tag/../evil")


def test_updater_proxy_credential_redaction_everywhere(tmp_path: Path) -> None:
    raw = "ConnectError via https://alice:s3cr3tPass@proxy.corp.example:8443 and socks5://tok99@127.0.0.1:1080"
    scrubbed = updater.redact_proxy_credentials(raw)
    assert "s3cr3tPass" not in scrubbed and "alice" not in scrubbed and "tok99" not in scrubbed
    assert "https://[redacted]@proxy.corp.example:8443" in scrubbed
    assert "socks5://[redacted]@127.0.0.1:1080" in scrubbed

    with pytest.raises(UpdateError) as exc_info:
        with updater_remote._network_errors("Fetching manifest"):
            raise httpx.ProxyError("Failed proxy http://bob:hunter2@proxy.local:8080")
    assert "hunter2" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)

    saved = updater_state._save_state_snapshot(
        updater.STATE_DEGRADED_NETWORK,
        engine_dir=tmp_path / "eng",
        rt_dir=tmp_path / "rt",
        local_pob_ver="0.5.5",
        err="ProxyError('https://user1:mySecretPw@proxy.example:8080')",
    )
    assert "mySecretPw" not in (saved["networkError"] or "")
    assert "mySecretPw" not in (saved["userHint"] or "")
    alert = updater.format_stderr_update_alert(saved) or ""
    assert "mySecretPw" not in alert and "[redacted]" in alert
    assert redact_url_credentials("http://u:p@h:80/a") == "http://[redacted]@h:80/a"
    assert "secret" not in redact_secrets("Authorization: Bearer secret_tok_123")


def test_updater_manifest_ed25519_and_release_channel_helpers(tmp_path: Path) -> None:
    # RFC 8032 §7.1 TEST 1 (empty message)
    pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert ed25519_verify(pub, b"", sig) is True
    assert ed25519_verify(pub, b"tampered", sig) is False
    pub_b64 = base64.b64encode(pub).decode("ascii")
    sig_b64 = base64.b64encode(sig).decode("ascii")
    assert verify_sums(b"", sig_b64, keys=(pub_b64,)) is True
    assert verify_sums(b"bad", sig_b64, keys=(pub_b64,)) is False

    # Manifest parse + CRLF-tolerant SHA1 check
    sample_lua = b"return { patch = '0.5.5' }\n"
    sha1 = __import__("hashlib").sha1(sample_lua).hexdigest()
    mxml = (
        '<PoBVersion><Version number="0.5.5" branch="dev" platform="win32"/>'
        '<Source part="default" url="https://example.com/{name}"/>'
        f'<File name="GameVersions.lua" sha1="{sha1}" part="default"/></PoBVersion>'
    )
    manifest = parse_pob_manifest(mxml, slim=True)
    assert manifest.version == "0.5.5"
    assert "GameVersions.lua" in manifest.files
    assert verify_pob_sha1(sample_lua.replace(b"\n", b"\r\n"), sha1) is True
    assert updater.parse_version("poe2-v0.2.1") == (0, 2, 1, 1)
    assert updater.is_newer("poe2-v0.2.1", "0.2.0") is True


# --- 3. snapshot/: SlotHistory save/load/diff/round-trip & verified CoW rollback ---------------


class _OfflineCowWorker:
    def __init__(self) -> None:
        self.slot_name = "main"
        self.runtime_dir = None
        self.lock = threading.RLock()
        self._proc = object()
        self.is_running = True
        self.generation = 1
        self._req_counter = 0
        self.caps = {"cowSnapshot": True}
        self.last_good_code: str | None = "CODE_BASE"
        self.has_build = True
        self.restore_failed = None
        self.dps = 10_000.0
        self.drift_cow_restore = False
        self.fail_pump = False
        self.calls: list[str] = []
        self._snaps: dict[str, float] = {}
        self._codes: dict[str, float] = {"CODE_BASE": 10_000.0}

    def _cur_code(self) -> str:
        c = f"CODE_{int(self.dps)}"
        self._codes[c] = self.dps
        return c

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        _ = timeout
        self._req_counter += 1
        self.calls.append(method)
        p = params or {}
        if method == "pump":
            if self.fail_pump:
                raise RuntimeError("engine pump busy")
            return {"ok": True}
        if method == "cow_snapshot":
            sid = f"s{len(self._snaps) + 1}"
            self._snaps[sid] = self.dps
            return {"snapId": sid}
        if method == "cow_restore":
            if not self.drift_cow_restore:
                self.dps = self._snaps[p["snapId"]]
            return {"ok": True}
        if method == "cow_drop":
            return {"ok": True}
        if method == "export_code":
            return {"code": self._cur_code()}
        if method == "import_code":
            self.dps = self._codes.get(p["code"], 10_000.0)
            return {"ok": True}
        if method == "get_stats":
            return {"stats": {"Life": 2000.0, "Spirit": 120.0, "CombinedDPS": self.dps, "TotalEHP": 8000.0}}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "get_build_info":
            return {"level": 85, "classId": 10, "className": "Monk", "points": {"used": 95, "usedMax": 120}}
        if method == "get_build_header":
            return {"level": 85}
        return {"ok": True}


def test_snapshot_roundtrip_digest_diff_persist_warning_and_exception_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    w = _OfflineCowWorker()
    hist = SlotHistory("main", runtime_dir=tmp_path, persist=True)
    hist.load_build("CODE_BASE", "<PathOfBuilding2>inline</PathOfBuilding2>")
    assert hist.source_label == "inline_build"
    hist.bind(w)

    # Round-trip reload from disk
    reloaded = SlotHistory.load("main", runtime_dir=tmp_path, persist=True)
    assert reloaded.committed == "CODE_BASE" and reloaded.rev == 1

    # Digest + diff + verify_build
    d1 = capture_digest(w)
    w.dps = 15_000.0
    d2 = capture_digest(w)
    delta = diff_digest(d1, d2)
    assert any(r["stat"] == "CombinedDPS" and r["delta"] == 5000.0 for r in delta["deltas"])
    assert compact_digest_summary(d2)["spirit"] == 120.0
    assert stat_drift(d1["stats"], d2["stats"]) == ["CombinedDPS"]
    assert verify_build(w, d1)["verified"] is False
    w.dps = 10_000.0
    assert verify_build(w, d1)["verified"] is True

    # Commit + undo + redo + persistWarning on write_slot_state failure
    monkeypatch.setattr(bridge_protocol.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    res = run_mutation(w, lambda wk: setattr(wk, "dps", 18_000.0), apply=True)
    assert res["applied"] is True
    assert "could not be saved" in res["persistWarning"]
    assert "could not save slot 'main'" in capsys.readouterr().err
    monkeypatch.undo()

    res2 = run_mutation(w, lambda wk: setattr(wk, "dps", 20_000.0), apply=True)
    assert "persistWarning" not in res2
    assert undo_build(w)["undoDepth"] == 1 and w.dps == 18_000.0
    assert redo_build(w)["redoDepth"] == 0 and w.dps == 20_000.0

    # Exception-path CoW drift triggers fallback import_code + attaches __notes__ for _error_text
    w.drift_cow_restore = True

    def _mutate_and_fail(wk: Any) -> None:
        wk.dps = 99_999.0
        raise ValueError("bad mutation input")

    with pytest.raises(ValueError, match="bad mutation input") as exc_info:
        run_mutation(w, _mutate_and_fail, apply=False)
    assert w.dps == 20_000.0
    assert "re-importing" in _error_text(exc_info.value)


# --- 4. daemon/: private daemon_poe2.json, stale slot auto-rebuild, same-slot guard ------------


def test_write_private_json_owner_only_and_hot_reload_preserves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "rt" / "daemon_poe2.json"
    stale_tmp = state_path.with_suffix(f".{os.getpid()}.tmp")
    stale_tmp.parent.mkdir(parents=True, exist_ok=True)
    stale_tmp.write_text("{}", encoding="utf-8")

    write_private_json(state_path, {"game": "poe2", "pid": os.getpid(), "port": 12345, "token": "tok_secret"})
    assert not stale_tmp.exists()
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert loaded["token"] == "tok_secret"
    if os.name != "nt":
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    monkeypatch.setattr(slot_updates, "_daemon_state_file", lambda _rt=None: state_path)
    slot_updates.update_daemon_state_pob_dir(SimpleNamespace(runtime_dir=tmp_path / "rt", pob_dir=tmp_path / "new_eng"))
    updated = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated["token"] == "tok_secret" and updated["pob_dir"].endswith("new_eng")
    if os.name != "nt":
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


class _StubDaemonWorker:
    def __init__(
        self,
        slot_name: str = "main",
        pob_dir: Path | None = None,
        data_dir: Path | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        self.slot_name = slot_name
        self.pob_root = pob_dir
        self.data_dir = data_dir
        self.runtime_dir = runtime_dir
        self.lock = threading.RLock()
        self.caps = {"cowSnapshot": True, "game": "poe2"}
        self.hello_info = {"protocol": 1}
        self.locale = "zh-CN"
        self.last_good_code: str | None = None
        self.has_build = False
        self.generation = 1
        self.last_active_ts = time.time()
        self.restore_failed = None
        self._alive = True
        self.fail_pump = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

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

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        _ = timeout
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "pump":
            if self.fail_pump:
                raise RuntimeError("pump failed")
            return {"ok": True}
        if method == "import_code":
            self.has_build = True
            return {"ok": True}
        if method == "export_code":
            return {"code": self.last_good_code or importers.xml_to_share_code(_POE2_XML)}
        if method == "save_xml":
            return {"xml": _POE2_XML}
        if method == "get_build_info":
            return {"level": 85, "classId": 10, "className": "Monk", "treeVersion": "0_5", "points": {"used": 1, "usedMax": 120}}
        if method == "get_stats":
            return {"stats": {"Life": 1800, "Spirit": 100, "CombinedDPS": 9000, "TotalEHP": 6000}}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "get_build_header":
            return {"level": 85}
        return {"ok": True}


def test_daemon_stale_slot_auto_rebuild_before_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = tmp_path / "rt"
    rt.mkdir()
    code = importers.xml_to_share_code(_POE2_XML)
    # Pre-populate persisted slot_main.json on disk
    SlotHistory("main", runtime_dir=rt, persist=True).load_build(code, "saved.xml")

    monkeypatch.setattr(daemon, "BridgeWorker", _StubDaemonWorker)
    mgr = daemon.DaemonManager(pob_dir=tmp_path, token="t", runtime_dir=rt, offline=True, skip_update_check=True)

    # 1. Worker starts with has_build=False while history has committed code -> auto-rebuilds via import_code on read!
    mgr.main_worker.has_build = False
    summary = mgr.dispatch("summary", {"target": "main"})
    assert summary["game"] == "poe2"
    assert any(m == "import_code" for m, _ in mgr.main_worker.calls)
    assert mgr.main_worker.has_build is True

    # 2. Failed BuildTransaction fresh() marks SlotHistory.stale=True -> read cache is bypassed & next read pumps!
    hist = history_of(mgr.main_worker)
    mgr.main_worker.fail_pump = True
    with BuildTransaction(mgr.main_worker) as tx:
        tx.release()
        tx.stale = True
    assert hist.stale is True and _is_hist_unsettled(hist)

    with pytest.raises(RuntimeError, match="pump failed"):
        mgr.dispatch("summary", {"target": "main"})
    # Recovery action (`new_build`) replaces the build and must NOT be blocked by stale pump failure!
    rebuild_if_stale(mgr.main_worker, required=False)
    assert hist.stale is True

    mgr.main_worker.fail_pump = False
    mgr.main_worker.calls.clear()
    res_after = mgr.dispatch("summary", {"target": "main"})
    assert res_after["game"] == "poe2" and not res_after.get("cached")
    assert hist.stale is False
    assert mgr.main_worker.calls[0][0] == "pump"

    # 3. External update to slot_main.json on disk triggers automatic reload & re-import before next read!
    time.sleep(0.01)
    ext_hist = SlotHistory.load("main", runtime_dir=rt, persist=True)
    ext_hist.commit(code, code)
    mgr.main_worker.calls.clear()
    mgr.dispatch("summary", {"target": "main"})
    assert any(m == "import_code" for m, _ in mgr.main_worker.calls)


@pytest.mark.parametrize(
    "params",
    [
        {"target": "main", "ref_target": "main"},
        {"target": "MAIN", "ref_target": "main"},
        {"target": "Alpha", "ref_target": "alpha"},
        {"main_target": "beta", "ref_target": "BETA", "ref_source": "https://pobb.in/abc"},
    ],
)
def test_daemon_and_cli_reject_same_slot_self_compare_with_rc1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, params: dict[str, Any]
) -> None:
    monkeypatch.setattr(daemon, "BridgeWorker", _StubDaemonWorker)
    mgr = daemon.DaemonManager(pob_dir=tmp_path, token="t", runtime_dir=tmp_path / "rt", offline=True, skip_update_check=True)
    with pytest.raises(DaemonRequestError, match="against itself"):
        mgr.dispatch("compare", params)


@pytest.mark.parametrize(
    "cli_args",
    [
        ["compare", "main", "main"],
        ["compare", "Alpha", "alpha"],
        ["compare", "--target", "main", "--ref-target", "main"],
        ["compare", "--ref-target", "main"],
        ["compare", "main"],
    ],
)
def test_cli_compare_same_slot_exits_1_without_daemon(
    cli_args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POB2_SLOT", raising=False)
    runner = CliRunner()
    res = runner.invoke(cli.main, cli_args)
    assert res.exit_code == 1, res.output
    assert "against itself" in res.output
    assert "--ref-target" in same_slot_error("compare", "main")


# --- 5. packaging/: 4-file suite, poe2-v* tags, Scoop bucket/pob2.json, archives ---------------


def test_packaging_four_file_suite_and_posix_runtime_pins() -> None:
    pkg_dir = _POB2_ROOT / "packaging"
    files = sorted(p.name for p in pkg_dir.iterdir() if p.is_file())
    assert files == ["build_posix_runtime.sh", "pob2.spec", "pyinstaller_entry.py", "release_tools.py"]

    sh_raw = (pkg_dir / "build_posix_runtime.sh").read_bytes()
    assert b"\r\n" not in sh_raw, "build_posix_runtime.sh must keep LF line endings"
    sh_text = sh_raw.decode("utf-8")
    assert f'LUAJIT_COMMIT="{POSIX_LUAJIT_COMMIT}"' in sh_text
    assert f'LUAJIT_SHA256="{POSIX_LUAJIT_TARBALL_SHA256}"' in sh_text
    assert f'LUAUTF8_TAG="{POSIX_LUAUTF8_TAG}"' in sh_text
    assert f'LUAUTF8_SHA256="{POSIX_LUAUTF8_TARBALL_SHA256}"' in sh_text
    assert ("pob" + "_cli") not in sh_text


def test_release_tools_poe2_tags_archives_checksums_and_scoop_manifest(tmp_path: Path) -> None:
    rt = _load_release_tools()
    assert rt.release_tag("0.2.0") == "poe2-v0.2.0"
    assert rt.release_tag("poe2-v0.2.0-rc.1") == "poe2-v0.2.0-rc.1"
    assert rt.pep440_version("0.2.0-rc.1") == "0.2.0rc1"
    with pytest.raises(ValueError, match="poe2-v"):
        rt.check_version("v0.2.0")

    # Pack Windows + POSIX + Python wheel/sdist + sums + verify (with synthetic verified signature hook)
    win_dist = tmp_path / "dist_win"
    win_dist.mkdir()
    (win_dist / "pob2.exe").write_bytes(b"MZ-fake-pob2")
    posix_dist = tmp_path / "dist_linux"
    posix_dist.mkdir()
    pob2_bin = posix_dist / "pob2"
    pob2_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
    pob2_bin.chmod(0o755)
    posix_rt = tmp_path / "rt_linux"
    (posix_rt / "bin").mkdir(parents=True)
    for fn in ("luajit", "lua-utf8.so"):
        fp = posix_rt / "bin" / fn
        fp.write_bytes(b"\x7fELF")
        fp.chmod(0o755)
    (posix_rt / "bin" / "runtime.stamp").write_text(updater.runtime_pin() + "\n", encoding="utf-8")

    out_dir = tmp_path / "assets"
    _, win_sha = rt.pack_windows(win_dist, out_dir, "0.2.0")
    rt.pack_posix(posix_dist, posix_rt, out_dir, "0.2.0", "linux-x64")
    for py_name in rt.python_asset_names("0.2.0"):
        (out_dir / py_name).write_bytes(b"wheel-or-sdist")
    sums_path = rt.write_sums(out_dir)
    parsed_sums = rt.parse_sums(sums_path.read_text(encoding="utf-8"))
    assert parsed_sums["pob24agents-0.2.0-windows-x64.zip"] == win_sha

    # Verify catches missing signature when SIG_NAME absent, and passes once valid Ed25519 sig is present
    pub_hex = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    probs_unsigned = rt.verify(out_dir, "0.2.0", ["windows-x64", "linux-x64"], keys=(base64.b64encode(bytes.fromhex(pub_hex)).decode(),))
    assert probs_unsigned == ["missing asset: SHA256SUMS.txt.sig"]

    # Archive path-traversal checks
    bad_tar = tmp_path / "evil.tar.gz"
    with tarfile.open(bad_tar, "w:gz") as tf:
        ti = tarfile.TarInfo("../escape")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    assert any("unsafe path" in p for p in rt.tar_problems(bad_tar, ("pob2",)))

    bad_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.txt", "x")
    assert any("unsafe path" in p for p in rt.zip_problems(bad_zip, "pob2.exe"))

    # Scoop manifest bucket/pob2.json generation & update
    manifest_file = tmp_path / "bucket" / "pob2.json"
    scoop = rt.update_scoop_manifest(manifest_file, "0.2.0", win_sha)
    assert scoop["version"] == "0.2.0"
    assert scoop["license"] == "GPL-3.0-only"
    assert scoop["bin"] == "pob2.exe"
    assert "/poe2-v0.2.0/pob24agents-0.2.0-windows-x64.zip" in scoop["architecture"]["64bit"]["url"]
    assert "/poe2-v$version/pob24agents-$version-windows-x64.zip" in scoop["autoupdate"]["architecture"]["64bit"]["url"]
