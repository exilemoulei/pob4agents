# SPDX-License-Identifier: GPL-3.0-only
"""Offline and live PoB2 engine tests for T3.6: 5-stage fix-gaps (cap+overcap, never equalize) & Spirit knapsack."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob2_cli import bridge_protocol, cli, daemon, updater
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.cli_rebalance import cmd_fix_gaps, cmd_spirit
from pob2_cli.rebalance import (
    POE2_CATALYSTS,
    AttributeNodeInfo,
    NetGapAudit,
    SpiritOption,
    StatSnapshot,
    apply_catalyst_to_item_raw,
    audit_equipped_slot,
    build_offline_pack_catalog_options,
    estimate_catalyst_gains,
    solve_attribute_respec,
    solve_catalyst_upgrades,
    solve_deficit_knapsack,
    solve_fix_gaps_live,
    solve_fix_gaps_offline,
    solve_forward_spirit_knapsack,
    solve_rune_allocation,
)

# Live PoE2 0.5.5 Mercenary build intentionally constructed with:
#   1. Red-text Str deficit (all 8 real +5 Attribute nodes set to Int -> Int surplus, Str deficit vs Braced Tower Shield)
#   2. Asymmetric resistances: Fire ~105% (+30 healthy overcap), Lightning ~80% (+5), Cold ~45% (-30 hard deficit)
#   3. Editable Body Armour + Helmet sockets (2+2) + uncorrupted Ring for Stage 2/3
#   4. Spirit over-reserved: Wind Dancer (30) + Clarity II (20) + Precision II (20) + Herald of Ice (30) + Cannibalism II (30) = 130 > 100
_LIVE_GAP_BUILD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
	<Build level="90" targetVersion="0_1" mainSocketGroup="1" className="Mercenary" ascendClassName="Witchhunter" characterLevelAutoMode="false"/>
	<Tree activeSpec="1">
		<Spec treeVersion="0_5" classId="9" classInternalId="9" ascendClassId="2" secondaryAscendClassId="nil" ascendancyInternalId="Mercenary2" nodes="50986,59779,42350,61438,28510,10247,28370,63526,48635,21755" masteryEffects="">
			<Overrides>
				<AttributeOverride strNodes="" dexNodes="" intNodes="42350,61438,28510,10247,28370,63526,48635,21755"/>
			</Overrides>
		</Spec>
	</Tree>
	<Skills activeSkillSet="1" defaultGemLevel="normalMaximum" defaultGemQuality="0">
		<SkillSet id="1">
			<Skill enabled="true" mainActiveSkill="1" mainActiveSkillCalcs="1" includeInFullDPS="nil" label="">
				<Gem count="1" skillId="IceNovaPlayer" gemId="Metadata/Items/Gems/SkillGemIceNova" variantId="IceNova" nameSpec="Ice Nova" level="12" quality="0" enabled="true" enableGlobal1="true" enableGlobal2="true" corrupted="nil"/>
			</Skill>
			<Skill enabled="true" mainActiveSkill="nil" includeInFullDPS="nil" label="PersistentBuff">
				<Gem count="1" skillId="ChargeRegulationPlayer" gemId="Metadata/Items/Gem/SkillGemChargeInfusion" variantId="ChargeRegulation" nameSpec="Charge Regulation" level="14" quality="0" enabled="true" enableGlobal1="true" enableGlobal2="true" corrupted="nil"/>
				<Gem count="1" skillId="SupportClarityPlayerTwo" gemId="Metadata/Items/Gems/SkillGemClaritySupportTwo" variantId="ClaritySupportTwo" nameSpec="Clarity II" level="1" quality="0" enabled="true" enableGlobal1="true" enableGlobal2="true" corrupted="nil"/>
				<Gem count="1" skillId="SupportPrecisionPlayerTwo" gemId="Metadata/Items/Gems/SkillGemPrecisionSupportTwo" variantId="PrecisionSupportTwo" nameSpec="Precision II" level="1" quality="0" enabled="true" enableGlobal1="true" enableGlobal2="true" corrupted="nil"/>
				<Gem count="1" skillId="SupportCannibalismPlayerTwo" gemId="Metadata/Items/Gems/SkillGemCannibalismSupportTwo" variantId="CannibalismSupportTwo" nameSpec="Cannibalism II" level="1" quality="0" enabled="true" enableGlobal1="true" enableGlobal2="true" corrupted="nil"/>
			</Skill>
			<Skill enabled="true" mainActiveSkill="nil" includeInFullDPS="nil" label="WindBuff">
				<Gem count="1" skillId="WindDancerPlayer" gemId="Metadata/Items/Gems/SkillGemWindDancer" variantId="WindDancer" nameSpec="Wind Dancer" level="10" quality="0" enabled="true" enableGlobal1="true" enableGlobal2="true" corrupted="nil"/>
			</Skill>
		</SkillSet>
	</Skills>
	<Config activeConfigSet="1">
		<ConfigSet id="1" title="Default">
			<Input name="resistancePenalty" number="-60"/>
		</ConfigSet>
	</Config>
	<Items activeItemSet="1" useSecondWeaponSet="false" showStatDifferences="true">
		<Item id="1">
Rarity: RARE
Test Wand
Attuned Wand
Item Level: 75
Implicits: 0
45% increased Spell Damage
		</Item>
		<Item id="2">
Rarity: RARE
Heavy Str Shield
Braced Tower Shield
Item Level: 75
Str: 28
Implicits: 0
+80 to maximum Life
		</Item>
		<Item id="3">
Rarity: RARE
Asymmetric Coat
Swiftstalker Coat
Item Level: 80
Sockets: S S
Implicits: 0
+165% to Fire Resistance
+140% to Lightning Resistance
+65% to Cold Resistance
		</Item>
		<Item id="4">
Rarity: RARE
Open Socket Cap
Runeforged Woven Cap
Item Level: 80
Sockets: S S
Implicits: 0
+80 to maximum Life
		</Item>
		<Item id="5">
Rarity: RARE
Uncorrupted Cold Ring
Breach Ring
Item Level: 80
Implicits: 0
+40% to Cold Resistance
+150 to Dexterity
+100 to Intelligence
+40 to maximum Life
		</Item>
		<ItemSet id="1" useSecondWeaponSet="false" title="Default">
			<Slot name="Weapon 1" itemId="1"/>
			<Slot name="Weapon 2" itemId="2"/>
			<Slot name="Body Armour" itemId="3"/>
			<Slot name="Helmet" itemId="4"/>
			<Slot name="Ring 1" itemId="5"/>
		</ItemSet>
	</Items>
</PathOfBuilding2>
"""


