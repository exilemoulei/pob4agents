# SPDX-License-Identifier: GPL-3.0-only
"""Offline and live PoB2 engine tests for T3.1: 287 runes, 875 ordered instillations, LiquidEmotions bridge, Pareto ranker."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob2_cli import bridge_protocol, cli_rune, updater
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.cli_rune import instill_group, rune_group
from pob2_cli.craft.price_book import PriceBook
from pob2_cli.craft.weights_loader import norm_key
from pob2_cli.rune import (
    apply_anoint_to_amulet_raw,
    apply_runes_to_item_raw,
    compute_pareto_frontier,
    eligible_runes_for_item,
    format_ordered_recipe,
    is_excluded_jewel_emotion,
    legacy_to_live_emotion,
    live_to_legacy_emotion,
    load_rune_instill_catalog,
    normalize_emotion_name,
    price_instill_recipe,
    rank_instillations,
    rank_runes_for_slot,
    resolve_item_augment_types,
    resolve_rune_effects_for_item,
    resolve_rune_price_ex,
    strip_replaceable_runes_from_item_raw,
)
from pob2_cli.rune.pareto_ranker import RankedCandidate

_LIVE_RUNE_INSTILL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
	<Build level="90" targetVersion="0_1" mainSocketGroup="1" className="Witch" ascendClassName="Stormweaver" characterLevelAutoMode="false"/>
	<Tree activeSpec="1">
		<Spec treeVersion="0_5" classId="1" classInternalId="1" ascendClassId="0" secondaryAscendClassId="nil" ascendancyInternalId="nil" nodes="54447" masteryEffects=""/>
	</Tree>
	<Skills activeSkillSet="1" defaultGemLevel="normalMaximum" defaultGemQuality="0">
		<SkillSet id="1">
			<Skill enabled="true" mainActiveSkill="1" mainActiveSkillCalcs="1" includeInFullDPS="nil" label="">
				<Gem count="1" skillId="IceNovaPlayer" gemId="Metadata/Items/Gems/SkillGemIceNova" variantId="IceNova" nameSpec="Ice Nova" level="20" quality="0" enabled="true" enableGlobal1="true" enableGlobal2="true" corrupted="nil"/>
			</Skill>
		</SkillSet>
	</Skills>
	<Config activeConfigSet="1"><ConfigSet id="1" title="Default"/></Config>
	<Items activeItemSet="1" useSecondWeaponSet="false" showStatDifferences="true">
		<Item id="1">
Rarity: RARE
Test Wand
Attuned Wand
Item Level: 82
Sockets: S
Implicits: 0
60% increased Spell Damage
		</Item>
		<Item id="2">
Rarity: RARE
Test Amulet
Solar Amulet
Item Level: 82
Implicits: 1
+15 to Spirit
+80 to maximum Life
		</Item>
		<ItemSet id="1" useSecondWeaponSet="false" title="Default">
			<Slot name="Weapon 1" itemId="1"/>
			<Slot name="Amulet" itemId="2"/>
		</ItemSet>
	</Items>
</PathOfBuilding2>
"""


# ---------------------------------------------------------------------------
# 1. Catalog counts: 287 runes (13 isSocketBound), 13 LiquidEmotions, 875 instills
# ---------------------------------------------------------------------------


def test_catalog_completeness_counts_and_excluded_ancient_jewel_emotions() -> None:
    cat = load_rune_instill_catalog()
    assert len(cat.runes) == 287
    assert len(cat.socket_bound_names) == 13
    assert "Passion of Aldur" in cat.socket_bound_names
    assert "Raven-Touched Shard" in cat.socket_bound_names
    assert "Greater Glacial Rune" not in cat.socket_bound_names

    assert len(cat.emotions) == 13
    assert len(cat.excluded_jewel_emotions) == 13
    assert all(e.startswith("Ancient ") for e in cat.excluded_jewel_emotions)
    assert all(not e.live_name.startswith("Ancient ") for e in cat.emotions)

    assert len(cat.instills) == 875
    assert len(cat.gold_respec_prices) == 100
    assert len(cat.spirit_supports) >= 25


