# SPDX-License-Identifier: GPL-3.0-only
"""Tests for PoE2 lifecycle CLI hooks, multi-source share URLs, #2401/#2536 post-load fixups, and Notes."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob2_cli import bridge_protocol, cli, updater
from pob2_cli.analyzer import extract_notes, extract_summary
from pob2_cli.bridge_client import BridgeWorker
from pob2_cli.errors import CrossGameHallucinationError
from pob2_cli.importers.post_load_fixups import apply_post_load_fixups, strip_pob_color_codes
from pob2_cli.importers.share_urls import (
    load_build_into_worker,
    parse_share_url,
    resolve_poe2_build_source,
)

_MINIMAL_POE2_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="90" targetVersion="0_1" className="Monk" ascendClassName="Invoker" mainSocketGroup="1"/>
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
    <ItemSet id="1" useSecondWeaponSet="false">
      <Slot name="Weapon 1" itemId="1"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" label="AuraGroup" mainActiveSkill="1">
        <Gem nameSpec="Herald of Ice" level="18" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="MainDamage" mainActiveSkill="1">
        <Gem nameSpec="Ice Strike" level="18" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Config activeConfigSet="1">
    <ConfigSet id="1"/>
  </Config>
  <Notes>^xFF8800Leveling Guide^7
^2Swap ring before Pinnacle Bosses
^7Keep Spirit &gt;= 100</Notes>
</PathOfBuilding2>
"""


def test_cli_preregisters_neon_craft_wave2_optional_hooks_and_core_commands() -> None:
    opt = cli._OPTIONAL_SUBCOMMANDS
    for name in ("rune", "instill", "fix-gaps", "spirit"):
        assert name in opt, f"Missing Wave 2 optional hook `{name}` in cli._OPTIONAL_SUBCOMMANDS"
    assert "rank-instill" not in opt
    assert opt["rune"][:2] == (".cli_rune", "rune_group")
    assert opt["instill"][:2] == (".cli_rune", "instill_group")
    assert opt["fix-gaps"][:2] == (".cli_rebalance", "cmd_fix_gaps")
    assert opt["spirit"][:2] == (".cli_rebalance", "cmd_spirit")

    for core in ("bootstrap", "status", "stop", "load", "export", "notes", "summary", "audit", "diagnose", "compare", "what-if", "diff", "undo", "redo"):
        assert core in cli._LAZY_SUBCOMMANDS
    assert "uniques" not in cli._LAZY_SUBCOMMANDS


def test_share_url_parsing_mock_transport_and_poe1_block() -> None:
    code = bridge_protocol.encode_xml_share_code(_MINIMAL_POE2_XML)
    cases = [
        ("https://pobb.in/abc123XYZ", "pobbin", "https://pobb.in/pob/abc123XYZ", code),
        ("https://poe2db.tw/pob/fooBar99", "poe2db", "https://poe2db.tw/pob/fooBar99/raw", code),
        ("https://poe.ninja/poe2/pob/9a8b7c", "poeninja", "https://poe.ninja/poe2/pob/raw/9a8b7c", code),
        ("https://maxroll.gg/poe2/pob/mrx42", "maxroll", "https://maxroll.gg/poe2/api/pob/mrx42", '{"pobCode": "' + code + '"}'),
        ("pob2://poeninja/z1y2", "poeninja", "https://poe.ninja/poe2/pob/raw/z1y2", code),
    ]
    for url, expected_kind, expected_dl, body in cases:
        parsed = parse_share_url(url)
        assert parsed == (expected_kind, expected_dl)

        def _handler(req: httpx.Request, *, _dl: str = expected_dl, _body: str = body) -> httpx.Response:
            assert str(req.url) == _dl
            return httpx.Response(200, text=_body)

        res = resolve_poe2_build_source(url, transport=httpx.MockTransport(_handler))
        assert res.source_kind == f"{expected_kind}_url"
        assert "<PathOfBuilding2>" in res.xml

    # PoE1 URLs must be hard-blocked by bidirectional guardrail (exit code 1)
    for poe1_url in (
        "https://poe.ninja/builds/settlers/character/foo/bar",
        "https://maxroll.gg/poe/pob/abc",
        "https://poedb.tw/us/Spark",
        "pob://pobbin/abc123",
    ):
        with pytest.raises(CrossGameHallucinationError):
            resolve_poe2_build_source(poe1_url)

    runner = CliRunner()
    cli_res = runner.invoke(cli.main, ["import", "https://maxroll.gg/poe/pob/abc"])
    assert cli_res.exit_code == 1


