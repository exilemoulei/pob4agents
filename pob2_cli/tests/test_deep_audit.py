# SPDX-License-Identifier: GPL-3.0-only
"""Tests for PoE2 S4/T3.4 deep audit, version stamps, item-101 resistance policy, and 3+ live PoE2 builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pob2_cli import updater
from pob2_cli.analyzer import (
    audit_build,
    build_version_stamp,
    diagnose_build,
    evaluate_resistances_cap_overcap,
)
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.formatters import format_audit_md, format_summary_md
from pob2_cli.importers.share_urls import load_build_into_worker

# Build 1: Monk Invoker (Tempest Bell + Cast on Critical + 100% Phys->Lightning + dead % Phys mod + Deflection + unequal healthy overcap resists)
_BUILD_1_MONK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Tempest Rod
Gothic Quarterstaff
Item Level: 82
Adds 100 to 180 Physical Damage
15% increased Attack Speed
    </Item>
    <Item id="2">
Rarity: NORMAL
Thawing Charm
Item Level: 75
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Charm 1" itemId="2" active="true"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" label="MainStrike" mainActiveSkill="1">
        <Gem nameSpec="Ice Strike" level="19" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="Bell" mainActiveSkill="1">
        <Gem nameSpec="Tempest Bell" level="19" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="MetaTrig" mainActiveSkill="1">
        <Gem nameSpec="Cast on Critical" level="18" quality="0" enabled="true"/>
        <Gem nameSpec="Spark" level="18" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1">
      <Input name="questFreythorn" boolean="true"/>
      <Input name="conditionUsingCharm" boolean="true"/>
      <Input name="customMods" string="100% of Physical Damage Converted to Lightning Damage
45% increased Physical Damage
+2000 to Evasion Rating
Gain Deflection Rating equal to 100% of Evasion Rating
+165% to Fire Resistance
+152% to Cold Resistance
+156% to Lightning Resistance
+30% to Chaos Resistance
+100 to Spirit"/>
    </ConfigSet>
  </Config>
</PathOfBuilding2>
"""

# Build 2: Mercenary Crossbow + High Armour + Elemental Armour + Infernal Cry (#714) + Shield Block
_BUILD_2_MERC_XBOW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="88" targetVersion="0_1" className="Mercenary" ascendClassName="Witchhunter" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="9" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Siege Bolt
Dyad Crossbow
Item Level: 80
Adds 90 to 160 Physical Damage
18% increased Attack Speed
    </Item>
    <Item id="2">
Rarity: RARE
Iron Wall
Steel Plate
Item Level: 80
+1200 to Armour
+100 to maximum Life
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Body Armour" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" label="CrossbowMain" mainActiveSkill="1">
        <Gem nameSpec="Galvanic Shards" level="19" quality="0" enabled="true"/>
        <Gem nameSpec="Heft" level="18" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="Warcry" mainActiveSkill="1">
        <Gem nameSpec="Infernal Cry" level="18" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1">
      <Input name="customMods" string="+12000 to Armour
100% of Armour also applies to Elemental Damage
+60% Chance to Block"/>
    </ConfigSet>
  </Config>
</PathOfBuilding2>
"""

# Build 3: Witch CI 1-Life ES + Ember Fusillade (#1850) + Weapon Swap
_BUILD_3_WITCH_CI_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="92" targetVersion="0_1" className="Witch" ascendClassName="Infernalist" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="1" ascendClassId="1" nodes="54447"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Arcane Wand
Galvanic Wand
Item Level: 82
60% increased Spell Damage
    </Item>
    <Item id="2">
Rarity: RARE
Silk Shroud
Altar Robe
Item Level: 82
+600 to maximum Energy Shield
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Body Armour" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" label="SpellMain" mainActiveSkill="1">
        <Gem nameSpec="Spark" level="20" quality="0" enabled="true"/>
        <Gem nameSpec="Elemental Focus" level="19" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="Volley" mainActiveSkill="1">
        <Gem nameSpec="Ember Fusillade" level="19" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1">
      <Input name="customMods" string="Maximum Life becomes 1, Immune to Chaos Damage
+3000 to maximum Energy Shield
+160% to Fire Resistance
+160% to Cold Resistance
+160% to Lightning Resistance"/>
    </ConfigSet>
  </Config>
</PathOfBuilding2>
"""


def test_version_stamp_flags_major_patch_mismatch() -> None:
    ok_stamp = build_version_stamp(pob2_version="0.23.1", tree_version="0_5")
    assert ok_stamp["gamePatch"] == "0.5.5"
    assert ok_stamp["patchMismatch"] is False
    assert ok_stamp["reviewStatus"] == "ok"

    stale_stamp = build_version_stamp(pob2_version="0.21.0", tree_version="0_4")
    assert stale_stamp["gamePatch"] == "0.4"
    assert stale_stamp["patchMismatch"] is True
    assert stale_stamp["reviewStatus"] == "[待复核]"
    assert stale_stamp["reviewFlag"] == "[待复核]"