# ---------------------------------------------------------------------------
# 1. Owner's item 101 Iron Law: Cap + Overcap Buffer, NEVER equalize Fire/Cold/Light
# ---------------------------------------------------------------------------


def test_resistance_cap_plus_overcap_never_equalizes_healthy_fire_overflow() -> None:
    snap = StatSnapshot(
        level=90, ascendancy="Witchhunter",
        str_val=100, req_str=115,  # -15 Str hard deficit
        dex_val=120, req_dex=100,  # +20 Dex surplus
        int_val=130, req_int=100,  # +30 Int surplus
        fire_total=108, fire_max=75,  # +33 healthy overcap -> MUST stay >= 108!
        cold_total=53, cold_max=75,   # -22 hard deficit!
        lightning_total=80, lightning_max=75,  # +5 overcap
        chaos_total=10, chaos_max=75,
        spirit_total=100, spirit_reserved=90, spirit_unreserved=10,
    )
    body = audit_equipped_slot(
        slot_name="Body Armour", item_id=1, name="Test Coat", base_name="Swiftstalker Coat",
        rarity="RARE", item_type="Body Armour",
        raw_text="Rarity: RARE\nTest Coat\nSwiftstalker Coat\nSockets: S S\nImplicits: 0\n+90 to maximum Life\n",
    )
    helm = audit_equipped_slot(
        slot_name="Helmet", item_id=2, name="Test Helm", base_name="Runeforged Woven Cap",
        rarity="RARE", item_type="Helmet",
        raw_text="Rarity: RARE\nTest Helm\nRuneforged Woven Cap\nSockets: S S\nImplicits: 0\n+60 to maximum Life\n",
    )
    ring = audit_equipped_slot(
        slot_name="Ring 1", item_id=3, name="Cold Ring", base_name="Breach Ring",
        rarity="RARE", item_type="Ring",
        raw_text="Rarity: RARE\nCold Ring\nBreach Ring\nImplicits: 0\n+40% to Cold Resistance\n",
    )
    attr_nodes = [
        AttributeNodeInfo(101, "int", alloc_mode=0),
        AttributeNodeInfo(102, "int", alloc_mode=0),
        AttributeNodeInfo(103, "int", alloc_mode=0),
        AttributeNodeInfo(104, "dex", alloc_mode=1),  # WS1-only -> MUST skip!
        AttributeNodeInfo(105, "int", alloc_mode=0, in_radius_jewel=True),  # Radius-sensitive -> MUST skip!
    ]
    audit = NetGapAudit(current=snap, bare_net=snap, slots=(body, helm, ring))
    report = solve_fix_gaps_offline(audit, attr_nodes, overcap_target=20, chaos_target=0)

    assert report.all_hard_floors_met is True
    # Stage 1 solved -15 Str using 3 allocMode==0 Int nodes, skipping WS-only & radius-jewel nodes
    assert len(report.stage1.swaps) == 3
    assert report.stage1.gold_cost > 0
    assert report.stage1.skipped_ws_only_count == 1
    assert report.stage1.skipped_radius_sensitive_count == 1
    assert report.after_verified.effective_attr_deficits() == {"str": 0, "dex": 0, "int": 0}

    # Resistances: Cold >= 75, Lightning >= 75, Fire preserved at >= 108 (NEVER equalized down!)
    assert report.after_verified.cold_total >= 75
    assert report.after_verified.lightning_total >= 75
    assert report.after_verified.fire_total >= 108
    # Overcap buffer improved Cold/Lightning using leftover armour sockets
    assert report.after_verified.cold_total >= 85
    assert report.stage5.needed is False


