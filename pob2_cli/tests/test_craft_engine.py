# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for PoE2 0.5.5 crafting engine (M1 DoD: all 6 hard constraints)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob2_cli.cli import main as cli_main
from pob2_cli.craft import (
    AffixSide,
    CraftingMod,
    CraftingState,
    CraftMdpSolver,
    ItemRarity,
    ModPoolEngine,
    PriceBook,
    RemovedLeverError,
    WeightConfidence,
    exact_three_choice_unveil_prob,
    inspect_item_text,
    parse_ggg_cx_snapshot,
    parse_poeninja_exchange_payload,
)


@pytest.fixture(scope="module")
def engine() -> ModPoolEngine:
    return ModPoolEngine()


@pytest.fixture(scope="module")
def solver(engine: ModPoolEngine) -> CraftMdpSolver:
    return CraftMdpSolver(engine)


# ---------------------------------------------------------------------------
# Constraint 1 (P0): PoE2 0.5.0+ Single Crafted Mod Limit (incl. Fractured!)
# ---------------------------------------------------------------------------


def test_p0_single_crafted_mod_limit_blocks_double_essence_and_fractured_essence(
    engine: ModPoolEngine,
) -> None:
    attuned = engine.resolve_base("Attuned Wand")
    t1_spell = engine.resolve_target("Wands", "SpellDamageOnWeapon8_", ilvl=82).matching_entries[0]
    junk_int = next(m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "Intelligence1")
    perf_sorc = engine.resolve_lever("Perfect Essence of Sorcery")
    sorc_mod = engine.lookup_forced_mod_entry(attuned, perf_sorc)
    assert sorc_mod is not None and sorc_mod.is_crafted_pool

    # 1. Start with a Magic item and apply a Greater Essence -> Rare with 1 Crafted mod
    magic_st = CraftingState(
        base=attuned, ilvl=82, rarity=ItemRarity.MAGIC, mods=[CraftingMod.from_entry(t1_spell)]
    )
    greater_ess = next(
        lv for lv in engine.list_levers("essence") if "Wands" in lv.forced_mods and lv.name.startswith("Greater ")
    )
    rare_after_ess = engine.sample_transition(magic_st, greater_ess.name, [], set(), random.Random(1))
    assert rare_after_ess.rarity == ItemRarity.RARE
    assert rare_after_ess.has_crafted_mod is True
    assert rare_after_ess.crafted_mod_count == 1

    # 2. Attempting a SECOND Crafted lever (Perfect Essence / Alloy) MUST be blocked (hit_prob == 0)
    rare_after_ess.mods.append(CraftingMod.from_entry(junk_int))
    blocked_double = engine.evaluate_action(
        rare_after_ess, {sorc_mod.mod_id}, "Perfect Essence of Sorcery"
    )
    assert blocked_double.hit_probability == 0.0
    assert "0.5.0+ 硬上限拦截" in blocked_double.notes

    # 3. Fracturing the Crafted mod MUST preserve `is_crafted=True` and PERMANENTLY block Perfect Essence / Alloy
    crafted_mod = CraftingMod.from_entry(sorc_mod, is_crafted=True)
    fractured_crafted = crafted_mod.with_fractured()
    assert fractured_crafted.fractured is True
    assert fractured_crafted.is_crafted is True

    st_frac_crafted = CraftingState(
        base=attuned,
        ilvl=82,
        rarity=ItemRarity.RARE,
        mods=[fractured_crafted, CraftingMod.from_entry(t1_spell), CraftingMod.from_entry(junk_int)],
    )
    assert st_frac_crafted.has_crafted_mod is True
    assert st_frac_crafted.has_fractured_crafted_mod is True

    alloy = next(lv for lv in engine.list_levers("alloy") if "Wands" in lv.forced_mods)
    alloy_mod_id = alloy.forced_mods["Wands"]
    blocked_after_frac = engine.evaluate_action(st_frac_crafted, {alloy_mod_id}, alloy.name)
    assert blocked_after_frac.hit_probability == 0.0
    assert "破裂锁死" in blocked_after_frac.notes

    # Sample transition must also refuse to add a second Crafted mod
    before_ids = [m.mod_id for m in st_frac_crafted.mods]
    after_try = engine.sample_transition(st_frac_crafted, alloy.name, [], {alloy_mod_id}, random.Random(7))
    assert [m.mod_id for m in after_try.mods] == before_ids

    # 4. Desecrated slot is INDEPENDENT of Crafted slot (still allowed up to 1 Desecrated mod!)
    blocked_fams = st_frac_crafted.blocked_families()
    amanamu = [
        m
        for m in engine.get_mod_tiers("Wands", "desecrated")
        if m.boss_tag == "amanamu" and not any(f in blocked_fams for f in m.families)
    ]
    assert len(amanamu) >= 2
    desec_eval = engine.evaluate_action(
        st_frac_crafted, {amanamu[0].mod_id}, "Ancient Jawbone", ["Omen of the Liege"]
    )
    assert desec_eval.hit_probability > 0.0
    assert desec_eval.weight_confidence == WeightConfidence.EXACT_DETERMINISTIC

    # Adding a second Desecrated mod IS blocked
    st_frac_crafted.mods.append(CraftingMod.from_entry(amanamu[0], is_desecrated=True))
    assert st_frac_crafted.has_desecrated is True
    second_bone = engine.evaluate_action(st_frac_crafted, {amanamu[1].mod_id}, "Ancient Jawbone")
    assert second_bone.hit_probability == 0.0


