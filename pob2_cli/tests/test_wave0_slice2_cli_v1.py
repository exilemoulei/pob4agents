# SPDX-License-Identifier: GPL-3.0-only
"""Wave 0 Phase B (Slice 2): CLI v1 symmetric contract, 0/1/5 exit codes, and anti-swallowing regression suite."""

from __future__ import annotations

import ast
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from pob2_cli import cli, cli_common, cli_craft, cli_item, updater
from pob2_cli.analyzer import diagnose_build, extract_summary, inspect_items
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.craft import price_book as craft_price_book
from pob2_cli.importers.share_urls import load_build_into_worker
from pob2_cli.rebalance.net_gap import audit_equipped_slot
from pob2_cli.rebalance.spirit_knapsack import solve_live_spirit_deficit
from pob2_cli.rune import rank_instillations, rank_runes_for_slot
from pob2_cli.rune.live_eval import (
    eval_item_raw_deltas_live,
    extract_build_profile,
    get_allocated_node_ids,
)

_SRC_PKG = Path(__file__).resolve().parents[1] / "src" / "pob2_cli"

_SAMPLE_RARE_WAND = (
    "Rarity: RARE\nStorm Spire\nAttuned Wand\n--------\nItem Level: 82\n--------\n"
    "110% increased Spell Damage (fractured)\n+20 to Intelligence\n"
)

_MINIMAL_BUILD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="85" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
  <Tree activeSpec="1">
    <Spec treeVersion="0_5" classId="10" ascendClassId="1" nodes="50986"/>
  </Tree>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Storm Stick
Gothic Quarterstaff
Item Level: 82
Adds 80 to 140 Physical Damage
20% increased Attack Speed
    </Item>
    <Item id="2">
Rarity: RARE
Doom Band
Ruby Ring
Item Level: 82
+85 to maximum Life
+35% to Fire Resistance
+30% to Cold Resistance
    </Item>
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Ring 1" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" label="Main" mainActiveSkill="1">
        <Gem nameSpec="Ice Strike" level="18" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1"/>
  </Config>
