"""AST architecture, budget, layer-DAG, intra-pkg cycle, spec-sync, and isolation linter for ``pob2_cli``."""

from __future__ import annotations

import ast
from pathlib import Path

from arch_lint_spec import (
    FORBIDDEN_RUNTIME_DEPS,
    SKIP_COPY_DIRS,
    check_ghost_layer_map,
    check_policy_and_leaks,
    check_spec_sync,
)

MAX_FILE_LINES = 650
MAX_FILE_BYTES = 24000
MAX_TEST_LINES = 1000
MAX_TEST_BYTES = 38000
MAX_FN_LINES = 200

# 5-layer unidirectional architecture (L0 -> L1 -> L2 -> L3 -> L4): exact real units only (0 ghosts).
LAYER_MAP: dict[str, int] = {
    "__init__": 0,
    "game_profile": 0,
    "os_util": 0,
    "compat": 0,
    "json_io": 0,
    "errors": 0,
    "han_fold": 0,
    "locale_cfg": 0,
    "share_codec": 0,
    "guardrails": 0,
    "updater": 1,
    "bridge_protocol": 1,
    "bridge_client": 1,
    "bridge_boot": 1,
    "importers": 1,
    "game_data": 1,
    "formatters": 2,
    "snapshot": 2,
    "analyzer": 2,
    "mutators": 2,
    "comparator": 2,
    "craft": 2,
    "rune": 2,
    "rebalance": 2,
    "daemon": 3,
    "__main__": 4,
    "cli_common": 4,
    "cli_errors": 4,
    "cli": 4,
    "cli_craft": 4,
    "cli_item": 4,
    "cli_ground": 4,
    "cli_import": 4,
    "cli_lifecycle": 4,
    "cli_build_io": 4,
    "cli_inspect": 4,
    "cli_mutate": 4,
    "cli_what_if_helpers": 4,
    "cli_rune": 4,
    "cli_rebalance": 4,
    "cli_help": 4,
}

BLOCKED_RPC_EXACT: frozenset[str] = frozenset(
    {
        "tj_open",
        "tj_close",
        "tattoo_open",
        "config_mod_search",
        "config_mod_add",
        "set_pob_options",
        "oauth_logout",
        "import_from_uri",
        "save_build_as",
    }
)
RPC_ALLOWLIST_FILES: frozenset[str] = frozenset({"bridge_client.py", "bridge_protocol.py"})
_DETACHED = "DETACHED" + "_PROCESS"
_CROSS_PKG = "pob" + "_cli"


def _fail(rule: str, path: str, line: int, problem: str, fix: str) -> str:
    return f"FAIL [{rule}] {path}:{line} -- {problem} | Fix: {fix}"


def _top_unit(rel: Path) -> str:
    return rel.stem if len(rel.parts) == 1 else rel.parts[0]


