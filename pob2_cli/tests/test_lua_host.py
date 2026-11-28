"""Tests for PoB2 Slim LuaJIT host, POSIX pin, manifest parser, #2529/#2582 shields, and live read-only engine."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from pob2_cli import bridge_protocol, updater
from pob2_cli.bridge_client import BridgeError, BridgeWorker
from pob2_cli.errors import CrossGameHallucinationError
from pob2_cli.updater import manifest as manifest_mod
from pob2_cli.updater import posix_runtime

_POE2_MINIMAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Test Missing Blade
Nonexistent 055 Phantom Sword
Item Level: 82
+100 to maximum Life
    </Item>
    <Item id="2">
Rarity: NORMAL
Iron Ring
Item Level: 10
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Ring 1" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1"/>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1"/>
  </Config>
</PathOfBuilding2>
"""


def test_poe2_manifest_defaults_to_master_branch_and_slim_filters_images() -> None:
    sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PoBVersion>
  <Version number="0.23.1"/>
  <Source part="runtime" platform="win32" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding-PoE2/{branch}/runtime/"/>
  <Source part="program" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding-PoE2/{branch}/src/"/>
  <File name="lua51.dll" part="runtime" runtime="win32" sha1="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/>
  <File name="zlib1.dll" part="runtime" runtime="win32" sha1="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"/>
  <File name="Launch.lua" part="program" sha1="cccccccccccccccccccccccccccccccccccccccc"/>
  <File name="TreeData/0_4/tree.lua" part="program" sha1="dddddddddddddddddddddddddddddddddddddddd"/>
  <File name="TreeData/0_5/tree.lua" part="program" sha1="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"/>
  <File name="Assets/huge_texture.dds.zst" part="program" sha1="ffffffffffffffffffffffffffffffffffffffff"/>
