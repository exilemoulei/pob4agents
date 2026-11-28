# SPDX-License-Identifier: GPL-3.0-only
"""Round 1 live acceptance regression tests for Craft, Item Inspector, Rune/Instill, and Rebalance."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob2_cli import bridge_protocol, updater
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.cli import main as cli_main
from pob2_cli.craft import (
    AffixSide,
    ModPoolEngine,
    UnknownBaseError,
    UnknownTargetError,
    inspect_item_text,
)
from pob2_cli.craft.item_inspector import _extract_values, _pick_best_entry
from pob2_cli.craft.weights_loader import strip_numbers_to_pattern
from pob2_cli.errors import CrossGameHallucinationError
from pob2_cli.rebalance import (
    SpiritOption,
    analyze_live_net_gap,
    solve_deficit_knapsack,
    solve_fix_gaps_live,
    solve_live_spirit_deficit,
    stat_snapshot_from_pob_stats,
)
from pob2_cli.rune import (
    apply_anoint_to_amulet_raw,
    apply_runes_to_item_raw,
    extract_existing_anoint,
    normalize_emotion_name,
    rank_instillations,
    strip_anoint_from_amulet_raw,
)

# Exact Round 1 Gate 1/2 repro texts (no local paths)
_RARE_ES_BODY = """Rarity: RARE
Doom Shelter
Vile Robe
Quality: 20
Energy Shield: 943
Item Level: 82
LevelReq: 65
148% increased Energy Shield
+96 to maximum Life
+41% to Fire Resistance
+38% to Cold Resistance
+31% to Lightning Resistance
+22% to Chaos Resistance
"""

_RARE_CN_GLOVES = """稀有度: 稀有
梦魇裹手
符文师匠万用裹手
品质: 20
能量护盾: 228
物品等级: 82
需求等级: 65
能量护盾提高 112%
44 生命上限
24 智慧
混沌抗性 22%
"""

_RARE_CN_ENBASE = """稀有度: 稀有
梦魇庇护
Vile Robe
物品等级: 82
需求等级: 65
能量护盾提高 148%
96 生命上限
"""

_MAGIC_WAND = """Rarity: MAGIC
Runed Apprentice Wand of the Storm
Attuned Wand
Quality: 12
Item Level: 82
LevelReq: 65
Gain 8% of Damage as Extra Lightning Damage
+26 to Intelligence
"""

_MAGIC_CHARM = """Rarity: MAGIC
Analyst's Grounding Charm of the Foliage
"""

_SEMI_BODY = """Rarity: RARE
Half Made Vestment
Vile Robe
Quality: 20
Energy Shield: 612
Item Level: 82
LevelReq: 65
{fractured}112% increased Energy Shield
+82 to maximum Life
+38% to Fire Resistance
"""

_POE1_CONTAMINATED = """Rarity: RARE
Sol Breath
Viridian Jewel
Item Level: 84
+12% to Fire Resistance
14% increased Attack Speed
Corrupted
"""


# ---------------------------------------------------------------------------
# P0-2: Sectionless item header-swallowing, full pool midpoint audit, composite/out-of-range & legality
# ---------------------------------------------------------------------------


def test_p0_sectionless_items_never_swallow_affixes_and_cli_parity(tmp_path: Path) -> None:
    runner = CliRunner()
    for name, text in (
        ("rare-es-body.txt", _RARE_ES_BODY),
        ("rare-cn-gloves.txt", _RARE_CN_GLOVES),
        ("rare-cn-enbase.txt", _RARE_CN_ENBASE),
        ("magic-wand.txt", _MAGIC_WAND),
        ("magic-charm-real.txt", _MAGIC_CHARM),
        ("semi-body.txt", _SEMI_BODY),
    ):
        fp = tmp_path / name
        fp.write_text(text, encoding="utf-8")
        r_it_txt = runner.invoke(cli_main, ["item", "inspect", "--text", text, "--json"])
        r_it_file = runner.invoke(cli_main, ["item", "inspect", "--file", str(fp), "--json"])
        r_cr_txt = runner.invoke(cli_main, ["craft", "inspect", "--text", text, "--json"])
        r_cr_file = runner.invoke(cli_main, ["craft", "inspect", "--file", str(fp), "--json"])
        assert r_it_txt.exit_code == 0, f"{name}: {r_it_txt.output}"
        assert r_it_txt.output == r_it_file.output == r_cr_txt.output == r_cr_file.output

    # rare-es-body: 2 prefixes (148% ES + 96 Life) + 4 resolved resistance suffixes -> illegal 4/3 suffixes + out-of-range warning
    rep_es = inspect_item_text(_RARE_ES_BODY)
    assert rep_es.base_slug == "Body_Armours_int"
    assert len(rep_es.prefixes) == 2
    assert len(rep_es.suffixes) == 4
    assert all(m.resolved for m in (*rep_es.prefixes, *rep_es.suffixes))
    assert rep_es.is_legal_0_5_5 is False
    assert any("110" in w and "exceeds single-mod max" in w for w in rep_es.warnings)

    # semi-body: fractured 112% ES + 82 Life + 38% Fire Res -> 2P / 1S, legal, out-of-range warning for 112 > 110
    rep_semi = inspect_item_text(_SEMI_BODY)
    assert len(rep_semi.prefixes) == 2 and len(rep_semi.suffixes) == 1
    assert rep_semi.prefixes[0].fractured is True
    assert rep_semi.is_legal_0_5_5 is True
    assert any("110" in w for w in rep_semi.warnings)

    # magic-wand & magic-charm
    rep_mw = inspect_item_text(_MAGIC_WAND)
    assert rep_mw.base_type == "Attuned Wand" and rep_mw.base_slug == "Wands"
    assert len(rep_mw.prefixes) == 1 and len(rep_mw.suffixes) == 1
    assert rep_mw.prefixes[0].resolved and rep_mw.suffixes[0].resolved
    assert rep_mw.is_legal_0_5_5 is True

    rep_mc = inspect_item_text(_MAGIC_CHARM)
    assert rep_mc.base_type == "Grounding Charm" and rep_mc.base_slug == "Charms"
    assert rep_mc.is_legal_0_5_5 is True

    # Valid 6-mod rare + 1 extra unresolved line must NOT falsely accuse suffix overflow (`isLegal055` counts resolved only!)
    six_plus_unres = """Rarity: RARE