# ---------------------------------------------------------------------------
# 2. LiquidEmotions legacy <-> live bridge & strict Ancient jewel exclusion
# ---------------------------------------------------------------------------


def test_liquid_emotions_bridge_and_ordered_recipe() -> None:
    assert legacy_to_live_emotion("Distilled Ire") == "Diluted Liquid Ire"
    assert legacy_to_live_emotion("Distilled Paranoia") == "Liquid Paranoia"
    assert legacy_to_live_emotion("Distilled Isolation") == "Concentrated Liquid Isolation"
    assert legacy_to_live_emotion("Distilled Contempt") == "Potent Liquid Contempt"
    assert live_to_legacy_emotion("Concentrated Liquid Suffering") == "Distilled Suffering"
    assert normalize_emotion_name("浓缩液态苦难").short == "Suffering"

    # All 13 radiusJewel=true Ancient emotions MUST be rejected!
    cat = load_rune_instill_catalog()
    for ancient in cat.excluded_jewel_emotions:
        assert is_excluded_jewel_emotion(ancient)
        with pytest.raises(ValueError, match="Time-Lost Jewel"):
            normalize_emotion_name(ancient)

    # Order preservation check: [Slot 1 -> Slot 2 -> Slot 3]
    inst = cat.lookup_instill("Fast Acting Toxins")
    assert inst is not None
    assert inst.recipe == ("Paranoia", "Greed", "Isolation")
    formatted = format_ordered_recipe(inst.recipe, lang="en")
    assert formatted == "[Slot 1: Liquid Paranoia -> Slot 2: Diluted Liquid Greed -> Slot 3: Concentrated Liquid Isolation]"
    # Reversed triplet MUST NOT resolve to Fast Acting Toxins
    rev = cat.lookup_instill_by_recipe("Isolation", "Greed", "Paranoia")
    assert rev is None or rev.name != "Fast Acting Toxins"


# ---------------------------------------------------------------------------
# 3. Four Item.lua hard boundaries & dual-write rendering
# ---------------------------------------------------------------------------


