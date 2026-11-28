"""Unit tests for pob2_cli architecture, ghost-layer, intra-pkg cycle, spec-sync, and leak lint gates."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

_POB2_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _POB2_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import arch_lint  # noqa: E402
import check as check_mod  # noqa: E402


def _make_minimal_pkg(tmp_path: Path) -> Path:
    """Construct a valid synthetic ``pob2_cli`` tree matching ``LAYER_MAP`` and ``pob2.spec``."""
    root = tmp_path / "pob2_cli"
    pkg = root / "src" / "pob2_cli"
    (pkg / "lua").mkdir(parents=True)
    (pkg / "data").mkdir(parents=True)
    (root / "packaging").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "pob24agents"\nlicense = { text = "GPL-3.0-only" }\n'
        'classifiers = ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]\n'
        'dependencies = ["click>=8.1.0", "httpx[socks]>=0.25.0", "tabulate>=0.9.0"]\n',
        encoding="utf-8",
    )

    real_pkg = _POB2_ROOT / "src" / "pob2_cli"
    cli_mods: list[str] = []
    subpkgs: list[str] = []
    for unit in sorted(arch_lint.LAYER_MAP):
        if (real_pkg / unit).is_dir():
            (pkg / unit).mkdir(parents=True, exist_ok=True)
            (pkg / unit / "__init__.py").write_text("", encoding="utf-8")
            subpkgs.append(f"pob2_cli.{unit}")
        else:
            content = '__version__ = "0.1.0"\n' if unit == "__init__" else ""
            (pkg / f"{unit}.py").write_text(content, encoding="utf-8")
            if unit.startswith("cli"):
                cli_mods.append(f"pob2_cli.{unit}")

    (pkg / "lua" / "headless_runner.lua").write_text("-- stub\n", encoding="utf-8")
    (pkg / "data" / "mechanics_facts.json").write_text("[]\n", encoding="utf-8")
    (root / "packaging" / "pyinstaller_entry.py").write_text(
        "from pob2_cli.cli import main\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )
    (root / "packaging" / "pob2.spec").write_text(
        "from PyInstaller.utils.hooks import collect_submodules\n"
        '_LUA_ASSETS = ("headless_runner.lua",)\n'
        '_DATA_ASSETS = ("mechanics_facts.json",)\n'
        f"_CLI_MODULES = {tuple(sorted(cli_mods))!r}\n"
        f"_SUBPACKAGES = {tuple(sorted(subpkgs))!r}\n"
        'hiddenimports = [*_CLI_MODULES, *_SUBPACKAGES, *collect_submodules("pob2_cli")]\n',
        encoding="utf-8",
    )
    return root


def test_real_pob2_cli_passes_arch_lint_with_zero_ghost_units() -> None:
    violations, stats = arch_lint.run_arch_lint(_POB2_ROOT)
    assert not violations, "\n".join(violations)
    assert int(stats["files"]) >= 70
    # Verify all ghost PoE1/future units were purged from LAYER_MAP
    for ghost in (
        "profile_import",
        "external_data",
        "poedb_client",
        "timeless",
        "waystone",
        "item_inspect",
        "trade_client",
        "trade_item",
        "trade_query",
        "trade_eval",
        "cli_optimize",
        "cli_data",
        "cli_timeless",
        "cli_trade",
    ):
        assert ghost not in arch_lint.LAYER_MAP


def test_agents_md_under_60_lines_and_uv_lock_valid() -> None:
    agents_lines = (_POB2_ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    assert len(agents_lines) < 60, f"pob2_cli/AGENTS.md has {len(agents_lines)} lines (must be <60)"
    lock = tomllib.loads((_POB2_ROOT / "uv.lock").read_text(encoding="utf-8"))
    names = [pkg.get("name") for pkg in lock.get("package", [])]
    assert "pob24agents" in names
    assert "pob4agents" not in names


def test_arch_lint_blocks_cross_pkg_detached_leak_and_ticks(tmp_path: Path) -> None:
    root = _make_minimal_pkg(tmp_path)
    pkg = root / "src" / "pob2_cli"

    cross_pkg = "pob" + "_cli"
    detached = "DETACHED" + "_PROCESS"
    leak = "C:" + "/Users/" + "mou" + "le" + "/work"
    (pkg / "compat.py").write_text(
        f"import {cross_pkg}\n"
        f"import subprocess\n"
        f"FLAG = getattr(subprocess, '{detached}', 0)\n"
        f'PATH = "{leak}"\n',
        encoding="utf-8",
    )
    (pkg / "ticks.py").write_text("TICK = 0.033\n", encoding="utf-8")

    violations, _ = arch_lint.run_arch_lint(root)
    joined = "\n".join(violations)
    assert "[cross-pkg-import]" in joined
    assert "[win-subprocess]" in joined
    assert "[local-path-leak]" in joined
    assert "[no-poe1-ticks]" in joined


def test_arch_lint_allows_public_repo_handle_without_false_positive(tmp_path: Path) -> None:
    root = _make_minimal_pkg(tmp_path)
    pkg = root / "src" / "pob2_cli"
    (pkg / "compat.py").write_text(
        'REPO = "https://github.com/exilemoulei/pob4agents"\n',
        encoding="utf-8",
    )
    violations, _ = arch_lint.run_arch_lint(root)
    assert not violations, "\n".join(violations)


def test_arch_lint_rejects_gpl_or_later_and_forbidden_deps(tmp_path: Path) -> None:
    root = _make_minimal_pkg(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "pob24agents"\nlicense = { text = "GPL-3.0-or-later" }\n'
        'classifiers = ["License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)"]\n'
        'dependencies = ["click>=8.1.0", "pydantic>=2.0"]\n',
        encoding="utf-8",
    )
    violations, _ = arch_lint.run_arch_lint(root)
    joined = "\n".join(violations)
    assert joined.count("[pkg-policy]") >= 3


def test_arch_lint_catches_ghost_layer_map_intra_pkg_cycle_and_spec_sync(tmp_path: Path) -> None:
    root = _make_minimal_pkg(tmp_path)
    pkg = root / "src" / "pob2_cli"

    # 1. Intra-package circular import: craft.alpha -> craft.beta -> craft.alpha
    (pkg / "craft" / "alpha.py").write_text("from .beta import B\nA = B\n", encoding="utf-8")
    (pkg / "craft" / "beta.py").write_text("from .alpha import A\nB = A\n", encoding="utf-8")

    # 2. Unlisted lua & data assets drifting from packaging/pob2.spec
    (pkg / "lua" / "unlisted_probe.lua").write_text("-- drift\n", encoding="utf-8")
    (pkg / "data" / "unlisted_table.json").write_text("{}\n", encoding="utf-8")

    # 3. Ghost LAYER_MAP unit (delete one module from synthetic tree)
    (pkg / "han_fold.py").unlink()

    violations, _ = arch_lint.run_arch_lint(root)
    joined = "\n".join(violations)
    assert "[intra-pkg-cycle]" in joined
    assert "craft.alpha -> craft.beta -> craft.alpha" in joined
    assert "[spec-sync]" in joined
    assert "unlisted_probe.lua" in joined
    assert "unlisted_table.json" in joined
    assert "[ghost-layer-map]" in joined
    assert "han_fold" in joined


def test_check_py_detects_offline_and_live_ratchets() -> None:
    mode_off, min_off, skip_off = check_mod.detect_test_ratchet({})
    assert mode_off == "offline"
    assert min_off >= 120
    assert skip_off <= 25

    mode_live, min_live, skip_live = check_mod.detect_test_ratchet({"POB2_ENGINE_DIR": "/opt/pob2"})
    assert mode_live == "live-engine"
    assert min_live >= 180
    assert skip_live <= 5
