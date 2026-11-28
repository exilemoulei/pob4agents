# SPDX-License-Identifier: GPL-3.0-only
"""Round 2 Final + Meditation Probe regression tests (2 P1 + 1 P2 + 4 P3)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob2_cli import bridge_protocol, updater
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.cli import main as cli_main
from pob2_cli.craft import (
    CraftMdpSolver,
    ModPoolEngine,
    UnknownTargetError,
    compute_next_step_outcomes,
    fetch_live_price_book,
    inspect_item_text,
)
from pob2_cli.craft.models import CraftingState, ItemRarity
from pob2_cli.errors import CrossGameHallucinationError
from pob2_cli.guardrails import validate_url
from pob2_cli.rebalance import (
    NetGapAudit,
    StatSnapshot,
    audit_equipped_slot,
    solve_fix_gaps_live,
    solve_fix_gaps_offline,
    stat_snapshot_from_pob_stats,
)
from pob2_cli.rebalance.step_runes import (
    SocketAssignment,
    Stage2RunePlan,
    format_max_res_drop_warning,
    inspect_augment_side_effects,
    parse_augment_stat_vector,
)
from pob2_cli.rune import load_rune_instill_catalog, normalize_emotion_with_warning

_FIVE_SOCKET_BODY_RAW = """Rarity: UNIQUE
Morior Invictus
Grand Regalia
Item Level: 82
Sockets: S S S S S
Implicits: 0
200% increased Armour, Evasion and Energy Shield
"""

_TWO_SOCKET_HELM_RAW = """Rarity: RARE
Storm Brow
Kamasan Tiara
Item Level: 82
Sockets: S S
Implicits: 0
+80 to maximum Life
"""

_LIVE_PRISM_GUARD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
\t<Build level="90" targetVersion="0_1" mainSocketGroup="1" className="Witch" ascendClassName="Stormweaver" characterLevelAutoMode="false"/>
\t<Tree activeSpec="1">
\t\t<Spec treeVersion="0_5" classId="1" classInternalId="1" ascendClassId="0" secondaryAscendClassId="nil" ascendancyInternalId="nil" nodes="54447" masteryEffects=""/>
\t</Tree>
\t<Skills activeSkillSet="1" defaultGemLevel="normalMaximum" defaultGemQuality="0">
\t\t<SkillSet id="1">
\t\t\t<Skill enabled="true" mainActiveSkill="1" mainActiveSkillCalcs="1" includeInFullDPS="nil" label="">
\t\t\t\t<Gem count="1" skillId="IceNovaPlayer" gemId="Metadata/Items/Gems/SkillGemIceNova" variantId="IceNova" nameSpec="Ice Nova" level="20" quality="0" enabled="true" enableGlobal1="true" enableGlobal2="true" corrupted="nil"/>
\t\t\t</Skill>
\t\t</SkillSet>
\t</Skills>
\t<Config activeConfigSet="1"><ConfigSet id="1" title="Default"/></Config>
\t<Items activeItemSet="1" useSecondWeaponSet="false" showStatDifferences="true">
\t\t<Item id="1">
Rarity: RARE
Test Regalia
Vile Robe
Item Level: 82
Sockets: S S
Implicits: 0
+90 to maximum Life
\t\t</Item>
\t\t<ItemSet id="1" useSecondWeaponSet="false" title="Default">
\t\t\t<Slot name="Body Armour" itemId="1"/>
\t\t</ItemSet>
\t</Items>
</PathOfBuilding2>
"""


# ---------------------------------------------------------------------------
# 1. [P1] Stage 2 Rune Recommendation Penalizes Negative Side Effects & Rejects MaxRes Drops
# ---------------------------------------------------------------------------


