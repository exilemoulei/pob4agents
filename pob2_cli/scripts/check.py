#!/usr/bin/env python3
"""Deterministic lint, architecture-DAG, spec-sync, isolation, and test ratchet gate for ``pob2_cli``.

Modes:
  python pob2_cli/scripts/check.py             # ruff + arch-lint + isolated pytest (env-aware ratchet)
  python pob2_cli/scripts/check.py --fast      # ruff + AST arch/spec-sync/isolation/leak lint (<3s)
  python pob2_cli/scripts/check.py --self-test # mutation proof in %TEMP% that every rule fires
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from arch_lint import SKIP_COPY_DIRS, run_arch_lint  # noqa: E402

# Environment-aware real test ratchets (offline >=120 contract; live engine >=180 & <=5 skipped contract).
MIN_PASSED_OFFLINE = 200
MAX_SKIPPED_OFFLINE = 22
MIN_PASSED_LIVE = 215
MAX_SKIPPED_LIVE = 5

MIN_PASSED_TESTS = MIN_PASSED_OFFLINE
MAX_SKIPPED_TESTS = MAX_SKIPPED_OFFLINE


def detect_test_ratchet(env: Mapping[str, str] | None = None) -> tuple[str, int, int]:
    """Return ``(mode, min_passed, max_skipped)`` based on whether ``POB2_ENGINE_DIR``/``POB2_ROOT`` is set."""
    src = env if env is not None else os.environ
    has_live_engine = bool(src.get("POB2_ENGINE_DIR", "").strip() or src.get("POB2_ROOT", "").strip())
    if has_live_engine:
        return ("live-engine", MIN_PASSED_LIVE, MAX_SKIPPED_LIVE)
    return ("offline", MIN_PASSED_OFFLINE, MAX_SKIPPED_OFFLINE)


def _remove_tree_bottom_up(path: Path) -> None:
    if not path.exists():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        root_p = Path(root)
        for fn in files:
            os.unlink(root_p / fn)
        for dn in dirs:
            os.rmdir(root_p / dn)
    os.rmdir(path)


def _copy_tree_filtered(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src):
        if entry.is_dir(follow_symlinks=False):
            if entry.name in SKIP_COPY_DIRS or entry.name.endswith(".egg-info"):
                continue
            _copy_tree_filtered(Path(entry.path), dst / entry.name)
        elif entry.is_file(follow_symlinks=False):
            if entry.name.endswith((".pyc", ".pyo")):
                continue
            (dst / entry.name).write_bytes(Path(entry.path).read_bytes())


def _utf8_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_ruff(pob2_root: Path) -> tuple[bool, str]:
    targets = [
        str(pob2_root / sub)
        for sub in ("src", "scripts", "packaging", "tests")
        if (pob2_root / sub).is_dir()
    ]
    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--config",
        str(pob2_root / "pyproject.toml"),
        *targets,
    ]
    proc = subprocess.run(
        cmd,
        env=_utf8_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    combined = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, combined


def run_isolated_pytest(
    pob2_root: Path,
    *,
    min_passed: int = MIN_PASSED_TESTS,
    max_skipped: int = MAX_SKIPPED_TESTS,
) -> tuple[bool, str]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="pob2_check_pytest_"))
    try:
        rt_dir = tmp_dir / "runtime"
        cache_dir = tmp_dir / "cache"
        rt_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

        env = _utf8_env()
        env["POB2_RUNTIME_DIR"] = str(rt_dir)
        env["POB2_CACHE_DIR"] = str(cache_dir)
        env["POB2_OFFLINE"] = "1"
        src_str = str(pob2_root / "src")
        prev_pp = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = f"{src_str}{os.pathsep}{prev_pp}" if prev_pp else src_str

        cmd = [sys.executable, "-m", "pytest", str(pob2_root / "tests"), "-q"]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (proc.stdout + "\n" + proc.stderr).strip()
        passed_m = re.search(r"(\d+)\s+passed", out)
        failed_m = re.search(r"(\d+)\s+failed", out)
        skipped_m = re.search(r"(\d+)\s+skipped", out)
        errors_m = re.search(r"(\d+)\s+error", out)
        passed = int(passed_m.group(1)) if passed_m else 0
        failed = int(failed_m.group(1)) if failed_m else 0
        skipped = int(skipped_m.group(1)) if skipped_m else 0
        errors = int(errors_m.group(1)) if errors_m else 0

        ok = (
            proc.returncode == 0
            and failed == 0
            and errors == 0
            and passed >= min_passed
            and skipped <= max_skipped
        )
        summary = (
            f"pytest: passed={passed}, failed={failed}, skipped={skipped}, errors={errors} "
            f"(ratchet>={min_passed}, max_skipped<={max_skipped})"
        )
        if not ok:
            return False, f"{summary}\n{out}"
        return True, summary
    finally:
        _remove_tree_bottom_up(tmp_dir)


def _run_fast_on_staged(check_script: Path, staged: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(check_script), "--fast", "--root", str(staged)],
        env=_utf8_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def run_self_test(pob2_root: Path) -> int:
    """Copy source to %TEMP%, inject mutations for every lint rule, assert each fails with [rule] & Fix:."""
    tmp_root = Path(tempfile.mkdtemp(prefix="pob2_check_selftest_"))
    try:
        staged = tmp_root / "pob2_cli"
        for sub in ("src", "packaging", "scripts", "tests"):
            if (pob2_root / sub).is_dir():
                _copy_tree_filtered(pob2_root / sub, staged / sub)
        for root_file in ("pyproject.toml", "AGENTS.md", "ATTRIBUTION.md"):
            src_f = pob2_root / root_file
            if src_f.is_file():
                (staged / root_file).write_bytes(src_f.read_bytes())

        pkg = staged / "src" / "pob2_cli"
        cross_pkg_name = "pob" + "_cli"
        detached_flag = "DETACHED" + "_PROCESS"
        leak_path = "C:" + "/Users/" + "mou" + "le" + "/secret"
        mutations: list[tuple[str, str, Path, str, str | None]] = [
            (
                "file-budget",
                "file-budget (>650L)",
                pkg / "compat.py",
                "\n" + "\n".join(f"# pad {i}" for i in range(660)) + "\n",
                None,
            ),
            (
                "fn-budget",
                "fn-budget (>200L)",
                pkg / "compat.py",
                "\n\ndef _selftest_huge_fn() -> int:\n"
                + "".join(f"    _x = {i}\n" for i in range(210))
                + "    return _x\n",
                None,
            ),
            (
                "layer-dag",
                "layer-dag (top-level L0->L4)",
                pkg / "errors.py",
                "\nfrom . import cli as _illegal_cli  # noqa: F401\n",
                None,
            ),
            (
                "layer-dag",
                "layer-dag (lazy function-body L0->L4)",
                pkg / "compat.py",
                "\n\ndef _selftest_lazy_upward() -> object:\n"
                "    from .cli import main\n\n"
                "    return main\n",
                None,
            ),
            (
                "cross-pkg-import",
                "cross-pkg-import (import PoE1 pkg)",
                pkg / "compat.py",
                f"\nimport {cross_pkg_name}  # noqa: F401\n",
                None,
            ),
            (
                "win-subprocess",
                "win-subprocess (forbidden creation flag)",
                pkg / "compat.py",
                f"\nimport subprocess\n_BAD_FLAG = getattr(subprocess, '{detached_flag}', 0)\n",
                None,
            ),
            (
                "blocked-rpc",
                "blocked-rpc (tj_open)",
                pkg / "compat.py",
                '\n\ndef _selftest_rpc(worker: object) -> None:\n'
                '    worker.call("tj_open", {})  # type: ignore[attr-defined]\n',
                None,
            ),
            (
                "local-path-leak",
                "local-path-leak (hardcoded local path)",
                pkg / "compat.py",
                f'\n_LEAK = "{leak_path}"\n',
                None,
            ),
            (
                "ghost-layer-map",
                "ghost-layer-map (non-existent LAYER_MAP unit)",
                staged / "scripts" / "arch_lint.py",
                '\nLAYER_MAP["ghost_nonexistent_unit"] = 2\n',
                None,
            ),
            (
                "intra-pkg-cycle",
                "intra-pkg-cycle (subpackage circular import)",
                pkg / "craft" / "models.py",
                "\nfrom .weights_loader import load_mods_view as _cycle_ref  # noqa: E402, F401\n",
                None,
            ),
            (
                "spec-sync",
                "spec-sync (asset/module drift in pob2.spec)",
                staged / "packaging" / "pob2.spec",
                '    "headless_runner.lua",\n',
                "",
            ),
        ]

        check_script = staged / "scripts" / "check.py"
        for expected_rule, label, target_file, snippet, replace_with in mutations:
            original = target_file.read_bytes()
            try:
                if replace_with is None:
                    target_file.write_bytes(original + snippet.encode("utf-8"))
                else:
                    text = original.decode("utf-8")
                    assert snippet in text, f"Expected snippet {snippet!r} in {target_file}"
                    target_file.write_bytes(text.replace(snippet, replace_with, 1).encode("utf-8"))
                proc = _run_fast_on_staged(check_script, staged)
                combined = proc.stdout + "\n" + proc.stderr
                if proc.returncode == 0:
                    print(f"SELF-TEST FAILED: mutation `{label}` unexpectedly passed!")
                    return 1
                if f"[{expected_rule}]" not in combined or "Fix:" not in combined:
                    print(f"SELF-TEST FAILED: mutation `{label}` missing [{expected_rule}]/Fix:\n{combined}")
                    return 1
                print(f"SELF-TEST OK [{label}]: caught mutation as expected.")
            finally:
                target_file.write_bytes(original)

        clean_proc = _run_fast_on_staged(check_script, staged)
        if clean_proc.returncode != 0:
            print(f"SELF-TEST FAILED: clean copy failed --fast:\n{clean_proc.stdout}\n{clean_proc.stderr}")
            return 1
        total_checks = len(mutations)
    finally:
        _remove_tree_bottom_up(tmp_root)

    if tmp_root.exists():
        print(f"SELF-TEST FAILED: leftover temp residue at {tmp_root}", file=sys.stderr)
        return 1
    print(f"SELF-TEST PASSED: all {total_checks} mutation checks + clean baseline verified (0 residue).")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="pob2_cli lint, architecture DAG, and test ratchet gate.")
    parser.add_argument("--fast", action="store_true", help="Run ruff + AST arch-lint only (<3s).")
    parser.add_argument("--self-test", action="store_true", help="Run mutation self-test in %%TEMP%%.")
    parser.add_argument("--root", type=Path, default=None, help="Override pob2_cli root directory.")
    parser.add_argument("--min-passed", type=int, default=None, help="Override minimum passed tests.")
    parser.add_argument("--max-skipped", type=int, default=None, help="Override maximum skipped tests.")
    args = parser.parse_args(argv)

    pob2_root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    if args.self_test:
        return run_self_test(pob2_root)

    t0 = time.perf_counter()
    ruff_ok, ruff_out = run_ruff(pob2_root)
    violations, stats = run_arch_lint(pob2_root)

    failed = False
    if not ruff_ok:
        failed = True
        print("FAIL [ruff] ruff check reported lint violations | Fix: run `python -m ruff check --config pob2_cli/pyproject.toml pob2_cli/src pob2_cli/scripts pob2_cli/packaging pob2_cli/tests`")
        if ruff_out:
            print(ruff_out)
    if violations:
        failed = True
        for v in violations:
            print(v)

    elapsed_fast = time.perf_counter() - t0
    if failed:
        print(f"check.py FAILED in {elapsed_fast:.2f}s ({len(violations)} arch violations, ruff_ok={ruff_ok}).")
        return 1

    ml, ml_f = stats["maxLines"]  # type: ignore[misc]
    mb, mb_f = stats["maxBytes"]  # type: ignore[misc]
    mf, mf_f = stats["maxFn"]  # type: ignore[misc]
    print(
        f"FAST GATE OK in {elapsed_fast:.2f}s | pkg_files={stats['files']} checked={stats['checkedFiles']} | "
        f"max={ml}L ({ml_f}), {mb}B ({mb_f}), fn={mf}L ({mf_f}) | ruff=0 | arch=0"
    )
    if args.fast:
        return 0

    mode_label, def_min_passed, def_max_skipped = detect_test_ratchet()
    min_passed = args.min_passed if args.min_passed is not None else def_min_passed
    max_skipped = args.max_skipped if args.max_skipped is not None else def_max_skipped
    print(f"INFO: mode={mode_label} | ratchet: min_passed>={min_passed}, max_skipped<={max_skipped}")

    pytest_ok, pytest_msg = run_isolated_pytest(
        pob2_root,
        min_passed=min_passed,
        max_skipped=max_skipped,
    )
    total_elapsed = time.perf_counter() - t0
    if not pytest_ok:
        print(f"FAIL [pytest] isolated {mode_label} test gate failed in {total_elapsed:.2f}s | Fix: inspect failures below.")
        print(pytest_msg)
        return 1

    print(f"FULL GATE OK ({mode_label}) in {total_elapsed:.2f}s | {pytest_msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
