# SPDX-License-Identifier: GPL-3.0-only
"""Domain tests for the orthogonal `pob2 what-if <equip|tree|skill|config|apply>` CLI group."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pob2_cli import cli, cli_mutate, cli_what_if_helpers
from pob2_cli.errors import DaemonRequestError
from pob2_cli.mutators.what_if import _apply_mutation_ops


def _mock_what_if(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        payload = dict(params or {})
        calls.append((action, payload))
        return {
            "ok": True,
            "game": "poe2",
            "applied": bool(payload.get("apply")),
            "before": {"primaryDps": 10000.0, "totalEhp": 20000.0, "life": 2000.0},
            "after": {"primaryDps": 12500.0, "totalEhp": 21000.0, "life": 2100.0},
            "delta": {"primaryDps": 2500.0, "primaryDpsPct": 25.0, "totalEhp": 1000.0, "totalEhpPct": 5.0},
            "detail": [],
        }

    monkeypatch.setattr(cli_mutate, "daemon_call_cli", _fake_daemon)
    return calls


def test_what_if_group_exposes_orthogonal_subcommands() -> None:
    runner = CliRunner()
    res = runner.invoke(cli.main, ["what-if", "--help"])
    assert res.exit_code == 0, res.output
    for sub in ("equip", "tree", "skill", "config", "apply"):
        assert sub in res.output


def test_what_if_equip_subcommand_and_slot_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _mock_what_if(monkeypatch)
    runner = CliRunner()

    item_file = tmp_path / "ring.txt"
    item_file.write_text("Rarity: RARE\nTest Ring\nRuby Ring\n+30 to maximum Life\n", encoding="utf-8")

    r_ok = runner.invoke(
        cli.main,
        ["what-if", "equip", "--slot", "Ring 1", "--item-file", str(item_file), "--json"],
    )
    assert r_ok.exit_code == 0, r_ok.output
    assert r_ok.stderr == ""
    data = json.loads(r_ok.stdout)
    assert data["ok"] is True and data["applied"] is False
    assert calls[-1][0] == "what_if"
    assert calls[-1][1]["slot"] == "Ring 1"
    assert "Ruby Ring" in calls[-1][1]["item_text"]

    r_swap = runner.invoke(cli.main, ["what-if", "equip", "--weapon-swap", "true", "--json"])
    assert r_swap.exit_code == 0, r_swap.output
    assert calls[-1][1]["weapon_swap"] is True

    r_bad_slot = runner.invoke(cli.main, ["what-if", "equip", "--slot", "NotASlot", "--json"])
    assert r_bad_slot.exit_code == 1
    assert r_bad_slot.stderr == ""
    err = json.loads(r_bad_slot.stdout)
    assert err["ok"] is False

    r_empty = runner.invoke(cli.main, ["what-if", "equip", "--json"])
    assert r_empty.exit_code == 1
    assert r_empty.stderr == ""
    assert json.loads(r_empty.stdout)["ok"] is False


def test_what_if_tree_skill_config_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_what_if(monkeypatch)
    runner = CliRunner()

    r_tree = runner.invoke(
        cli.main,
        ["what-if", "tree", "--tree-node", "12345", "--isolated-node", "--alloc-mode", "1", "--json"],
    )
    assert r_tree.exit_code == 0, r_tree.output
    assert calls[-1][1]["tree_node"] == 12345
    assert calls[-1][1]["anoint_preview"] is True
    assert calls[-1][1]["alloc_mode"] == 1

    r_skill = runner.invoke(
        cli.main,
        ["what-if", "skill", "--gem-group", "1", "--gem-index", "2", "--gem-level", "20", "--set1", "true", "--json"],
    )
    assert r_skill.exit_code == 0, r_skill.output
    assert calls[-1][1]["gem_group"] == 1
    assert calls[-1][1]["gem_index"] == 2
    assert calls[-1][1]["gem_level"] == 20
    assert calls[-1][1]["set1"] is True

    r_cfg = runner.invoke(
        cli.main,
        ["what-if", "config", "--level", "95", "--config-key", "enemyIsBoss", "--config-value", "Pinnacle", "--json"],
    )
    assert r_cfg.exit_code == 0, r_cfg.output
    assert calls[-1][1]["level"] == 95
    assert calls[-1][1]["config_key"] == "enemyIsBoss"
    assert calls[-1][1]["config_value"] == "Pinnacle"


def test_what_if_apply_multi_domain_spec_and_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _mock_what_if(monkeypatch)
    runner = CliRunner()

    spec = {
        "slot": "Amulet",
        "itemText": "Rarity: RARE\nSolar Amulet\n+40 to Spirit",
        "treeNode": 45678,
        "configKey": "conditionFullLife",
        "configValue": "true",
        "level": 92,
    }
    r_inline = runner.invoke(cli.main, ["what-if", "apply", "--spec", json.dumps(spec), "--json"])
    assert r_inline.exit_code == 0, r_inline.output
    assert r_inline.stderr == ""
    sent = calls[-1][1]
    assert sent["slot"] == "Amulet"
    assert sent["tree_node"] == 45678
    assert sent["config_key"] == "conditionFullLife"
    assert sent["config_value"] is True
    assert sent["level"] == 92
    assert sent["apply"] is False

    spec_file = tmp_path / "combo.json"
    spec_file.write_text(json.dumps({"level": 93, "weaponSwap": True}), encoding="utf-8")
    r_file = runner.invoke(cli.main, ["what-if", "apply", "--spec-file", str(spec_file), "--commit", "--json"])
    assert r_file.exit_code == 0, r_file.output
    assert calls[-1][1]["apply"] is True
    assert calls[-1][1]["weapon_swap"] is True

    r_bad_key = runner.invoke(cli.main, ["what-if", "apply", "--spec", '{"bogusKey": 1}', "--json"])
    assert r_bad_key.exit_code == 1
    assert r_bad_key.stderr == ""
    err = json.loads(r_bad_key.stdout)
    assert err["ok"] is False and err["errorKind"] == "invalid_spec"

    r_bad_json = runner.invoke(cli.main, ["what-if", "apply", "--spec", "{not-json", "--json"])
    assert r_bad_json.exit_code == 1
    assert r_bad_json.stderr == ""
    assert json.loads(r_bad_json.stdout)["ok"] is False


class _FakeWorker:
    """Minimal in-memory BridgeWorker stub for unit testing what-if mutators."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}
        self.pob_version = "0.23.1"
        self.lock = threading.RLock()

    def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        payload = dict(params or {})
        self.calls.append((method, payload))
        val = self.responses.get(method, {"ok": True})
        return val(payload) if callable(val) else dict(val)