# ---------------------------------------------------------------------------
# 2. Step 0 Protections (Mirrored, 13 isSocketBound, Crafted slot hint, Darkness Enthroned)
# ---------------------------------------------------------------------------


def test_step0_protects_mirrored_socketbound_and_honors_darkness_enthroned_multiplier() -> None:
    mirrored = audit_equipped_slot(
        slot_name="Boots", item_id=1, name="Mirror Boots", base_name="Drakeskin Boots",
        rarity="RARE", item_type="Boots",
        raw_text="Rarity: RARE\nMirror Boots\nDrakeskin Boots\nSockets: S\nRune: Glacial Rune\nImplicits: 1\n{enchant}{rune}+12% to Cold Resistance\nMirrored\n",
    )
    assert mirrored.editable_socket_count == 0
    assert mirrored.stripped_runes == ()

    bound_wand = audit_equipped_slot(
        slot_name="Weapon 1", item_id=2, name="Bound Wand", base_name="Attuned Wand",
        rarity="RARE", item_type="Wand",
        raw_text="Rarity: RARE\nBound Wand\nAttuned Wand\nSockets: S S\nRune: Passion of Aldur\nRune: Lesser Desert Rune\nImplicits: 1\n{enchant}{rune}Gain 6% of Damage as Extra Fire Damage\n",
    )
    assert bound_wand.preserved_bound_runes == ("Passion of Aldur",)
    assert bound_wand.stripped_runes == ("Lesser Desert Rune",)
    assert bound_wand.editable_socket_count == 1

    # Darkness Enthroned belt acting as Gloves with +100% increased effect of Socketed Items (2.0x!)
    belt = audit_equipped_slot(
        slot_name="Belt", item_id=3, name="Darkness Enthroned", base_name="Fine Belt",
        rarity="UNIQUE", item_type="Belt",
        raw_text=(
            "Rarity: UNIQUE\nDarkness Enthroned\nFine Belt\nSockets: S S\n"
            "This item gains bonuses from Socketed items as though it was a Gloves\n"
            "100% increased effect of Socketed Items\nImplicits: 0\n"
        ),
    )
    assert belt.parity.override_type == "gloves"
    assert belt.parity.effect_multiplier("Rune") == pytest.approx(2.0)

    plan = solve_rune_allocation((belt,), {"cold": 30}, {"cold": 0}, char_level=90)
    # Under 2.0x multiplier, 2x Lesser Glacial Rune (+20% each = +40% @ 1.0 Ex total) beats 1x Greater (+32% @ 5.0 Ex)!
    assert plan.remaining_hard["cold"] == 0
    assert plan.total_cost_ex <= 1.0
    assert all(a.effect_multiplier == pytest.approx(2.0) for a in plan.assignments)
    # With only 1 socket, a single 2.0x socketed rune solves a 30% Cold gap in 1 socket!
    belt_1s = audit_equipped_slot(
        slot_name="Belt", item_id=4, name="Darkness Enthroned 1S", base_name="Fine Belt",
        rarity="UNIQUE", item_type="Belt",
        raw_text=(
            "Rarity: UNIQUE\nDarkness Enthroned 1S\nFine Belt\nSockets: S\n"
            "This item gains bonuses from Socketed items as though it was a Gloves\n"
            "100% increased effect of Socketed Items\nImplicits: 0\n"
        ),
    )
    plan_1s = solve_rune_allocation((belt_1s,), {"cold": 30}, {"cold": 0}, char_level=90)
    assert len(plan_1s.assignments) == 1
    assert plan_1s.assignments[0].gains["cold"] >= 30
    assert plan_1s.remaining_hard["cold"] == 0