def test_p1_stage2_rejects_rune_of_the_prism_and_penalizes_negative_side_effects() -> None:
    cat = load_rune_instill_catalog()
    prism = cat.lookup_rune("Rune of the Prism")
    assert prism is not None
    prism_mods = prism.slots["body armour"].stats
    fx_prism = inspect_augment_side_effects(prism_mods, multiplier=1.0)
    assert fx_prism.reduces_max_res is True
    assert fx_prism.max_res_deltas == {"fire": -10, "cold": -10, "lightning": -10}

    # Negative Spirit-per-level & negative resistance lines are subtracted rather than ignored
    blossom = cat.lookup_rune("Rune of the Blossom")
    assert blossom is not None
    vec_blossom_90 = parse_augment_stat_vector(blossom.slots["body armour"].stats, char_level=90)
    assert vec_blossom_90["spirit"] == 5  # +50 - (90 // 2) = +5, NOT +50

    sycophant = cat.lookup_rune("Idol of the Sycophant")
    assert sycophant is not None
    vec_syc = parse_augment_stat_vector(sycophant.slots["martial weapon wand or staff"].stats, char_level=90)
    assert vec_syc["fire"] == -20 and vec_syc["cold"] == -20 and vec_syc["lightning"] == -20

    # Life/Mana/Spirit/AttrReq penalties vs beneficial defensive reductions ("on you" / "against you")
    assert inspect_augment_side_effects(("15% less maximum Life",)).penalty_ex >= 150.0
    assert inspect_augment_side_effects(("25% reduced Spirit",)).penalty_ex >= 150.0
    assert inspect_augment_side_effects(("20% increased Attribute Requirements",)).penalty_ex >= 150.0
    assert inspect_augment_side_effects(("Hits against you have 20% reduced Critical Damage Bonus",)).penalty_ex == 0.0
    assert inspect_augment_side_effects(("15% reduced Slowing Potency of Debuffs on You",)).penalty_ex == 0.0

    # PoB2 MaxRes derivation when MaxRes is crushed below 75% even with positive overcap
    crushed = stat_snapshot_from_pob_stats(
        {
            "FireResist": -10,
            "MissingFireResist": 35,
            "FireResistTotal": -10,
            "ColdResist": 25,
            "ColdResistOverCap": 50,
            "ColdResistTotal": 75,
            "MissingColdResist": 0,
            "LightningResist": 25,
            "LightningResistOverCap": 50,
            "LightningResistTotal": 75,
            "MissingLightningResist": 0,
        }
    )
    assert crushed.fire_max == 25.0 and crushed.cold_max == 25.0 and crushed.lightning_max == 25.0

    # Offline Stage 2 + full offline solver on 5-socket Body Armour with bing.xml-like deficits
    sl_body = audit_equipped_slot(
        slot_name="Body Armour",
        item_id=1,
        name="Morior Invictus",
        base_name="Grand Regalia",
        rarity="UNIQUE",
        item_type="Body Armour",
        raw_text=_FIVE_SOCKET_BODY_RAW,
        catalog=cat,
    )
    sl_helm = audit_equipped_slot(
        slot_name="Helmet",
        item_id=2,
        name="Storm Brow",
        base_name="Kamasan Tiara",
        rarity="RARE",
        item_type="Helmet",
        raw_text=_TWO_SOCKET_HELM_RAW,
        catalog=cat,
    )
    bare = StatSnapshot(
        level=90,
        ascendancy="Invoker",
        str_val=200,
        req_str=150,
        dex_val=200,
        req_dex=150,
        int_val=200,
        req_int=150,
        fire_total=-10.0,
        fire_max=75.0,
        cold_total=-50.0,
        cold_max=75.0,
        lightning_total=-50.0,
        lightning_max=75.0,
        chaos_total=75.0,
        chaos_max=75.0,
        spirit_total=150.0,
        spirit_reserved=120.0,
        spirit_unreserved=30.0,
    )
    audit = NetGapAudit(current=bare, bare_net=bare, slots=(sl_body, sl_helm))
    rep = solve_fix_gaps_offline(audit, [], catalog=cat)
    chosen_names = [a.rune_name for a in rep.stage2.assignments]
    assert "Rune of the Prism" not in chosen_names
    assert any("[rune-reduces-max-res]" in w and "Rune of the Prism" in w and "-10%" in w for w in rep.stage2.warnings)
    assert any("[rune-reduces-max-res]" in w for w in rep.warnings)
    assert rep.after_verified.fire_max == 75.0
    assert rep.after_verified.cold_max == 75.0
    assert rep.after_verified.lightning_max == 75.0

    w_drop = format_max_res_drop_warning("Rune of the Prism", bare, crushed, slot_name="Body Armour")
    assert w_drop is not None and "[rune-reduces-max-res]" in w_drop and "75%->25% (-50%)" in w_drop


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native lua51.dll live PoB2 test")
def test_p1_live_after_verified_guard_and_bing_xml_max_res_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live Stage 2 MaxRes verification tests.")

    worker = BridgeWorker(slot_name="r2_prism_guard", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        # Part A: Force-inject Rune of the Prism into initial Stage 2 return to prove live afterVerified guard catches & rejects it
        worker.call(
            "import_code",
            {"code": bridge_protocol.encode_xml_share_code(_LIVE_PRISM_GUARD_XML), "mode": "replace"},
        )
        import pob2_cli.rebalance.solver as solver_mod

        real_solve = solver_mod.solve_rune_allocation
        call_count = {"n": 0}

        def _injected_solve(*args, **kwargs):
            call_count["n"] += 1
            excluded = kwargs.get("excluded_runes") or set()
            if call_count["n"] == 1 and "Rune of the Prism" not in excluded:
                bad_asgn = SocketAssignment(
                    slot_name="Body Armour",
                    socket_index=0,
                    rune_name="Rune of the Prism",
                    zh_cn="崇敬稜镜符文",
                    price_ex=1.5,
                    effect_multiplier=1.0,
                    gains={"fire": 20, "cold": 20, "lightning": 20},
                    purpose="hard_floor",
                )
                return Stage2RunePlan(
                    assignments=(bad_asgn,),
                    total_cost_ex=1.5,
                    remaining_hard={"str": 0, "dex": 0, "int": 0, "fire": 55, "cold": 55, "lightning": 55, "chaos": 0, "spirit": 0},
                    remaining_overcap={"fire": 75, "cold": 75, "lightning": 75},
                )
            return real_solve(*args, **kwargs)

        with monkeypatch.context() as mp:
            mp.setattr(solver_mod, "solve_rune_allocation", _injected_solve)
            rep_guard = solve_fix_gaps_live(worker, apply=False)
        assert call_count["n"] >= 2
        assert "Rune of the Prism" not in [a.rune_name for a in rep_guard.stage2.assignments]
        assert rep_guard.after_verified.fire_max == 75.0
        assert rep_guard.after_verified.cold_max == 75.0
        assert rep_guard.after_verified.lightning_max == 75.0
        assert any("[rune-reduces-max-res]" in w and "75%->65% (-10%)" in w for w in rep_guard.warnings)
        assert rep_guard.cow_rollback_verified is True

        # Part B: Real bing.xml end-to-end check (if present in engine Builds dir)
        bing_candidates = [eng / "PoE2" / "Builds" / "bing.xml", eng / "Builds" / "bing.xml"]
        bing_xml = next((p for p in bing_candidates if p.is_file()), None)
        if bing_xml is not None:
            worker.call(
                "import_code",
                {"code": bridge_protocol.encode_xml_share_code(bing_xml.read_text(encoding="utf-8")), "mode": "replace"},
            )
            rep_bing = solve_fix_gaps_live(worker, apply=False).to_dict()
            s2_runes = [a["rune"] for a in rep_bing["stages"]["stage2Runes"]["assignments"]]
            assert "Rune of the Prism" not in s2_runes
            after_res = rep_bing["afterVerified"]["resistances"]
            assert after_res["fireMax"] >= 75.0
            assert after_res["coldMax"] >= 75.0
            assert after_res["lightningMax"] >= 75.0
            assert any("[rune-reduces-max-res]" in w and "Rune of the Prism" in w for w in rep_bing["warnings"])
            assert rep_bing["cowRollbackVerified"] is True
    finally:
        worker.stop()


# ---------------------------------------------------------------------------
# 2. [P1] Greater/Perfect Currency Mod-Family Floor Exemption
# ---------------------------------------------------------------------------


def test_p1_greater_perfect_currency_mod_family_exemption_focus_and_pruning() -> None:
    eng = ModPoolEngine()
    focus = eng.resolve_base("Crystal Focus")
    assert focus.poe2db_slug == "Foci"
    state_rare = CraftingState(base=focus, ilvl=82, rarity=ItemRarity.RARE)

    # +2 to Level of all Spell Skills on Focus has max required_level=41 < Perfect Exalted floor=50 -> EXEMPTED!
    t_spell_lvl = eng.resolve_target("Foci", "+2 to Level of all Spell Skills", ilvl=82)
    assert any(m.mod_id == "GlobalSpellGemsLevel2" and m.required_level == 41 for m in t_spell_lvl.matching_entries)

    ev_perf_ex = eng.evaluate_action(state_rare, set(t_spell_lvl.matching_mod_ids), "Perfect Exalted Orb")
    assert ev_perf_ex.hit_probability > 0.0
    assert ev_perf_ex.target_weight == 250
    assert [m.mod_id for m in ev_perf_ex.matching_tiers] == ["GlobalSpellGemsLevel2"]

    # Lower tier GlobalSpellGemsLevel1 (req=10) MUST NOT leak into Perfect Exalted pool (only highest tier req=41 exempted)
    elig_perf = eng.eligible_pool(state_rare, target_rarity=ItemRarity.RARE, min_mod_level=50)
    elig_ids = {m.mod_id for m, _ in elig_perf}
    assert "GlobalSpellGemsLevel2" in elig_ids
    assert "GlobalSpellGemsLevel1" not in elig_ids

    # Multi-tier families whose max tier >= 50 (e.g. WeaponCasterDamagePrefix T6..T2 req<=46, T1 req=60) prune ALL tiers < 50
    spell_dmg_tiers = [m for m, _ in elig_perf if m.family_id == "WeaponCasterDamagePrefix"]
    assert len(spell_dmg_tiers) == 1 and spell_dmg_tiers[0].required_level == 60
    assert all(m.required_level >= 50 for m in spell_dmg_tiers)

    # Magic Greater=55 / Perfect=70 & Greater=35 thresholds also honor family exemption
    state_norm = CraftingState(base=focus, ilvl=82, rarity=ItemRarity.NORMAL)
    for orb_name in ("Greater Orb of Transmutation", "Perfect Orb of Transmutation"):
        ev_tr = eng.evaluate_action(state_norm, set(t_spell_lvl.matching_mod_ids), orb_name)
        assert ev_tr.hit_probability > 0.0 and ev_tr.target_weight == 250

    # MDP transition cache agrees with ModPoolEngine on exempted family probability
    solver = CraftMdpSolver(eng)
    pb = fetch_live_price_book(offline=True)
    next_actions = solver.evaluate_next_actions(state_rare, (t_spell_lvl,), pb)
    perf_act = next(a for a in next_actions if a.lever == "Perfect Exalted Orb" and not a.omens)
    assert perf_act.hit_probability == pytest.approx(ev_perf_ex.hit_probability, rel=1e-6)


# ---------------------------------------------------------------------------
# 3. [P2] `craft odds --outcomes` Full Next-Step ModFamily Probability Distribution
# ---------------------------------------------------------------------------


def test_p2_craft_odds_outcomes_distribution_sum_tiers_and_cli() -> None:
    runner = CliRunner()

    # 1. Standalone `--outcomes` WITHOUT `--target` on Crystal Focus + Perfect Exalted Orb
    r_json = runner.invoke(
        cli_main,
        ["craft", "odds", "--base", "Crystal Focus", "--lever", "Perfect Exalted Orb", "--outcomes", "--json"],
    )
    assert r_json.exit_code == 0, r_json.output
    data = json.loads(r_json.output)
    assert data["game"] == "poe2"
    assert data["baseSlug"] == "Foci"
    assert data["lever"] == "Perfect Exalted Orb"
    assert data["minModLevelFloor"] == 50
    assert data["totalPoolWeight"] > 0
    outcomes = data["outcomes"]
    assert len(outcomes) == data["familyCount"] and len(outcomes) >= 10

    # Probabilities sum ~= 1.0 and sorted descending
    probs = [o["probability"] for o in outcomes]
    assert sum(probs) == pytest.approx(1.0, abs=1e-4)
    assert data["probabilitySum"] == pytest.approx(1.0, abs=1e-4)
    assert probs == sorted(probs, reverse=True)
    assert sum(o["weight"] for o in outcomes) == data["totalPoolWeight"]

    # Exempted +2 Spell Skills family present with single exempted T1 (req=41)
    gem_fam = next(o for o in outcomes if o["familyId"] == "IncreaseSocketedGemLevel")
    assert gem_fam["slot"] == "suffix"
    assert gem_fam["weight"] == 250
    assert gem_fam["tierRange"] == "T1"
    assert gem_fam["reqLevelRange"] == [41, 41]
    assert gem_fam["exemptedBelowFloor"] is True

    # Non-exempted WeaponCasterDamagePrefix family pruned to req >= 50 (T1 req=60) vs T1-T6 on normal Exalted Orb
    sd_perf = next(
        o for o in outcomes if o["familyId"] == "WeaponCasterDamagePrefix" and "Spell Damage" in o["name"]
    )
    assert sd_perf["slot"] == "prefix"
    assert sd_perf["exemptedBelowFloor"] is False
    assert sd_perf["reqLevelRange"] == [60, 60]
    assert sd_perf["tierRange"] == "T1"

    eng = ModPoolEngine()
    st_focus = CraftingState(base=eng.resolve_base("Crystal Focus"), ilvl=82, rarity=ItemRarity.RARE)
    out_std_ex = compute_next_step_outcomes(eng, st_focus, lever_name="Exalted Orb")
    sd_std = next(
        o for o in out_std_ex["outcomes"] if o["familyId"] == "WeaponCasterDamagePrefix" and "Spell Damage" in o["name"]
    )
    assert sd_std["tierCount"] > sd_perf["tierCount"] and sd_std["tierRange"] == "T1-T6"

    # 2. Markdown table output (no --json, no --lever -> defaults to Orb of Transmutation on Normal base)
    r_md = runner.invoke(cli_main, ["craft", "odds", "--base", "Attuned Wand", "--outcomes"])
    assert r_md.exit_code == 0, r_md.output
    assert "Orb of Transmutation" in r_md.output
    assert "FamilyId" in r_md.output

    # 3. Combined `--outcomes` + `--want`
    r_both = runner.invoke(
        cli_main,
        [
            "craft",
            "odds",
            "--base",
            "Crystal Focus",
            "--lever",
            "Perfect Exalted Orb",
            "--want",
            "+2 to Level of all Spell Skills",
            "--outcomes",
            "--json",
        ],
    )
    assert r_both.exit_code == 0, r_both.output
    both_data = json.loads(r_both.output)
    assert both_data["evaluation"]["hitProbability"] > 0.0
    assert len(both_data["outcomes"]) == data["familyCount"]

    # 4. Neither `--target` nor `--outcomes` fails cleanly with exit code 1
    r_empty = runner.invoke(cli_main, ["craft", "odds", "--base", "Attuned Wand", "--json"])
    assert r_empty.exit_code == 1
    assert "--outcomes" in r_empty.output


# ---------------------------------------------------------------------------
# 4. [P3 x 4] UX Polish: [no-affix-lines], instill [tier-coerced]/reason, CN domain block, target similarity
# ---------------------------------------------------------------------------


def test_p3_1_no_affix_lines_warning_on_magic_and_rare_header_only_items() -> None:
    runner = CliRunner()
    rare_header_only = "Rarity: RARE\nDoom Shelter\nVile Robe\nItem Level: 82\n"
    magic_header_only = "Rarity: MAGIC\nAttuned Wand\nItem Level: 82\n"
    normal_white = "Rarity: NORMAL\nVile Robe\nItem Level: 82\n"

    for raw in (rare_header_only, magic_header_only):
        rep = inspect_item_text(raw)
        assert any("[no-affix-lines]" in w for w in rep.warnings)
        for subcmd in (["craft", "inspect"], ["item", "inspect"]):
            res = runner.invoke(cli_main, [*subcmd, "--text", raw, "--json"])
            assert res.exit_code == 0, res.output
            w_list = json.loads(res.output)["warnings"]
            assert any("[no-affix-lines]" in w for w in w_list)

    rep_norm = inspect_item_text(normal_white)
    assert not any("[no-affix-lines]" in w for w in rep_norm.warnings)


def test_p3_2_instill_lookup_tier_coerced_warnings_and_zero_match_reason() -> None:
    runner = CliRunner()

    # Exact 0.5.5 live names & short names produce zero tier-coerced warnings
    em_ok, w_ok = normalize_emotion_with_warning("Concentrated Liquid Isolation")
    assert em_ok.short == "Isolation" and w_ok is None
    em_short, w_short = normalize_emotion_with_warning("Isolation")
    assert em_short.short == "Isolation" and w_short is None

    # Wrong live tier ("Diluted Liquid Isolation" -> "Concentrated Liquid Isolation") emits [tier-coerced]
    em_coerced, w_coerced = normalize_emotion_with_warning("Diluted Liquid Isolation")
    assert em_coerced.short == "Isolation"
    assert w_coerced is not None and "[tier-coerced]" in w_coerced and "Concentrated Liquid Isolation" in w_coerced

    r_coerced = runner.invoke(
        cli_main,
        ["instill", "lookup", "--emotions", "Diluted Liquid Greed,Liquid Ire,Concentrated Liquid Envy", "--json"],
    )
    assert r_coerced.exit_code == 0, r_coerced.output
    d_coerced = json.loads(r_coerced.output)
    assert d_coerced["count"] == 1 and d_coerced["instills"][0]["name"] == "Initiative"
    coerced_warns = [w for w in d_coerced["warnings"] if "[tier-coerced]" in w]
    assert len(coerced_warerc := coerced_warns) == 2
    assert any("Diluted Liquid Ire" in w for w in coerced_warerc)
    assert any("Liquid Envy" in w for w in coerced_warerc)

    # Zero-match query includes explicit `reason` and `[no-match]` warning
    r_zero_q = runner.invoke(cli_main, ["instill", "lookup", "nonexistent_notable_zzzz", "--json"])
    assert r_zero_q.exit_code == 0, r_zero_q.output
    d_zero_q = json.loads(r_zero_q.output)
    assert d_zero_q["count"] == 0
    assert "reason" in d_zero_q and "nonexistent_notable_zzzz" in d_zero_q["reason"]
    assert any("[no-match]" in w for w in d_zero_q["warnings"])

    # Zero-match `--emotions` triplet (if unallocated) explains why 0 recipes matched
    cat = load_rune_instill_catalog()
    shorts = [e.short for e in cat.emotions]
    unassigned: tuple[str, str, str] | None = None
    for a in shorts:
        for b in shorts:
            for c in shorts:
                if (a, b, c) not in cat.instills_by_recipe:
                    unassigned = (a, b, c)
                    break
            if unassigned:
                break
        if unassigned:
            break
    assert unassigned is not None
    r_zero_em = runner.invoke(
        cli_main,
        ["instill", "lookup", "--emotions", ",".join(unassigned), "--json"],
    )
    assert r_zero_em.exit_code == 0, r_zero_em.output
    d_zero_em = json.loads(r_zero_em.output)
    assert d_zero_em["count"] == 0
    assert d_zero_em.get("reason")
    assert any("[no-match]" in w for w in d_zero_em["warnings"])


@pytest.mark.parametrize(
    "cn_poe1_url",
    [
        "https://poe.game.qq.com",
        "https://poe.game.qq.com/",
        "https://poe.game.qq.com/share/AbCdEfGh123",
        "https://poe.game.qq.com/character-window/get-items",
        "http://bbs.poe.game.qq.com/thread/123",
        "poe.game.qq.com/share/xyz987",
    ],
)
def test_p3_3_entire_poe_game_qq_domain_blocked_with_wegame_remediation(cn_poe1_url: str) -> None:
    with pytest.raises(CrossGameHallucinationError) as exc:
        validate_url(cn_poe1_url, expected_game="poe2")
    assert exc.value.exit_code == 1
    assert exc.value.rule_id == "URL_POE1_CN_PROFILE"
    assert "https://www.wegame.com.cn/helper/poe2/#/share/<code>" in exc.value.remediation

    runner = CliRunner()
    res = runner.invoke(cli_main, ["import", "https://poe.game.qq.com/share/AbCdEfGh123"])
    assert res.exit_code == 1
    assert "https://www.wegame.com.cn/helper/poe2/#/share/<code>" in res.output


def test_p3_4_unknown_target_similarity_ranked_candidates_prioritize_t2_maximum_life() -> None:
    eng = ModPoolEngine()
    # Direct suggestion ranking: `T2 Life` prioritizes `T2 maximum Life` #1
    sugs_t2_life = eng.suggest_targets("Body_Armours_int", "T2 Life", limit=6)
    assert sugs_t2_life[0] == "T2 maximum Life"

    # Misspelled target queries preserve requested tier & rank closest stat phrase at the top
    with pytest.raises(UnknownTargetError) as exc_life:
        eng.resolve_target("Body_Armours_int", "T2 Lfie")
    assert exc_life.value.suggestions[0] == "T2 maximum Life"

    with pytest.raises(UnknownTargetError) as exc_sd:
        eng.resolve_target("Wands", "T2 Spell Damge")
    assert "T2 increased Spell Damage" in exc_sd.value.suggestions[:2]