def test_what_if_config_key_uses_config_var() -> None:
    worker = _FakeWorker()
    ops = _apply_mutation_ops(worker, {"config_key": "enemyLevel", "config_value": 82})
    assert ops == [{"op": "set_config", "var": "enemyLevel", "value": 82}]
    assert worker.calls == [("set_config", {"var": "enemyLevel", "value": 82})]


def test_what_if_skill_gem_index_sends_index_key() -> None:
    worker = _FakeWorker()
    ops = _apply_mutation_ops(
        worker,
        {"gem_group": 12, "gem_index": 1, "gem_level": 19, "gem_quality": 20, "gem_enabled": False},
    )
    assert worker.calls == [
        ("set_gem", {"group": 12, "index": 1, "level": 19, "quality": 20, "enabled": False})
    ]
    assert ops[0]["op"] == "set_gem" and ops[0]["index"] == 1
    assert "gem" not in ops[0]

    with pytest.raises(DaemonRequestError, match="1-based"):
        _apply_mutation_ops(worker, {"gem_group": 12, "gem_index": 0, "gem_level": 19})


def test_what_if_skill_weapon_set_noop_rejected_and_real_change_ok() -> None:
    noop_worker = _FakeWorker(
        {
            "set_skill_weapon_set": {
                "ok": True,
                "index": 12,
                "set1": False,
                "set2": True,
                "gem1EnableGlobal1": False,
                "gem1EnableGlobal2": True,
                "changed": False,
                "noOp": True,
            }
        }
    )
    with pytest.raises(DaemonRequestError, match="POE2-NO-OP"):
        _apply_mutation_ops(noop_worker, {"gem_group": 12, "set1": False})

    ok_worker = _FakeWorker(
        {
            "set_skill_weapon_set": {
                "ok": True,
                "index": 12,
                "set1": False,
                "set2": True,
                "gem1EnableGlobal1": False,
                "gem1EnableGlobal2": True,
                "changed": True,
                "noOp": False,
            }
        }
    )
    ops = _apply_mutation_ops(ok_worker, {"gem_group": 12, "gem_index": 2, "set1": False})
    assert ok_worker.calls == [("set_skill_weapon_set", {"index": 12, "gemIndex": 2, "set1": False})]
    assert ops[0]["changed"] is True
    assert ops[0]["gem1EnableGlobal1"] is False