# ---------------------------------------------------------------------------
# Constraint 3: Hard-blocking PoE1 & 0.4/0.5 Removed Crafting Levers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "removed_name",
    [
        "Orb of Scouring",
        "Orb of Alteration",
        "重铸石",
        "改造石",
        "Omen of Homogenising Exaltation",
        "Omen of Homogenising Coronation",
        "Omen of Recombination",
        "Recombinator",
        "同质化预兆",
        "重组器",
    ],
)
def test_removed_levers_raise_explicit_error_with_055_alternatives(
    engine: ModPoolEngine, removed_name: str
) -> None:
    with pytest.raises(RemovedLeverError) as exc_info:
        engine.resolve_lever(removed_name)
    assert "0.5.5" in str(exc_info.value)
    assert exc_info.value.alternatives


# ---------------------------------------------------------------------------
# Constraint 4: Omen of Whittling Exact Required-Level & Tie Collateral Alert
# ---------------------------------------------------------------------------


def test_whittling_sorts_by_required_level_not_tier_and_alerts_on_protected_tie(
    engine: ModPoolEngine,
) -> None:
    attuned = engine.resolve_base("Attuned Wand")
    t1_lt = engine.resolve_target("Wands", "GlobalLightningSpellGemsLevelWeapon5", ilvl=82).matching_entries[0]
    t1_sp = engine.resolve_target("Wands", "SpellDamageOnWeapon8_", ilvl=82).matching_entries[0]
    low_int = next(m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "Intelligence1")
    low_mana = next(m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "IncreasedMana1")
    cast_target = engine.resolve_target("Wands", "IncreasedCastSpeed", ilvl=82)

    assert low_int.required_level == 1 and low_mana.required_level == 1
    assert t1_lt.required_level > low_int.required_level

    # Safe snipe: junk low_int has unique lowest required_level
    st = CraftingState(
        base=attuned,
        ilvl=82,
        rarity=ItemRarity.RARE,
        mods=[
            CraftingMod.from_entry(t1_lt),
            CraftingMod.from_entry(t1_sp),
            CraftingMod.from_entry(low_int),
        ],
    )
    prot = {t1_lt.mod_id, t1_sp.mod_id}
    res_safe = engine.evaluate_action(
        st, set(cast_target.matching_mod_ids), "Chaos Orb", ["Omen of Whittling"], protected_mod_ids=prot
    )
    assert res_safe.collateral_remove_probability == 0.0
    assert res_safe.whittling is not None
    assert res_safe.whittling.min_required_level == 1
    assert res_safe.whittling.collateral_alert is False
    assert len(res_safe.whittling.targets) == 1
    assert res_safe.whittling.targets[0].mod_id == low_int.mod_id

    # Tie collateral risk: protecting low_mana (also req_lvl=1) triggers red collateral_alert & 50% risk
    st.mods.append(CraftingMod.from_entry(low_mana))
    prot_tied = {t1_lt.mod_id, t1_sp.mod_id, low_mana.mod_id}
    res_tied = engine.evaluate_action(
        st, set(cast_target.matching_mod_ids), "Chaos Orb", ["Omen of Whittling"], protected_mod_ids=prot_tied
    )
    assert res_tied.collateral_remove_probability == pytest.approx(0.5)
    assert res_tied.whittling is not None
    assert res_tied.whittling.is_tied is True
    assert res_tied.whittling.collateral_alert is True
    assert "高危误伤预警" in res_tied.whittling.summary


