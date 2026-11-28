"""Tests for PoE2 BuildTransaction, BuildScope, SlotHistory, CoW kernel, node_power_kernel, and agent_probes."""

from __future__ import annotations

import collections
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from pob2_cli import bridge_protocol, updater
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.game_profile import POE2_CLASS_IDS
from pob2_cli.snapshot import (
    BuildScope,
    BuildTransaction,
    SlotHistory,
    capture_digest,
    compact_digest_summary,
    load_build,
    redo_build,
    run_mutation,
    stat_drift,
    undo_build,
)

_POE2_COW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: NORMAL
Iron Ring
Item Level: 10
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Ring 1" itemId="1"/>
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


class _FakeCowWorker:
    """In-memory fake worker implementing cow_snapshot/cow_restore/export_code/import_code for offline tx tests."""

    def __init__(self) -> None:
        self.slot_name = "test"
        self.runtime_dir = None
        self.lock = threading.RLock()
        self._proc = object()
        self.generation = 1
        self._req_counter = 0
        self.caps = {"cowSnapshot": True}
        self.last_good_code: str | None = "CODE_INIT"
        self.restore_failed = None
        self.state: dict[str, Any] = {"life": 1000.0, "spirit": 100.0, "ws2": 0, "attr": "Str", "set1": True, "set2": False}
        self._snaps: dict[str, dict[str, Any]] = {}
        self._codes: dict[str, dict[str, Any]] = {"CODE_INIT": dict(self.state)}

    def _encode_cur(self) -> str:
        code = f"CODE_{self.state['life']}_{self.state['ws2']}_{self.state['attr']}_{self.state['set1']}_{self.state['set2']}"
        self._codes[code] = dict(self.state)
        return code

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._req_counter += 1
        p = params or {}
        if method == "cow_snapshot":
            sid = f"cow_{len(self._snaps) + 1}"
            self._snaps[sid] = dict(self.state)
            return {"snapId": sid}
        if method == "cow_restore":
            sid = p["snapId"]
            self.state = dict(self._snaps[sid])
            if p.get("drop"):
                self._snaps.pop(sid, None)
            return {"ok": True}
        if method == "cow_drop":
            self._snaps.pop(p.get("snapId", ""), None)
            return {"ok": True}
        if method == "export_code":
            return {"code": self._encode_cur()}
        if method == "import_code":
            self.state = dict(self._codes[p["code"]])
            return {"ok": True}
        if method == "get_stats":
            return {"stats": {"Life": self.state["life"], "Spirit": self.state["spirit"], "CombinedDPS": 5000.0}}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "get_build_info":
            return {"level": 90, "classId": 10, "className": "Monk", "points": {"used": 1 + self.state["ws2"], "usedMax": 120}}
        if method == "get_build_header":
            return {"level": 90}
        if method == "pump":
            return {"ok": True}
        return {"ok": True}


def test_offline_build_transaction_cow_rollback_and_slot_history_undo_redo(tmp_path: Path) -> None:
    worker = _FakeCowWorker()
    hist = SlotHistory("main", runtime_dir=tmp_path, persist=True)
    hist.committed = "CODE_INIT"
    hist.bind(worker)

    # 1. What-if mutation uses CoW and rolls back cleanly
    res = run_mutation(
        worker,
        lambda w: w.state.update({"life": 1400.0, "ws2": 3, "attr": "Int", "set1": False, "set2": True}),
        apply=False,
        scope=BuildScope.ACTIVE_SETS,
    )
    assert res["applied"] is False
    assert res["game"] == "poe2"
    assert worker.state["life"] == 1000.0
    assert worker.state["ws2"] == 0
    assert worker.state["attr"] == "Str"
    assert worker.state["set1"] is True and worker.state["set2"] is False
    assert "restoreFallback" not in res
    assert hist.rev == 0

    # 2. Apply mutation commits to SlotHistory, bumps rev, and supports undo/redo
    applied = run_mutation(
        worker,
        lambda w: w.state.update({"life": 1600.0, "ws2": 2}),
        apply=True,
        scope=BuildScope.ACTIVE_SETS,
    )
    assert applied["applied"] is True
    assert worker.state["life"] == 1600.0
    assert hist.rev == 1
    assert hist.depths() == {"undoDepth": 1, "redoDepth": 0}
    assert hist.path.is_file()

    u_res = undo_build(worker)
    assert u_res["action"] == "undo" and worker.state["life"] == 1000.0
    assert hist.depths() == {"undoDepth": 0, "redoDepth": 1}

    r_res = redo_build(worker)
    assert r_res["action"] == "redo" and worker.state["life"] == 1600.0
    assert hist.depths() == {"undoDepth": 1, "redoDepth": 0}

    # 3. Verify compact summary carries PoE2 Spirit & stat_drift detects tiny drifts
    digest = capture_digest(worker)
    compact = compact_digest_summary(digest)
    assert compact["game"] == "poe2" and compact["spirit"] == 100.0
    assert stat_drift({"Life": 1000.0, "Str": 25.0}, {"Life": 1000.0, "Str": 30.0}) == ["Str"]