def test_what_if_equip_unequip_does_not_swallow_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[dict[str, Any]] = []

    def _fake_call(action: str, params: dict[str, Any]) -> dict[str, Any]:
        captured.append({"action": action, **params})
        return {"ok": True, "game": "poe2", "applied": False, "detail": [], "diff": {"deltas": []}}

    monkeypatch.setattr(cli_mutate, "daemon_call_cli", _fake_call)
    runner = CliRunner()

    # 1) Flag form: --slot "Body Armour" --unequip --json
    r1 = runner.invoke(cli.main, ["what-if", "equip", "--slot", "Body Armour", "--unequip", "--json"])
    assert r1.exit_code == 0, r1.output
    assert json.loads(r1.output)["ok"] is True
    assert captured[-1]["unequip_slot"] == "Body Armour"
    assert "slot" not in captured[-1]

    # 2) Backwards-compatible value form: --unequip "Body Armour" --json
    r2 = runner.invoke(cli.main, ["what-if", "equip", "--unequip", "Body Armour", "--json"])
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output)["ok"] is True
    assert captured[-1]["unequip_slot"] == "Body Armour"

    # 3) Item file form: --slot "Body Armour" --item-file <file> --json
    armour_file = tmp_path / "body_armour.txt"
    armour_file.write_text("Rarity: RARE\nTest Vest\nVile Robe\n+80 to maximum Energy Shield\n", encoding="utf-8")
    r_file = runner.invoke(
        cli.main,
        ["what-if", "equip", "--slot", "Body Armour", "--item-file", str(armour_file), "--json"],
    )
    assert r_file.exit_code == 0, r_file.output
    assert json.loads(r_file.output)["ok"] is True
    assert captured[-1]["slot"] == "Body Armour"
    assert "Vile Robe" in captured[-1]["item_text"]
    assert "unequip_slot" not in captured[-1]

    # Bare --unequip without --slot fails cleanly as JSON usage_error (not "Invalid slot '--json'")
    r3 = runner.invoke(cli.main, ["what-if", "equip", "--unequip", "--json"])
    assert r3.exit_code == 1
    err = json.loads(r3.output)
    assert err["errorKind"] == "usage_error"
    assert "--json" not in err["error"]


def test_what_if_apply_nested_and_flat_spec() -> None:
    nested = cli_what_if_helpers.load_apply_spec(
        '{"config":{"level":95,"enemyLevel":82,"conditionEnemyFrozen":"true"},'
        '"tree":{"tree_node":331,"alloc_mode":1},'
        '"skill":{"group":12,"index":1,"level":19,"set1":false},'
        '"equip":{"slot":"Body Armour","unequip":true}}',
        None,
    )
    assert nested["level"] == 95
    assert nested["config_vars"] == {"enemyLevel": 82, "conditionEnemyFrozen": True}
    assert nested["tree_node"] == 331 and nested["alloc_mode"] == 1
    assert nested["gem_group"] == 12 and nested["gem_index"] == 1 and nested["gem_level"] == 19
    assert nested["set1"] is False
    assert nested["unequip_slot"] == "Body Armour"
    assert "slot" not in nested

    # Multiple config_vars execute cleanly in _apply_mutation_ops
    worker = _FakeWorker()
    ops = _apply_mutation_ops(worker, {"level": 95, "config_vars": {"enemyLevel": 82}})
    assert any(op["op"] == "set_config" and op["var"] == "enemyLevel" and op["value"] == 82 for op in ops)

    # Flat spec still works
    flat = cli_what_if_helpers.load_apply_spec('{"level":95}', None)
    assert flat == {"level": 95}
