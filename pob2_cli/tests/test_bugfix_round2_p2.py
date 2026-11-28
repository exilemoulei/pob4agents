# SPDX-License-Identifier: GPL-3.0-only
"""Round 2 Gate 1 regression tests for P2-a (`--slot` pre-validation) & P2-b (`craft inspect` latency/lazy-l10n)."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob2_cli import daemon
from pob2_cli.cli import main as cli_main
from pob2_cli.comparator import normalize_slot_arg
from pob2_cli.craft import UnknownBaseError, inspect_item_text, load_craft_data
from pob2_cli.craft.en_bases import load_en_base_whitelist
from pob2_cli.craft.item_inspector import _default_engine
from pob2_cli.craft.zh_lookup import CraftL10nCatalog, load_craft_l10n
from pob2_cli.errors import CrossGameHallucinationError, DaemonRequestError
from pob2_cli.guardrails import VALID_POE2_EQUIPMENT_SLOTS, normalize_poe2_equipment_slot

_RARE_HELMET_EN = """Rarity: RARE
Doom Crown
Kamasan Tiara
Quality: 20
Energy Shield: 310
Item Level: 82
LevelReq: 65
98% increased Energy Shield
+85 to maximum Life
+38% to Fire Resistance
+35% to Cold Resistance
+23% to Chaos Resistance
"""

_RARE_ES_BODY_EN = """Rarity: RARE
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

_RARE_TW_ROBE = """稀有度: 稀有
虛空庇護
Vile Robe
物品等級: 82
能量護盾提高 102%
+88 最大生命
火焰抗性 +39%
"""

_POE1_VIRIDIAN_JEWEL = """Rarity: RARE
Sol Breath
Viridian Jewel
Item Level: 84
+12% to Fire Resistance
14% increased Attack Speed
Corrupted
"""


# ---------------------------------------------------------------------------
# P2-a: Validate `--slot` against PoE2 equipment whitelist BEFORE build-loaded / daemon check
# ---------------------------------------------------------------------------


def test_p2a_uniques_bad_slot_rejected_before_build_loaded_without_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt_dir = tmp_path / "isolated_rt"
    rt_dir.mkdir()
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(rt_dir))
    monkeypatch.setenv("POB2_OFFLINE", "1")

    runner = CliRunner()

    # X05 exact repro: `pob2 item db --slot "Ring 9" --json` with NO build loaded & NO daemon running
    res = runner.invoke(cli_main, ["item", "db", "--slot", "Ring 9", "--json"])
    assert res.exit_code == 1
    assert "[POE2-INVALID-SLOT]" in res.output
    assert "Ring 9" in res.output
    assert "Valid slots:" in res.output
    assert "Ring 1" in res.output and "Charm 1" in res.output
    assert "has no build loaded" not in res.output
    assert "Traceback" not in res.output
    assert res.stderr == ""
    err_json = json.loads(res.stdout)
    assert err_json["ok"] is False and err_json["game"] == "poe2"
    assert err_json["code"] == "POE2-INVALID-SLOT"
    assert not (rt_dir / "daemon_poe2.json").exists(), "Invalid --slot must fail before spawning daemon!"

    # PoE1 Flask 3..5 still raises CrossGameHallucinationError before daemon
    res_f5 = runner.invoke(cli_main, ["item", "db", "--slot", "Flask 5", "--json"])
    assert res_f5.exit_code == 1
    assert res_f5.stderr == ""
    assert json.loads(res_f5.stdout)["code"] == "POE2-CROSS-GAME-BLOCK"
    assert "has no build loaded" not in res_f5.output
    assert not (rt_dir / "daemon_poe2.json").exists()

    # Other `--slot` / `--unequip` CLI commands share identical pre-validation before daemon contact
    for argv in (
        ["what-if", "equip", "--slot", "Ring 9", "--item-text", _RARE_HELMET_EN, "--json"],
        ["what-if", "equip", "--unequip", "Ring 9", "--json"],
        ["items", "--slot", "Ring 9", "--json"],
        ["rune", "rank", "--slot", "Ring 9", "--json"],
    ):
        r_cmd = runner.invoke(cli_main, argv)
        assert r_cmd.exit_code == 1, f"{argv}: {r_cmd.output}"
        assert "[POE2-INVALID-SLOT]" in r_cmd.output, f"{argv}: {r_cmd.output}"
        assert "has no build loaded" not in r_cmd.output
        assert "Traceback" not in r_cmd.output
        assert not (rt_dir / "daemon_poe2.json").exists()


