"""Spec-sync, ghost LAYER_MAP, pyproject policy, and local-path leak checks for ``pob2_cli``."""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

FORBIDDEN_RUNTIME_DEPS: frozenset[str] = frozenset({"pydantic", "typer", "rich", "mcp"})
SKIP_COPY_DIRS: frozenset[str] = frozenset(
    {"__pycache__", "build", "dist", ".git", ".venv", ".runtime", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
_TEXT_EXTS: frozenset[str] = frozenset(
    {".py", ".spec", ".lua", ".md", ".toml", ".txt", ".json", ".yml", ".yaml", ".sh", ".ps1"}
)
_FORBIDDEN_FILES: frozenset[str] = frozenset({"PROGRESS.md", "SPEC.md"})
_BUDGET_IGNORE = "code-budget:" + " ignore"
_USER_TOK = "mou" + "le"

_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("win-user-path", re.compile(r"(?i)(?<![A-Za-z0-9_])C:[/\\]Users(?:[/\\]|$)")),
    ("local-f-drive", re.compile(r"(?i)(?<![A-Za-z0-9_])F:[/\\]")),
    ("posix-home", re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[A-Za-z0-9._-]+/")),
    ("smb-host", re.compile(r"(?i)\\\\Tui7(?:\\|$)")),
    ("local-seed", re.compile(r"(?i)(?:Poe" + r"Charm3\[|PoE2" + r"cache)")),
    (
        "local-user",
        re.compile(
            r"(?i)(?:(?<![A-Za-z0-9_])"
            + _USER_TOK
            + r"(?![A-Za-z0-9_])|"
            + _USER_TOK
            + r"[-_]host|[/\\]"
            + _USER_TOK
            + r"(?:[/\\]|$))"
        ),
    ),
)


def _fail(rule: str, path: str, line: int, problem: str, fix: str) -> str:
    return f"FAIL [{rule}] {path}:{line} -- {problem} | Fix: {fix}"


def check_ghost_layer_map(pkg_root: Path, layer_map: dict[str, int]) -> list[str]:
    if not pkg_root.is_dir():
        return []
    real_units = {f.stem for f in pkg_root.glob("*.py")} | {
        d.name
        for d in pkg_root.iterdir()
        if d.is_dir() and d.name not in SKIP_COPY_DIRS and d.name not in ("data", "lua") and any(d.rglob("*.py"))
    }
    bad: list[str] = []
    for unit, layer in sorted(layer_map.items()):
        if layer not in (0, 1, 2, 3, 4):
            bad.append(_fail("ghost-layer-map", "scripts/arch_lint.py", 1, f"Invalid layer `{layer}` for `{unit}`", "Use layer 0..4."))
        if unit not in real_units:
            bad.append(
                _fail(
                    "ghost-layer-map",
                    "scripts/arch_lint.py",
                    1,
                    f"Ghost LAYER_MAP entry `{unit}` does not exist under src/pob2_cli/",
                    "Delete ghost entry from LAYER_MAP in scripts/arch_lint.py.",
                )
            )
    return bad


def _extract_spec_tuples(spec_tree: ast.Module) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for stmt in spec_tree.body:
        name = None
        val = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name, val = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name, val = stmt.target.id, stmt.value
        if name in ("_LUA_ASSETS", "_DATA_ASSETS", "_CLI_MODULES", "_SUBPACKAGES") and val is not None:
            try:
                seq = ast.literal_eval(val)
            except (ValueError, SyntaxError):
                continue
            if isinstance(seq, (tuple, list, set, frozenset)):
                out[name] = {str(x) for x in seq}
    return out


def check_spec_sync(
    pob2_root: Path,
    pkg_root: Path,
    extremes: dict[str, tuple[int, str]],
    *,
    check_size_fn: Callable[..., str],
    check_cross_pkg_fn: Callable[[ast.Module, str], list[str]],
) -> list[str]:
    spec_path = pob2_root / "packaging" / "pob2.spec"
    entry_path = pob2_root / "packaging" / "pyinstaller_entry.py"
    if not spec_path.is_file():
        return [_fail("spec-sync", "packaging/pob2.spec", 1, "Missing PyInstaller spec file", "Restore pob2_cli/packaging/pob2.spec.")]
    bad: list[str] = []
    if not entry_path.is_file():
        bad.append(_fail("spec-sync", "packaging/pyinstaller_entry.py", 1, "Missing pyinstaller_entry.py", "Restore pob2_cli/packaging/pyinstaller_entry.py."))
    spec_text = check_size_fn(spec_path, "packaging/pob2.spec", bad, extremes)
    spec_tree = ast.parse(spec_text, filename="packaging/pob2.spec")
    bad.extend(check_cross_pkg_fn(spec_tree, "packaging/pob2.spec"))
    consts = _extract_spec_tuples(spec_tree)
    if 'collect_submodules("pob2_cli")' not in spec_text and "collect_submodules('pob2_cli')" not in spec_text:
        bad.append(_fail("spec-sync", "packaging/pob2.spec", 1, "Missing collect_submodules('pob2_cli') in hiddenimports", "Include collect_submodules('pob2_cli') in pob2.spec."))

    lua_dir = pkg_root / "lua"
    actual_lua = {f.name for f in lua_dir.glob("*.lua")} if lua_dir.is_dir() else set()
    data_dir = pkg_root / "data"
    actual_data = (
        {f.name for f in data_dir.iterdir() if f.is_file() and f.name.endswith((".json", ".json.gz", ".lua.gz"))}
        if data_dir.is_dir()
        else set()
    )
    actual_cli = {f"pob2_cli.{f.stem}" for f in pkg_root.glob("cli*.py")} if pkg_root.is_dir() else set()
    actual_subs: set[str] = set()
    if pkg_root.is_dir():
        for d in sorted(pkg_root.iterdir()):
            if not d.is_dir() or d.name in SKIP_COPY_DIRS or d.name in ("data", "lua") or not any(d.rglob("*.py")):
                continue
            if not (d / "__init__.py").is_file():
                bad.append(_fail("spec-sync", f"src/pob2_cli/{d.name}", 1, f"Subpackage `{d.name}` missing __init__.py", f"Create src/pob2_cli/{d.name}/__init__.py."))
            else:
                actual_subs.add(f"pob2_cli.{d.name}")

    for key, actual, label in (
        ("_LUA_ASSETS", actual_lua, "lua asset"),
        ("_DATA_ASSETS", actual_data, "data asset"),
        ("_CLI_MODULES", actual_cli, "CLI module"),
        ("_SUBPACKAGES", actual_subs, "subpackage"),
    ):
        declared = consts.get(key)
        if declared is None:
            bad.append(_fail("spec-sync", "packaging/pob2.spec", 1, f"Missing `{key}` manifest tuple in pob2.spec", f"Define `{key}` in packaging/pob2.spec."))
            continue
        for missing in sorted(actual - declared):
            bad.append(_fail("spec-sync", "packaging/pob2.spec", 1, f"Unlisted {label} `{missing}` missing from `{key}`", f"Add `{missing}` to `{key}` in packaging/pob2.spec."))
        for extra in sorted(declared - actual):
            bad.append(_fail("spec-sync", "packaging/pob2.spec", 1, f"Stale {label} `{extra}` in `{key}` does not exist", f"Remove `{extra}` from `{key}` in packaging/pob2.spec."))

    cli_path = pkg_root / "cli.py"
    if cli_path.is_file():
        cli_tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename="src/pob2_cli/cli.py")
        for stmt in cli_tree.body:
            tid = (
                stmt.target.id
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                else (
                    stmt.targets[0].id
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                    else ""
                )
            )
            val = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
            if tid in ("_LAZY_SUBCOMMANDS", "_OPTIONAL_SUBCOMMANDS") and isinstance(val, ast.Dict):
                for item in val.values:
                    if isinstance(item, ast.Tuple) and item.elts and isinstance(item.elts[0], ast.Constant) and isinstance(item.elts[0].value, str):
                        mod_rel = item.elts[0].value.lstrip(".")
                        if not ((pkg_root / f"{mod_rel}.py").is_file() or (pkg_root / mod_rel / "__init__.py").is_file()):
                            bad.append(_fail("spec-sync", "src/pob2_cli/cli.py", item.lineno, f"Subcommand target `{item.elts[0].value}` missing under src/pob2_cli/", f"Create `{mod_rel}` or update `{tid}`."))
    return bad


def check_policy_and_leaks(pob2_root: Path) -> list[str]:
    bad: list[str] = []
    pp = pob2_root / "pyproject.toml"
    if not pp.is_file():
        bad.append(_fail("pkg-policy", "pyproject.toml", 1, "Missing pyproject.toml", "Restore pob2_cli/pyproject.toml."))
    else:
        proj = tomllib.loads(pp.read_text(encoding="utf-8")).get("project", {})
        lic = proj.get("license", {})
        lic_text = lic.get("text") if isinstance(lic, dict) else lic
        if lic_text != "GPL-3.0-only":
            bad.append(_fail("pkg-policy", "pyproject.toml", 1, f"License must be GPL-3.0-only, got `{lic_text}`", 'Set license = { text = "GPL-3.0-only" }.'))
        for clf in proj.get("classifiers", []):
            if "or later" in str(clf).lower() or "gplv3+" in str(clf).lower():
                bad.append(_fail("pkg-policy", "pyproject.toml", 1, f"Invalid classifier `{clf}`", "Use GNU General Public License v3 (GPLv3)."))
        for dep in proj.get("dependencies", []):
            pkg = re.split(r"[<>=!~;\[\s]", str(dep).strip(), maxsplit=1)[0].lower()
            if pkg in FORBIDDEN_RUNTIME_DEPS:
                bad.append(_fail("pkg-policy", "pyproject.toml", 1, f"Forbidden runtime dep `{dep}`", "Restrict deps to click, httpx[socks], tabulate."))

    for fp in sorted(pob2_root.rglob("*")):
        if not fp.is_file():
            continue
        rel = fp.relative_to(pob2_root)
        if any(p in SKIP_COPY_DIRS or p.endswith(".egg-info") for p in rel.parts):
            continue
        disp = rel.as_posix()
        if fp.name in _FORBIDDEN_FILES or fp.name.endswith("_PLAN.md"):
            bad.append(_fail("local-path-leak", disp, 1, f"Forbidden internal doc `{fp.name}`", "Remove bot working notes from git."))
            continue
        if fp.suffix.lower() not in _TEXT_EXTS:
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _BUDGET_IGNORE in line:
                bad.append(_fail("budget-ignore", disp, lineno, "Forbidden budget-ignore pragma", "Split file under size budget."))
            for tag, pat in _LEAK_PATTERNS:
                if pat.search(line):
                    bad.append(_fail("local-path-leak", disp, lineno, f"Local path/username leak ({tag})", "Use POB2_ENGINE_DIR/POB2_DATA_DIR env vars instead."))
                    break
    return bad