def test_four_item_lua_hard_boundaries_parity_override_req_level_and_shaman() -> None:
    cat = load_rune_instill_catalog()

    # Boundary 1: 21-slot type parity
    wand = resolve_item_augment_types("Wand")
    assert wand.base_type == "caster" and wand.specific_type == "wand"
    assert "weapon" not in wand.accepted_keys
    assert "martial weapon wand or staff" in wand.accepted_keys

    sceptre = resolve_item_augment_types("Sceptre")
    assert sceptre.base_type == "caster" and sceptre.specific_type == "sceptre"
    assert "weapon" not in sceptre.accepted_keys
    assert "martial weapon wand or staff" not in sceptre.accepted_keys

    qstaff = resolve_item_augment_types("Quarterstaff", sub_type="Warstaff")
    assert qstaff.base_type == "weapon" and qstaff.specific_type == "quarterstaff"
    assert "caster" not in qstaff.accepted_keys

    buckler = resolve_item_augment_types("Shield", sub_type="Evasion", base_name="Ornate Buckler")
    assert buckler.base_type == "armour" and buckler.specific_type == "buckler"
    assert "shield" not in buckler.accepted_keys

    # Boundary 2: Unique socketedAugmentTypeOverride + effectModifier (e.g. Darkness Enthroned)
    belt_raw = (
        "Rarity: UNIQUE\nDarkness Enthroned\nFine Belt\nSockets: S S\n"
        "This item gains bonuses from Socketed items as though it was a Gloves\n"
        "75% increased effect of Socketed Items\n"
    )
    belt = resolve_item_augment_types("Belt", raw_text=belt_raw)
    assert belt.override_type == "gloves"
    assert belt.accepted_keys == ("armour", "gloves")
    assert belt.effect_multiplier("Rune") == pytest.approx(1.75)

    # Boundary 3: rune.req_level <= char_level (Perfect Iron Rune requires level 50)
    perf_iron = cat.lookup_rune("Perfect Iron Rune")
    assert perf_iron is not None and perf_iron.req_level == 50
    mods_ok, rej_ok = resolve_rune_effects_for_item(perf_iron, resolve_item_augment_types("Body Armour"), char_level=50)
    assert mods_ok and not rej_ok
    mods_bad, rej_bad = resolve_rune_effects_for_item(perf_iron, resolve_item_augment_types("Body Armour"), char_level=30)
    assert not mods_bad and rej_bad and "requires level 50" in rej_bad[0]

    # Boundary 4: Non-Shaman strips Bonded: lines; Shaman preserves them
    desert = cat.lookup_rune("Desert Rune")
    assert desert is not None
    mods_non_shaman, _ = resolve_rune_effects_for_item(desert, wand, char_level=90, is_shaman=False)
    mods_shaman, _ = resolve_rune_effects_for_item(desert, wand, char_level=90, is_shaman=True)
    assert all(not m.startswith("Bonded:") for m in mods_non_shaman)
    assert any(m.startswith("Bonded:") for m in mods_shaman)

    # Dual-write rendering + isSocketBound / Mirrored protection (Passion of Aldur is caster/weapon socket-bound)
    bound_wand_raw = (
        "Rarity: RARE\nBound Wand\nAttuned Wand\nSockets: S S\n"
        "Rune: Passion of Aldur\nRune: Lesser Desert Rune\nImplicits: 2\n"
        "{enchant}{rune}Gain 6% of Damage as Extra Fire Damage\n60% increased Spell Damage\n"
    )
    stripped, removed, kept = strip_replaceable_runes_from_item_raw(bound_wand_raw, item_type="Wand")
    assert removed == ["Lesser Desert Rune"]
    assert kept == ["Passion of Aldur"]
    assert "Rune: Passion of Aldur" in stripped
    assert "Lesser Desert Rune" not in stripped

    with pytest.raises(ValueError, match="isSocketBound"):
        apply_runes_to_item_raw(bound_wand_raw, ["Desert Rune"], item_type="Wand", char_level=90)

    clean_wand_raw = "Rarity: RARE\nDemo Wand\nAttuned Wand\nSockets: S\nImplicits: 0\n50% increased Spell Damage\n"
    rendered = apply_runes_to_item_raw(clean_wand_raw, ["Desert Rune"], item_type="Wand", char_level=90, is_shaman=False)
    assert "Rune: Desert Rune" in rendered
    assert "{enchant}{rune}Gain 8% of Damage as Extra Fire Damage" in rendered
    assert "Bonded:" not in rendered
    assert "Implicits: 1" in rendered

    amulet_raw = "Rarity: RARE\nDemo Amulet\nSolar Amulet\nImplicits: 1\n+15 to Spirit\n"
    anointed = apply_anoint_to_amulet_raw(amulet_raw, "Storm Swell")
    assert "{enchant}Allocates Storm Swell" in anointed
    assert "Implicits: 2" in anointed


# ---------------------------------------------------------------------------
# 4. Positive-price anti-poisoning & Pareto frontier math
# ---------------------------------------------------------------------------