def test_item_101_resistance_policy_never_penalizes_unequal_healthy_overcap() -> None:
    # Unequal healthy overcap (105% / 92% / 98%) MUST have 0 issues!
    healthy = evaluate_resistances_cap_overcap(
        {
            "Fire": {"final": 75, "total": 105, "cap": 75},
            "Cold": {"final": 75, "total": 92, "cap": 75},
            "Lightning": {"final": 75, "total": 98, "cap": 75},
            "Chaos": {"final": 20, "total": 20, "cap": 75},
        },
        overcap_target=15,
    )
    assert healthy["policy"] == "cap_plus_overcap_buffer_no_equalization"
    assert healthy["allElementalCapped"] is True
    assert healthy["issues"] == []

    # Uncapped Fire + low overcap Cold must be flagged accurately
    broken = evaluate_resistances_cap_overcap(
        {
            "Fire": {"final": 62, "total": 62, "cap": 75},
            "Cold": {"final": 75, "total": 78, "cap": 75},
            "Lightning": {"final": 75, "total": 95, "cap": 75},
            "Chaos": {"final": -10, "total": -10, "cap": 75},
        },
        overcap_target=15,
    )
    codes = {i["code"] for i in broken["issues"]}
    assert "resistance_uncapped" in codes
    assert "resistance_low_overcap_buffer" in codes
    assert "chaos_resistance_low" in codes


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 3-build S4/T3.4 deep audit E2E")
def test_live_deep_audit_across_three_distinct_poe2_builds(tmp_path: Path) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 3-build deep audit tests.")

    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()

        # --- Build 1: Monk Invoker (Conversion dead mods + Deflection + Tempest Bell + Cast on Critical + unequal healthy overcap) ---
        load_build_into_worker(worker, xml_code=_BUILD_1_MONK_XML, auto_fix=True)
        a1 = audit_build(worker, overcap_target=15)
        assert a1["game"] == "poe2" and a1["gamePatch"] == "0.5.5"
        # [PoB2未模拟] red cards for Tempest Bell (#2123) and Cast on Critical (#181/#2166)
        rc_issues = {rc["issue"] for rc in a1["unsimulatedRedCards"]}
        assert "#2123" in rc_issues and "#181/#2166" in rc_issues
        # Two-step terminal conversion 0% dead Physical mod caught!
        dead_mods = a1["conversion"]["deadMods"]
        assert any(dm["sourceDamageType"] == "Physical" and dm["wastedIncPct"] >= 45 for dm in dead_mods)
        # Tabulate attribution present
        assert isinstance(a1["tabulateAttribution"], list) and len(a1["tabulateAttribution"]) > 0
        # Deflection 95% illusion caught with raw MaxHit side-by-side
        assert a1["deflection"]["chance"] > 0 and a1["deflection"]["isIllusionRisk"] is True
        assert a1["deflection"]["rawPhysMaxHit"] > 0
        # 4-dim dehydration preserves quest* (+100 Spirit) & strips Charm bloat
        deh1 = a1["dehydration"]
        assert deh1["preservedQuestRewards"] is True and deh1["preservedResistancePenalty"] is True
        assert deh1["scenarios"]["PinnacleSustained"]["spirit"] >= 100
        # Item-101: unequal healthy overcap Fire/Cold/Lightning MUST NOT trigger any resistance issue!
        assert a1["resistances"]["allElementalCapped"] is True
        assert a1["resistances"]["issues"] == []
        # Markdown renders cleanly
        md1 = format_audit_md(a1)
        assert "[PoB2未模拟]" in md1 and "ArmourRatio=10" in md1

        # --- Build 2: Mercenary Crossbow (ArmourRatio=10 + Elemental Armour errata + Crossbow EffectiveReloadTime + 50% Block cap + Infernal Cry #714) ---
        load_build_into_worker(worker, xml_code=_BUILD_2_MERC_XBOW_XML, auto_fix=True)
        a2 = audit_build(worker)
        assert a2["armour"]["armourRatio"] == 10
        ladder = a2["armour"]["physicalLadder"]
        assert [r["hitDamage"] for r in ladder] == [1000, 5000, 10000, 20000]
        assert ladder[0]["mitigationPct"] > ladder[-1]["mitigationPct"]
        assert a2["armour"]["hasElementalArmour"] is True
        assert "CalcDefence.lua:2400" in a2["armour"]["tooltipErrata"]
        assert a2["block"]["maxCap"] == 50
        assert a2["crossbow"] is not None and a2["crossbow"]["effectiveReloadTime"] > 0
        assert any(rc["issue"] == "#714" for rc in a2["unsimulatedRedCards"])

        # --- Build 3: Witch CI 1-Life ES + Ember Fusillade (#1850) + WS1/WS2 swap audit + diagnose ---
        load_build_into_worker(worker, xml_code=_BUILD_3_WITCH_CI_XML, auto_fix=True)
        diag3 = diagnose_build(worker)
        a3 = diag3["audit"]
        assert a3["esChaosCi"]["isCI"] is True
        assert a3["esChaosCi"]["ciStunFreezeLockRisk"] is True
        assert a3["weaponSetSwapAudit"]["checked"] is True
        assert any(rc["issue"] == "#1850" for rc in a3["unsimulatedRedCards"])
        assert "PoB2 v" in format_summary_md(diag3["summary"])
    finally:
        worker.stop()