</PoBVersion>
"""
    win_m = manifest_mod.parse_pob_manifest(sample_xml, include_win32_runtime=True, slim=True)
    assert win_m.version == "0.23.1"
    assert win_m.branch == "master"
    assert "lua51.dll" in win_m.files
    assert "Launch.lua" in win_m.files
    assert "TreeData/0_5/tree.lua" in win_m.files
    assert "Assets/huge_texture.dds.zst" not in win_m.files
    url = manifest_mod.build_manifest_file_url(win_m, win_m.files["Launch.lua"])
    assert "/PathOfBuilding-PoE2/master/src/Launch.lua" in url

    posix_m = manifest_mod.parse_pob_manifest(sample_xml, include_win32_runtime=False, slim=True)
    assert "lua51.dll" not in posix_m.files
    assert "Launch.lua" in posix_m.files


def test_bootstrap_force_flag_and_cli_failure_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from click.testing import CliRunner

    from pob2_cli import cli as cli_mod
    from pob2_cli import cli_lifecycle

    rt = tmp_path / "rt"
    eng = rt / "engine"
    (eng / "PoE2").mkdir(parents=True)
    (eng / "PoE2" / "Launch.lua").write_text("-- launch\n", encoding="utf-8")
    (eng / "Data" / "bridge").mkdir(parents=True)
    (eng / "Data" / "bridge" / "bridge.lua").write_text("-- bridge\n", encoding="utf-8")
    monkeypatch.setattr(updater, "is_windows", lambda: False)
    (eng / "bin").mkdir(parents=True)
    (eng / "bin" / "luajit").write_bytes(b"fake")

    res = updater.bootstrap_engine(runtime_dir=rt, force=False)
    assert res["state"] == updater.STATE_UP_TO_DATE
    assert res["source"] == "existing"

    monkeypatch.setattr(
        cli_lifecycle,
        "bootstrap_engine",
        lambda **_kw: {"state": updater.STATE_BOOTSTRAP_MISSING, "userHint": "network down"},
    )
    out = CliRunner().invoke(cli_mod.main, ["bootstrap", "--json"])
    assert out.exit_code == 1
    payload = json.loads(out.output)
    assert payload["ok"] is False
    assert payload["errorKind"] == "bootstrap_missing"


def test_posix_runtime_pins_2460b3f_and_rejects_stock_path_luajit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert updater.POSIX_LUAJIT_COMMIT.startswith("2460b3f")
    assert updater.POSIX_LUAUTF8_TAG == "0.1.6"
    assert "2460b3ff93a1c955de3d62cfc825de7d68dc272e" in posix_runtime.runtime_pin()
    assert "c += 41" in posix_runtime._SELF_TEST_LUA

    monkeypatch.delenv("POB2_LUAJIT", raising=False)
    monkeypatch.delenv("POB2_ENGINE_CMD", raising=False)
    # Must NOT pick up arbitrary stock `luajit` from PATH when <engine>/bin/luajit is absent
    assert updater.resolve_luajit_binary(tmp_path) is None
    bin_lj = tmp_path / "bin" / "luajit"
    bin_lj.parent.mkdir(parents=True)
    bin_lj.write_bytes(b"fake-pinned-luajit")
    assert updater.resolve_luajit_binary(tmp_path) == bin_lj


def test_windows_slim_command_never_uses_pob_zh_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "is_windows", lambda: True)
    monkeypatch.delenv("POB2_ENGINE_CMD", raising=False)
    poe2 = tmp_path / "PoE2"
    poe2.mkdir(parents=True)
    (poe2 / "Launch.lua").write_text("-- launch\n", encoding="utf-8")
    (poe2 / "lua51.dll").write_bytes(b"MZ")
    (poe2 / "zlib1.dll").write_bytes(b"MZ")
    bridge = tmp_path / "Data" / "bridge" / "bridge.lua"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("-- bridge\n", encoding="utf-8")

    cmd, env = updater.build_engine_command(tmp_path, runtime_dir=tmp_path / "rt")
    assert "_lua-host" in cmd
    assert all("pob-zh.exe" not in arg.lower() for arg in cmd)
    assert env["POB_GAME"] == "poe2"
    assert env["POB2_READONLY_GAME_DIR"] == "1"


def test_bridge_protocol_rejects_poe1_xml_and_blocked_rpcs() -> None:
    poe1_xml = "<PathOfBuilding><Build level='90'/></PathOfBuilding>"
    with pytest.raises(CrossGameHallucinationError) as exc_info:
        bridge_protocol.validate_poe2_xml_root(poe1_xml)
    assert exc_info.value.to_dict()["game"] == "poe2"
    assert exc_info.value.to_dict()["detectedGame"] == "poe1"

    for bad_rpc in ("tj_open", "tj_close", "tattoo_open", "import_tree_url", "compare_load", "save_build_as"):
        with pytest.raises(BridgeError):
            bridge_protocol.validate_rpc_method(bad_rpc)


def test_python_2582_missing_base_detector_flags_critical_weapon_drop() -> None:
    alerts = bridge_protocol.detect_dropped_items_against_loaded(_POE2_MINIMAL_XML, loaded_item_ids={2})
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["issue"] == "#2582"
    assert alert["code"] == "missing_item_base_dropped"
    assert alert["severity"] == "critical"
    assert alert["isWeaponSlot"] is True
    assert alert["missingBase"] == "Nonexistent 055 Phantom Sword"
    assert alert["affectedSlots"] == ["Weapon 1"]


def _dir_mtime_fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """Lightweight top-level & PoE2/Data mtime+size snapshot to verify 0 bytes written to read-only seed."""
    snap: dict[str, tuple[int, int]] = {}
    for target in (root, root / "PoE2", root / "PoE2" / "Data"):
        if target.is_dir():
            for child in target.iterdir():
                if child.is_file():
                    st = child.stat()
                    snap[str(child)] = (st.st_size, st.st_mtime_ns)
    return snap


def _external_engine_env() -> tuple[Path | None, Path | None]:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data = Path(data_raw) if data_raw else None
    if eng is not None and not updater.is_valid_engine_dir(eng, data_dir=data):
        return None, None
    return eng, data


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native lua51.dll host test")
def test_live_pob2_slim_host_shields_zh_dict_and_zero_writes(tmp_path: Path) -> None:
    eng, data_dir = _external_engine_env()
    if eng is None:
        pytest.skip("Set POB2_ENGINE_DIR (and optional POB2_DATA_DIR) to run live PoB2 engine tests.")

    before_fp = _dir_mtime_fingerprint(eng)
    rt_dir = tmp_path / "rt"
    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=rt_dir)
    try:
        worker.start()
        assert worker.caps.get("winLuaHost") is True
        assert worker.caps.get("localeSwitch") is True
        assert worker.caps.get("mainOutputShield") is True
        assert worker.caps.get("missingBaseDetector") is True
        assert worker.caps.get("readonlyGameDir") is True

        # 1. Verify Chinese dictionary translation (zh-CN, zh-TW, en)
        loc_cn = worker.set_locale("zh-CN")
        assert loc_cn.get("ok") is True and loc_cn.get("dictLoaded") is True
        tr_cn = worker.call("translate_text", {"text": "Spirit"})
        assert tr_cn["translated"] == "精魂"
        loc_tw = worker.set_locale("zh-TW")
        assert loc_tw.get("ok") is True and loc_tw.get("dictLoaded") is True
        loc_en = worker.set_locale("en")
        assert loc_en.get("translate") is False

        # 2. Verify #2529 mainOutput == nil shield (both nil output & Lua error paths)
        probe_nil = worker.call("probe_main_output_shield", {"mode": "nil"})
        assert probe_nil["ok"] is True
        assert probe_nil["shieldDuringFault"]["triggered"] is True
        assert probe_nil["mainOutputIsTable"] is True
        probe_err = worker.call("probe_main_output_shield", {"mode": "error"})
        assert probe_err["ok"] is True
        assert probe_err["shieldDuringFault"]["triggered"] is True

        # 3. Verify #2582 missing 0.5.5 base silent weapon drop detector on live import_code
        code = bridge_protocol.encode_xml_share_code(_POE2_MINIMAL_XML)
        imp = worker.call("import_code", {"code": code, "mode": "replace"})
        dropped = imp.get("droppedItems") or []
        assert any(d.get("issue") == "#2582" and d.get("missingBase") == "Nonexistent 055 Phantom Sword" for d in dropped)
        assert any(d.get("isWeaponSlot") is True and d.get("severity") == "critical" for d in dropped)

        diag = worker.get_engine_diagnostics()
        assert diag["missingBaseDetector"]["droppedCount"] >= 1
        assert diag["mainOutputShield"]["installed"] is True

        # 4. Verify live build info & stats calculation still succeed cleanly
        info = worker.call("get_build_info", {})
        assert isinstance(info, dict)
        assert info.get("level") == 90
        assert info.get("className") == "Monk"
        stats_res = worker.call("get_stats", {})
        assert isinstance(stats_res.get("stats"), dict)
        assert stats_res.get("count", 0) > 0
        assert hashlib.sha1(b"ok").hexdigest()  # sanity
    finally:
        worker.stop()

    after_fp = _dir_mtime_fingerprint(eng)
    assert after_fp == before_fp, "Read-only PoE2 seed directory must have 0 bytes modified/created!"
