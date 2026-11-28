# SPDX-License-Identifier: GPL-3.0-only
"""L0 deterministic CLI contract matrix: cold-start budget, full leaf-command audit, and AGENTS.md sync."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner

from pob2_cli import cli, cli_common, cli_inspect, cli_item, cli_mutate
from pob2_cli.analyzer.summary import inspect_tree
from pob2_cli.cli_errors import _emit_error_and_resolve_code
from pob2_cli.daemon.client import _rpc_result
from pob2_cli.errors import DaemonRequestError, InternalBugError
from pob2_cli.importers.share_urls import resolve_poe2_build_source
from pob2_cli.snapshot.commands import undo_build
from pob2_cli.snapshot.helpers import clean_points_dict
from pob2_cli.snapshot.history import history_of

_REPO_POB2_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_POB2_ROOT / "src"


def _walk_leaf_commands(group: click.Group, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], click.Command]]:
    ctx = click.Context(group)
    leaves: list[tuple[tuple[str, ...], click.Command]] = []
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None or cmd.hidden:
            continue
        path = (*prefix, name)
        if isinstance(cmd, click.Group):
            leaves.extend(_walk_leaf_commands(cmd, path))
        else:
            leaves.append((path, cmd))
    return leaves


def test_cold_start_import_pob2_cli_under_budget() -> None:
    """Measure cold subprocess `import pob2_cli.cli` and enforce <150ms * POB2_BENCH_SLACK."""
    slack = float(os.environ.get("POB2_BENCH_SLACK", "1.0") or "1.0")
    budget_ms = 150.0 * max(1.0, slack)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_SRC_ROOT)
    env["POB2_OFFLINE"] = "1"
    # Warm OS filesystem cache once, then measure median of 3 cold imports
    samples: list[float] = []
    for _ in range(4):
        t0 = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-X", "importtime", "-c", "import pob2_cli.cli"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # Parse exact Python importtime cumulative microseconds for pob2_cli.cli if present
        import_ms = None
        for line in proc.stderr.splitlines():
            if line.strip().endswith("pob2_cli.cli"):
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2 and parts[1].isdigit():
                    import_ms = int(parts[1]) / 1000.0
        samples.append(import_ms if import_ms is not None else elapsed_ms)

    measured_ms = sorted(samples[1:])[1]
    print(f"COLD_START_IMPORT_MS={measured_ms:.2f} (budget={budget_ms:.1f}ms, samples={[round(s, 2) for s in samples[1:]]})")
    assert measured_ms < budget_ms, f"import pob2_cli.cli took {measured_ms:.2f}ms >= {budget_ms:.1f}ms budget"


def test_removed_duplicate_aliases_and_canonical_commands() -> None:
    ctx = click.Context(cli.main)
    top_names = set(cli.main.list_commands(ctx))
    assert "uniques" not in top_names, "Top-level alias `uniques` must be removed (merged into `item db`)."
    assert "rank-instill" not in top_names, "Top-level alias `rank-instill` must be removed (merged into `instill rank`)."

    leaves = {" ".join(path) for path, _ in _walk_leaf_commands(cli.main)}
    assert "item db" in leaves
    assert "instill rank" in leaves
    for sub in ("what-if equip", "what-if tree", "what-if skill", "what-if config", "what-if apply"):
        assert sub in leaves


def test_every_leaf_command_help_and_json_error_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every leaf command must pass `--help` (exit 0) and emit zero-stderr JSON envelopes on invalid `--json` calls."""

    def _fail_daemon(action: str, _params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        raise DaemonRequestError(f"[POE2-EMPTY-SLOT] Slot 'main' has no build loaded for {action}.")

    monkeypatch.setattr(cli_common, "daemon_call_cli", _fail_daemon)
    monkeypatch.setattr(cli_inspect, "daemon_call_cli", _fail_daemon)
    monkeypatch.setattr(cli_item, "daemon_call_cli", _fail_daemon)
    monkeypatch.setattr(cli_mutate, "daemon_call_cli", _fail_daemon)

    runner = CliRunner()
    leaves = _walk_leaf_commands(cli.main)
    assert len(leaves) >= 35

    for path, cmd in leaves:
        r_help = runner.invoke(cli.main, [*path, "--help"])
        assert r_help.exit_code == 0, f"{' '.join(path)} --help failed: {r_help.output}"
        assert r_help.stdout.strip(), f"{' '.join(path)} --help produced empty stdout"

        has_json = any(
            isinstance(p, click.Option) and ("--json" in p.opts)
            for p in cmd.params
        )
        if not has_json:
            continue

        # Trigger deterministic bad-option error with `--json` to verify zero-stderr JSON error contract
        r_err = runner.invoke(cli.main, [*path, "--json", "--__nonexistent_contract_flag__"])
        assert r_err.exit_code in (1, 5), f"{' '.join(path)} unexpected exit {r_err.exit_code}: {r_err.output}"
        assert r_err.stderr == "", f"{' '.join(path)} polluted stderr on --json error: {r_err.stderr!r}"
        assert "Traceback" not in r_err.stdout
        parsed = json.loads(r_err.stdout)
        assert parsed.get("ok") is False, f"{' '.join(path)} JSON error missing ok=False: {parsed}"
        assert parsed.get("game") == "poe2"
        assert isinstance(parsed.get("error"), str) and parsed["error"].strip()


def test_agents_md_has_zero_ghost_modules() -> None:
    """Verify every L0..L4 module listed in `pob2_cli/AGENTS.md` exists under `src/pob2_cli/`."""
    agents_md = (_REPO_POB2_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    pkg_root = _SRC_ROOT / "pob2_cli"
    real_units = {f.stem for f in pkg_root.glob("*.py")} | {
        d.name for d in pkg_root.iterdir() if d.is_dir() and (d / "__init__.py").is_file()
    }

    declared: set[str] = set()
    for line in agents_md.splitlines():
        if re.match(r"^\s*-\s+`L[0-4]`:", line):
            tokens = re.findall(r"`([A-Za-z0-9_*]+)`", line.split(":", 1)[1])
            declared.update(tokens)

    assert declared, "Expected L0..L4 module declarations in pob2_cli/AGENTS.md"
    ghosts = sorted(tok for tok in declared if "*" not in tok and tok not in real_units)
    assert not ghosts, f"Ghost modules found in pob2_cli/AGENTS.md: {ghosts}"
    missing = sorted(u for u in real_units if u not in declared)
    assert not missing, f"Real modules missing from pob2_cli/AGENTS.md: {missing}"


class _StubWorker:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.lock = threading.RLock()

    def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        val = self.responses.get(method, {"ok": True})
        return val(dict(params or {})) if callable(val) else dict(val)


def test_error_envelopes_internal_error_and_empty_history() -> None:
    # Internal Python bugs must map to rc=1 internal_error, NOT rc=5 daemon_error
    assert _emit_error_and_resolve_code(None, AttributeError("CONFIG_KEY")) == 1
    assert _emit_error_and_resolve_code(None, KeyError("missing")) == 1
    with pytest.raises(InternalBugError):
        _rpc_result({"ok": False, "errorKind": "internal_error", "error": "CONFIG_KEY"})

    # Empty undo history raises [POE2-EMPTY-HISTORY]
    worker = _StubWorker()
    history_of(worker).load_build("dummy", "test")
    with pytest.raises(DaemonRequestError, match="POE2-EMPTY-HISTORY"):
        undo_build(worker)


def test_poe_ninja_403_and_html_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pob2_cli.importers.share_urls.check_public_https_url", lambda _u: None)
    monkeypatch.setattr("pob2_cli.importers.url_guard.check_public_https_url", lambda _u: None)

    transport_403 = httpx.MockTransport(
        lambda req: httpx.Response(403, text="<html><title>Just a moment...</title></html>", request=req)
    )
    with pytest.raises(DaemonRequestError, match="POE2-FETCH-BLOCKED"):
        resolve_poe2_build_source("https://poe.ninja/poe2/pob/abc123", transport=transport_403)

    transport_html200 = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<!DOCTYPE html><html><title>Just a moment...</title></html>", request=req)
    )
    with pytest.raises(DaemonRequestError, match="POE2-FETCH-BLOCKED"):
        resolve_poe2_build_source("https://poe.ninja/poe2/pob/abc123", transport=transport_html200)


def test_strip_pob_color_codes_from_points_display() -> None:
    cleaned = clean_points_dict({"used": 117, "usedMax": 123, "display": "^7117 / 123 ^xDD002224 / 24"})
    assert cleaned["display"] == "117 / 123 24 / 24"
    assert "^" not in cleaned["display"]

    worker = _StubWorker(
        {
            "get_tree_state": {"allocatedNodes": [1, 2], "overrides": {}},
            "get_weapon_set_state": {"ok": True},
            "get_build_info": {
                "classId": 9,
                "className": "Mercenary",
                "ascendClassName": "Gemling Legionnaire",
                "treeVersion": "0_5",
                "points": {"used": 117, "usedMax": 123, "display": "^7117 / 123 ^xDD002224 / 24"},
            },
        }
    )
    tree_out = inspect_tree(worker)
    assert tree_out["points"]["display"] == "117 / 123 24 / 24"


def test_skill_md_budget() -> None:
    skill_path = Path(__file__).resolve().parents[2] / "skills" / "pob2" / "SKILL.md"
    raw = skill_path.read_bytes()
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert len(lines) <= 90, f"SKILL.md has {len(lines)} lines (>90)"
    assert len(raw) <= 10240, f"SKILL.md is {len(raw)} bytes (>10240)"
    assert all(len(ln) <= 500 for ln in lines), "SKILL.md has a line > 500 chars"