def _find_bfs_targets(tree_nodes: dict[str, Any], start_ids: list[int]) -> tuple[int, int, int]:
    """BFS from allocated start nodes to find (first_attr_node, second_attr_node, ws2_normal_node)."""
    visited = set(start_ids)
    q: collections.deque[tuple[int, int]] = collections.deque((nid, 0) for nid in start_ids)
    attr_nodes: list[int] = []
    normal_nodes: list[int] = []

    while q and (len(attr_nodes) < 2 or len(normal_nodes) < 3):
        cur, depth = q.popleft()
        nd = tree_nodes.get(str(cur)) or {}
        for nxt in nd.get("linked") or []:
            if nxt in visited:
                continue
            nobj = tree_nodes.get(str(nxt))
            if not isinstance(nobj, dict):
                continue
            if nobj.get("asc") or nobj.get("type") in ("ClassStart", "AscendClassStart", "Keystone", "Socket"):
                continue
            visited.add(nxt)
            if nobj.get("attribute"):
                attr_nodes.append(nxt)
            elif nobj.get("type") in ("Normal", "Notable"):
                normal_nodes.append(nxt)
            if depth < 8:
                q.append((nxt, depth + 1))

    assert len(attr_nodes) >= 2, f"Expected >=2 reachable PoE2 attribute nodes, found {attr_nodes}"
    assert len(normal_nodes) >= 1, f"Expected >=1 reachable PoE2 normal node, found {normal_nodes}"
    return attr_nodes[0], attr_nodes[1], normal_nodes[0]