def _resolve_import(rel: Path, node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name
            if name == "pob2_cli":
                out.append(("__init__", "pob2_cli"))
            elif name.startswith("pob2_cli."):
                out.append((name.split(".", 2)[1], name))
            else:
                out.append((f"ext:{name.split('.')[0]}", name))
        return out
    if node.level == 0:
        mod = node.module or ""
        if mod == "pob2_cli":
            out.extend((a.name, f"pob2_cli.{a.name}") for a in node.names)
        elif mod.startswith("pob2_cli."):
            out.append((mod.split(".", 2)[1], mod))
        elif mod:
            out.append((f"ext:{mod.split('.')[0]}", mod))
        return out
    pkg_parts = list(rel.parts[:-1])
    up = node.level - 1
    if up > len(pkg_parts):
        return out
    resolved = pkg_parts[: len(pkg_parts) - up] + [p for p in (node.module or "").split(".") if p]
    if not resolved:
        out.extend((a.name, f"pob2_cli.{a.name}") for a in node.names if a.name != "*")
    else:
        out.append((resolved[0], "pob2_cli." + ".".join(resolved)))
    return out


def _module_ref_exists(pkg_root: Path, parts: list[str]) -> str | None:
    if not parts:
        return "__init__"
    if pkg_root.joinpath(*parts).with_suffix(".py").is_file():
        return ".".join(parts)
    if (pkg_root.joinpath(*parts) / "__init__.py").is_file():
        return ".".join([*parts, "__init__"])
    return None


def _resolve_intra_targets(pkg_root: Path, rel: Path, node: ast.Import | ast.ImportFrom) -> list[str]:
    out: list[str] = []
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.name.startswith("pob2_cli."):
                hit = _module_ref_exists(pkg_root, a.name[len("pob2_cli.") :].split("."))
                if hit:
                    out.append(hit)
        return out
    pkg_parts = list(rel.parts[:-1])
    if node.level == 0:
        mod = node.module or ""
        if not mod.startswith("pob2_cli"):
            return out
        base_parts = [p for p in mod[len("pob2_cli") :].strip(".").split(".") if p]
    else:
        up = node.level - 1
        if up > len(pkg_parts):
            return out
        base_parts = pkg_parts[: len(pkg_parts) - up] + [p for p in (node.module or "").split(".") if p]
    for a in node.names:
        hit = (
            _module_ref_exists(pkg_root, base_parts)
            if a.name == "*"
            else (_module_ref_exists(pkg_root, [*base_parts, a.name]) or _module_ref_exists(pkg_root, base_parts))
        )
        if hit:
            out.append(hit)
    return out


def _is_tc_guard(node: ast.If) -> bool:
    t = node.test
    return (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or (
        isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"
    )


def _iter_runtime_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []

    def _walk(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_tc_guard(node):
            for s in node.orelse:
                _walk(s)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
            return
        for ch in ast.iter_child_nodes(node):
            _walk(ch)

    _walk(tree)
    return found


def _iter_top_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []

    def _collect(stmts: list[ast.stmt]) -> None:
        for s in stmts:
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                found.append(s)
            elif isinstance(s, ast.If) and not _is_tc_guard(s):
                _collect(s.body)
                _collect(s.orelse)
            elif isinstance(s, ast.Try):
                _collect(s.body)
                for h in s.handlers:
                    _collect(h.body)
                _collect(s.orelse)
                _collect(s.finalbody)

    _collect(tree.body)
    return found


def _check_cross_pkg(tree: ast.Module, disp: str) -> list[str]:
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == _CROSS_PKG or a.name.startswith(f"{_CROSS_PKG}."):
                    bad.append(_fail("cross-pkg-import", disp, node.lineno, f"Forbidden import `{a.name}`", "Keep pob2_cli isolated from PoE1."))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == _CROSS_PKG or mod.startswith(f"{_CROSS_PKG}."):
                bad.append(_fail("cross-pkg-import", disp, node.lineno, f"Forbidden import `from {mod}`", "Keep pob2_cli isolated from PoE1."))
        elif isinstance(node, ast.Call):
            fn = node.func
            is_imp = (isinstance(fn, ast.Attribute) and fn.attr == "import_module") or (
                isinstance(fn, ast.Name) and fn.id == "__import__"
            )
            if is_imp and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                tgt = node.args[0].value
                if tgt == _CROSS_PKG or tgt.startswith(f"{_CROSS_PKG}."):
                    bad.append(_fail("cross-pkg-import", disp, node.lineno, f"Forbidden dynamic import `{tgt}`", "Never import PoE1 package."))
    return bad


def _check_win_subproc(tree: ast.Module, rel_posix: str) -> list[str]:
    bad: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == _DETACHED) or (
            isinstance(node, ast.Name) and node.id == _DETACHED
        ):
            bad.append(_fail("win-subprocess", rel_posix, node.lineno, f"Forbidden flag `{_DETACHED}`", "Use os_util hidden spawn helpers."))
        elif isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Name)
                and fn.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == _DETACHED
            ):
                bad.append(_fail("win-subprocess", rel_posix, node.lineno, f"Forbidden getattr `{_DETACHED}`", "Use os_util hidden spawn helpers."))
            if (
                rel_posix != "os_util.py"
                and isinstance(fn, ast.Attribute)
                and fn.attr in ("Popen", "run", "call", "check_call", "check_output")
                and isinstance(fn.value, ast.Name)
                and fn.value.id in ("subprocess", "sub")
                and not any(
                    kw.arg is None
                    and isinstance(kw.value, ast.Call)
                    and isinstance(kw.value.func, ast.Name)
                    and kw.value.func.id == "win_hidden_subprocess_kwargs"
                    for kw in node.keywords
                )
            ):
                bad.append(_fail("win-subprocess", rel_posix, node.lineno, f"Unwrapped `{fn.value.id}.{fn.attr}`", "Pass **win_hidden_subprocess_kwargs() or use os_util.spawn_detached_process."))
    return bad