</PathOfBuilding2>
"""


# ---------------------------------------------------------------------------
# 1. `craft odds|plan|next --want` (single & multiple) and `--target` migration error (`rc=1`)
# ---------------------------------------------------------------------------


def test_craft_want_single_and_multiple_and_legacy_target_migration_error(tmp_path: Path) -> None:
    runner = CliRunner()

    # Single `--want` on `craft odds`
    r_odds_1 = runner.invoke(
        cli.main,
        ["craft", "odds", "--base", "Attuned Wand", "--ilvl", "82", "--want", "T2 Spell Damage", "--json"],
    )
    assert r_odds_1.exit_code == 0, r_odds_1.output
    d_odds_1 = json.loads(r_odds_1.output)
    assert d_odds_1["ok"] is True and d_odds_1["game"] == "poe2"
    assert len(d_odds_1["actions"]) >= 1

    # Multiple `--want` on `craft odds`, `craft plan`, and `craft next`
    r_odds_2 = runner.invoke(
        cli.main,
        [
            "craft",
            "odds",
            "--base",
            "Attuned Wand",
            "--ilvl",
            "82",
            "--want",
            "T2 Spell Damage",
            "--want",
            "T2 Cast Speed",
            "--json",
        ],
    )
    assert r_odds_2.exit_code == 0, r_odds_2.output
    d_odds_2 = json.loads(r_odds_2.output)
    assert d_odds_2["ok"] is True and d_odds_2["game"] == "poe2"

    r_plan = runner.invoke(
        cli.main,
        [
            "craft",
            "plan",
            "--base",
            "Attuned Wand",
            "--ilvl",
            "82",
            "--want",
            "T2 Spell Damage",
            "--want",
            "T2 Cast Speed",
            "--sims",
            "60",
            "--json",
        ],
    )
    assert r_plan.exit_code == 0, r_plan.output
    d_plan = json.loads(r_plan.output)
    assert d_plan["ok"] is True and d_plan["game"] == "poe2"
    assert len(d_plan["targets"]) == 2

    item_file = tmp_path / "half_wand.txt"
    item_file.write_text(_SAMPLE_RARE_WAND, encoding="utf-8")
    r_next = runner.invoke(
        cli.main,
        [
            "craft",
            "next",
            "--file",
            str(item_file),
            "--want",
            "T2 Spell Damage",
            "--want",
            "T2 Cast Speed",
            "--json",
        ],
    )
    assert r_next.exit_code == 0, r_next.output
    d_next = json.loads(r_next.output)
    assert d_next["ok"] is True and d_next["game"] == "poe2"
    assert "inspection" in d_next

    # Passing `--target` to `craft odds|plan|next` MUST fail immediately with rc=1 & [POE2-CLI-MIGRATION]
    for sub_argv in (
        ["craft", "odds", "--base", "Attuned Wand", "--target", "T2 Spell Damage"],
        ["craft", "plan", "--base", "Attuned Wand", "--target", "T2 Spell Damage"],
        ["craft", "next", "--text", _SAMPLE_RARE_WAND, "--target", "T2 Cast Speed"],
        ["craft", "plan", "--target", "T2 maximum Life"],
    ):
        res_mig = runner.invoke(cli.main, sub_argv)
        assert res_mig.exit_code == 1, f"{sub_argv}: {res_mig.output}"
        assert "[POE2-CLI-MIGRATION]" in res_mig.output
        assert "--want" in res_mig.output
        assert "Traceback" not in res_mig.output

    # Migration error with `--json` emits `ok: False, game: 'poe2'`
    res_mig_json = runner.invoke(
        cli.main,
        ["craft", "odds", "--base", "Attuned Wand", "--target", "T2 Spell Damage", "--json"],
    )
    assert res_mig_json.exit_code == 1
    assert "[POE2-CLI-MIGRATION]" in res_mig_json.output


# ---------------------------------------------------------------------------
# 2. Trailing `--json`, `--compact`, `--out <file>` & Unified JSON Envelope across ALL CLI modules
# ---------------------------------------------------------------------------


def test_zero_adhoc_json_dumps_outside_cli_common() -> None:
    """AST-verify that no `cli*.py` file other than `cli_common.py` calls `json.dumps`."""
    offenders: list[str] = []
    for py_file in sorted(_SRC_PKG.glob("cli*.py")):
        if py_file.name == "cli_common.py":
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=py_file.name)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "dumps"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "json"
            ):
                offenders.append(f"{py_file.name}:{node.lineno}")
    assert offenders == [], f"Ad-hoc json.dumps found in CLI modules: {offenders}"


def test_every_leaf_cli_command_supports_trailing_json_compact_out_and_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("POB2_OFFLINE", "1")
    runner = CliRunner()

    # 1. Verify EVERY leaf command across the entire CLI tree exposes `--json`, `--compact`, `--out`
    root_ctx = click.Context(cli.main, info_name="pob2")
    checked_leaf_cmds: list[str] = []
    for name in cli.main.list_commands(root_ctx):
        cmd = cli.main.get_command(root_ctx, name)
        assert cmd is not None
        if isinstance(cmd, click.Group):
            sub_ctx = click.Context(cmd, info_name=name, parent=root_ctx)
            for sub_name in cmd.list_commands(sub_ctx):
                sub_cmd = cmd.get_command(sub_ctx, sub_name)
                assert sub_cmd is not None
                opt_flags = {o for p in sub_cmd.params for o in p.opts}
                assert {"--json", "--compact", "--out"} <= opt_flags, f"`pob2 {name} {sub_name}` missing output_options: {opt_flags}"
                checked_leaf_cmds.append(f"{name} {sub_name}")
        else:
            opt_flags = {o for p in cmd.params for o in p.opts}
            assert {"--json", "--compact", "--out"} <= opt_flags, f"`pob2 {name}` missing output_options: {opt_flags}"
            checked_leaf_cmds.append(name)
    assert len(checked_leaf_cmds) >= 35

    # 2. Mock daemon_call_cli across CLI modules to test representative commands from EVERY cli_*.py module
    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        p = params or {}
        if action == "status":
            return {"pid": 1234, "engineDir": "eng", "runtimeDir": "rt", "lang": "zh-CN", "langSource": "default", "main": {"alive": True, "hasLoadedBuild": True, "rev": 1}, "caps": {"cowSnapshot": True}}
        if action == "summary":
            return {"className": "Monk", "ascendClassName": "Invoker", "level": 90, "primaryDps": 50000.0, "totalEhp": 25000.0, "life": 2200, "energyShield": 1800, "spirit": 150, "spiritUnreserved": 30, "pob2Version": "0.5.5", "gamePatch": "0.5.5"}
        if action == "notes":
            return {"hasNotes": True, "lineCount": 1, "text": "Clean notes"}
        if action == "what_if":
            return {"applied": False, "before": {"primaryDps": 100.0, "totalEhp": 100.0, "life": 100.0}, "after": {"primaryDps": 120.0, "totalEhp": 100.0, "life": 100.0}, "delta": {"primaryDps": 20.0, "primaryDpsPct": 20.0, "totalEhp": 0.0, "totalEhpPct": 0.0, "life": 0.0}, "detail": []}
        if action == "uniques":
            return {"slot": p.get("slot", "Ring 1"), "stat": p.get("stat") or "CombinedDPS", "scannedCount": 12, "results": [{"rank": 1, "name": "Polcirkeln", "baseName": "Sapphire Ring", "delta": 4200.0, "deltaPct": 12.5}]}
        if action == "items":
            return {"slot": p.get("slot"), "slots": [{"name": "Ring 1", "selItemId": 2}], "items": [{"id": 2, "name": "Doom Band", "baseName": "Ruby Ring"}], "raw": _SAMPLE_RARE_WAND}
        return {"action": action, "params": p}

    for mod in (cli_common, cli.importlib.import_module("pob2_cli.cli_lifecycle"), cli.importlib.import_module("pob2_cli.cli_build_io"), cli.importlib.import_module("pob2_cli.cli_inspect"), cli.importlib.import_module("pob2_cli.cli_mutate"), cli.importlib.import_module("pob2_cli.cli_rebalance"), cli_item):
        if hasattr(mod, "daemon_call_cli"):
            monkeypatch.setattr(mod, "daemon_call_cli", _fake_daemon)

    xml_in = tmp_path / "in_build.xml"
    xml_in.write_text(_MINIMAL_BUILD_XML, encoding="utf-8")

    representative_calls: list[tuple[str, list[str]]] = [
        ("cli.py:about", ["about"]),
        ("cli.py:lang", ["lang"]),
        ("cli_help.py:help", ["help", "--brief"]),
        ("cli_lifecycle.py:status", ["status", "--no-start"]),
        ("cli_build_io.py:notes", ["notes", "--target", "main"]),
        ("cli_inspect.py:summary", ["summary", "--slot", "ref"]),
        ("cli_mutate.py:what-if", ["what-if", "config", "--level", "92", "--target", "main"]),
        ("cli_craft.py:odds", ["craft", "odds", "--base", "Attuned Wand", "--want", "T2 Spell Damage"]),
        ("cli_item.py:db", ["item", "db", "--slot", "Ring 1", "--sort", "CombinedDPS", "--limit", "5"]),
        ("cli_item.py:inspect", ["item", "inspect", "--slot", "Weapon 1", "--target", "main"]),
        ("cli_ground.py:ground", ["ground", "spirit"]),
        ("cli_import.py:import", ["import", str(xml_in)]),
        ("cli_rune.py:rune-inspect", ["rune", "inspect", "Desert Rune"]),
        ("cli_rebalance.py:spirit", ["spirit", "--budget", "120"]),
    ]

    for idx, (label, base_argv) in enumerate(representative_calls):
        out_f = tmp_path / f"out_{idx}.json"
        res = runner.invoke(cli.main, [*base_argv, "--json", "--compact", "--out", str(out_f)])
        assert res.exit_code == 0, f"{label} failed: {res.output}"
        assert out_f.is_file(), f"{label} did not create --out file"
        raw_written = out_f.read_text(encoding="utf-8")
        # Single-line compact JSON (trailing newline only)
        non_empty_lines = [ln for ln in raw_written.splitlines() if ln.strip()]
        assert len(non_empty_lines) == 1, f"{label} --compact wrote multiple lines:\n{raw_written}"
        parsed = json.loads(non_empty_lines[0])
        assert parsed["ok"] is True, f"{label} missing ok=True: {parsed}"
        assert parsed["game"] == "poe2", f"{label} missing game='poe2': {parsed}"

    # Root-level `--json --compact --out` propagation via `ctx.obj` also works
    root_out = tmp_path / "root_propagated.json"
    res_root = runner.invoke(cli.main, ["--json", "--compact", "--out", str(root_out), "about"])
    assert res_root.exit_code == 0, res_root.output
    root_lines = [ln for ln in root_out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(root_lines) == 1
    assert json.loads(root_lines[0])["ok"] is True

    # Build-slot `--slot` alias rejects equipment slot names (`rc=1`) while accepting build-slot slugs
    bad_alias = runner.invoke(cli.main, ["summary", "--slot", "Ring 1"])
    assert bad_alias.exit_code == 1
    assert "--target" in bad_alias.output


# ---------------------------------------------------------------------------
# 3. `0 / 1 / 5` Exit Codes (including injected `RuntimeError` in `cli_craft.py` -> `rc=5`!)
# ---------------------------------------------------------------------------


def test_exit_codes_0_1_5_and_injected_runtime_error_in_cli_craft(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()

    # rc=1: CLI unknown flag / missing required option / validation / guardrail
    assert runner.invoke(cli.main, ["summary", "--no-such-option-xyz"]).exit_code == 1
    assert runner.invoke(cli.main, ["craft", "plan", "--base", "Attuned Wand"]).exit_code == 1
    assert runner.invoke(cli.main, ["craft", "odds", "--base", "Viridian Jewel", "--want", "T1 Life"]).exit_code == 1
    assert runner.invoke(cli.main, ["craft", "odds", "--base", "Attuned Wand"]).exit_code == 1

    # rc=5: Injected unexpected `RuntimeError` inside `cli_craft.py` MUST exit with 5 (NEVER 1!)
    from pob2_cli import craft as craft_pkg

    class _ExplodingSolver:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def plan_craft(self, **_kw: Any) -> Any:
            raise RuntimeError("injected internal MDP solver explosion")

        def evaluate_next_actions(self, *_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("injected internal odds explosion")

    monkeypatch.setattr(craft_pkg, "CraftMdpSolver", _ExplodingSolver)

    r_plan_5 = runner.invoke(
        cli.main,
        ["craft", "plan", "--base", "Attuned Wand", "--want", "T2 Spell Damage"],
    )
    assert r_plan_5.exit_code == 5, f"Expected exit code 5 on unexpected RuntimeError, got {r_plan_5.exit_code}: {r_plan_5.output}"
    assert "injected internal MDP solver explosion" in r_plan_5.output
    assert "Traceback" not in r_plan_5.output

    r_odds_5 = runner.invoke(
        cli.main,
        ["craft", "odds", "--base", "Attuned Wand", "--want", "T2 Spell Damage"],
    )
    assert r_odds_5.exit_code == 5, f"Expected exit code 5 on unexpected RuntimeError, got {r_odds_5.exit_code}: {r_odds_5.output}"
    assert "Traceback" not in r_odds_5.output

    # Also verify `inspect_item_text` raising an unexpected RuntimeError in `cli_craft` / `cli_item` exits 5
    def _boom_inspect(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("unexpected inspector internal crash")

    monkeypatch.setattr(cli_craft, "inspect_item_text", _boom_inspect)
    monkeypatch.setattr(cli_item, "inspect_item_text", _boom_inspect)
    assert runner.invoke(cli.main, ["craft", "inspect", "--text", _SAMPLE_RARE_WAND]).exit_code == 5
    assert runner.invoke(cli.main, ["item", "inspect", "--text", _SAMPLE_RARE_WAND]).exit_code == 5


# ---------------------------------------------------------------------------
# 4. `pob2 item db --slot ...` and `pob2 item inspect --slot ...` (offline/mocked + live engine)
# ---------------------------------------------------------------------------


def test_item_db_and_item_inspect_slot_prevalidation_and_mocked_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt_dir = tmp_path / "isolated_rt"
    rt_dir.mkdir()
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(rt_dir))
    monkeypatch.setenv("POB2_OFFLINE", "1")
    runner = CliRunner()

    # Invalid slot rejected with rc=1 `[POE2-INVALID-SLOT]` BEFORE spawning/calling daemon
    for bad_argv in (
        ["item", "db", "--slot", "Ring 9", "--json"],
        ["item", "inspect", "--slot", "Ring 9", "--json"],
    ):
        r_bad = runner.invoke(cli.main, bad_argv)
        assert r_bad.exit_code == 1, f"{bad_argv}: {r_bad.output}"
        assert "[POE2-INVALID-SLOT]" in r_bad.output
        assert not (rt_dir / "daemon_poe2.json").exists()

    # PoE1 slot (`Flask 5`) rejected with rc=1 `[POE2-CROSS-GAME-BLOCK]`
    for poe1_argv in (
        ["item", "db", "--slot", "Flask 5"],
        ["item", "inspect", "--slot", "Flask 5"],
    ):
        r_f5 = runner.invoke(cli.main, poe1_argv)
        assert r_f5.exit_code == 1
        assert "[POE2-CROSS-GAME-BLOCK]" in r_f5.output
        assert not (rt_dir / "daemon_poe2.json").exists()

    # Mocked daemon call verifies `item db`, `uniques` alias parity, and `item inspect --slot`
    captured_calls: list[tuple[str, dict[str, Any]]] = []

    def _mock_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        p = dict(params or {})
        captured_calls.append((action, p))
        if action == "uniques":
            return {
                "game": "poe2",
                "slot": p["slot"],
                "stat": p.get("stat") or "CombinedDPS",
                "scannedCount": 20,
                "results": [
                    {
                        "rank": 1,
                        "name": "Dream Fragments",
                        "baseName": "Sapphire Ring",
                        "delta": 3500.0,
                        "deltaPct": 11.2,
                        "sanctifiedRollRange": {"minScalar": 0.78, "maxScalar": 1.22},
                    }
                ],
            }
        if action == "items":
            if p.get("slot") == "Belt":
                return {"game": "poe2", "slot": "Belt", "slots": [], "items": [], "raw": None}
            return {
                "game": "poe2",
                "slot": p["slot"],
                "slots": [{"name": p["slot"], "selItemId": 1}],
                "items": [{"id": 1, "name": "Storm Spire", "baseName": "Attuned Wand"}],
                "raw": _SAMPLE_RARE_WAND,
            }
        raise AssertionError(f"Unexpected daemon action {action}")

    monkeypatch.setattr(cli_item, "daemon_call_cli", _mock_daemon)

    r_db = runner.invoke(
        cli.main,
        ["item", "db", "--slot", "ring1", "--sort", "TotalEHP", "--limit", "5", "--target", "ref", "--json"],
    )
    assert r_db.exit_code == 0, r_db.output
    db_json = json.loads(r_db.output)
    assert db_json["ok"] is True and db_json["game"] == "poe2"
    assert db_json["slot"] == "Ring 1" and db_json["stat"] == "TotalEHP"
    assert captured_calls[-1] == (
        "uniques",
        {"target": "ref", "slot": "Ring 1", "stat": "TotalEHP", "top": 5, "eval_sanctified": True},
    )

    # Top-level duplicate alias `pob2 uniques` is removed under D1=A (merged into `pob2 item db`)
    r_uniq = runner.invoke(
        cli.main,
        ["uniques", "--slot", "ring1", "--json"],
    )
    assert r_uniq.exit_code == 1
    assert r_uniq.stderr == ""
    assert json.loads(r_uniq.stdout)["errorKind"] == "usage_error"

    # `pob2 item inspect --slot` fetches equipped item and runs full craft/affix inspection
    r_insp = runner.invoke(
        cli.main,
        ["item", "inspect", "--slot", "weapon1", "--target", "ref", "--json"],
    )
    assert r_insp.exit_code == 0, r_insp.output
    insp_json = json.loads(r_insp.output)
    assert insp_json["ok"] is True and insp_json["game"] == "poe2"
    assert insp_json["slot"] == "Weapon 1" and insp_json["targetSlot"] == "ref"
    assert insp_json["baseType"] == "Attuned Wand" and insp_json["baseSlug"] == "Wands"
    assert insp_json["slots"]["openPrefixes"] == 2 and insp_json["slots"]["openSuffixes"] == 2

    # Empty equipped slot returns clean rc=1 error
    r_empty_slot = runner.invoke(cli.main, ["item", "inspect", "--slot", "Belt"])
    assert r_empty_slot.exit_code == 1
    assert "empty" in r_empty_slot.output.lower()


# ---------------------------------------------------------------------------
# 5. `pob2 help --brief` (contains `--want`, `item db`, `item inspect`, zero PoE1 references)
# ---------------------------------------------------------------------------


def test_help_brief_cheatsheet_pure_poe2_zero_poe1_leakage() -> None:
    runner = CliRunner()
    res = runner.invoke(cli.main, ["help", "--brief"])
    assert res.exit_code == 0, res.output
    out = res.output

    for required_tok in (
        "--want",
        "item db",
        "item inspect",
        "craft odds",
        "craft plan",
        "craft next",
        "--slot",
        "--target",
        "Path of Exile 2",
    ):
        assert required_tok in out, f"Missing `{required_tok}` in `pob2 help --brief`:\n{out}"

    low = out.lower()
    for forbidden_poe1 in (
        "poe1",
        "path of exile 1",
        "3.30",
        "bench",
        "anoint",
        "map-regex",
        "tattoo",
        "cluster jewel",
        "scouring",
        "alteration",
        "flask 5",
    ):
        assert forbidden_poe1 not in low, f"PoE1 leakage `{forbidden_poe1}` found in `pob2 help --brief`:\n{out}"

    # Topic help + JSON envelope on `pob2 help item db --json`
    res_topic = runner.invoke(cli.main, ["help", "item", "db", "--json"])
    assert res_topic.exit_code == 0, res_topic.output
    topic_json = json.loads(res_topic.output)
    assert topic_json["ok"] is True and topic_json["game"] == "poe2"
    assert topic_json["command"] == "pob2 item db"
    assert "--slot" in topic_json["flags"]


# ---------------------------------------------------------------------------
# 6. `rune/live_eval.py` & sibling solvers emit explicit degradation `warnings` (zero silent swallowing)
# ---------------------------------------------------------------------------


def test_live_eval_and_solver_degradation_emits_explicit_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DegradedWorker:
        def __init__(self, *, fail_tx: bool = False, fail_diag: bool = False) -> None:
            self.lock = threading.RLock()
            self.fail_tx = fail_tx
            self.fail_diag = fail_diag

        def get_engine_diagnostics(self) -> dict[str, Any]:
            if self.fail_diag:
                raise RuntimeError("diag probe offline")
            return {"ok": True, "game": "poe2", "engineWarnings": []}

        def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
            p = params or {}
            if method in ("item_tooltip", "gem_tooltip", "get_tree_state", "node_power", "get_weapon_set_state", "list_config"):
                raise RuntimeError(f"{method} rpc unavailable")
            if method == "get_stats":
                if self.fail_tx:
                    raise RuntimeError("stats rpc crashed")
                return {"stats": {"CombinedDPS": 12000.0, "Spirit": 100.0, "SpiritUnreserved": -20.0, "Life": 2000.0, "TotalEHP": 8000.0}}
            if method == "list_items":
                return {
                    "slots": [
                        {"name": "Weapon 1", "selItemId": 1},
                        {"name": "Amulet", "selItemId": 2},
                    ],
                    "items": [
                        {"id": 1, "name": "W", "baseName": "Attuned Wand", "type": "Wand"},
                        {"id": 2, "name": "A", "baseName": "Solar Amulet", "type": "Amulet"},
                    ],
                }
            if method == "item_raw":
                if int(p.get("id") or 0) == 2:
                    return {"raw": "Rarity: RARE\nSun Torc\nSolar Amulet\nImplicits: 1\n+15 to Spirit\n+60 to maximum Life"}
                return {"raw": "Rarity: RARE\nStorm Wand\nAttuned Wand\nSockets: S\n60% increased Spell Damage"}
            if method == "list_skills":
                return {
                    "mainSocketGroup": 1,
                    "groups": [
                        {"index": 1, "enabled": True, "label": "Main", "gems": [{"index": 1, "nameSpec": "Spark", "enabled": True}]},
                        {"index": 2, "enabled": True, "label": "Herald", "gems": [{"index": 1, "gemId": "HeraldOfThunder", "nameSpec": "Herald of Thunder", "enabled": True}]},
                    ],
                }
            if method == "cow_snapshot_capture":
                if self.fail_tx:
                    raise RuntimeError("cow snapshot unavailable")
                return {"ok": True, "snapshotId": "s1"}
            if method in ("cow_snapshot_restore", "cow_snapshot_discard", "add_item", "set_group", "export_code", "get_build_info", "get_sidebar", "get_build_header"):
                if self.fail_tx:
                    raise RuntimeError(f"{method} unavailable")
                return {"ok": True, "code": "FAKE", "level": 85, "className": "Sorceress"}
            return {"ok": True}

    # 1. Direct `rune/live_eval.py` helpers emit `[live-eval-degraded]` warnings
    w_prof_fail: list[str] = []
    assert extract_build_profile(_DegradedWorker(fail_tx=True), warnings=w_prof_fail) is None
    assert any("[live-eval-degraded]" in w and "get_stats" in w for w in w_prof_fail)

    w_nodes_fail: list[str] = []
    assert get_allocated_node_ids(_DegradedWorker(), warnings=w_nodes_fail) == set()
    assert any("[live-eval-degraded]" in w and "get_tree_state" in w for w in w_nodes_fail)

    w_tt_fail: list[str] = []
    deltas = eval_item_raw_deltas_live(
        _DegradedWorker(),
        "Weapon 1",
        ["Rarity: RARE\nStorm Wand\nAttuned Wand\nSockets: S\n80% increased Spell Damage"],
        "CombinedDPS",
        12000.0,
        warnings=w_tt_fail,
    )
    assert len(deltas) == 1
    assert any("[live-eval-degraded]" in w and "item_tooltip" in w for w in w_tt_fail)

    # 2. `rank_runes_for_slot` & `rank_instillations` propagate `[live-eval-degraded]` warnings
    rune_rep = rank_runes_for_slot(
        slot_name="Weapon 1",
        item_type="Wand",
        top_live_eval=3,
        worker=_DegradedWorker(fail_tx=True),
    )
    assert rune_rep.eval_mode == "offline_heuristic"
    assert rune_rep.evaluated_count > 0
    assert any("[live-eval-degraded]" in w for w in rune_rep.warnings)

    inst_rep = rank_instillations(top_live_eval=3, worker=_DegradedWorker())
    assert inst_rep.evaluated_count > 0
    assert any("[live-eval-degraded]" in w for w in inst_rep.warnings)

    # 3. `SpiritKnapsackPlan`, `extract_summary`, `diagnose_build`, `audit_equipped_slot`, and `fetch_live_price_book`
    sp_plan = solve_live_spirit_deficit(_DegradedWorker())
    assert any("[spirit-degraded]" in w for w in sp_plan.warnings)
    assert any("[live-eval-degraded]" in w for w in sp_plan.warnings)

    summ = extract_summary(_DegradedWorker(fail_diag=True))
    assert any("[summary-degraded]" in w for w in summ.get("warnings") or [])

    diag = diagnose_build(_DegradedWorker(fail_diag=False), include_node_power=True)
    assert any("[diagnose-degraded]" in w for w in diag.get("warnings") or [])

    slot_warns: list[str] = []
    sl_audit = audit_equipped_slot(
        slot_name="Ring 1",
        item_id=1,
        name="Broken",
        base_name="NonExistentBaseXYZ",
        rarity="RARE",
        item_type="Ring",
        raw_text="garbage",
        warnings=slot_warns,
    )
    assert sl_audit.has_open_crafted_slot is False
    assert any("[net-gap-degraded]" in w for w in slot_warns)

    import httpx

    def _fail_get(*_a: Any, **_kw: Any) -> Any:
        raise httpx.ConnectError("simulated network outage")

    monkeypatch.setattr(httpx.Client, "get", _fail_get)
    pb = craft_price_book.fetch_live_price_book(offline=False, timeout_s=0.5)
    assert any("[price-book-degraded]" in w for w in pb.warnings)


# ---------------------------------------------------------------------------
# 7. Live PoB2 Engine E2E (`item db` & `item inspect --slot` via real BridgeWorker & CLI)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 engine E2E test")
def test_live_engine_item_db_and_item_inspect_slot_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 Slice 2 CLI tests.")

    rt_dir = tmp_path / "live_slice2_rt"
    rt_dir.mkdir()
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(rt_dir))
    monkeypatch.setenv("POB2_OFFLINE", "1")

    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=rt_dir)
    try:
        worker.start()
        load_build_into_worker(worker, xml_code=_MINIMAL_BUILD_XML, auto_fix=True)

        # Direct `inspect_items(..., include_raw=True)` returns raw PoB2 item text for Ring 1
        it_ring = inspect_items(worker, slot="Ring 1", include_raw=True)
        assert it_ring["slot"] == "Ring 1"
        assert it_ring["raw"] and "Ruby Ring" in it_ring["raw"]

        # Route `cli_item.daemon_call_cli` to the live BridgeWorker for fast zero-spawn E2E verification
        from pob2_cli.comparator import rank_uniques

        def _live_bridge_dispatch(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
            p = params or {}
            if action == "items":
                return inspect_items(worker, slot=p.get("slot"), include_raw=bool(p.get("include_raw")))
            if action == "uniques":
                return rank_uniques(
                    worker,
                    slot=str(p["slot"]),
                    stat=p.get("stat"),
                    top=int(p.get("top") or 5),
                    eval_sanctified=bool(p.get("eval_sanctified", True)),
                    max_scan=40,
                )
            raise AssertionError(f"Unexpected live dispatch action: {action}")

        monkeypatch.setattr(cli_item, "daemon_call_cli", _live_bridge_dispatch)
        runner = CliRunner()

        r_insp = runner.invoke(cli.main, ["item", "inspect", "--slot", "Ring 1", "--json", "--compact"])
        assert r_insp.exit_code == 0, r_insp.output
        d_insp = json.loads(r_insp.output.strip())
        assert d_insp["ok"] is True and d_insp["game"] == "poe2"
        assert d_insp["slot"] == "Ring 1"
        assert d_insp["baseType"] == "Ruby Ring"
        assert d_insp["isLegal055"] is True

        r_db = runner.invoke(cli.main, ["item", "db", "--slot", "Ring 1", "--limit", "5", "--json", "--compact"])
        assert r_db.exit_code == 0, r_db.output
        d_db = json.loads(r_db.output.strip())
        assert d_db["ok"] is True and d_db["game"] == "poe2"
        assert d_db["scannedCount"] >= 5 and len(d_db["results"]) >= 1
    finally:
        worker.stop()
