# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-only
"""PyInstaller --onedir spec for standalone ``pob2`` on Windows, macOS, and Linux.

Bundles ``pob2_cli/lua/*.lua``, ``pob2_cli/data/*.{json,json.gz}``, ``ATTRIBUTION.md``, and all
``pob2_cli`` CLI/subpackage hidden imports. On macOS and Linux, set ``POB2_BUNDLE_RUNTIME`` to a
directory holding ``bin/luajit`` and ``bin/lua-utf8.so`` (from ``build_posix_runtime.sh``) to
embed the pinned 2460b3f LuaJIT runtime inside the standalone archive.
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

spec_dir = SPECPATH
src_dir = os.path.abspath(os.path.join(spec_dir, "..", "src"))
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)
entry_script = os.path.join(spec_dir, "pyinstaller_entry.py")

# Explicit asset & module manifests verified by scripts/arch_lint.py [spec-sync].
_LUA_ASSETS = (
    "agent_probes.lua",
    "cow_spec_snapshot.lua",
    "deep_audit_probe.lua",
    "dehydration_and_fixup.lua",
    "fast_calc_cow.lua",
    "headless_runner.lua",
    "node_power_kernel.lua",
    "pob_host.lua",
    "poe2_shields.lua",
    "unique_rank_kernel.lua",
    "win32_ffi.lua",
    "zh_json.lua",
    "zh_reverse.lua",
    "zh_translate.lua",
)
_DATA_ASSETS = (
    "mechanics_facts.json",
    "modsview_weights.json.gz",
    "poe2_craft_l10n.json.gz",
    "poe2_en_bases.json.gz",
    "rune_instill_catalog.json.gz",
    "wegame_runes_gems.json.gz",
    "weights_overrides.json",
)
_CLI_MODULES = (
    "pob2_cli.cli",
    "pob2_cli.cli_build_io",
    "pob2_cli.cli_common",
    "pob2_cli.cli_craft",
    "pob2_cli.cli_errors",
    "pob2_cli.cli_ground",
    "pob2_cli.cli_help",
    "pob2_cli.cli_import",
    "pob2_cli.cli_inspect",
    "pob2_cli.cli_item",
    "pob2_cli.cli_lifecycle",
    "pob2_cli.cli_mutate",
    "pob2_cli.cli_rebalance",
    "pob2_cli.cli_rune",
    "pob2_cli.cli_what_if_helpers",
)
_SUBPACKAGES = (
    "pob2_cli.analyzer",
    "pob2_cli.comparator",
    "pob2_cli.craft",
    "pob2_cli.daemon",
    "pob2_cli.formatters",
    "pob2_cli.game_data",
    "pob2_cli.guardrails",
    "pob2_cli.importers",
    "pob2_cli.mutators",
    "pob2_cli.rebalance",
    "pob2_cli.rune",
    "pob2_cli.snapshot",
    "pob2_cli.updater",
)

_LUA_DIR = os.path.join(src_dir, "pob2_cli", "lua")
_DATA_DIR = os.path.join(src_dir, "pob2_cli", "data")
_ATTRIBUTION = os.path.abspath(os.path.join(spec_dir, "..", "ATTRIBUTION.md"))

datas = (
    collect_data_files("certifi")
    + [(os.path.join(_DATA_DIR, name), os.path.join("pob2_cli", "data")) for name in _DATA_ASSETS]
    + ([(_ATTRIBUTION, ".")] if os.path.isfile(_ATTRIBUTION) else [])
    + [(os.path.join(_LUA_DIR, name), os.path.join("pob2_cli", "lua")) for name in _LUA_ASSETS]
)

bundle_runtime = os.environ.get("POB2_BUNDLE_RUNTIME", "").strip()
if bundle_runtime:
    bundle_runtime = os.path.abspath(bundle_runtime)
    for name in ("luajit", "lua-utf8.so"):
        rt_file = os.path.join(bundle_runtime, "bin", name)
        if not os.path.isfile(rt_file):
            raise SystemExit(f"POB2_BUNDLE_RUNTIME: missing {rt_file}")
        datas.append((rt_file, os.path.join("runtime", "bin")))

hiddenimports = sorted(
    {
        "socksio",
        *_CLI_MODULES,
        *_SUBPACKAGES,
        *collect_submodules("pob2_cli"),
    }
)

a = Analysis(
    [entry_script],
    pathex=[src_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pob2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pob2",
)