def _is_blocked_rpc(name: str) -> bool:
    return name in BLOCKED_RPC_EXACT or name.startswith("compare_") or name.endswith("_tree_url")


def _check_blocked_rpc(tree: ast.AST, rel_posix: str) -> list[str]:
    if rel_posix in RPC_ALLOWLIST_FILES:
        return []
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        if fname == "call" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            if _is_blocked_rpc(node.args[0].value):
                bad.append(_fail("blocked-rpc", rel_posix, node.lineno, f"Forbidden PoB2 RPC `{node.args[0].value}`", "Remove blocked RPC call."))
        elif fname in ("call_batch", "_call_batch"):
            for arg in node.args:
                if isinstance(arg, (ast.List, ast.Tuple)):
                    for elt in arg.elts:
                        if isinstance(elt, (ast.List, ast.Tuple)) and elt.elts:
                            head = elt.elts[0]
                            if isinstance(head, ast.Constant) and isinstance(head.value, str) and _is_blocked_rpc(head.value):
                                bad.append(_fail("blocked-rpc", rel_posix, head.lineno, f"Forbidden batch RPC `{head.value}`", "Remove blocked RPC from batch."))
    return bad


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    visited: set[str] = set()
    stack: list[str] = []
    in_stack: set[str] = set()
    cycles: list[list[str]] = []

    def _dfs(node: str) -> None:
        visited.add(node)
        stack.append(node)
        in_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt in in_stack:
                cycles.append([*stack[stack.index(nxt) :], nxt])
            elif nxt not in visited:
                _dfs(nxt)
        stack.pop()
        in_stack.remove(node)

    for n in sorted(graph):
        if n not in visited:
            _dfs(n)
    return cycles


def _check_size(
    fpath: Path,
    disp: str,
    violations: list[str],
    extremes: dict[str, tuple[int, str]],
    *,
    max_l: int = MAX_FILE_LINES,
    max_b: int = MAX_FILE_BYTES,
    track: bool = True,
) -> str:
    raw = fpath.read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n")
    lines = len(text.splitlines())
    bcount = max(len(raw), len(text.encode("utf-8")) + lines)
    if track:
        if lines > extremes["maxLines"][0]:
            extremes["maxLines"] = (lines, disp)
        if bcount > extremes["maxBytes"][0]:
            extremes["maxBytes"] = (bcount, disp)
    if lines > max_l or bcount > max_b:
        violations.append(_fail("file-budget", disp, 1, f"{lines}L / {bcount}B exceeds {max_l}L / {max_b}B", "Split into smaller submodules."))
    return text


def _check_py_file(
    fpath: Path,
    disp: str,
    violations: list[str],
    extremes: dict[str, tuple[int, str]],
    *,
    max_l: int = MAX_FILE_LINES,
    max_b: int = MAX_FILE_BYTES,
    check_fn: bool = True,
) -> ast.Module:
    text = _check_size(fpath, disp, violations, extremes, max_l=max_l, max_b=max_b, track=check_fn)
    tree = ast.parse(text, filename=disp)
    if check_fn:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                if span > extremes["maxFn"][0]:
                    extremes["maxFn"] = (span, f"{disp}:{node.name}")
                if span > MAX_FN_LINES:
                    violations.append(_fail("fn-budget", disp, node.lineno, f"Function `{node.name}` spans {span}L (limit {MAX_FN_LINES}L)", "Split into focused helpers."))
    return tree