def test_strip_notes_color_codes_and_2401_offline_fixup() -> None:
    cleaned = strip_pob_color_codes("^xFF8800Bossing^7\n^2Use WS2 for Curse")
    assert cleaned == "Bossing\nUse WS2 for Curse"

    class _Fake2401Worker:
        def __init__(self) -> None:
            self.lock = threading.RLock()
            self.caps: dict[str, Any] = {}
            self.disabled: list[int] = []
            self.spirit_unres = -30.0

        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            p = params or {}
            if method == "get_stats":
                return {"stats": {"Spirit": 100.0, "SpiritUnreserved": self.spirit_unres}}
            if method == "get_weapon_set_state":
                return {
                    "useSecondWeaponSet": False,
                    "skillWeaponSets": [
                        {"index": 1, "label": "MainAura", "enabled": True, "set1": True, "set2": False},
                        {"index": 2, "label": "SwapAura", "enabled": True, "set1": False, "set2": True},
                    ],
                }
            if method == "list_skills":
                return {"groups": [{"label": "MainAura", "slot": ""}, {"label": "SwapAura", "slot": "Weapon 1 Swap"}]}
            if method == "set_group" and p.get("enabled") is False:
                self.disabled.append(int(p["index"]))
                self.spirit_unres = 20.0
                return {"ok": True}
            if method == "get_notes":
                return {"text": "^x00FF00Hello^7\n^1World"}
            return {"ok": True}

    fw = _Fake2401Worker()
    out = apply_post_load_fixups(fw, auto_fix=True)
    assert fw.disabled == [2]
    assert any(f.get("issue") == "#2401" and f.get("autoFixed") is True for f in out["fixups"])
    assert out["notes"]["text"] == "Hello\nWorld"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows live PoB2 post-load #2536 & Notes test")
def test_live_load_2536_main_skill_calibration_notes_and_summary(tmp_path: Path) -> None:
    eng_raw = os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip()
    data_raw = os.environ.get("POB2_DATA_DIR", "").strip()
    eng = Path(eng_raw) if eng_raw else None
    data_dir = Path(data_raw) if data_raw else None
    if eng is None or not updater.is_valid_engine_dir(eng, data_dir=data_dir):
        pytest.skip("Set POB2_ENGINE_DIR to run live PoB2 core command tests.")

    worker = BridgeWorker(slot_name="test", pob_dir=eng, data_dir=data_dir, runtime_dir=tmp_path / "rt")
    try:
        worker.start()
        assert worker.caps.get("deepAuditProbe") is True
        assert worker.caps.get("uniqueRankKernel") is True

        # Group #1 is Herald of Ice (0 DPS), Group #2 is Ice Strike (>0 DPS). #2536 must auto-select Group #2!
        res = load_build_into_worker(worker, xml_code=_MINIMAL_POE2_XML, auto_fix=True)
        assert res["loaded"] is True
        fix_2536 = [f for f in res["postLoadFixups"] if f.get("issue") == "#2536"]
        assert len(fix_2536) == 1
        assert fix_2536[0]["beforeGroup"] == 1 and fix_2536[0]["afterGroup"] == 2
        assert fix_2536[0]["afterDps"] > 0

        # Notes stripped of PoB colour escapes
        notes = extract_notes(worker)
        assert notes["hasNotes"] is True
        assert "^x" not in notes["text"] and "^7" not in notes["text"] and "^2" not in notes["text"]
        assert "Leveling Guide" in notes["text"] and "Swap ring before Pinnacle Bosses" in notes["text"]

        # Summary carries PoB2 version & 0.5.5 gamePatch stamp
        summ = extract_summary(worker)
        assert summ["game"] == "poe2"
        assert summ["gamePatch"] == "0.5.5"
        assert summ["pob2Version"] and summ["pob2Version"] != "unknown"
        assert summ["versionStamp"]["reviewStatus"] == "ok"
        assert float(summ["primaryDps"]) > 0
    finally:
        worker.stop()