def test_p2a_slot_whitelist_aliases_and_daemon_rpc_ordering(tmp_path: Path) -> None:
    for canon in VALID_POE2_EQUIPMENT_SLOTS:
        assert normalize_poe2_equipment_slot(canon) == canon
        assert normalize_slot_arg(canon.lower()) == canon

    assert normalize_poe2_equipment_slot("ring1") == "Ring 1"
    assert normalize_poe2_equipment_slot("ring3") == "Ring 3"
    assert normalize_poe2_equipment_slot("weapon1swap") == "Weapon 1 Swap"
    assert normalize_poe2_equipment_slot("body_armour") == "Body Armour"
    assert normalize_poe2_equipment_slot("Jewel 26725") == "Jewel 26725"
    assert normalize_poe2_equipment_slot("Weapon 1 Jewel Socket 2") == "Weapon 1 Jewel Socket 2"
    assert normalize_poe2_equipment_slot("戒指 2") == "Ring 2"
    assert normalize_poe2_equipment_slot("咒符 3") == "Charm 3"

    for bad in ("Ring 9", "Boots 2", "Charm 4", "Jewel 9", "NotASlot"):
        with pytest.raises(DaemonRequestError, match=r"\[POE2-INVALID-SLOT\]"):
            normalize_slot_arg(bad)

    # Even direct DaemonManager.dispatch validates `slot` BEFORE `empty_slot_error`
    mgr = daemon.DaemonManager(
        pob_dir=tmp_path,
        token="tok",
        runtime_dir=tmp_path / "rt_mgr",
        offline=True,
        skip_update_check=True,
    )
    with pytest.raises(DaemonRequestError, match=r"\[POE2-INVALID-SLOT\]"):
        mgr.dispatch("uniques", {"target": "never-loaded", "slot": "Ring 9"})
    with pytest.raises(DaemonRequestError, match=r"\[POE2-INVALID-SLOT\]"):
        mgr.dispatch("what_if", {"target": "never-loaded", "slot": "Ring 9"})
    with pytest.raises(DaemonRequestError, match="Slot 'never-loaded' has no build loaded"):
        mgr.dispatch("uniques", {"target": "never-loaded", "slot": "Ring 1"})


# ---------------------------------------------------------------------------
# P2-b: Lazy l10n loading, per-slug lazy compilation, warm cache hits (<20ms), and 100% output parity
# ---------------------------------------------------------------------------


def test_p2b_english_inspect_skips_craft_l10n_and_compiles_single_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_craft_l10n.cache_clear()
    load_en_base_whitelist.cache_clear()
    load_craft_data.cache_clear()
    _default_engine.cache_clear()

    def _forbid_l10n_init(self: CraftL10nCatalog, data_dir: Path | None = None) -> None:
        pytest.fail("CraftL10nCatalog (poe2_craft_l10n.json.gz) MUST NOT load for non-CJK inputs!")

    monkeypatch.setattr(CraftL10nCatalog, "__init__", _forbid_l10n_init)

    # 1. English Rare helmet & Vile Robe do NOT trigger CraftL10nCatalog load
    rep_helm = inspect_item_text(_RARE_HELMET_EN)
    assert rep_helm.base_type == "Kamasan Tiara" and rep_helm.base_slug == "Helmets_int"
    assert rep_helm.is_legal_0_5_5 is True
    assert len(rep_helm.prefixes) == 2 and len(rep_helm.suffixes) == 3
    assert all(m.resolved for m in (*rep_helm.prefixes, *rep_helm.suffixes))

    # Only `Helmets_int` was compiled in LazyModsBySlug (1 out of 81 classes!)
    bundle = load_craft_data()
    assert dict.__len__(bundle.mods_by_slug) == 1
    assert len(bundle.mods_by_slug) == 81

    rep_body = inspect_item_text(_RARE_ES_BODY_EN)
    assert rep_body.base_type == "Vile Robe" and rep_body.base_slug == "Body_Armours_int"
    assert rep_body.is_legal_0_5_5 is False
    assert any("[out-of-range]" in w and "composite pure+hybrid" in w for w in rep_body.warnings)
    assert dict.__len__(bundle.mods_by_slug) == 2

    # 2. English Magic charm, PoE1 Viridian Jewel block, and garbage UnknownBaseError also skip CraftL10nCatalog
    rep_charm = inspect_item_text("Rarity: MAGIC\nAnalyst's Grounding Charm of the Foliage\n")
    assert rep_charm.base_type == "Grounding Charm" and rep_charm.base_slug == "Charms"

    with pytest.raises(CrossGameHallucinationError):
        inspect_item_text(_POE1_VIRIDIAN_JEWEL)

    with pytest.raises(UnknownBaseError) as exc_garb:
        inspect_item_text("garbage")
    assert "[POE2-UNKNOWN-BASE]" in str(exc_garb.value)
    assert len(exc_garb.value.suggestions) == 5

    assert load_craft_l10n.cache_info().currsize == 0
    assert load_en_base_whitelist.cache_info().hits >= 1
    assert load_craft_data.cache_info().hits >= 1


