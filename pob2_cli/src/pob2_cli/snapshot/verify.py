"""Rollback verifier: confirm the restored PoE2 build recalculates identical stats and passive points."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from .checkpoint import supports_direct_calc
from .helpers import extract_points_used, parse_numeric_or_none


def _stat_number(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, str) and not val.strip():
        return None
    return parse_numeric_or_none(val)


def stat_drift(before: dict[str, Any], after: dict[str, Any], keys: Iterable[str] | None = None) -> list[str]:
    """Sorted stat keys whose values differ (1e-6 relative tolerance; inf/nan compared by kind)."""
    differing: list[str] = []
    for k in set(before) | set(after) if keys is None else keys:
        b, a = _stat_number(before.get(k)), _stat_number(after.get(k))
        if b is None and a is None:
            continue
        if b is None or a is None:
            differing.append(k)
        elif math.isnan(b) or math.isnan(a):
            if not (math.isnan(b) and math.isnan(a)):
                differing.append(k)
        elif math.isinf(b) or math.isinf(a):
            if a != b:
                differing.append(k)
        elif abs(a - b) > 1e-6 * max(1.0, abs(b)):
            differing.append(k)
    return sorted(differing)


def _points_before(before_digest: dict[str, Any]) -> int | None:
    raw = before_digest.get("pointsUsed")
    num = parse_numeric_or_none(raw) if (raw is not None and not isinstance(raw, bool)) else None
    if num is not None and math.isfinite(num):
        return int(num)
    return extract_points_used(before_digest.get("buildInfo"), before_digest.get("stats") or {})


def _points_after(worker: Any, res: dict[str, Any]) -> int | None:
    pts = extract_points_used(stats=res.get("stats") or {}, pts=res.get("points"))
    if pts is not None:
        return pts
    for rpc_name in ("get_build_info", "get_tree_state"):
        try:
            aux = worker.call(rpc_name, {}) or {}
        except (RuntimeError, OSError):
            continue
        pts = extract_points_used(aux, pts=aux.get("points") if isinstance(aux, dict) else None)
        if pts is not None:
            return pts
    return None


def verify_build(worker: Any, before_digest: dict[str, Any], *, code: str | None = None) -> dict[str, Any]:
    """Check the live build against ``before_digest`` after a rollback."""
    before_digest = before_digest if isinstance(before_digest, dict) else {}
    if code:
        if worker.call("export_code", {}).get("code") == code:
            return {"verified": True, "detail": "restored build exports identical code"}
    use_direct = "skillFlags" in before_digest and supports_direct_calc(worker)
    res = worker.call("direct_calc" if use_direct else "get_stats", {}) or {}
    differing = stat_drift(before_digest.get("stats") or {}, res.get("stats") or {})
    before_pts = _points_before(before_digest)
    if before_pts is not None and _points_after(worker, res) != before_pts:
        differing = sorted([*differing, "pointsUsed"])
    if differing:
        what = "from before the run" if code is not None else "after CoW restore"
        return {
            "verified": False,
            "detail": f"{len(differing)} stat(s) differ {what} (e.g. {', '.join(differing[:5])})",
            "differing": differing,
        }
    if code is not None:
        return {"verified": True, "detail": "export code differs, but every stat recalculates identical"}
    return {"verified": True, "detail": "CoW snapshot restored with identical stats"}


def verify_cow_restored(worker: Any, before_digest: dict[str, Any]) -> dict[str, Any]:
    return verify_build(worker, before_digest)