# ---------------------------------------------------------------------------
# Constraint 5: 4-Level Weight Confidence & <0.01% Exact Combinatorics Oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_name,slug,mod_query,expected_side",
    [
        ("Attuned Wand", "Wands", "SpellDamageOnWeapon8_", AffixSide.PREFIX),
        ("Solar Amulet", "Amulets", "AllAttributes9_", AffixSide.SUFFIX),
        ("Amethyst Ring", "Rings", "Strength8", AffixSide.SUFFIX),
        ("Vile Robe", "Body_Armours_int", "Intelligence8", AffixSide.SUFFIX),
        ("Broadhead Quiver", "Quivers", "AddedPhysicalDamage9", AffixSide.PREFIX),
    ],
)
def test_five_canonical_slots_single_step_probability_exact_combinatorics(
    engine: ModPoolEngine,
    base_name: str,
    slug: str,
    mod_query: str,
    expected_side: AffixSide,
) -> None:
    """Verify exact single-step probability matches raw ModsView weight ratios within <0.01%."""
    base = engine.resolve_base(base_name)
    assert base.poe2db_slug == slug
    target = engine.resolve_target(slug, mod_query, ilvl=82)
    assert target.affix_side == expected_side

    # Independent combinatorial sum over raw eligible normal mods on same side at ilvl 82
    all_normal = [
        m
        for m in engine.get_mod_tiers(slug, "normal")
        if m.affix_side == expected_side and m.required_level <= 82 and m.weight > 0
    ]
    raw_total_w = sum(m.weight for m in all_normal)
    raw_target_w = sum(m.weight for m in all_normal if m.mod_id in target.matching_mod_ids)
    expected_p = raw_target_w / raw_total_w
    assert expected_p > 0.0

    omen = (
        "Omen of Sinistral Exaltation"
        if expected_side == AffixSide.PREFIX
        else "Omen of Dextral Exaltation"
    )
    st = CraftingState(base=base, ilvl=82, rarity=ItemRarity.RARE)
    ev = engine.evaluate_action(st, set(target.matching_mod_ids), "Exalted Orb", [omen])
    assert ev.weight_confidence == WeightConfidence.VERIFIED_COMMUNITY
    assert abs(ev.hit_probability - expected_p) < 1e-4  # < 0.01% error bound


def test_four_tier_weight_confidence_and_element_lock_tags(engine: ModPoolEngine) -> None:
    # 1. Exact deterministic (Perfect Essence / Boss Omen / Fracture)
    attuned = engine.resolve_base("Attuned Wand")
    st = CraftingState(base=attuned, ilvl=82, rarity=ItemRarity.RARE)
    amanamu = [m for m in engine.get_mod_tiers("Wands", "desecrated") if m.boss_tag == "amanamu"]
    ev_boss = engine.evaluate_action(st, {amanamu[0].mod_id}, "Ancient Jawbone", ["Omen of the Liege"])
    assert ev_boss.weight_confidence == WeightConfidence.EXACT_DETERMINISTIC
    assert ev_boss.hit_probability == pytest.approx(min(1.0, 3.0 / len(amanamu)), rel=1e-6)

    # 2. Empirical estimate (bare desecrated default 2500 without boss omen)
    ev_bare = engine.evaluate_action(st, {amanamu[0].mod_id}, "Ancient Jawbone")
    assert ev_bare.weight_confidence == WeightConfidence.EMPIRICAL_ESTIMATE
    assert amanamu[0].weight == 2500

    # 3. Unmeasured (Charms / Waystones)
    charm = engine.resolve_base("Charms")
    charm_mods = engine.get_mod_tiers("Charms", "normal")
    assert charm_mods
    st_c = CraftingState(base=charm, ilvl=82, rarity=ItemRarity.NORMAL)
    ev_c = engine.evaluate_action(st_c, {charm_mods[0].mod_id}, "Orb of Transmutation")
    assert ev_c.weight_confidence == WeightConfidence.UNMEASURED

    # 4. Dynamic elemental lock tags (Withered Wand blocks Lightning Spell Skills)
    withered = engine.resolve_base("Withered Wand")
    lt_t = engine.resolve_target("Wands", "GlobalLightningSpellGemsLevelWeapon5", ilvl=82)
    ch_t = engine.resolve_target("Wands", "GlobalChaosSpellGemsLevelWeapon5", ilvl=82)
    st_w = CraftingState(base=withered, ilvl=82, rarity=ItemRarity.RARE)
    assert engine.evaluate_action(st_w, set(lt_t.matching_mod_ids), "Exalted Orb").hit_probability == 0.0
    assert engine.evaluate_action(st_w, set(ch_t.matching_mod_ids), "Exalted Orb").hit_probability > 0.0

    # 5. Exact 3-choice unveil math
    uniform = [(m, 1000) for m in amanamu]
    assert exact_three_choice_unveil_prob(uniform, {amanamu[0].mod_id}) == pytest.approx(
        min(1.0, 3.0 / len(amanamu)), rel=1e-6
    )