def _dir_mtime_fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    snap: dict[str, tuple[int, int]] = {}
    for target in (root, root / "PoE2", root / "PoE2" / "Data"):
        if target.is_dir():
            for child in target.iterdir():
                if child.is_file():
                    st = child.stat()
                    snap[str(child)] = (st.st_size, st.st_mtime_ns)
    return snap


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 CoW & kernel verification")
def test_live_poe2_cow_ws2_skill_sets_attribute_rollback_and_kernels(tmp_path: Path) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live PoE2 CoW and kernel tests.")

    before_fp = _dir_mtime_fingerprint(eng)
    rt_dir = tmp_path / "rt"
    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=rt_dir)
    try:
        worker.start()
        assert worker.caps.get("cowSnapshot") is True
        assert worker.caps.get("nodePowerKernel") is True
        assert worker.caps.get("agentProbes") is True

        # Load PoE2 Monk build + add a real skill group + allocate one baseline attribute node (Str=1)
        code = bridge_protocol.encode_xml_share_code(_POE2_COW_XML)
        load_build(worker, xml_code=code)
        worker.call("add_group", {"label": "MainWS", "gems": [{"nameSpec": "Ice Strike", "level": 18, "quality": 0}]})
        worker.call("set_skill_weapon_set", {"index": 1, "set1": True, "set2": False, "enableGlobal1": True, "enableGlobal2": False})

        tdata = worker.call("tree_data", {})
        tstate0 = worker.call("get_tree_state", {})
        attr_node_1, attr_node_2, ws2_node = _find_bfs_targets(tdata["nodes"], tstate0["allocatedNodes"])

        # Allocate attr_node_1 as Strength (+5 Str, allocMode=0) BEFORE taking the CoW snapshot
        worker.call("set_alloc_mode", {"mode": 0})
        worker.call("tree_attribute", {"id": attr_node_1, "attribute": 1})

        # Capture full pre-mutation baseline
        before_stats = worker.call("get_stats", {})["stats"]
        before_tree = worker.call("get_tree_state", {})
        before_ws = worker.call("get_weapon_set_state", {})
        before_digest = capture_digest(worker)

        assert str(attr_node_1) in before_ws["hashOverrides"]
        assert before_ws["weaponSet2Used"] == 0
        assert before_ws["skillWeaponSets"][0]["set1"] is True
        assert before_ws["skillWeaponSets"][0]["set2"] is False

        # Run compound PoE2 mutation inside CoW BuildTransaction:
        #   (A) Switch to Weapon Set 2 (allocMode=2, weaponSwap=True) & allocate passive nodes in WS2
        #   (B) Switch existing attribute node (+5 Str -> +5 Int) AND allocate second attribute node (+5 Dex) in WS2
        #   (C) Flip skill group set1/set2 & enableGlobal1/2
        with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False) as tx:
            assert tx.base is not None and tx.base.kind == "cow"

            worker.call("set_alloc_mode", {"mode": 2})
            worker.call("set_weapon_swap", {"on": True})
            worker.call("tree_click", {"id": ws2_node})
            worker.call("tree_attribute", {"id": attr_node_1, "attribute": 3})  # Str -> Int
            worker.call("tree_attribute", {"id": attr_node_2, "attribute": 2})  # allocate Dex node
            worker.call("set_skill_weapon_set", {"index": 1, "set1": False, "set2": True, "enableGlobal1": False, "enableGlobal2": True})

            mid_stats = worker.call("get_stats", {})["stats"]
            mid_ws = worker.call("get_weapon_set_state", {})
            assert mid_ws["allocMode"] == 2
            assert mid_ws["useSecondWeaponSet"] is True
            assert mid_ws["weaponSet2Used"] >= 1
            assert mid_ws["skillWeaponSets"][0]["set1"] is False
            assert mid_ws["skillWeaponSets"][0]["set2"] is True
            assert mid_stats.get("Int") != before_stats.get("Int") or mid_stats.get("Str") != before_stats.get("Str")

            check = tx.rollback_verified(before_digest)
            assert check["verified"] is True, f"CoW rollback check failed: {check}"
            assert tx.notes == [], f"Expected pure Lua CoW restore without fallback, got: {tx.notes}"

        # Assert 100% field-by-field idempotence after CoW rollback
        after_stats = worker.call("get_stats", {})["stats"]
        after_tree = worker.call("get_tree_state", {})
        after_ws = worker.call("get_weapon_set_state", {})

        assert stat_drift(before_stats, after_stats) == []
        assert after_stats == before_stats
        assert after_tree["allocatedNodes"] == before_tree["allocatedNodes"]
        assert after_tree["overrides"] == before_tree["overrides"]
        assert after_ws == before_ws

        # Verify PoE2 node_power_kernel (8 class IDs + dual weapon sets, no PoE1 quadrants/clusters)
        np_res = worker.call("node_power", {"maxDepth": 3}, timeout=45.0)
        assert np_res["game"] == "poe2"
        assert tuple(np_res["validClassIds"]) == POE2_CLASS_IDS
        assert np_res["classId"] == 10 and np_res["className"] == "Monk"
        assert np_res["validPoe2Class"] is True
        assert isinstance(np_res["weaponSet"], dict)
        assert len(np_res["report"]) > 0
        assert "clusterCount" not in np_res

        # Verify PoE2 agent_probes (Spirit + Deflection + weaponSet, zero PoE1 triggerCliffs)
        pr_res = worker.call("agent_probe_build", {}, timeout=30.0)
        assert pr_res["game"] == "poe2"
        assert "spirit" in pr_res["resourceSlack"] and "spiritDemanded" in pr_res["resourceSlack"]
        assert "deflectionChance" in pr_res["defenceSlack"]
        assert "weaponSet" in pr_res
        assert "triggerCliffs" not in pr_res
    finally:
        worker.stop()

    after_fp = _dir_mtime_fingerprint(eng)
    assert after_fp == before_fp, "Read-only PoE2 seed directory must have 0 bytes modified/created!"
