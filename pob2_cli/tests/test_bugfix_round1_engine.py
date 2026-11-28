# SPDX-License-Identifier: GPL-3.0-only
"""Round 1 engine/mutator/daemon/analyzer bugfix regression & live E2E tests."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pob2_cli import cli, daemon, updater
from pob2_cli.analyzer import audit_build
from pob2_cli.analyzer.deep_audit import _build_findings_from_probe
from pob2_cli.bridge_client import (
    BridgeWorker,
    _wrap_rpc_rejection,
    sanitize_bridge_error,
    validate_poe2_spec_class_ids,
)
from pob2_cli.cli_mutate import _format_what_if_with_details_md
from pob2_cli.errors import DaemonRequestError
from pob2_cli.importers.post_load_fixups import _strip_bonded_lines_from_item_raw
from pob2_cli.importers.share_urls import (
    extract_active_spec_node_ids,
    load_build_into_worker,
    resolve_poe2_build_source,
)
from pob2_cli.mutators import run_what_if
from pob2_cli.snapshot import stat_drift

_BASE_MONK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Starter Staff
Wrapped Quarterstaff
Item Level: 70
Adds 25 to 45 Physical Damage
    </Item>
    <Item id="2">
Rarity: NORMAL
Iron Ring
Item Level: 20
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Ring 1" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" label="Main" mainActiveSkill="1">
        <Gem nameSpec="Ice Strike" level="18" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1"/>
  </Config>
</PathOfBuilding2>
"""

_DISCONNECTED_TREE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986,56349,47175,999991,999992"/>
  </Tree>
  <Items activeItemSet="1"><ItemSet id="1" useSecondWeaponSet="false"/></Items>
  <Skills activeSkillSet="1"><SkillSet id="1"/></Skills>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding2>
"""

_ORACLE_BONDED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Druid" ascendClassName="Oracle" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="11" ascendClassId="1" nodes="61525"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Oracle Hood
Runeforged Woven Cap
Sockets: S
Rune: Desert Rune
Item Level: 82
+58 to maximum Life
+40% to Lightning Resistance
    </Item>
    <Item id="2">
Rarity: RARE
Oracle Vest
Vile Robe
Item Level: 82
Implicits: 2
{enchant}{rune}+14% to Fire Resistance
{enchant}{rune}Bonded: +20 to maximum Life
+90 to maximum Life
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Helmet" itemId="1"/>
      <Slot name="Body Armour" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1"><SkillSet id="1"/></Skills>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding2>
"""

_TRUE_WS_SWAP_SPIRIT_GAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Warrior" ascendClassName="Titan" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="6" ascendClassId="1" nodes="47175"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Spirit Sceptre
Hallowed Sceptre
Item Level: 82
+50 to Spirit
    </Item>
    <Item id="2">
Rarity: RARE
Swap Mace
Flanged Mace
Item Level: 82
Adds 50 to 90 Physical Damage
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Weapon 1 Swap" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" label="Aura1" mainActiveSkill="1" set1="true" set2="true">
        <Gem nameSpec="Herald of Ash" level="20" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="Aura2" mainActiveSkill="1" set1="true" set2="true">
        <Gem nameSpec="Herald of Ice" level="20" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="Aura3" mainActiveSkill="1" set1="true" set2="true">
        <Gem nameSpec="Herald of Thunder" level="20" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="Aura4" mainActiveSkill="1" set1="true" set2="true">
        <Gem nameSpec="Wind Dancer" level="20" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1">
      <Input name="questFreythorn" boolean="true"/>
      <Input name="customMods" string="+200 to Strength
+200 to Dexterity
+200 to Intelligence"/>
    </ConfigSet>
  </Config>