# ---------------------------------------------------------------------------
# Constraint 6: Anti-Poisoning PriceBook (GGG CX + poe.ninja + No Zero Prices)
# ---------------------------------------------------------------------------


def test_price_book_anti_poisoning_and_feed_parsers(engine: ModPoolEngine) -> None:
    # Refuses 0 Ex pricing when fallback disabled
    pb_strict = PriceBook.build({}, use_fallback_prices=False)
    whittle = engine.resolve_lever("Omen of Whittling")
    with pytest.raises(ValueError, match="refusing to price unknown currencies at 0"):
        pb_strict.action_cost(whittle, (), use_category_fallback=False)

    # Thin-volume dust quote (<0.08 Div) gets clamped to anti-poisoning floor
    pb_clamped = PriceBook.build(
        {"Omen of the Liege": 0.1, "Chaos Orb": 12.0, "Greater Chaos Orb": 5.0},
        volumes_div={"Omen of the Liege": 0.01, "Greater Chaos Orb": 0.05},
        use_fallback_prices=True,
    )
    liege = engine.resolve_lever("Omen of the Liege")
    g_chaos = engine.resolve_lever("Greater Chaos Orb")
    assert (pb_clamped.price_of(liege) or 0.0) >= 25.0
    assert (pb_clamped.price_of(g_chaos) or 0.0) >= 12.0  # monotonicity enforced

    # Official GGG CX API snapshot parser
    cx_payload = {
        "markets": [
            {
                "league": "Standard",
                "market_id": "divine|exalted",
                "volume_traded": {"divine": 100, "exalted": 42000},
            },
            {
                "league": "Standard",
                "market_id": "chaos|exalted",
                "volume_traded": {"chaos": 1000, "exalted": 14000},
            },
        ]
    }
    p_cx, v_cx = parse_ggg_cx_snapshot(cx_payload, league="Standard")
    assert p_cx["divine_orb"] == pytest.approx(420.0)
    assert p_cx["chaos_orb"] == pytest.approx(14.0)
    assert v_cx["chaos_orb"] > 0

    # poe.ninja/poe2 overview parser
    ninja_payload = {
        "core": {"rates": {"exalted": 400.0}},
        "items": [{"id": "omen-of-whittling", "name": "Omen of Whittling"}],
        "lines": [{"id": "omen-of-whittling", "primaryValue": 0.35, "volumePrimaryValue": 12.5}],
    }
    p_n, v_n = parse_poeninja_exchange_payload(ninja_payload)
    assert p_n["omen_of_whittling"] == pytest.approx(140.0)
    assert v_n["omen_of_whittling"] == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# Constraint 2: Clean-Room MDP Solver, Concrete Replay, Item Inspector & CLI
# ---------------------------------------------------------------------------


def test_mdp_solver_replay_protect_existing_exclude_and_item_inspector(
    engine: ModPoolEngine, solver: CraftMdpSolver
) -> None:
    plan = solver.plan_craft(
        base="Attuned Wand",
        ilvl=82,
        target_mods=["T2 Spell Damage", "T2 Cast Speed"],
        base_cost_ex=2.0,
        buy_price_ex=10000.0,
        n_sims=300,
        seed=42,
    )
    assert plan.mdp_states_solved >= 5
    assert plan.mdp_iterations >= 1
    assert plan.expected_cost_ex > 2.0
    assert plan.p50_cost_ex <= plan.p80_cost_ex <= plan.p95_cost_ex
    assert len(plan.policy_steps) >= 1
    assert plan.recommendation == "craft"

    # Buy recommendation triggers when market price is far cheaper than self-craft
    buy_plan = solver.plan_craft(
        base="Attuned Wand",
        ilvl=82,
        target_mods=["T1 Spell Damage", "T1 Cast Speed"],
        buy_price_ex=1.0,
        n_sims=100,
        seed=7,
    )
    assert buy_plan.recommendation == "buy"

    # --exclude blocks excluded currency from all recommendations
    ssf_plan = solver.plan_craft(
        base="Attuned Wand",
        ilvl=82,
        target_mods=["T2 Spell Damage"],
        excluded=["Perfect Orb of Transmutation", "Omen of Whittling"],
        n_sims=100,
        seed=7,
    )
    assert all(s.action != "Perfect Orb of Transmutation" for s in ssf_plan.policy_steps)

    # Trilingual item inspector + double-Crafted legality detector
    illegal_item_text = """
稀 有 度: 稀有
虚空魔杖
Attuned Wand
--------
物品等级: 82
--------
+3 to Level of all Spell Skills (crafted)
Gain 18% of Damage as Extra Physical Damage (crafted)
+(5-8) to Intelligence
"""
    rep = inspect_item_text(illegal_item_text, engine)
    assert rep.is_legal_0_5_5 is False
    assert rep.crafted_mod_count == 2
    assert any("Crafted" in iss for iss in rep.legality_issues)
    assert rep.whittling_preview is not None