# ---------------------------------------------------------------------------
# 3. Stage 3: Exactly 13 PoE2 Catalysts (no Prismatic) & skips {unscalable}
# ---------------------------------------------------------------------------


def test_stage3_13_poe2_catalysts_skip_unscalable_and_corrupted() -> None:
    assert len(POE2_CATALYSTS) == 13
    assert all("Prismatic" not in c.name for c in POE2_CATALYSTS)

    raw_ring = (
        "Rarity: RARE\nTest Ring\nBreach Ring\nImplicits: 0\n"
        "+40% to Fire Resistance\n"
        "{unscalable}+30% to Fire Resistance\n"
    )
    gains, skipped = estimate_catalyst_gains(raw_ring, "Xoph's Catalyst", quality_pct=20)
    assert gains["fire"] == 8  # Only scalable +40% * 20% = +8%; {unscalable} +30% skipped!
    assert skipped == 1

    rendered = apply_catalyst_to_item_raw(raw_ring, "Xoph's Catalyst", quality_pct=20)
    assert "Catalyst: Fire" in rendered and "CatalystQuality: 20" in rendered

    corrupted_ring = audit_equipped_slot(
        slot_name="Ring 2", item_id=4, name="Corrupt Ring", base_name="Breach Ring",
        rarity="RARE", item_type="Ring",
        raw_text=raw_ring + "Corrupted\n",
    )
    assert corrupted_ring.catalyst_eligible is False
    c_plan = solve_catalyst_upgrades((corrupted_ring,), {"fire": 8}, {"fire": 0})
    assert c_plan.actions == ()


# ---------------------------------------------------------------------------
# 4. Stage 4 & `pob2 spirit`: Multi-choice deficit rescue & forward packing
# ---------------------------------------------------------------------------