Test Vestment
Vile Robe
Item Level: 82
105% increased Energy Shield
+90 to maximum Energy Shield
+95 to maximum Life
+41% to Fire Resistance
+38% to Cold Resistance
+31% to Lightning Resistance
Bananas Per Second increased by 42%
"""
    rep_unres = inspect_item_text(six_plus_unres)
    assert rep_unres.is_legal_0_5_5 is True
    assert any("[unrecognized]" in w for w in rep_unres.warnings)


@pytest.mark.parametrize(
    "slug",
    ["Body_Armours_int", "Rings", "Amulets", "Boots_str", "Crossbows", "Quarterstaves"],
)
def test_p0_all_pool_tiers_midpoint_roundtrip_resolution(slug: str) -> None:
    eng = ModPoolEngine()
    tiers = [m for m in eng.get_mod_tiers(slug) if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX)]
    assert len(tiers) >= 50
    pat_map: dict[str, list] = {}
    for m in tiers:
        pat_map.setdefault(strip_numbers_to_pattern(m.stat_text), []).append(m)

    def _mid_repl(match: re.Match[str]) -> str:
        a, b = float(match.group(1)), float(match.group(2))
        mid = (a + b) / 2.0
        if a.is_integer() and b.is_integer():
            return str(round(mid))
        return f"{mid:.2f}".rstrip("0").rstrip(".")

    resolved_count = 0
    for m in tiers:
        inst = re.sub(r"\(([+-]?\d+(?:\.\d+)?)\s*-\s*([+-]?\d+(?:\.\d+)?)\)", _mid_repl, m.stat_text)
        cands = pat_map.get(strip_numbers_to_pattern(inst), [])
        picked = _pick_best_entry(
            cands,
            ilvl=max(82, m.required_level),
            side_hint=None,
            tier_hint=None,
            name_hint=None,
            is_crafted=m.is_crafted_pool,
            is_desecrated=(m.source_pool == "desecrated"),
            vals=_extract_values(inst),
            catalyst_scalar=1.0,
        )
        if picked is not None:
            resolved_count += 1
    assert resolved_count / len(tiers) >= 0.995


# ---------------------------------------------------------------------------
# P1-1 / P1-2 / P1-3 / P1-4: 1768-base whitelist, CN/TW base & affix reverse-lookup, PoE1 block, 0 tracebacks
# ---------------------------------------------------------------------------


def test_p1_base_whitelist_poe1_block_garbage_no_traceback_and_cn_affixes() -> None:
    eng = ModPoolEngine()
    runner = CliRunner()

    # Garbage base NEVER matches `garb` substring -> UnknownBaseError with Top-5 suggestions, rc=1, 0 tracebacks
    with pytest.raises(UnknownBaseError) as exc_info:
        eng.resolve_base("garbage")
    assert len(exc_info.value.suggestions) == 5

    r_garb = runner.invoke(cli_main, ["item", "inspect", "--text", "garbage"])
    assert r_garb.exit_code == 1
    assert "[POE2-UNKNOWN-BASE]" in r_garb.output
    assert "Traceback" not in r_garb.output

    # PoE1 bases MUST raise CrossGameHallucinationError ([POE2-CROSS-GAME-BLOCK], rc=1)
    for poe1_base in ("Viridian Jewel", "Vaal Regalia", "Mageblood", "Astral Plate", "Stygian Vise", "Cluster Jewel"):
        with pytest.raises(CrossGameHallucinationError):
            eng.resolve_base(poe1_base)

    r_poe1_item = runner.invoke(cli_main, ["item", "inspect", "--text", _POE1_CONTAMINATED])
    assert r_poe1_item.exit_code == 1
    assert "[POE2-CROSS-GAME-BLOCK]" in r_poe1_item.output
    assert "Traceback" not in r_poe1_item.output

    r_poe1_odds = runner.invoke(cli_main, ["craft", "odds", "--base", "Viridian Jewel", "--want", "T1 Life"])
    assert r_poe1_odds.exit_code == 1
    assert "[POE2-CROSS-GAME-BLOCK]" in r_poe1_odds.output
    assert "Traceback" not in r_poe1_odds.output

    # CN base + CN affixes work offline (even without POB2_DATA_DIR!)
    rep_cn_g = inspect_item_text(_RARE_CN_GLOVES)
    assert rep_cn_g.base_type == "Runemastered Utility Wraps"
    assert rep_cn_g.base_slug == "Gloves_dex_int"
    assert sum(1 for m in (*rep_cn_g.prefixes, *rep_cn_g.suffixes) if m.resolved) == 3
    assert any("[not-in-base-pool]" in w for w in rep_cn_g.warnings)

    rep_cn_b = inspect_item_text(_RARE_CN_ENBASE)
    assert rep_cn_b.base_type == "Vile Robe" and rep_cn_b.base_slug == "Body_Armours_int"
    assert len(rep_cn_b.prefixes) == 2 and all(m.resolved for m in rep_cn_b.prefixes)
    assert any("110" in w for w in rep_cn_b.warnings)

    # Unrecognized Chinese line MUST emit warning (never silent empty prefixes/suffixes/warnings!)
    unrec_cn = "稀有度: 稀有\n测试袍\nVile Robe\n物品等级: 82\n神秘未知中文词条 99%\n"
    rep_u = inspect_item_text(unrec_cn)
    assert any("[unrecognized]" in w for w in rep_u.warnings)


# ---------------------------------------------------------------------------
# P2-6 / P2-7 / P2-9: 3 live emotion tiers, CLI help polish, target suggestions & Implicits: N
# ---------------------------------------------------------------------------


def test_p2_emotion_three_tiers_implicits_parity_and_target_suggestions() -> None:
    runner = CliRunner()
    # All 3 live 0.5.5 tier prefixes accepted for amulet emotions
    for raw_em, expected_short in (
        ("Diluted Liquid Greed", "Greed"),
        ("Liquid Greed", "Greed"),
        ("Concentrated Liquid Greed", "Greed"),
        ("Diluted Liquid Ire", "Ire"),
        ("Liquid Ire", "Ire"),
        ("Concentrated Liquid Ire", "Ire"),
        ("Diluted Liquid Paranoia", "Paranoia"),
        ("Liquid Paranoia", "Paranoia"),
        ("Concentrated Liquid Paranoia", "Paranoia"),
        ("Distilled Envy", "Envy"),
        ("Envy", "Envy"),
    ):
        assert normalize_emotion_name(raw_em).short == expected_short

    r_em = runner.invoke(
        cli_main,
        ["instill", "lookup", "--emotions", "Diluted Liquid Greed,Liquid Ire,Concentrated Liquid Envy", "--json"],
    )
    assert r_em.exit_code == 0, r_em.output
    assert json.loads(r_em.output)["instills"][0]["name"] == "Initiative"

    r_br = runner.invoke(cli_main, ["instill", "bridge", "Concentrated Liquid Greed", "--json"])
    assert r_br.exit_code == 0 and json.loads(r_br.output)["short"] == "Greed"

    # Help polish checks
    r_rh = runner.invoke(cli_main, ["rune", "rank", "--help"])
    assert r_rh.exit_code == 0 and "ITEM_TYPE" in r_rh.output
    r_ch = runner.invoke(cli_main, ["craft", "odds", "--help"])
    assert r_ch.exit_code == 0 and "T1 Spell Damage" in r_ch.output

    # Bad --want exits rc=1 with Top-6 valid suggestions and 0 tracebacks
    eng = ModPoolEngine()
    with pytest.raises(UnknownTargetError) as exc_t:
        eng.resolve_target("Body_Armours_int", "nonexistent_mod_xyz")
    assert len(exc_t.value.suggestions) == 6
    r_bt = runner.invoke(cli_main, ["craft", "odds", "--base", "Vile Robe", "--want", "nonexistent_mod_xyz"])
    assert r_bt.exit_code == 1
    assert "[POE2-UNKNOWN-TARGET]" in r_bt.output
    assert "Traceback" not in r_bt.output

    # Implicits: N increment vs preserve vs strip on Amulets & Runes
    am_no_anoint = "Rarity: RARE\nTest Am\nSolar Amulet\nImplicits: 1\n+15 to Spirit\n+80 to maximum Life"
    am_with = apply_anoint_to_amulet_raw(am_no_anoint, "Initiative")
    assert "Implicits: 2" in am_with and "{enchant}Allocates Initiative" in am_with and "+15 to Spirit" in am_with
    am_replaced = apply_anoint_to_amulet_raw(am_with, "Wild Storm")
    assert "Implicits: 2" in am_replaced and "Initiative" not in am_replaced and "{enchant}Allocates Wild Storm" in am_replaced
    assert extract_existing_anoint(am_replaced) == "Wild Storm"
    am_stripped, old_name = strip_anoint_from_amulet_raw(am_replaced)
    assert old_name == "Wild Storm" and "Implicits: 1" in am_stripped and "Allocates" not in am_stripped

    # Runes Implicits: N when missing header + Corrupted stays at end
    wand_no_imp = "Rarity: RARE\nW\nAttuned Wand\nSockets: S\n50% increased Spell Damage\nCorrupted"
    wand_r = apply_runes_to_item_raw(wand_no_imp, ["Desert Rune"], item_type="Wand", char_level=90)
    assert wand_r.splitlines()[-1] == "Corrupted" and "Implicits: 1" in wand_r


# ---------------------------------------------------------------------------
# P2-3 & P2-4 offline checks: Spirit overshoot avoidance & PoB2 MaxRes derivation
# ---------------------------------------------------------------------------


def test_p2_spirit_knapsack_avoids_overshoot_and_net_gap_maxres_derivation() -> None:
    opts = [
        SpiritOption("big", "g_big", "aura_toggle", "Big 100", 100, 0.0, 0.0, loss_score=0.005),
        SpiritOption("a1", "g_a1", "aura_toggle", "Small 30 A", 30, 0.0, 0.0, loss_score=0.005),
        SpiritOption("a2", "g_a2", "aura_toggle", "Small 30 B", 30, 0.0, 0.0, loss_score=0.005),
    ]
    chosen, freed = solve_deficit_knapsack(opts, deficit=60)
    assert freed == 60
    assert {o.option_id for o in chosen} == {"a1", "a2"}

    # PoB2 get_stats omits FireResistMax; must derive 90% from FireResistOver75 / MissingFireResist
    snap90 = stat_snapshot_from_pob_stats(
        {
            "FireResist": 90, "FireResistOver75": 15, "FireResistOverCap": 1, "FireResistTotal": 91,
            "MissingFireResist": 0, "ColdResist": 90, "ColdResistOver75": 15, "ColdResistOverCap": 23,
            "LightningResist": 90, "LightningResistOver75": 15, "LightningResistOverCap": 22,
            "ChaosResist": 0, "MissingChaosResist": 75,
        }
    )
    assert snap90.fire_max == 90.0 and snap90.fire_total == 91.0
    assert snap90.cold_max == 90.0 and snap90.lightning_max == 90.0 and snap90.chaos_max == 75.0


# ---------------------------------------------------------------------------
# Live PoB2 seed build checks (尖刺最终版.xml & long.xml when POB2_ENGINE_DIR is set)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native lua51.dll live PoB2 test")
def test_live_seed_builds_instill_speed_accuracy_spirit_and_net_gap(tmp_path: Path) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live seed build tests.")

    spike_xml = eng / "Builds" / "尖刺最终版.xml"
    long_xml = eng / "Builds" / "long.xml"
    if not spike_xml.is_file():
        pytest.skip("Seed build XML not present in POB2_ENGINE_DIR/Builds.")

    worker = BridgeWorker(slot_name="r1_craft", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        worker.call("import_code", {"code": bridge_protocol.encode_xml_share_code(spike_xml.read_text(encoding="utf-8")), "mode": "replace"})

        # 1. rank-instill --budget 20 < 2.5s and finds Initiative (克敌在先) with both gainVsEquipped & gainVsUnanointed
        t0 = time.perf_counter()
        irep = rank_instillations(metric="CombinedDPS", max_budget_ex=20.0, limit=5, worker=worker)
        inst_sec = time.perf_counter() - t0
        assert inst_sec < 2.5, f"rank_instill(--budget 20) took {inst_sec:.2f}s (limit 2.5s)"
        assert irep.bis_anchor is not None
        assert irep.bis_anchor.name == "Initiative" and irep.bis_anchor.zh_cn == "克敌在先"
        assert irep.bis_anchor.details["existingAnoint"] == "Zarokh's Gift"
        assert irep.bis_anchor.details["gainVsUnanointed"] > 10000.0
        assert "gainVsEquipped" in irep.bis_anchor.details

        # 2. solve_live_spirit_deficit < 3.0s on 尖刺最终版.xml
        t0 = time.perf_counter()
        sp = solve_live_spirit_deficit(worker)
        sp_sec = time.perf_counter() - t0
        assert sp_sec < 3.0, f"solve_live_spirit_deficit took {sp_sec:.2f}s (limit 3.0s)"
        assert sp.achieved_spirit >= sp.target_spirit and sp.remaining_deficit == 0

        # 3. net_gap & fix-gaps on 尖刺最终版.xml: 90% MaxRes, currentInGame vs withoutConditionalAndCharm + notes
        ng = analyze_live_net_gap(worker)
        assert ng.current_in_game is not None
        assert ng.current.fire_max == 90.0 and ng.bare_net.fire_max == 90.0
        fg = solve_fix_gaps_live(worker, overcap_target=20, chaos_target=0, apply=False)
        fgd = fg.to_dict()
        assert "currentInGame" in fgd and "withoutConditionalAndCharm" in fgd and "snapshotNotes" in fgd
        assert fgd["cowRollbackVerified"] is True

        # 4. long.xml Spirit deficit < 3.0s & consistent deltaDps (0 phantom positive gain on unrelated buffs!)
        if long_xml.is_file():
            worker.call("import_code", {"code": bridge_protocol.encode_xml_share_code(long_xml.read_text(encoding="utf-8")), "mode": "replace"})
            t0 = time.perf_counter()
            sp_l = solve_live_spirit_deficit(worker)
            sp_l_sec = time.perf_counter() - t0
            assert sp_l_sec < 3.0, f"long.xml solve_live_spirit_deficit took {sp_l_sec:.2f}s (limit 3.0s)"
            assert all(o.delta_dps <= 1e-6 for o in sp_l.all_evaluated)
            assert sp_l.achieved_spirit >= sp_l.target_spirit
    finally:
        worker.stop()