</PathOfBuilding2>
"""

_RARE_RING_TEXT = """Rarity: RARE
Test Band
Ruby Ring
Item Level: 82
+80 to maximum Life
+45% to Fire Resistance
+40% to Cold Resistance
+35 to Strength"""

_RARE_ARMOUR_TEXT = """Rarity: RARE
Test Plate
Steel Plate
Item Level: 82
+120 to maximum Life
+42% to Fire Resistance
+42% to Lightning Resistance"""

_RARE_WEAPON_TEXT = """Rarity: RARE
Test Quarterstaff
Gothic Quarterstaff
Item Level: 82
Adds 150 to 260 Physical Damage
24% increased Attack Speed"""


def _engine_env() -> tuple[Path | None, Path | None]:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data = Path(data_raw) if data_raw else None
    if eng is not None and not updater.is_valid_engine_dir(eng, data_dir=data):
        return None, None
    return eng, data


def test_p1_11_missing_local_file_path_raises_file_not_found() -> None:
    for missing in (
        "nonexistent_build.xml",
        "missing_code.pob",
        "missing_code.pob2",
        "notes_export.txt",
        "subdir/missing_build.xml",
        r"Z:\nonexistent_dir\missing.xml",
    ):
        with pytest.raises(DaemonRequestError) as exc_info:
            resolve_poe2_build_source(missing)
        assert f"Build file not found: {missing}" in str(exc_info.value)


def test_p1_7_invalid_poe2_class_id_blocked_before_engine() -> None:
    for bad_id in ("0", "3", "4", "5", "12", "abc"):
        bad_xml = f'<PathOfBuilding2><Tree activeSpec="1"><Spec treeVersion="0_5" classId="{bad_id}" nodes="50986"/></Tree></PathOfBuilding2>'
        with pytest.raises(DaemonRequestError) as exc_info:
            validate_poe2_spec_class_ids(bad_xml)
        assert "[POE2-INVALID-CLASS-ID]" in str(exc_info.value)
        with pytest.raises(DaemonRequestError) as exc_info2:
            resolve_poe2_build_source(bad_xml)
        assert "[POE2-INVALID-CLASS-ID]" in str(exc_info2.value)

    for good_id in (1, 2, 6, 7, 8, 9, 10, 11):
        ok_xml = f'<PathOfBuilding2><Tree activeSpec="1"><Spec treeVersion="0_5" classId="{good_id}" nodes="50986"/></Tree></PathOfBuilding2>'
        validate_poe2_spec_class_ids(ok_xml)


def test_p1_8_sanitize_bridge_error_strips_lua_traceback_and_respects_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        "RPC 'tree_click' failed: no node 999999\n"
        "stack traceback:\n"
        "\t[C]: in function 'error'\n"
        "\tData/bridge/bridge.lua:1355: in function 'tree_click'\n"
    )
    monkeypatch.delenv("POB2_DEBUG", raising=False)
    cleaned = sanitize_bridge_error(raw)
    assert "\n" not in cleaned
    assert "stack traceback" not in cleaned.lower()
    assert "no node 999999" in cleaned
    assert "pob2 tree" in cleaned

    exc = _wrap_rpc_rejection("tree_click", {"code": "lua_error", "message": "no node 999999\nstack traceback:\n\tfoo"})
    assert exc.kind == "request"
    assert "stack traceback" not in str(exc).lower()

    monkeypatch.setenv("POB2_DEBUG", "1")
    assert "stack traceback:" in sanitize_bridge_error(raw)


def test_p2_1_weapon_set_swap_audit_no_false_positive_critical() -> None:
    base_res = {"issues": []}

    # 1. WS2 empty -> downgraded to info, NEVER critical
    probe_empty_ws2 = {
        "weaponSetSwapAudit": {
            "checked": True,
            "activeWeaponSet": 1,
            "ws1Populated": True,
            "ws2Populated": False,
            "ws2Empty": True,
            "hasSwapSpiritDrop": False,
            "hasSwapAttrRedText": True,
            "ws1": {"weaponSet": 1, "populated": True, "spiritUnreserved": 88, "attrDeficits": {"Str": {"deficit": -10}}},
            "ws2": {"weaponSet": 2, "populated": False, "spiritUnreserved": 88, "attrDeficits": {"Str": {"deficit": -10}}},
        }
    }
    f_empty = [f for f in _build_findings_from_probe(probe_empty_ws2, {}, base_res) if f["category"] == "weapon_set_swap_gap"]
    assert len(f_empty) == 1 and f_empty[0]["severity"] == "info"

    # 2. Both WS1 and WS2 populated with identical deficits -> advisory, NEVER critical
    probe_identical = {
        "weaponSetSwapAudit": {
            "checked": True,
            "activeWeaponSet": 1,
            "ws1Populated": True,
            "ws2Populated": True,
            "hasSwapSpiritDrop": False,
            "hasSwapAttrRedText": True,
            "ws1": {"weaponSet": 1, "populated": True, "spiritUnreserved": 40, "attrDeficits": {"Int": {"deficit": -20}}},
            "ws2": {"weaponSet": 2, "populated": True, "spiritUnreserved": 40, "attrDeficits": {"Int": {"deficit": -20}}},
        }
    }
    f_ident = [f for f in _build_findings_from_probe(probe_identical, {}, base_res) if f["category"] == "weapon_set_swap_gap"]
    assert len(f_ident) == 1 and f_ident[0]["severity"] == "advisory"

    # 3. Populated WS2 introduces a NEW attribute deficit -> critical
    probe_new_attr = {
        "weaponSetSwapAudit": {
            "checked": True,
            "activeWeaponSet": 1,
            "ws1Populated": True,
            "ws2Populated": True,
            "hasSwapSpiritDrop": False,
            "hasSwapAttrRedText": True,
            "ws1": {"weaponSet": 1, "populated": True, "spiritUnreserved": 40, "attrDeficits": {}},
            "ws2": {"weaponSet": 2, "populated": True, "spiritUnreserved": 40, "attrDeficits": {"Dex": {"deficit": -25}}},
        }
    }
    f_attr = [f for f in _build_findings_from_probe(probe_new_attr, {}, base_res) if f["category"] == "weapon_set_swap_gap"]
    assert len(f_attr) == 1 and f_attr[0]["severity"] == "critical"

    # 4. Populated WS2 drops SpiritUnreserved into deficit -> critical
    probe_spirit_drop = {
        "weaponSetSwapAudit": {
            "checked": True,
            "activeWeaponSet": 1,
            "ws1Populated": True,
            "ws2Populated": True,
            "hasSwapSpiritDrop": True,
            "hasSwapAttrRedText": False,
            "ws1": {"weaponSet": 1, "populated": True, "spiritUnreserved": 30, "attrDeficits": {}},
            "ws2": {"weaponSet": 2, "populated": True, "spiritUnreserved": -20, "attrDeficits": {}},
        }
    }
    f_sp = [f for f in _build_findings_from_probe(probe_spirit_drop, {}, base_res) if f["category"] == "weapon_set_swap_gap"]
    assert len(f_sp) == 1 and f_sp[0]["severity"] == "critical"


def test_p0_1_what_if_item_equip_uses_slot_name_and_raises_on_null_id() -> None:
    class _FakeEquipWorker:
        def __init__(self, *, return_null_id: bool = False) -> None:
            self.slot_name = "test"
            self.runtime_dir = None
            self.lock = threading.RLock()
            self.caps = {"cowSnapshot": True}
            self.last_good_code = "CODE0"
            self.pob_version = "0.23.1"
            self._req_counter = 0
            self.return_null_id = return_null_id
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.life = 1000.0

        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            self._req_counter += 1
            p = dict(params or {})
            self.calls.append((method, p))
            if method == "cow_snapshot":
                return {"snapId": "cow_1"}
            if method == "cow_restore":
                self.life = 1000.0
                return {"ok": True}
            if method == "cow_drop":
                return {"ok": True}
            if method == "add_item":
                if self.return_null_id:
                    return {"slots": []}
                self.life = 1080.0
                return {
                    "item": {"id": 42, "name": "Test Band, Ruby Ring"},
                    "slots": [{"name": "Ring 1", "selItemId": 42}],
                }
            if method == "unequip_slot":
                assert "slotName" in p
                self.life = 900.0
                return {"ok": True}
            if method == "get_stats":
                return {"stats": {"Life": self.life, "CombinedDPS": 5000.0, "TotalEHP": 8000.0}}
            if method == "get_sidebar":
                return {"warnings": []}
            if method == "get_build_info":
                return {"level": 90, "treeVersion": "0_5", "points": {"used": 10}}
            if method == "get_build_header":
                return {"level": 90}
            return {"ok": True}

    fw = _FakeEquipWorker()
    res = run_what_if(fw, {"slot": "ring1", "item_text": _RARE_RING_TEXT}, apply=False)
    add_calls = [p for m, p in fw.calls if m == "add_item"]
    assert add_calls and add_calls[0]["slotName"] == "Ring 1"
    assert res["detail"][0]["itemId"] == 42
    assert res["detail"][0]["slot"] == "Ring 1"
    assert float(res["after"]["life"]) == 1080.0

    # Unequip uses slotName
    res_un = run_what_if(fw, {"unequip_slot": "ring1"}, apply=False)
    assert res_un["detail"][0]["op"] == "unequip_slot" and res_un["detail"][0]["slot"] == "Ring 1"

    # Missing item ID raises DaemonRequestError instead of silently returning itemId: null
    fw_bad = _FakeEquipWorker(return_null_id=True)
    with pytest.raises(DaemonRequestError, match="did not return an item ID"):
        run_what_if(fw_bad, {"slot": "Ring 1", "item_text": _RARE_RING_TEXT}, apply=False)


def test_p1_5_extract_active_spec_nodes_and_strip_bonded_lines() -> None:
    nodes = extract_active_spec_node_ids(_DISCONNECTED_TREE_XML)
    assert nodes == {50986, 56349, 47175, 999991, 999992}

    raw = (
        "Rarity: RARE\nOracle Vest\nVile Robe\nImplicits: 2\n"
        "{enchant}{rune}+14% to Fire Resistance\n"
        "{enchant}{rune}Bonded: +20 to maximum Life\n"
        "+90 to maximum Life"
    )
    cleaned, removed = _strip_bonded_lines_from_item_raw(raw)
    assert removed == ["{enchant}{rune}Bonded: +20 to maximum Life"]
    assert "Bonded:" not in cleaned
    assert "Implicits: 1" in cleaned


def test_p1_6_empty_slot_guard_blocks_unloaded_slots_without_spawning_worker(tmp_path: Path) -> None:
    mgr = daemon.DaemonManager(
        pob_dir=tmp_path,
        token="tok",
        runtime_dir=tmp_path / "rt",
        offline=True,
        skip_update_check=True,
    )
    for action in (
        "summary",
        "diagnose",
        "audit",
        "items",
        "skills",
        "tree",
        "config_list",
        "what_if",
        "diff",
        "compare",
        "uniques",
        "notes",
        "export",
        "spirit",
    ):
        with pytest.raises(DaemonRequestError) as exc_info:
            mgr.dispatch(action, {"target": "never-loaded"})
        assert "Slot 'never-loaded' has no build loaded" in str(exc_info.value)
        assert "pob2 load <source> --target never-loaded" in str(exc_info.value)

    # Guard MUST NOT spawn a worker for never-loaded!
    assert "never-loaded" not in mgr._workers

    # With a cold (unloaded) main worker, summary is blocked while status & offline spirit --budget work
    class _ColdWorker:
        def __init__(self) -> None:
            self.slot_name = "main"
            self.runtime_dir = tmp_path / "rt"
            self.lock = threading.RLock()
            self.has_build = False
            self.last_good_code = None
            self.generation = 1
            self.restore_failed = None
            self.hello_info: dict[str, Any] = {}
            self.caps: dict[str, Any] = {}

        def is_alive(self) -> bool:
            return True

        def ensure_alive(self) -> None:
            pass

        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"ok": True}

    mgr.main_worker = _ColdWorker()  # type: ignore[assignment]
    with pytest.raises(DaemonRequestError, match="Slot 'main' has no build loaded"):
        mgr.dispatch("summary", {"target": "main"})
    st = mgr.dispatch("status", {})
    assert st["main"]["hasLoadedBuild"] is False
    sp_pack = mgr.dispatch("spirit", {"target": "main", "budget": 120})
    assert sp_pack["mode"] == "pack"


def test_p1_9_cli_what_if_markdown_renders_tree_path_and_isolated_effect() -> None:
    sample = {
        "mode": "WHAT-IF",
        "before": {"primaryDps": 10000, "totalEhp": 5000, "life": 2000},
        "after": {"primaryDps": 11550, "totalEhp": 5200, "life": 2050},
        "diff": {"deltas": []},
        "detail": [
            {
                "op": "tree_click",
                "nodeId": 12345,
                "nodeName": "Big Notable",
                "pointsUsedBefore": 49,
                "pointsUsedAfter": 60,
                "nodesDeltaCount": 11,
                "allocatedPathNodes": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                "connectivityWarning": "Allocating node 12345 auto-allocated 10 intermediate travel node(s).",
                "isolatedNodeEffect": {
                    "delta": {"primaryDps": 760.0, "primaryDpsPct": 7.6, "totalEhp": 50.0, "totalEhpPct": 1.0, "life": 0.0}
                },
            }
        ],
    }
    md = _format_what_if_with_details_md(sample)
    assert "points 49 -> 60 (delta +11)" in md
    assert "path travel nodes: 10" in md
    assert "Isolated 1-node effect:" in md and "+7.60%" in md


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 Round 1 engine E2E tests")
def test_live_p0_1_what_if_equip_on_real_builds_and_idempotent_rollback(tmp_path: Path) -> None:
    eng, data_dir = _engine_env()
    if eng is None:
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 Round 1 tests.")

    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        builds_dir = eng / "PoE2" / "Builds"
        targets: list[tuple[str, Path | None]] = []
        for fn in ("long.xml", "bing.xml"):
            p = builds_dir / fn
            if p.is_file():
                targets.append((fn, p))
        if not targets:
            targets.append(("fallback_monk", None))

        for label, bpath in targets:
            if bpath is not None:
                load_build_into_worker(worker, source=str(bpath), auto_fix=True)
            else:
                load_build_into_worker(worker, xml_code=_BASE_MONK_XML, auto_fix=True)

            before_stats = worker.call("get_stats", {})["stats"]
            for slot_nm, raw_txt in (
                ("Ring 1", _RARE_RING_TEXT),
                ("Body Armour", _RARE_ARMOUR_TEXT),
                ("Weapon 1", _RARE_WEAPON_TEXT),
            ):
                wi = run_what_if(worker, {"slot": slot_nm, "item_text": raw_txt}, apply=False)
                assert wi["applied"] is False, f"{label} {slot_nm}"
                assert wi["detail"][0]["itemId"] is not None, f"{label} {slot_nm} returned null itemId"
                assert wi["detail"][0]["slot"] == slot_nm
                assert len(wi["diff"]["deltas"]) > 0, f"{label} {slot_nm} produced no stat deltas!"
                assert "restoreFallback" not in wi
                assert stat_drift(before_stats, worker.call("get_stats", {})["stats"]) == []

            # Also verify P2-1 zero false-positive critical on real builds
            aud = audit_build(worker)
            ws_crits = [
                f for f in aud["findings"] if f["category"] == "weapon_set_swap_gap" and f["severity"] == "critical"
            ]
            assert ws_crits == [], f"Unexpected false-positive critical weapon_set_swap_gap on {label}: {ws_crits}"
    finally:
        worker.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 tree path cascade, anoint preview, and fast nodeIds")
def test_live_p1_9_p1_10_tree_path_anoint_preview_and_fast_node_ids(tmp_path: Path) -> None:
    eng, data_dir = _engine_env()
    if eng is None:
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 tree & node_power tests.")

    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        load_build_into_worker(worker, xml_code=_BASE_MONK_XML, auto_fix=True)
        before_stats = worker.call("get_stats", {})["stats"]
        before_tree = worker.call("get_tree_state", {})["allocatedNodes"]

        # Pick 15 unallocated Notable nodes from live tree_data and a notable with pathDist >= 5
        tdata = worker.call("tree_data", {})
        notables: list[int] = []
        far_notable: int | None = None
        for nid_s, nd in (tdata.get("nodes") or {}).items():
            if not isinstance(nd, dict) or nd.get("type") != "Notable" or nd.get("asc"):
                continue
            nid = int(nid_s)
            if nid in before_tree:
                continue
            if len(notables) < 15:
                notables.append(nid)
            if far_notable is None:
                hov = worker.call("node_hover", {"id": nid})
                cost = hov.get("cost")
                if isinstance(cost, int) and 4 <= cost <= 15:
                    far_notable = nid
            if len(notables) >= 15 and far_notable is not None:
                break

        assert far_notable is not None and len(notables) == 15

        # 1. Normal what-if --tree-node discloses multi-point path cascade + isolatedNodeEffect + rollback
        wi_path = run_what_if(worker, {"tree_node": far_notable}, apply=False)
        op = wi_path["detail"][0]
        assert op["nodeId"] == far_notable
        assert op["nodesDeltaCount"] >= 4
        assert op["pointsUsedAfter"] - op["pointsUsedBefore"] == op["nodesDeltaCount"]
        assert len(op["allocatedPathNodes"]) == op["nodesDeltaCount"] - 1
        assert "connectivityWarning" in op
        assert "isolatedNodeEffect" in op and "delta" in op["isolatedNodeEffect"]
        assert "restoreFallback" not in wi_path
        assert worker.call("get_tree_state", {})["allocatedNodes"] == before_tree
        assert stat_drift(before_stats, worker.call("get_stats", {})["stats"]) == []

        # 2. --anoint-preview evaluates single node in isolation (1 point, 0 path travel nodes) + clean rollback
        wi_iso = run_what_if(worker, {"tree_node": far_notable, "anoint_preview": True}, apply=False)
        op_iso = wi_iso["detail"][0]
        assert op_iso["isolatedMode"] is True
        assert op_iso["nodesDeltaCount"] == 1
        assert op_iso["allocatedPathNodes"] == []
        assert "connectivityWarning" in op_iso
        assert worker.call("get_tree_state", {})["allocatedNodes"] == before_tree
        assert stat_drift(before_stats, worker.call("get_stats", {})["stats"]) == []

        # Combining --apply with --anoint-preview is blocked
        with pytest.raises(DaemonRequestError, match="Cannot combine --apply"):
            run_what_if(worker, {"tree_node": far_notable, "anoint_preview": True}, apply=True)

        # 3. Fast nodeIds subset evaluation in node_power (<1s vs ~15-35s full scan!)
        t0 = time.perf_counter()
        np_sub = worker.call("node_power", {"nodeIds": notables}, timeout=15.0)
        elapsed_s = time.perf_counter() - t0
        assert elapsed_s < 2.0, f"Subset node_power took {elapsed_s:.2f}s (expected <1s)"
        assert np_sub["filteredNodeIds"] == notables
        assert len(np_sub["report"]) == len(notables)
    finally:
        worker.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 P1-5 disconnected nodes, Bonded, and P2-1 real swap gap")
def test_live_p1_5_disconnected_nodes_non_shaman_bonded_and_true_ws_swap_critical(tmp_path: Path) -> None:
    eng, data_dir = _engine_env()
    if eng is None:
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 P1-5/P2-1 tests.")

    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()

        # 1. P1-5 Disconnected nodes warning on load
        ld_disc = load_build_into_worker(worker, xml_code=_DISCONNECTED_TREE_XML, auto_fix=True)
        assert ld_disc.get("disconnectedNodesDropped", 0) >= 2
        assert any("tree-disconnected" in w for w in (ld_disc.get("warnings") or []))

        # 2. P1-5 Non-Shaman Oracle Bonded rune stripping & deep_audit warning
        ld_ora = load_build_into_worker(worker, xml_code=_ORACLE_BONDED_XML, auto_fix=True)
        assert any(f.get("issue") == "non_shaman_bonded_runes" for f in ld_ora.get("postLoadFixups") or [])
        aud_ora = audit_build(worker)
        assert any(f.get("category") == "inactive_bonded_runes" for f in aud_ora.get("findings") or [])

        # 3. P2-1 Real WS1 <-> WS2 Spirit deficit on populated WS2 IS marked critical
        load_build_into_worker(worker, xml_code=_TRUE_WS_SWAP_SPIRIT_GAP_XML, auto_fix=False)
        aud_ws = audit_build(worker)
        ws_f = [f for f in aud_ws.get("findings") or [] if f.get("category") == "weapon_set_swap_gap"]
        assert len(ws_f) == 1 and ws_f[0]["severity"] == "critical"
    finally:
        worker.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live daemon CLI guardrails E2E")
def test_live_daemon_cli_empty_slot_invalid_class_missing_file_and_no_lua_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng, data_dir = _engine_env()
    if eng is None:
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 daemon CLI tests.")

    rt_dir = tmp_path / "rt_cli"
    rt_dir.mkdir()
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(rt_dir))
    monkeypatch.setenv("POB2_ENGINE_DIR", str(eng))
    if data_dir is not None:
        monkeypatch.setenv("POB2_DATA_DIR", str(data_dir))
    monkeypatch.setenv("POB2_OFFLINE", "1")
    monkeypatch.delenv("POB2_DEBUG", raising=False)

    runner = CliRunner()
    valid_xml = tmp_path / "monk.xml"
    valid_xml.write_text(_BASE_MONK_XML, encoding="utf-8")
    disc_xml = tmp_path / "disc.xml"
    disc_xml.write_text(_DISCONNECTED_TREE_XML, encoding="utf-8")
    bad_cls_xml = tmp_path / "bad_class.xml"
    bad_cls_xml.write_text(
        '<PathOfBuilding2><Build level="90" className="Witch"/><Tree activeSpec="1"><Spec treeVersion="0_5" classId="5" nodes="54447"/></Tree></PathOfBuilding2>',
        encoding="utf-8",
    )

    try:
        # 1. P1-6 Empty slot guard -> rc=1, NO fake Lv1 Ranger
        r_empty = runner.invoke(cli.main, ["summary", "--target", "never-loaded"])
        assert r_empty.exit_code == 1
        assert "Slot 'never-loaded' has no build loaded" in r_empty.output
        assert "Ranger" not in r_empty.output

        # 2. P1-11 Missing file error -> rc=1, "Build file not found:"
        r_miss = runner.invoke(cli.main, ["load", str(tmp_path / "does_not_exist.xml")])
        assert r_miss.exit_code == 1
        assert "Build file not found:" in r_miss.output

        # 3. P1-7 Invalid classId=5 -> rc=1, [POE2-INVALID-CLASS-ID], 0 Lua stack tracebacks
        r_cls = runner.invoke(cli.main, ["load", str(bad_cls_xml)])
        assert r_cls.exit_code == 1
        assert "[POE2-INVALID-CLASS-ID]" in r_cls.output
        assert "stack traceback" not in r_cls.output.lower()

        # 4. P1-5 Disconnected nodes warning emitted on load
        r_disc = runner.invoke(cli.main, ["load", str(disc_xml), "--json"])
        assert r_disc.exit_code == 0
        # Output may have stderr warning mixed in CliRunner; parse JSON from first '{'
        js_start = r_disc.output.find("{")
        assert js_start >= 0
        disc_payload = json.loads(r_disc.output[js_start:])
        assert disc_payload.get("disconnectedNodesDropped", 0) >= 2
        assert "Warning:" in r_disc.output[:js_start] or "tree-disconnected" in r_disc.output

        # 5. Load valid build, then trigger invalid tree node -> rc=1, clean single-line error, ZERO stack traceback
        r_ok = runner.invoke(cli.main, ["load", str(valid_xml)])
        assert r_ok.exit_code == 0
        r_bad_node = runner.invoke(cli.main, ["what-if", "tree", "--tree-node", "999999"])
        assert r_bad_node.exit_code == 1
        assert "no node 999999" in r_bad_node.output
        assert "stack traceback" not in r_bad_node.output.lower()
    finally:
        daemon.stop_daemon(runtime_dir=rt_dir)
