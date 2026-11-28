# SPDX-License-Identifier: GPL-3.0-only
"""Round 3 regression suite covering findings N1–N7 (1 P1 + 2 P2 + 4 P3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pob2_cli import cli, cli_common, cli_inspect, cli_item, craft, daemon, importers, updater
from pob2_cli.analyzer import run_deep_audit
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.craft.models import CraftingState, ItemRarity, extract_mod_value_range
from pob2_cli.craft.pool_engine import ModPoolEngine
from pob2_cli.craft.pool_outcomes import compute_next_step_outcomes, render_outcomes_md
from pob2_cli.daemon.read_cache import LOOP_GUARD_WARNING, SlotReadCache
from pob2_cli.errors import BootstrapMissingError, CrossGameHallucinationError
from pob2_cli.guardrails import validate_url
from pob2_cli.importers.post_load_fixups import (
    apply_post_load_fixups,
    scan_res_override_lines_in_items,
)
from pob2_cli.importers.share_urls import load_build_into_worker, resolve_poe2_build_source
from pob2_cli.os_util import win_hidden_subprocess_kwargs
from pob2_cli.rebalance import (
    NetGapAudit,
    StatSnapshot,
    audit_equipped_slot,
    solve_fix_gaps_live,
    solve_fix_gaps_offline,
)

_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")

_SYNTH_BING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="92" targetVersion="0_1" className="Mercenary" ascendClassName="Gemling Legionnaire" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="9" ascendClassId="3" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1" useSecondWeaponSet="false">
    <Item id="1">
Rarity: UNIQUE
Crown of Eyes
Runemastered Vermeil Circlet
Item Level: 79
Sockets: S S
Rune: Greater Desert Rune
Rune: Greater Desert Rune
LevelReq: 55
Implicits: 1
{enchant}{rune}+36% to Fire Resistance
124% increased Energy Shield
+14 to all Attributes
Fire Resistance is -10%
Corrupted
    </Item>
    <Item id="2">
Rarity: RARE
Viper March
Sandsworn Sandals
Item Level: 81
Sockets: S
Rune: Greater Glacial Rune
LevelReq: 75
Implicits: 1
{enchant}{rune}+18% to Cold Resistance
100% increased Energy Shield
Cold Resistance is +37%
Lightning Resistance is +37%
{crafted}Fire Resistance is +32%
    </Item>
    <Item id="3">
Rarity: RARE
Normal Ruby Ring
Ruby Ring
Item Level: 82
Implicits: 1
+25% to Fire Resistance
+85 to maximum Life
+35% to Fire Resistance
+30% to Cold Resistance
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Helmet" itemId="1"/>
      <Slot name="Boots" itemId="2"/>
      <Slot name="Ring 1" itemId="3"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" mainActiveSkill="1">
        <Gem nameSpec="Mace Strike" level="20" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding2>
"""

_CLEAN_CONTROL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="85" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1" useSecondWeaponSet="false">
    <Item id="1">