def test_p2b_chinese_inspect_parity_cache_hits_and_warm_latency() -> None:
    load_craft_l10n.cache_clear()
    load_en_base_whitelist.cache_clear()
    load_craft_data.cache_clear()
    _default_engine.cache_clear()

    # First Chinese Rare (`符文师匠万用裹手`) loads bundled l10n once without scanning 67k optional stats.json
    rep_cn = inspect_item_text(_RARE_CN_GLOVES)
    assert rep_cn.base_type == "Runemastered Utility Wraps"
    assert rep_cn.base_slug == "Gloves_dex_int"
    assert sum(1 for m in (*rep_cn.prefixes, *rep_cn.suffixes) if m.resolved) == 3
    assert any("[not-in-base-pool]" in w for w in rep_cn.warnings)
    assert load_craft_l10n.cache_info().currsize == 1
    cat = load_craft_l10n()
    assert cat._optional_stats_loaded is False

    # Traditional Chinese parity
    rep_tw = inspect_item_text(_RARE_TW_ROBE)
    assert rep_tw.base_type == "Vile Robe" and rep_tw.base_slug == "Body_Armours_int"
    assert len(rep_tw.prefixes) == 2 and len(rep_tw.suffixes) == 1
    assert all(m.resolved for m in (*rep_tw.prefixes, *rep_tw.suffixes))
    assert load_craft_l10n.cache_info().hits >= 1

    # Unrecognized mod warning parity (EN & ZH)
    rep_unrec = inspect_item_text("Rarity: RARE\nMystery\nVile Robe\nItem Level: 82\n99% increased Bananas\n")
    assert any("[unrecognized]" in w for w in rep_unrec.warnings)

    # Composite 2-line hybrid prefix detection parity on Vile Robe (`% increased ES` + `+X to maximum Mana` or Stun Threshold)
    hybrid_mod = next(
        m for m in load_craft_data().mods_by_slug["Body_Armours_int"] if "\n" in m.stat_text and m.source_pool == "normal"
    )
    inst_hybrid = re.sub(r"\(([+-]?\d+(?:\.\d+)?)\s*-\s*[+-]?\d+(?:\.\d+)?\)", r"\1", hybrid_mod.stat_text)
    rep_hyb = inspect_item_text(f"Rarity: RARE\nHybrid Check\nVile Robe\nItem Level: 85\n{inst_hybrid}\n")
    assert len(rep_hyb.prefixes) + len(rep_hyb.suffixes) == 1
    assert (rep_hyb.prefixes + rep_hyb.suffixes)[0].resolved is True

    # Warm in-process repeated calls MUST average well under < 20ms ( scaled by POB2_TEST_PERF_SLACK )
    slack = float(os.environ.get("POB2_TEST_PERF_SLACK", "1.0") or "1.0")
    t0_en = time.perf_counter()
    for _ in range(10):
        r_e = inspect_item_text(_RARE_ES_BODY_EN)
    avg_en_ms = (time.perf_counter() - t0_en) * 1000.0 / 10.0
    assert r_e.to_dict() == inspect_item_text(_RARE_ES_BODY_EN).to_dict()

    t0_zh = time.perf_counter()
    for _ in range(10):
        r_z = inspect_item_text(_RARE_CN_GLOVES)
    avg_zh_ms = (time.perf_counter() - t0_zh) * 1000.0 / 10.0
    assert r_z.to_dict() == rep_cn.to_dict()

    assert avg_en_ms < 20.0 * slack, f"Warm EN inspect_item_text avg {avg_en_ms:.2f}ms >= {20.0 * slack}ms"
    assert avg_zh_ms < 20.0 * slack, f"Warm ZH inspect_item_text avg {avg_zh_ms:.2f}ms >= {20.0 * slack}ms"

    # CLI JSON parity for English & Chinese rare + Viridian Jewel & garbage base
    runner = CliRunner()
    r_en_cli = runner.invoke(cli_main, ["craft", "inspect", "--text", _RARE_HELMET_EN, "--json"])
    assert r_en_cli.exit_code == 0 and json.loads(r_en_cli.output)["baseSlug"] == "Helmets_int"
    r_cn_cli = runner.invoke(cli_main, ["craft", "inspect", "--text", _RARE_CN_GLOVES, "--json"])
    assert r_cn_cli.exit_code == 0 and json.loads(r_cn_cli.output)["baseSlug"] == "Gloves_dex_int"

    r_vj = runner.invoke(cli_main, ["craft", "inspect", "--text", _POE1_VIRIDIAN_JEWEL, "--json"])
    assert r_vj.exit_code == 1 and r_vj.stderr == "" and json.loads(r_vj.stdout)["code"] == "POE2-CROSS-GAME-BLOCK" and "Traceback" not in r_vj.output

    r_gb = runner.invoke(cli_main, ["craft", "inspect", "--text", "garbage", "--json"])
    assert r_gb.exit_code == 1 and r_gb.stderr == "" and "[POE2-UNKNOWN-BASE]" in r_gb.output and "Traceback" not in r_gb.output