def _check_layers_and_intra(
    pkg_root: Path,
    rel: Path,
    rel_posix: str,
    tree: ast.Module,
    violations: list[str],
    edges: dict[str, set[str]],
    sites: dict[tuple[str, str], tuple[str, int]],
    intra_top: dict[str, dict[str, set[str]]],
    intra_rt: dict[str, dict[str, set[str]]],
    intra_sites: dict[tuple[str, str], tuple[str, int]],
) -> None:
    src_u = _top_unit(rel)
    src_l = LAYER_MAP.get(src_u)
    if src_l is None:
        violations.append(_fail("layer-dag", rel_posix, 1, f"Unit `{src_u}` missing from LAYER_MAP", "Register unit in scripts/arch_lint.py."))
    else:
        edges.setdefault(src_u, set())
    for node in _iter_runtime_imports(tree):
        for dst_u, _ in _resolve_import(rel, node):
            if dst_u.startswith("ext:") or dst_u == src_u or src_l is None:
                continue
            dst_l = LAYER_MAP.get(dst_u)
            if dst_l is not None and dst_l > src_l:
                violations.append(_fail("layer-dag", rel_posix, node.lineno, f"Upward import L{src_l} `{src_u}` -> L{dst_l} `{dst_u}`", "Depend only downward (L4 -> L0)."))
    for node in _iter_top_imports(tree):
        for dst_u, _ in _resolve_import(rel, node):
            if not dst_u.startswith("ext:") and dst_u != src_u and src_l is not None and dst_u in LAYER_MAP:
                edges.setdefault(src_u, set()).add(dst_u)
                sites.setdefault((src_u, dst_u), (rel_posix, node.lineno))

    if len(rel.parts) >= 2:
        sub = rel.parts[0]
        src_m = ".".join(rel.with_suffix("").parts)
        intra_top.setdefault(sub, {}).setdefault(src_m, set())
        if not src_m.endswith(".__init__"):
            intra_rt.setdefault(sub, {}).setdefault(src_m, set())
        for node in _iter_top_imports(tree):
            for dst_m in _resolve_intra_targets(pkg_root, rel, node):
                if dst_m.startswith(f"{sub}.") and dst_m != src_m:
                    intra_top[sub][src_m].add(dst_m)
                    intra_sites.setdefault((src_m, dst_m), (rel_posix, node.lineno))
        if not src_m.endswith(".__init__"):
            for node in _iter_runtime_imports(tree):
                for dst_m in _resolve_intra_targets(pkg_root, rel, node):
                    if dst_m.startswith(f"{sub}.") and not dst_m.endswith(".__init__") and dst_m != src_m:
                        intra_rt[sub][src_m].add(dst_m)
                        intra_sites.setdefault((src_m, dst_m), (rel_posix, node.lineno))