def test_cli_craft_and_item_inspect_smoke() -> None:
    runner = CliRunner()
    sample_item = (
        "Rarity: RARE\nStorm Spire\nAttuned Wand\n--------\nItem Level: 82\n--------\n"
        "110% increased Spell Damage (fractured)\n+6 to Intelligence\n"
    )
    r_item = runner.invoke(cli_main, ["item", "inspect", "--text", sample_item, "--json"])
    assert r_item.exit_code == 0, r_item.output
    data = json.loads(r_item.output)
    assert data["game"] == "poe2"
    assert data["slots"]["openPrefixes"] == 2
    assert data["slots"]["openSuffixes"] == 2
    assert data["isLegal055"] is True

    r_plan = runner.invoke(
        cli_main,
        ["craft", "plan", "--base", "Attuned Wand", "--ilvl", "82", "--want", "T2 Spell Damage", "--sims", "80", "--json"],
    )
    assert r_plan.exit_code == 0, r_plan.output
    pdata = json.loads(r_plan.output)
    assert pdata["game"] == "poe2"
    assert pdata["weightConfidence"] == "verified_community"
    assert pdata["p50CostEx"] <= pdata["p80CostEx"] <= pdata["p95CostEx"]

    # Removed lever via CLI exits with code 1 (LegacyPatchTrapError)
    r_block = runner.invoke(
        cli_main,
        ["craft", "odds", "--base", "Attuned Wand", "--want", "T1 Spell Damage", "--lever", "Recombinator", "--json"],
    )
    assert r_block.exit_code == 1


def test_craft_inspect_base_only_standalone() -> None:
    runner = CliRunner()
    r = runner.invoke(cli_main, ["craft", "inspect", "--base", "Desolate Crossbow", "--ilvl", "77", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] is True
    assert data["mode"] == "base_pool"
    assert data["baseType"] == "Desolate Crossbow"
    assert data["ilvl"] == 77
    assert data["totalPoolWeight"] > 0
    assert data["familyCount"] > 0
    assert isinstance(data["outcomes"], list) and len(data["outcomes"]) > 0


def test_craft_inspect_base_only_non_tty_open_stdin_does_not_hang() -> None:
    """Regression: non-TTY subprocess with genuinely open `os.pipe()` stdin must not deadlock on `craft inspect --base`."""
    src_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_root)
    env["POB2_OFFLINE"] = "1"
    r_fd, w_fd = os.pipe()
    try:
        # Keep w_fd open in parent so any sys.stdin.read() in child blocks forever until timeout=10.
        proc = subprocess.run(
            [sys.executable, "-m", "pob2_cli.cli", "craft", "inspect", "--base", "Desolate Crossbow", "--ilvl", "77", "--json"],
            stdin=r_fd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=10,
        )
    finally:
        os.close(r_fd)
        os.close(w_fd)

    assert proc.returncode == 0, f"rc={proc.returncode}, stderr={proc.stderr!r}, stdout={proc.stdout[:400]!r}"
    assert proc.stderr == ""
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["mode"] == "base_pool"
    assert data["baseType"] == "Desolate Crossbow"
    assert data["ilvl"] == 77


def test_craft_inspect_file_dash_reads_stdin() -> None:
    """Explicit `--file -` reads item text from stdin even when combined with `--base` override."""
    runner = CliRunner()
    sample = (
        "Rarity: RARE\nStorm Spire\nAttuned Wand\n--------\nItem Level: 82\n--------\n"
        "110% increased Spell Damage (fractured)\n+6 to Intelligence\n"
    )
    r = runner.invoke(
        cli_main,
        ["craft", "inspect", "--file", "-", "--base", "Attuned Wand", "--json"],
        input=sample,
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] is True
    assert data["baseType"] == "Attuned Wand"
    assert data["slots"]["openPrefixes"] == 2


def test_craft_target_migration_json_zero_stderr() -> None:
    runner = CliRunner()
    r = runner.invoke(cli_main, ["craft", "odds", "--base", "Desolate Crossbow", "--target", "main", "--json"])
    assert r.exit_code == 1
    assert r.stderr == ""
    err = json.loads(r.stdout)
    assert err["errorKind"] == "cli_migration"
    assert err["code"] == "POE2-CLI-MIGRATION"
