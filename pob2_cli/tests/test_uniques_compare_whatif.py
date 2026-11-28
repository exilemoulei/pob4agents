# SPDX-License-Identifier: GPL-3.0-only
"""Tests for PoE2 keyword-free full-slot unique ranking (incl. Flask/Charm), 5-dim compare, what-if, and diff."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pob2_cli import updater
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.comparator import compare_builds, normalize_slot_arg, rank_uniques
from pob2_cli.comparator.compare import _splice_dimension_xml
from pob2_cli.errors import CrossGameHallucinationError
from pob2_cli.formatters import format_compare_md, format_uniques_md, format_what_if_md
from pob2_cli.importers.share_urls import load_build_into_worker
from pob2_cli.mutators import diff_against_source, run_what_if
from pob2_cli.snapshot import redo_build, stat_drift, undo_build

_STARTER_MONK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="80" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Starter Stick
Wrapped Quarterstaff
Item Level: 60
Adds 20 to 40 Physical Damage
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
        <Gem nameSpec="Ice Strike" level="12" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1"/>
  </Config>
</PathOfBuilding2>
"""

_UPGRADED_REF_MONK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Endgame Quarterstaff
Gothic Quarterstaff
Item Level: 82
Adds 140 to 240 Physical Damage
25% increased Attack Speed
    </Item>
    <Item id="2">
Rarity: RARE
Endgame Ring
Iron Ring
Item Level: 80
Adds 15 to 30 Cold Damage to Attacks
+80 to maximum Life
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Ring 1" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" label="Main" mainActiveSkill="1">
        <Gem nameSpec="Ice Strike" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Heft" level="20" quality="0" enabled="true"/>
        <Gem nameSpec="Elemental Focus" level="20" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1">
      <Input name="customMods" string="30% more Attack Damage"/>
    </ConfigSet>
  </Config>
</PathOfBuilding2>
"""


def test_offline_slot_alias_guardrail_and_five_dim_xml_splice() -> None:
    assert normalize_slot_arg("ring1") == "Ring 1"
    assert normalize_slot_arg("flask1") == "Flask 1"
    assert normalize_slot_arg("charm1") == "Charm 1"
    with pytest.raises(CrossGameHallucinationError):
        normalize_slot_arg("Flask 5")

    for dim in ("items", "tree", "main_skill", "supports", "config"):
        spliced = _splice_dimension_xml(_STARTER_MONK_XML, _UPGRADED_REF_MONK_XML, dim)
        assert "<PathOfBuilding2>" in spliced
        if dim == "supports":
            assert "Heft" in spliced and 'level="12"' in spliced
        if dim == "main_skill":
            assert 'level="20"' in spliced and "Heft" not in spliced
        if dim == "config":
            assert "30% more Attack Damage" in spliced


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 uniques, compare, what-if, diff E2E")
def test_live_uniques_flask_charm_five_dim_compare_and_cow_what_if(tmp_path: Path) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 uniques/compare/what-if tests.")

    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        load_build_into_worker(worker, xml_code=_STARTER_MONK_XML, auto_fix=True)

        # 1. Keyword-free full-slot unique ranking: Ring 1, Flask 1, AND Charm 1 + Sanctified 0.78x..1.22x
        u_ring = rank_uniques(worker, slot="Ring 1", top=5, eval_sanctified=True, max_scan=60)
        assert u_ring["game"] == "poe2" and u_ring["scannedCount"] >= 5 and len(u_ring["results"]) >= 1
        assert "sanctifiedRollRange" in u_ring["results"][0]
        assert u_ring["results"][0]["sanctifiedRollRange"]["maxScalar"] == 1.22
        assert u_ring["results"][0]["sanctifiedRollRange"]["minScalar"] == 0.78
        assert "Unique Ranking" in format_uniques_md(u_ring)

        u_flask = rank_uniques(worker, slot="Flask 1", top=5, eval_sanctified=True, max_scan=40)
        assert u_flask["scannedCount"] >= 1 and len(u_flask["results"]) >= 1

        u_charm = rank_uniques(worker, slot="Charm 1", top=5, eval_sanctified=True, max_scan=40)
        assert u_charm["scannedCount"] >= 1 and len(u_charm["results"]) >= 1

        # 2. 5-Dimension Compare Power Report (Items / Tree / Main Skill / Supports / Config)
        before_stats = worker.call("get_stats", {})["stats"]
        cmp_res = compare_builds(worker, ref_xml_code=_UPGRADED_REF_MONK_XML)
        assert cmp_res["game"] == "poe2"
        assert cmp_res["gap"]["deltaDps"] > 0
        dim_map = {d["dimension"]: d for d in cmp_res["dimensions"]}
        assert set(dim_map) == {"items", "tree", "main_skill", "supports", "config"}
        assert dim_map["items"]["deltaDps"] > 0
        assert dim_map["main_skill"]["deltaDps"] > 0
        assert dim_map["supports"]["deltaDps"] > 0
        assert dim_map["config"]["deltaDps"] > 0
        assert "5-Dimension Compare Report" in format_compare_md(cmp_res)
        # Compare MUST NOT mutate the live build!
        assert stat_drift(before_stats, worker.call("get_stats", {})["stats"]) == []

        # 3. CoW what-if (rollback verified 100% idempotent, zero fallback)
        wi_res = run_what_if(
            worker,
            {"level": 95, "custom_mods": "+250 to maximum Life\n50% increased Attack Speed"},
            apply=False,
        )
        assert wi_res["applied"] is False
        assert "restoreFallback" not in wi_res
        assert float(wi_res["after"]["life"]) > float(wi_res["before"]["life"])
        assert float(wi_res["after"]["primaryDps"]) > float(wi_res["before"]["primaryDps"])
        assert stat_drift(before_stats, worker.call("get_stats", {})["stats"]) == []
        assert "What-If" in format_what_if_md(wi_res)

        # 4. diff_against_source without mutating live build
        df_res = diff_against_source(worker, xml_code=_UPGRADED_REF_MONK_XML)
        assert float(df_res["after"]["primaryDps"]) > float(df_res["before"]["primaryDps"])
        assert stat_drift(before_stats, worker.call("get_stats", {})["stats"]) == []

        # 5. Apply + undo + redo cycle
        ap_res = run_what_if(worker, {"level": 92}, apply=True)
        assert ap_res["applied"] is True and ap_res["undoDepth"] == 1
        assert worker.call("get_build_info", {})["level"] == 92
        undo_build(worker)
        assert worker.call("get_build_info", {})["level"] == 80
        redo_build(worker)
        assert worker.call("get_build_info", {})["level"] == 92
    finally:
        worker.stop()