def run_arch_lint(pob2_root: Path) -> tuple[list[str], dict[str, object]]:
    pkg_root = pob2_root / "src" / "pob2_cli"
    py_files = sorted(pkg_root.rglob("*.py")) if pkg_root.is_dir() else []
    violations: list[str] = check_ghost_layer_map(pkg_root, LAYER_MAP)
    edges: dict[str, set[str]] = {}
    sites: dict[tuple[str, str], tuple[str, int]] = {}
    intra_top: dict[str, dict[str, set[str]]] = {}
    intra_rt: dict[str, dict[str, set[str]]] = {}
    intra_sites: dict[tuple[str, str], tuple[str, int]] = {}
    extremes: dict[str, tuple[int, str]] = {"maxLines": (0, ""), "maxBytes": (0, ""), "maxFn": (0, "")}

    if (pkg_root / "ticks.py").exists():
        violations.append(_fail("no-poe1-ticks", "src/pob2_cli/ticks.py", 1, "PoE1 ticks.py forbidden in pob2_cli", "Delete ticks.py."))

    for fp in py_files:
        rel = fp.relative_to(pkg_root)
        rp = rel.as_posix()
        tree = _check_py_file(fp, rp, violations, extremes)
        _check_layers_and_intra(pkg_root, rel, rp, tree, violations, edges, sites, intra_top, intra_rt, intra_sites)
        violations.extend(_check_cross_pkg(tree, f"src/pob2_cli/{rp}"))
        violations.extend(_check_win_subproc(tree, rp))
        violations.extend(_check_blocked_rpc(tree, rp))

    extra = 0
    lua_dir = pkg_root / "lua"
    if lua_dir.is_dir():
        for fp in sorted(lua_dir.glob("*.lua")):
            _check_size(fp, fp.relative_to(pkg_root).as_posix(), violations, extremes)
            extra += 1
    for sub in ("scripts", "packaging"):
        d = pob2_root / sub
        if d.is_dir():
            for fp in sorted(d.rglob("*.py")):
                if any(p in SKIP_COPY_DIRS for p in fp.parts):
                    continue
                disp = fp.relative_to(pob2_root).as_posix()
                tree = _check_py_file(fp, disp, violations, extremes)
                violations.extend(_check_cross_pkg(tree, disp))
                extra += 1
    tests_dir = pob2_root / "tests"
    if tests_dir.is_dir():
        for fp in sorted(tests_dir.rglob("*.py")):
            if any(p in SKIP_COPY_DIRS for p in fp.parts):
                continue
            disp = fp.relative_to(pob2_root).as_posix()
            tree = _check_py_file(fp, disp, violations, extremes, max_l=MAX_TEST_LINES, max_b=MAX_TEST_BYTES, check_fn=False)
            violations.extend(_check_cross_pkg(tree, disp))
            extra += 1

    for cyc in _find_cycles(edges):
        sf, sl = sites.get((cyc[0], cyc[1]), ("src/pob2_cli", 1))
        violations.append(_fail("layer-dag", sf, sl, f"Circular import cycle: {' -> '.join(cyc)}", "Break cycle via lower layer."))

    seen_intra: set[tuple[str, ...]] = set()
    for sub in sorted(set(intra_top) | set(intra_rt)):
        for graph in (intra_top.get(sub, {}), intra_rt.get(sub, {})):
            for cyc in _find_cycles(graph):
                key = tuple(cyc)
                if key not in seen_intra:
                    seen_intra.add(key)
                    sf, sl = intra_sites.get((cyc[0], cyc[1]), (f"src/pob2_cli/{sub}", 1))
                    violations.append(
                        _fail(
                            "intra-pkg-cycle",
                            sf,
                            sl,
                            f"Intra-package circular import in `{sub}`: {' -> '.join(cyc)}",
                            "Extract shared types/helpers into a leaf module within the subpackage.",
                        )
                    )

    violations.extend(
        check_spec_sync(pob2_root, pkg_root, extremes, check_size_fn=_check_size, check_cross_pkg_fn=_check_cross_pkg)
    )
    violations.extend(check_policy_and_leaks(pob2_root))
    return violations, {
        "files": len(py_files),
        "checkedFiles": len(py_files) + extra,
        "maxLines": extremes["maxLines"],
        "maxBytes": extremes["maxBytes"],
        "maxFn": extremes["maxFn"],
    }


__all__ = [
    "BLOCKED_RPC_EXACT",
    "FORBIDDEN_RUNTIME_DEPS",
    "LAYER_MAP",
    "MAX_FILE_BYTES",
    "MAX_FILE_LINES",
    "MAX_FN_LINES",
    "MAX_TEST_BYTES",
    "MAX_TEST_LINES",
    "RPC_ALLOWLIST_FILES",
    "SKIP_COPY_DIRS",
    "run_arch_lint",
]