def test_stage4_spirit_knapsack_deficit_rescue_and_forward_packing() -> None:
    # Deficit = 25 Spirit: downgrading Clarity II->I (-10, loss=0.5) + Cannibalism II->I (-15, loss=0.8) = 25 Spirit @ loss 1.3
    # MUST beat disabling main DPS aura (-30, loss=12.0) and respect choice_group exclusivity!
    opts = [
        SpiritOption("c_down", "g_clarity", "support_downgrade", "Clarity II -> I", 10, -50, 0, loss_score=0.5),
        SpiritOption("c_dis", "g_clarity", "support_disable", "Disable Clarity II", 20, -200, 0, loss_score=3.0),
        SpiritOption("k_down", "g_cannib", "support_downgrade", "Cannibalism II -> I", 15, 0, -300, loss_score=0.8),
        SpiritOption("aura_off", "g_herald", "aura_toggle", "Disable Herald of Ice", 30, -4000, 0, loss_score=12.0),
    ]
    chosen, freed = solve_deficit_knapsack(opts, deficit=25)
    assert freed == 25
    assert {o.option_id for o in chosen} == {"c_down", "k_down"}

    # Forward packing: --budget 220 via both API and CLI
    pack_opts = build_offline_pack_catalog_options(metric="CombinedDPS")
    picked, used = solve_forward_spirit_knapsack(pack_opts, budget=220)
    assert 0 < used <= 220
    assert len({o.choice_group for o in picked}) == len(picked)  # Zero duplicate tiers from same support family!

    runner = CliRunner()
    res = runner.invoke(cmd_spirit, ["--budget", "220", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["game"] == "poe2" and data["mode"] == "pack" and 0 < data["achievedSpirit"] <= 220


# ---------------------------------------------------------------------------
# 5. Stage 5 fallback & Stage 1 WS1/WS2 dual-set protection
# ---------------------------------------------------------------------------


def test_stage1_respects_ws2_requirements_and_stage5_emits_only_when_hard_unmet() -> None:
    # WS1 only needs 80 Dex, but WS2 needs 110 Dex -> Dex surplus is only 10 (120 - 110), NOT 40!
    snap = StatSnapshot(
        level=90, ascendancy="Witchhunter",
        str_val=80, req_str=110,  # -30 Str deficit
        dex_val=120, req_dex=80,
        int_val=80, req_int=80,
        fire_total=75, fire_max=75, cold_total=75, cold_max=75,
        lightning_total=75, lightning_max=75, chaos_total=0, chaos_max=75,
        spirit_total=100, spirit_reserved=100, spirit_unreserved=0,
        ws2_str_val=80, ws2_req_str=110,
        ws2_dex_val=120, ws2_req_dex=110,  # Leaves only +10 safe Dex surplus!
        ws2_int_val=80, ws2_req_int=80,
    )
    nodes = [AttributeNodeInfo(i, "dex", alloc_mode=0) for i in range(1, 7)]
    s1 = solve_attribute_respec(snap, nodes)
    # Must swap at most 2 nodes (+10 Str) so WS2 Dex NEVER drops below 110!
    assert len(s1.swaps) == 2
    assert s1.remaining_deficits["str"] == 20

    gloves = audit_equipped_slot(
        slot_name="Gloves", item_id=1, name="Weak Gloves", base_name="Suede Bracers",
        rarity="RARE", item_type="Gloves",
        raw_text="Rarity: RARE\nWeak Gloves\nSuede Bracers\nImplicits: 0\n+20 to maximum Life\n",
    )
    rep = solve_fix_gaps_offline(NetGapAudit(current=snap, bare_net=snap, slots=(gloves,)), nodes)
    assert rep.all_hard_floors_met is False
    assert rep.stage5.needed is True
    assert rep.stage5.target_slot == "Gloves"
    assert rep.stage5.required_stats.get("str", 0) == 20


# ---------------------------------------------------------------------------
# 6. Live PoB2 BridgeWorker E2E closed-loop verification (POB2_ENGINE_DIR)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native lua51.dll live PoB2 fix-gaps E2E")
def test_live_pob2_fix_gaps_and_spirit_e2e_cow_verified(tmp_path: Path) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 5-stage fix-gaps & Spirit E2E test.")

    worker = BridgeWorker(slot_name="fix_gaps_e2e", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        code = bridge_protocol.encode_xml_share_code(_LIVE_GAP_BUILD_XML)
        worker.call("import_code", {"code": code, "mode": "replace"})
        before_raw_stats = worker.call("get_stats", {})["stats"]

        report = solve_fix_gaps_live(worker, overcap_target=20, chaos_target=0, apply=False)

        # Confirm starting build genuinely had all 3 deficits + healthy Fire overcap
        assert report.before.effective_attr_deficits()["str"] > 0
        assert report.before.resistance_hard_deficits()["cold"] > 0
        assert report.before.fire_total > report.before.fire_max + 15
        assert report.before.spirit_deficit() > 0

        # Confirm live CoW-verified solved state satisfies ALL hard floors without equalizing Fire
        after = report.after_verified
        assert report.all_hard_floors_met is True
        assert after.effective_attr_deficits() == {"str": 0, "dex": 0, "int": 0}
        assert after.fire_total >= after.fire_max
        assert after.cold_total >= after.cold_max
        assert after.lightning_total >= after.lightning_max
        # Fire's healthy overcap preserved untouched (no equalization penalty!)
        assert after.fire_total >= report.before.fire_total
        # Spirit deficit fully resolved (SpiritUnreserved >= 0)
        assert after.spirit_unreserved >= 0.0
        assert after.spirit_deficit() == 0
        assert len(report.stage1.swaps) >= 1
        assert len(report.stage2.assignments) >= 1
        assert len(report.stage4.selected) >= 1
        assert report.stage5.needed is False

        # Confirm CoW rolled back original build with 0 pollution!
        assert report.cow_rollback_verified is True
        after_rollback_stats = worker.call("get_stats", {})["stats"]
        assert after_rollback_stats == before_raw_stats
    finally:
        worker.stop()

    # Also smoke-test `pob2 fix-gaps --build ... --json` CLI command end-to-end
    xml_file = tmp_path / "gap_build.xml"
    xml_file.write_text(_LIVE_GAP_BUILD_XML, encoding="utf-8")
    runner = CliRunner()
    cli_res = runner.invoke(cmd_fix_gaps, ["--build", str(xml_file), "--overcap", "20", "--json"])
    assert cli_res.exit_code == 0, cli_res.output
    cli_data = json.loads(cli_res.output)
    assert cli_data["game"] == "poe2"
    assert cli_data["evalMode"] == "live_cow"
    assert cli_data["applied"] is False
    assert cli_data["allHardFloorsMet"] is True
    assert cli_data["cowRollbackVerified"] is True


def test_cli_rebalance_flag_guards_and_poe1_url_rejection() -> None:
    runner = CliRunner()
    assert runner.invoke(cmd_fix_gaps, ["--build", "x.xml", "--apply"]).exit_code != 0
    assert runner.invoke(cmd_spirit, ["--budget", "100", "--apply"]).exit_code != 0
    assert runner.invoke(cmd_spirit, ["--build", "x.xml", "--apply"]).exit_code != 0
    # PoE1 share URL rejected cleanly via universal build resolver + guardrail (exit 1)
    poe1_res = runner.invoke(cli.main, ["fix-gaps", "--build", "https://maxroll.gg/poe/pob/abc123"])
    assert poe1_res.exit_code != 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native lua51.dll live daemon CLI chain E2E")
def test_live_daemon_cli_chain_load_rune_instill_fix_gaps_spirit_and_undo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live daemon hot-slot CLI chain E2E test.")

    # Extend _LIVE_GAP_BUILD_XML with Sockets: S on Weapon 1 and an equipped Gold Amulet (neutral to Str/Res/Spirit deficits)
    chain_xml = (
        _LIVE_GAP_BUILD_XML.replace(
            "Attuned Wand\nItem Level: 75\nImplicits: 0",
            "Attuned Wand\nItem Level: 75\nSockets: S\nImplicits: 0",
            1,
        )
        .replace(
            '\t\t<ItemSet id="1"',
            '\t\t<Item id="6">\nRarity: RARE\nChain Amulet\nGold Amulet\nItem Level: 80\nImplicits: 0\n+40 to maximum Life\n\t\t</Item>\n\t\t<ItemSet id="1"',
            1,
        )
        .replace(
            '\t\t\t<Slot name="Ring 1" itemId="5"/>',
            '\t\t\t<Slot name="Ring 1" itemId="5"/>\n\t\t\t<Slot name="Amulet" itemId="6"/>',
            1,
        )
    )
    xml_file = tmp_path / "daemon_chain_build.xml"
    xml_file.write_text(chain_xml, encoding="utf-8")

    rt_dir = tmp_path / "rt"
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(rt_dir))
    monkeypatch.setenv("POB2_OFFLINE", "1")

    srv_err: list[BaseException] = []

    def _run_srv() -> None:
        try:
            daemon._serve_daemon(
                pob_dir=eng,
                port=0,
                data_dir=data_dir,
                runtime_dir=rt_dir,
                offline=True,
                skip_update_check=True,
                disable_global_idle=True,
            )
        except BaseException as exc:
            srv_err.append(exc)

    srv_thread = threading.Thread(target=_run_srv, name="pob2-test-daemon", daemon=True)
    srv_thread.start()

    deadline = time.time() + 45.0
    while time.time() < deadline:
        if srv_err:
            raise srv_err[0]
        st = daemon.read_daemon_state(rt_dir)
        if st is not None and daemon._probe_daemon(st):
            break
        time.sleep(0.15)
    else:
        raise AssertionError("Timed out waiting for local test PoB2 daemon to start.")

    runner = CliRunner()
    try:
        # 1. `pob2 load`
        res_load = runner.invoke(cli.main, ["load", str(xml_file), "--json"])
        assert res_load.exit_code == 0, res_load.output

        # 2. `pob2 rune rank --slot "Weapon 1" --json` (NO --type! Must use hot daemon slot & finish <3s)
        t0 = time.perf_counter()
        res_rune = runner.invoke(cli.main, ["rune", "rank", "--slot", "Weapon 1", "--json"])
        rune_sec = time.perf_counter() - t0
        assert res_rune.exit_code == 0, res_rune.output
        d_rune = json.loads(res_rune.output)
        assert d_rune["evalMode"] == "live_cow"
        assert 5 <= d_rune["evaluatedCount"] <= 28
        assert d_rune["shortlistedFrom"] >= d_rune["evaluatedCount"]
        assert d_rune["bisAnchor"] is not None and d_rune["bisAnchor"]["gain"] > 0.0
        assert rune_sec < 5.0, f"`pob2 rune rank --slot 'Weapon 1'` took {rune_sec:.2f}s"

        # 3. `pob2 instill rank --json` (live node_power + CoW on equipped Amulet)
        res_inst = runner.invoke(cli.main, ["instill", "rank", "--top-live", "10", "--json"])
        assert res_inst.exit_code == 0, res_inst.output
        d_inst = json.loads(res_inst.output)
        assert d_inst["evalMode"] == "live_cow"
        assert d_inst["evaluatedCount"] == 10
        assert d_inst["bisAnchor"] is not None and d_inst["bisAnchor"]["gain"] > 0.0
        assert "[Slot 1:" in d_inst["bisAnchor"]["orderedDisplayEn"]

        # 4. `pob2 fix-gaps --json` (NO --build! Dry-run with CoW rollback verified)
        res_fg = runner.invoke(cli.main, ["fix-gaps", "--json"])
        assert res_fg.exit_code == 0, res_fg.output
        d_fg = json.loads(res_fg.output)
        assert d_fg["evalMode"] == "live_cow"
        assert d_fg["applied"] is False
        assert d_fg["allHardFloorsMet"] is True
        assert d_fg["cowRollbackVerified"] is True

        # 5. `pob2 spirit --json` (NO --build / --budget! Live hot-slot deficit rescue)
        res_sp = runner.invoke(cli.main, ["spirit", "--json"])
        assert res_sp.exit_code == 0, res_sp.output
        d_sp = json.loads(res_sp.output)
        assert d_sp["evalMode"] == "live_cow"
        assert d_sp["applied"] is False
        assert d_sp["targetSpirit"] > 0 and d_sp["achievedSpirit"] >= d_sp["targetSpirit"]

        # 6. `pob2 fix-gaps --apply --json` -> commits to hot slot with undo history!
        res_apply = runner.invoke(cli.main, ["fix-gaps", "--apply", "--json"])
        assert res_apply.exit_code == 0, res_apply.output
        d_apply = json.loads(res_apply.output)
        assert d_apply["applied"] is True
        assert d_apply["allHardFloorsMet"] is True
        assert d_apply["undoDepth"] >= 1

        # 7. Confirm hot slot actually changed (`pob2 spirit` now sees 0 Spirit deficit!)
        res_sp_after = runner.invoke(cli.main, ["spirit", "--json"])
        assert res_sp_after.exit_code == 0, res_sp_after.output
        d_sp_after = json.loads(res_sp_after.output)
        assert d_sp_after["targetSpirit"] == 0 and d_sp_after["remainingDeficit"] == 0

        # 8. `pob2 undo --json` -> restores original build (Spirit deficit returns!)
        res_undo = runner.invoke(cli.main, ["undo", "--json"])
        assert res_undo.exit_code == 0, res_undo.output
        res_sp_restored = runner.invoke(cli.main, ["spirit", "--json"])
        assert res_sp_restored.exit_code == 0, res_sp_restored.output
        assert json.loads(res_sp_restored.output)["targetSpirit"] == d_sp["targetSpirit"]

        # 9. `pob2 spirit --apply --json` -> commits Spirit rescue & `pob2 undo` reverts it!
        res_sp_apply = runner.invoke(cli.main, ["spirit", "--apply", "--json"])
        assert res_sp_apply.exit_code == 0, res_sp_apply.output
        d_sp_apply = json.loads(res_sp_apply.output)
        assert d_sp_apply["applied"] is True and d_sp_apply["afterSpiritUnreserved"] >= 0.0
        assert d_sp_apply["undoDepth"] >= 1
        assert runner.invoke(cli.main, ["undo", "--json"]).exit_code == 0
    finally:
        runner.invoke(cli.main, ["stop"])
        daemon.stop_daemon(rt_dir)
        srv_thread.join(timeout=15.0)