Rarity: RARE
Safe Band
Ruby Ring
Item Level: 82
+85 to maximum Life
+35% to Fire Resistance
+31% to Cold Resistance
+28% to Lightning Resistance
+19% to Chaos Resistance
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Ring 1" itemId="1"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1"><SkillSet id="1"/></Skills>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding2>
"""


# ---------------------------------------------------------------------------
# N1 [P1]: Cold-start lazy imports (`import pob2_cli.cli` avoids heavy graph)
# ---------------------------------------------------------------------------


def test_n1_cli_import_does_not_eagerly_load_heavy_modules_and_meets_budget() -> None:
    env = os.environ.copy()
    prev = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = f"{_SRC_DIR}{os.pathsep}{prev}" if prev else _SRC_DIR

    probe_code = (
        "import json, sys, pob2_cli.cli, pob2_cli.cli_common, pob2_cli.daemon; "
        "forbidden = ["
        "'httpx', "
        "'pob2_cli.daemon.rpc_handlers', "
        "'pob2_cli.daemon.rpc_rebalance', "
        "'pob2_cli.rebalance', "
        "'pob2_cli.analyzer', "
        "'pob2_cli.importers.share_urls', "
        "'pob2_cli.updater.remote', "
        "'pob2_cli.updater.bootstrap'"
        "]; "
        "leaked = [m for m in forbidden if m in sys.modules]; "
        "print(json.dumps({'leaked': leaked, 'count': len(sys.modules)}))"
    )

    timings_ms: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-c", probe_code],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            **win_hidden_subprocess_kwargs(),
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout.strip())
        assert data["leaked"] == [], f"Eager heavy imports leaked into cold CLI import: {data['leaked']}"
        timings_ms.append(elapsed_ms)

    slack = float(os.environ.get("POB2_TEST_PERF_SLACK", "1.0") or "1.0")
    median_ms = sorted(timings_ms)[1]
    assert median_ms <= 300.0 * slack, f"Cold `import pob2_cli.cli` median {median_ms:.1f}ms exceeded budget"

    # Verify lazy `__all__` exports across daemon, updater, craft, and importers resolve cleanly
    for mod in (daemon, updater, craft, importers):
        for sym in mod.__all__:
            assert hasattr(mod, sym), f"{mod.__name__} missing lazy export {sym!r}"


# ---------------------------------------------------------------------------
# N2 [P2]: Unified `--json` error envelope across ALL CLI / Daemon / Usage errors
# ---------------------------------------------------------------------------


def test_n2_unified_json_error_envelope_across_all_error_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt_dir = tmp_path / "rt_n2"
    rt_dir.mkdir()
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(rt_dir))
    monkeypatch.setenv("POB2_OFFLINE", "1")
    runner = CliRunner()

    # 1. Invalid equipment slot (`rc=1`, `code="POE2-INVALID-SLOT"`, zero stderr on `--json`)
    r_slot = runner.invoke(cli.main, ["item", "db", "--slot", "Ring 9", "--json"])
    assert r_slot.exit_code == 1
    assert r_slot.stderr == ""
    d_slot = json.loads(r_slot.stdout)
    assert d_slot["ok"] is False and d_slot["game"] == "poe2"
    assert d_slot["errorKind"] == "invalid_slot"
    assert d_slot["code"] == "POE2-INVALID-SLOT"
    assert "\n" not in d_slot["error"]

    # 2. Self-compare (`rc=1`, `errorKind="same_slot"`, zero stderr on `--json`)
    r_cmp = runner.invoke(cli.main, ["compare", "main", "main", "--json", "--compact"])
    assert r_cmp.exit_code == 1
    assert r_cmp.stderr == ""
    assert "\n" not in r_cmp.stdout.strip()
    d_cmp = json.loads(r_cmp.stdout)
    assert d_cmp["ok"] is False and d_cmp["game"] == "poe2"
    assert d_cmp["errorKind"] == "same_slot"
    assert d_cmp["code"] == "POE2-SAME-SLOT"

    # 3. Click UsageError — BOTH trailing `--json` and root `--json`!
    for usage_argv in (
        ["craft", "odds", "--json"],
        ["--json", "craft", "odds"],
        ["summary", "--no-such-flag-xyz", "--json"],
        ["--json", "--compact", "unknown-subcmd-xyz"],
        ["item", "inspect", "--file", str(tmp_path / "does_not_exist.txt"), "--json"],
    ):
        r_use = runner.invoke(cli.main, usage_argv)
        assert r_use.exit_code == 1, f"{usage_argv}: {r_use.output}"
        assert r_use.stderr == ""
        d_use = json.loads(r_use.stdout)
        assert d_use["ok"] is False and d_use["game"] == "poe2"
        assert d_use["errorKind"] == "usage_error"
        assert d_use["code"] == "POE2-USAGE-ERROR"
        assert d_use["error"] and "\n" not in d_use["error"]

    # 4. Empty slot & missing file via daemon request errors (`rc=1`)
    def _mock_daemon_errors(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        p = params or {}
        if action == "summary":
            raise daemon.DaemonRequestError(
                f"Slot '{p.get('target')}' has no build loaded; run `pob2 load <source> --target {p.get('target')}` first."
            )
        if action == "load":
            raise daemon.DaemonRequestError(f"Build file not found: {p.get('source')}")
        if action == "status":
            raise BootstrapMissingError("No local PoB2 engine installed in runtime directory.")
        raise RuntimeError("simulated LuaJIT worker crash\nstack traceback:\n\tfoo.lua:12: in main")

    monkeypatch.setattr(cli_common, "daemon_call_cli", _mock_daemon_errors)
    monkeypatch.setattr(cli_inspect, "daemon_call_cli", _mock_daemon_errors)
    from pob2_cli import cli_build_io, cli_lifecycle

    monkeypatch.setattr(cli_build_io, "daemon_call_cli", _mock_daemon_errors)
    monkeypatch.setattr(cli_lifecycle, "daemon_call_cli", _mock_daemon_errors)

    r_empty = runner.invoke(cli.main, ["summary", "--target", "never-loaded", "--json"])
    assert r_empty.exit_code == 1
    assert r_empty.stderr == ""
    d_empty = json.loads(r_empty.stdout)
    assert d_empty["ok"] is False and d_empty["game"] == "poe2"
    assert d_empty["errorKind"] == "empty_slot" and d_empty["code"] == "POE2-EMPTY-SLOT"

    r_miss = runner.invoke(cli.main, ["load", "missing.xml", "--json"])
    assert r_miss.exit_code == 1
    d_miss = json.loads(r_miss.stdout)
    assert d_miss["ok"] is False and d_miss["game"] == "poe2"
    assert d_miss["errorKind"] == "file_not_found" and d_miss["code"] == "POE2-FILE-NOT-FOUND"

    r_boot = runner.invoke(cli.main, ["status", "--json"])
    assert r_boot.exit_code == 1
    d_boot = json.loads(r_boot.stdout)
    assert d_boot["ok"] is False and d_boot["errorKind"] == "bootstrap_missing"

    # 5. Daemon / unexpected fault (`rc=5`, `errorKind="daemon_error"`, Lua traceback sanitized to single line)
    r_fault = runner.invoke(cli.main, ["tree", "--json", "--compact"])
    assert r_fault.exit_code == 5
    assert r_fault.stderr == ""
    assert "\n" not in r_fault.stdout.strip()
    d_fault = json.loads(r_fault.stdout)
    assert d_fault["ok"] is False and d_fault["game"] == "poe2"
    assert d_fault["errorKind"] == "daemon_error"
    assert "stack traceback" not in d_fault["error"].lower()


# ---------------------------------------------------------------------------
# N3 [P2]: Per-tier numeric roll ranges in `craft odds --outcomes` & `--want` `matchedTiers`
# ---------------------------------------------------------------------------


def test_n3_craft_odds_outcomes_tiers_value_ranges_and_want_matched_tiers() -> None:
    assert extract_mod_value_range("+(85-96) to maximum Life") == [85, 96]
    assert extract_mod_value_range("-(4-6) Physical Damage taken from Attack Hits") == [-6, -4]
    assert extract_mod_value_range("Adds (15-22) to (31-45) Fire Damage") == [[15, 22], [31, 45]]
    assert extract_mod_value_range("(38-42)% increased Energy Shield\n+(29-33) to maximum Mana") == [
        [38, 42],
        [29, 33],
    ]
    assert extract_mod_value_range("+2 to Level of all Spell Skills") == [2, 2]
    assert extract_mod_value_range("Bears the Mark of the Abyssal Lord") is None

    eng = ModPoolEngine()
    robe = eng.resolve_base("Vile Robe")
    st_rare = CraftingState(base=robe, ilvl=82, rarity=ItemRarity.RARE)
    dist = compute_next_step_outcomes(eng, st_rare, lever_name="Exalted Orb")
    life_fam = next(o for o in dist["outcomes"] if o["familyId"] == "IncreasedLife")
    tiers = life_fam["tiers"]
    assert len(tiers) == life_fam["tierCount"] and len(tiers) >= 5
    assert [t["tier"] for t in tiers] == [f"T{i}" for i in range(1, len(tiers) + 1)]
    assert sum(t["weight"] for t in tiers) == life_fam["weight"]
    assert sum(t["probability"] for t in tiers) == pytest.approx(life_fam["probability"], abs=1e-6)
    for t in tiers:
        vr = t["valueRange"]
        assert isinstance(vr, list) and len(vr) == 2 and vr[0] <= vr[1] and vr[1] > 0
        assert isinstance(t["ilvlReq"], int) and t["statText"]

    md_table = render_outcomes_md(dist)
    assert "最佳阶数值" in md_table and "T1:" in md_table

    runner = CliRunner()
    # CLI `--want` WITHOUT `--lever` exposes top-level `matchedTiers`
    r_want = runner.invoke(
        cli.main,
        ["craft", "odds", "--base", "Vile Robe", "--ilvl", "82", "--want", "T2 maximum Life", "--json"],
    )
    assert r_want.exit_code == 0, r_want.output
    d_want = json.loads(r_want.stdout)
    mt_no_lever = d_want["matchedTiers"]
    assert [t["tier"] for t in mt_no_lever] == ["T1", "T2"]
    assert mt_no_lever[0]["valueRange"][0] > mt_no_lever[1]["valueRange"][0]

    # CLI `--want` WITH `--lever` exposes `matchedTiers` on both top-level and `evaluation`
    r_want_lv = runner.invoke(
        cli.main,
        [
            "craft",
            "odds",
            "--base",
            "Vile Robe",
            "--ilvl",
            "82",
            "--lever",
            "Exalted Orb",
            "--want",
            "T2 maximum Life",
            "--json",
        ],
    )
    assert r_want_lv.exit_code == 0, r_want_lv.output
    d_want_lv = json.loads(r_want_lv.stdout)
    mt_lv = d_want_lv["evaluation"]["matchedTiers"]
    assert d_want_lv["matchedTiers"] == mt_lv
    assert sum(t["weight"] for t in mt_lv) == d_want_lv["evaluation"]["targetWeight"]
    assert sum(t["probability"] for t in mt_lv) == pytest.approx(
        d_want_lv["evaluation"]["hitProbability"], abs=1e-5
    )


# ---------------------------------------------------------------------------
# N4 [P3]: Cross-game URL guardrail equivalent command (`pob load`, NEVER `pob import`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "poe1_url",
    [
        "https://poe.game.qq.com/character-window/get-items",
        "https://poedb.tw/us/Modifiers",
        "https://poe.ninja/builds/settlers",
        "https://maxroll.gg/poe/build-guides/righteous-fire",
        "https://pob.codes/abc123",
    ],
)
def test_n4_poe1_url_and_pobbin_guardrail_emit_pob_load_never_pob_import(poe1_url: str) -> None:
    with pytest.raises(CrossGameHallucinationError) as exc:
        validate_url(poe1_url, expected_game="poe2")
    assert exc.value.equivalent_command == f'pob load "{poe1_url}"'
    assert "pob import" not in exc.value.format_human()

    # Shared `pobb.in` link serving a PoE1 `<PathOfBuilding>` XML also suggests `pob load "<url>"`
    pobbin_url = "https://pobb.in/PoE1Test99"
    poe1_xml = '<PathOfBuilding><Build level="95" className="Marauder"/></PathOfBuilding>'
    with pytest.raises(CrossGameHallucinationError) as exc_pobbin:
        resolve_poe2_build_source(pobbin_url, fetcher=lambda _u, _t: poe1_xml)
    assert exc_pobbin.value.equivalent_command == f'pob load "{pobbin_url}"'
    assert "pob import" not in exc_pobbin.value.format_human()


# ---------------------------------------------------------------------------
# N5 [P3]: `item db` vs `uniques` 100% schema parity (incl. cache hits) & help clarity
# ---------------------------------------------------------------------------


def test_n5_item_db_and_uniques_identical_schema_across_cache_hits_and_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SlotReadCache(max_entries=16)
    base_payload = {
        "game": "poe2",
        "slot": "Helmet",
        "stat": "CombinedDPS",
        "baseDps": 25000.0,
        "baseEhp": 14000.0,
        "scannedCount": 18,
        "results": [{"name": "Crown of Eyes", "baseName": "Vermeil Circlet", "deltaDpsPct": 8.4}],
        "pob2Version": "0.5.5",
        "gamePatch": "0.5.5",
    }

    def _cached_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        assert action == "uniques"
        p = dict(params or {})
        hit = cache.lookup("main", rev=1, lang="zh-CN", action="uniques", params=p)
        if hit is not None:
            return hit
        cache.store("main", rev=1, lang="zh-CN", action="uniques", params=p, payload=base_payload)
        return dict(base_payload)

    monkeypatch.setattr(cli_item, "daemon_call_cli", _cached_daemon)
    runner = CliRunner()

    # 1st call (cache miss), 2nd call (cache hit), 3rd call (loop-guard threshold!) via canonical `item db`
    r_u1 = runner.invoke(cli.main, ["item", "db", "--slot", "Helmet", "--json"])
    r_u2 = runner.invoke(cli.main, ["item", "db", "--slot", "Helmet", "--json"])
    r_db3 = runner.invoke(cli.main, ["item", "db", "--slot", "Helmet", "--json"])
    assert r_u1.exit_code == 0 and r_u2.exit_code == 0 and r_db3.exit_code == 0

    # Verify underlying cache actually triggered loopGuardWarning on raw 4th lookup, while CLI schema stripped it
    raw_4th = _cached_daemon("uniques", {"target": "main", "slot": "Helmet", "stat": None, "top": 15, "eval_sanctified": True})
    assert raw_4th.get("loopGuardWarning") == LOOP_GUARD_WARNING

    j_u1, j_u2, j_db3 = json.loads(r_u1.stdout), json.loads(r_u2.stdout), json.loads(r_db3.stdout)
    assert j_u1 == j_u2 == j_db3
    assert set(j_u1.keys()) == set(j_db3.keys())
    assert "loopGuardWarning" not in j_db3 and "cached" not in j_db3

    # Help & brief clarity (top-level `uniques` alias removed)
    h_db = runner.invoke(cli.main, ["item", "db", "--help"]).stdout
    h_br = runner.invoke(cli.main, ["help", "--brief"]).stdout
    assert "--target" in h_db and "results" in h_db
    assert "item db" in h_br and "results" in h_br


# ---------------------------------------------------------------------------
# N6 [P3]: `--out` help text & `Wrote <path>` on stderr with empty stdout
# ---------------------------------------------------------------------------


def test_n6_out_option_help_empty_stdout_and_wrote_stderr(tmp_path: Path) -> None:
    runner = CliRunner()

    help_about = runner.invoke(cli.main, ["about", "--help"]).stdout
    help_root = runner.invoke(cli.main, ["--help"]).stdout
    assert "Write output to PATH instead of stdout." in help_about
    assert "Write output to PATH instead of stdout." in help_root

    out_json = tmp_path / "about_out.json"
    r_out = runner.invoke(cli.main, ["about", "--json", "--out", str(out_json)])
    assert r_out.exit_code == 0
    assert r_out.stdout == ""
    assert r_out.stderr.strip() == f"Wrote {out_json}"
    parsed = json.loads(out_json.read_text(encoding="utf-8"))
    assert parsed["ok"] is True and parsed["game"] == "poe2"

    out_txt = tmp_path / "brief_help.txt"
    r_txt = runner.invoke(cli.main, ["help", "--brief", "--out", str(out_txt)])
    assert r_txt.exit_code == 0
    assert r_txt.stdout == ""
    assert r_txt.stderr.strip() == f"Wrote {out_txt}"
    assert "Path of Exile 2" in out_txt.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# N7 [P3 / Data Fidelity]: `[res-override-line]` across `load`, `audit`, and `fix-gaps`
# ---------------------------------------------------------------------------


def test_n7_res_override_detector_unit_and_mocked_load_audit_fix_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Pure detector: catches negative override & positive/crafted/fractured ignored, zero false positive on `+35% to Fire Resistance`
    scan_clean = scan_res_override_lines_in_items(
        [("Ring 1", "Ruby Ring", "Rarity: RARE\nDoom Band\nRuby Ring\n+35% to Fire Resistance\n+17% to Fire and Chaos Resistances")]
    )
    assert scan_clean["matches"] == [] and scan_clean["warnings"] == []

    scan_bing = scan_res_override_lines_in_items(
        [
            ("Helmet", "Crown of Eyes", "Rarity: UNIQUE\nCrown of Eyes\nFire Resistance is -10%"),
            (
                "Boots",
                "Viper March",
                "Rarity: RARE\nViper March\nCold Resistance is +37%\nLightning Resistance is +37%\n{crafted}Fire Resistance is +32%\n{fractured}Chaos Resistance is -5%",
            ),
        ]
    )
    assert scan_bing["affectedElements"] == ["Fire", "Cold", "Lightning", "Chaos"]
    assert scan_bing["affectedSlots"] == ["Helmet", "Boots"]
    effects = {(m["slot"], m["cleanLine"]): m["poB2Effect"] for m in scan_bing["matches"]}
    assert effects[("Helmet", "Fire Resistance is -10%")] == "override"
    assert effects[("Boots", "Cold Resistance is +37%")] == "ignored"
    assert effects[("Boots", "Lightning Resistance is +37%")] == "ignored"
    assert effects[("Boots", "Fire Resistance is +32%")] == "ignored"
    assert effects[("Boots", "Chaos Resistance is -5%")] == "override"
    assert len(scan_bing["warnings"]) == 1 and scan_bing["warnings"][0].startswith("[res-override-line]")

    # 2. Mocked worker verification across `apply_post_load_fixups` (`load`), `run_deep_audit` (`audit`), and `solve_fix_gaps_offline` (`fix-gaps`)
    class _ResOverrideWorker:
        def __init__(self, xml_text: str) -> None:
            self.lock = threading.RLock()
            self.caps = {"deepAuditProbe": False}
            self.pob_version = "0.5.5"
            self._xml = xml_text

        def get_engine_diagnostics(self) -> dict[str, Any]:
            return {"ok": True, "game": "poe2", "engineWarnings": []}

        def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
            _ = params
            if method == "save_xml":
                return {"xml": self._xml}
            if method == "get_build_info":
                return {"className": "Mercenary", "ascendClassName": "Gemling Legionnaire", "level": 92, "treeVersion": "0_5"}
            if method == "get_stats":
                return {"stats": {"Spirit": 160, "SpiritUnreserved": 34}}
            if method == "deep_audit_probe":
                return {"resists": {"Fire": {"final": -10, "total": -10, "cap": 75}, "Cold": {"final": 4, "total": 4, "cap": 75}, "Lightning": {"final": -50, "total": -50, "cap": 75}, "Chaos": {"final": 75, "total": 88, "cap": 75}}, "pob2Version": "0.5.5", "treeVersion": "0_5"}
            if method == "eval_honest_dehydration":
                return {"scenarios": {}, "dpsInflationRatio": 1.0, "ehpInflationRatio": 1.0}
            return {}

    w_bing = _ResOverrideWorker(_SYNTH_BING_XML)
    fix_res = apply_post_load_fixups(w_bing, auto_fix=True)
    assert any("[res-override-line]" in w for w in fix_res["warnings"])
    assert any(f["issue"] == "res_override_lines" for f in fix_res["fixups"])

    audit_res = run_deep_audit(w_bing)
    assert any("[res-override-line]" in w for w in audit_res["warnings"])
    assert any(i.get("code") == "res_override_line" for i in audit_res["resistances"]["issues"])
    assert any(f.get("code") == "res_override_line" for f in audit_res["findings"])

    w_clean = _ResOverrideWorker(_CLEAN_CONTROL_XML)
    assert not any("[res-override-line]" in w for w in apply_post_load_fixups(w_clean)["warnings"])
    assert not any("[res-override-line]" in w for w in run_deep_audit(w_clean)["warnings"])

    # Offline `fix-gaps` surfaces `[res-override-line]` in `warnings` & `snapshotNotes.resOverrideWarning`
    sl_boots = audit_equipped_slot(
        slot_name="Boots",
        item_id=2,
        name="Viper March",
        base_name="Sandsworn Sandals",
        rarity="RARE",
        item_type="Boots",
        raw_text="Rarity: RARE\nViper March\nSandsworn Sandals\nCold Resistance is +37%\n{crafted}Fire Resistance is +32%",
    )
    snap = StatSnapshot(
        level=92, ascendancy="Gemling Legionnaire",
        str_val=295, req_str=291, dex_val=212, req_dex=80, int_val=672, req_int=195,
        fire_total=-10, fire_max=75, cold_total=4, cold_max=75, lightning_total=-50, lightning_max=75, chaos_total=88, chaos_max=75,
        spirit_total=160, spirit_reserved=126, spirit_unreserved=34,
    )
    fg_dict = solve_fix_gaps_offline(NetGapAudit(current=snap, bare_net=snap, slots=(sl_boots,)), []).to_dict()
    assert any("[res-override-line]" in w and "Boots" in w for w in fg_dict["warnings"])
    assert "[res-override-line]" in fg_dict["snapshotNotes"]["resOverrideWarning"]

    # CLI `pob2 load --json` emits `[res-override-line]` on stderr while keeping stdout valid JSON
    from pob2_cli import cli_build_io

    monkeypatch.setattr(
        cli_build_io,
        "daemon_call_cli",
        lambda *_a, **_kw: {
            "game": "poe2",
            "loaded": True,
            "sourceKind": "pob2_xml_file",
            "buildInfo": {"className": "Mercenary", "ascendClassName": "Gemling Legionnaire", "level": 92},
            "fixupWarnings": fix_res["warnings"],
            "warnings": fix_res["warnings"],
        },
    )
    r_load = CliRunner().invoke(cli.main, ["load", "synth_bing.xml", "--json"])
    assert r_load.exit_code == 0
    assert "[res-override-line]" in r_load.stderr
    d_load = json.loads(r_load.stdout)
    assert any("[res-override-line]" in w for w in d_load["warnings"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native lua51.dll live PoB2 test")
def test_n7_live_engine_bing_xml_load_audit_fix_gaps(tmp_path: Path) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live N7 resistance-override verification.")

    bing_candidates = [eng / "PoE2" / "Builds" / "bing.xml", eng / "Builds" / "bing.xml"]
    bing_path = next((p for p in bing_candidates if p.is_file()), None)
    xml_payload = bing_path.read_text(encoding="utf-8") if bing_path is not None else _SYNTH_BING_XML

    worker = BridgeWorker(slot_name="r3_n7_live", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        load_res = load_build_into_worker(worker, xml_code=xml_payload, auto_fix=True)
        assert any("[res-override-line]" in w for w in (load_res.get("warnings") or ()))

        audit_res = run_deep_audit(worker)
        assert any("[res-override-line]" in w for w in (audit_res.get("warnings") or ()))
        assert any(f.get("code") == "res_override_line" for f in (audit_res.get("findings") or ()))

        fg_res = solve_fix_gaps_live(worker, apply=False).to_dict()
        assert any("[res-override-line]" in w for w in fg_res["warnings"])
        assert "[res-override-line]" in fg_res["snapshotNotes"].get("resOverrideWarning", "")

        # Clean control build on live engine produces zero `[res-override-line]` warnings
        clean_load = load_build_into_worker(worker, xml_code=_CLEAN_CONTROL_XML, auto_fix=True)
        assert not any("[res-override-line]" in w for w in (clean_load.get("warnings") or ()))
    finally:
        worker.stop()