def test_anti_poisoning_prices_and_pareto_frontier() -> None:
    cat = load_rune_instill_catalog()
    # Poisoned PriceBook with 0.0 / -5.0 Ex quotes MUST be ignored in favor of strictly positive baselines!
    poisoned = PriceBook(
        prices_by_key={
            norm_key("Concentrated Liquid Isolation"): 0.0,
            norm_key("Soul Core of Azcapa"): -5.0,
        },
        source_summary="poisoned",
    )
    for inst in cat.instills[:50]:
        p = price_instill_recipe(inst, price_book=poisoned, catalog=cat)
        assert p["totalCostEx"] > 0.0
    for rune in cat.runes[:50]:
        px, _ = resolve_rune_price_ex(rune, price_book=poisoned)
        assert px > 0.0

    # Synthetic Pareto test: Budget pick gets 85% of BiS at 10% of cost; dominated expensive weak option excluded!
    cands = [
        RankedCandidate("BiS Mega", "毕业", cost_ex=200.0, gain=100.0, gain_pct=10.0, efficiency_per_ex=0.5, price_source="test"),
        RankedCandidate("Dominated Trap", "智商税", cost_ex=150.0, gain=70.0, gain_pct=7.0, efficiency_per_ex=0.46, price_source="test"),
        RankedCandidate("Budget King", "平替王", cost_ex=20.0, gain=85.0, gain_pct=8.5, efficiency_per_ex=4.25, price_source="test"),
        RankedCandidate("Starter", "起步", cost_ex=2.0, gain=40.0, gain_pct=4.0, efficiency_per_ex=20.0, price_source="test"),
    ]
    bis, champ, frontier, _ranked = compute_pareto_frontier(cands)
    assert bis is not None and bis.name == "BiS Mega"
    assert champ is not None and champ.name == "Budget King"
    assert [c.name for c in frontier] == ["Starter", "Budget King", "BiS Mega"]
    assert all(c.name != "Dominated Trap" for c in frontier)

    # Full offline rankers execute cleanly
    rune_rep = rank_runes_for_slot(item_type="Wand", char_level=90, metric="CombinedDPS", limit=10)
    assert rune_rep.evaluated_count > 10
    assert rune_rep.bis_anchor is not None and rune_rep.bis_anchor.cost_ex > 0.0
    assert len(rune_rep.pareto_frontier) >= 1

    inst_rep = rank_instillations(metric="CombinedDPS", limit=10)
    assert inst_rep.evaluated_count == 875
    assert inst_rep.bis_anchor is not None
    assert "[Slot 1:" in inst_rep.bis_anchor.details["orderedDisplayEn"]


# ---------------------------------------------------------------------------
# 5. CLI tests for rune_group, instill_group, cmd_rank_instill
# ---------------------------------------------------------------------------


def test_cli_rune_instill_and_rank_instill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(tmp_path / "no_daemon"))
    runner = CliRunner()

    r1 = runner.invoke(rune_group, ["inspect", "Passion of Aldur", "--json"])
    assert r1.exit_code == 0, r1.output
    d1 = json.loads(r1.output)
    assert d1["isSocketBound"] is True and d1["priceEx"] > 0.0

    # Without a running daemon and without --type, `rune rank` fails cleanly with actionable hint
    r_missing = runner.invoke(rune_group, ["rank", "--slot", "Weapon 1"])
    assert r_missing.exit_code != 0

    r2 = runner.invoke(rune_group, ["rank", "--type", "Wand", "--slot", "Weapon 1", "--level", "90", "--json"])
    assert r2.exit_code == 0, r2.output
    d2 = json.loads(r2.output)
    assert d2["game"] == "poe2" and d2["evalMode"] == "offline_heuristic"
    assert d2["evaluatedCount"] > 10 and d2["bisAnchor"] is not None
    assert any("offline heuristic" in w for w in d2.get("warnings") or [])

    r2_explicit = runner.invoke(rune_group, ["rank", "--type", "Wand", "--offline-heuristic", "--json"])
    assert r2_explicit.exit_code == 0, r2_explicit.output
    d2_explicit = json.loads(r2_explicit.output)
    assert d2_explicit["evalMode"] == "offline_heuristic"
    assert not any("fell back" in w for w in d2_explicit.get("warnings") or [])

    r3 = runner.invoke(instill_group, ["lookup", "Storm Swell", "--json"])
    assert r3.exit_code == 0, r3.output
    d3 = json.loads(r3.output)
    assert d3["count"] >= 1 and "[Slot 1:" in d3["instills"][0]["orderedDisplayEn"]

    r4 = runner.invoke(instill_group, ["bridge", "Ancient Diluted Liquid Ire"])
    assert r4.exit_code != 0  # Rejects Ancient radiusJewel=true emotion!

    r5 = runner.invoke(instill_group, ["rank", "--metric", "CombinedDPS", "--limit", "5", "--json"])
    assert r5.exit_code == 0, r5.output
    d5 = json.loads(r5.output)
    assert d5["evalMode"] == "offline_heuristic"
    assert d5["evaluatedCount"] == 875 and d5["bisAnchor"] is not None
    assert any("offline heuristic" in w for w in d5.get("warnings") or [])


def test_rune_rank_slot_mapping_and_empty_frontier_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(tmp_path / "no_daemon"))
    runner = CliRunner()

    # Unique armour type auto-maps to Body Armour and echoes slot & itemType in JSON
    r_body = runner.invoke(
        rune_group,
        ["rank", "--type", "Body Armour", "--metric", "TotalEHP", "--json"],
    )
    assert r_body.exit_code == 0, r_body.output
    data = json.loads(r_body.output)
    assert data["slot"] == "Body Armour"
    assert data["itemType"] == "Body Armour"

    # Ambiguous type without --slot fails fast with structured usage_error (rc=1)
    r_ring = runner.invoke(rune_group, ["rank", "--type", "Ring", "--json"])
    assert r_ring.exit_code == 1
    err = json.loads(r_ring.output)
    assert err["errorKind"] == "usage_error"
    assert err["code"] == "POE2-USAGE-ERROR"

    # Empty Pareto frontier prints explicit summary line in Markdown
    md = cli_rune._format_pareto_human(
        {
            "patch": "0.5.5",
            "kind": "rune",
            "evalMode": "live_cow",
            "slot": "Body Armour",
            "itemType": "Body Armour",
            "metric": "TotalEHP",
            "evaluatedCount": 5,
            "bisAnchor": None,
            "budgetChampion": None,
            "paretoFrontier": [],
            "topCandidates": [{"name": "Iron Rune", "gain": -10.0, "costEx": 0.5}],
            "warnings": [],
        }
    )
    assert "slot=Body Armour" in md
    assert "[Summary]" in md
    assert "Iron Rune" in md


# ---------------------------------------------------------------------------
# 6. Live PoB2 BridgeWorker CoW verification (when POB2_ENGINE_DIR is set)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native lua51.dll live PoB2 CoW test")
def test_live_pob2_rune_and_instill_pareto_ranking_cow_idempotent(tmp_path: Path) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 rune & instill CoW tests.")

    worker = BridgeWorker(slot_name="rune_e2e", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        code = bridge_protocol.encode_xml_share_code(_LIVE_RUNE_INSTILL_XML)
        worker.call("import_code", {"code": code, "mode": "replace"})
        before_stats = worker.call("get_stats", {})["stats"]
        before_dps = float(before_stats.get("CombinedDPS") or 0.0)
        assert before_dps > 0.0

        # Live CoW rune ranking on Wand (Weapon 1) with default top_live_eval=28 shortlist (<3s)
        wand_elig = eligible_runes_for_item(resolve_item_augment_types("Wand"), char_level=90)
        assert len(wand_elig) >= 5
        t0 = time.perf_counter()
        r_rep = rank_runes_for_slot(
            slot_name="Weapon 1",
            char_level=90,
            metric="CombinedDPS",
            max_budget_ex=100.0,
            limit=8,
            worker=worker,
        )
        rune_elapsed = time.perf_counter() - t0
        assert r_rep.eval_mode == "live_cow"
        assert 5 <= r_rep.evaluated_count <= 28
        assert r_rep.shortlisted_from is not None and r_rep.shortlisted_from >= r_rep.evaluated_count
        assert r_rep.bis_anchor is not None and r_rep.bis_anchor.gain > 0.0
        assert rune_elapsed < 5.0, f"Live rune rank shortlist exceeded latency budget: {rune_elapsed:.2f}s"

        # Live CoW instillation ranking on Amulet (top 8 prescored notables)
        i_rep = rank_instillations(metric="CombinedDPS", top_live_eval=8, limit=5, worker=worker)
        assert i_rep.eval_mode == "live_cow"
        assert i_rep.evaluated_count == 8
        assert i_rep.shortlisted_from == 875
        assert i_rep.bis_anchor is not None and i_rep.bis_anchor.gain > 0.0
        assert "[Slot 1:" in i_rep.bis_anchor.details["orderedDisplayEn"]

        # Verify CoW left original build 100% untouched!
        after_stats = worker.call("get_stats", {})["stats"]
        assert float(after_stats.get("CombinedDPS") or 0.0) == pytest.approx(before_dps)
    finally:
        worker.stop()
