# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 keyword-free full-slot unique ranking (including Flask 1..2 & Charm 1..3) + Sanctified 0.78x-1.22x (L2)."""

from __future__ import annotations

from typing import Any

from ..analyzer.version_stamp import attach_version_stamp
from ..guardrails import normalize_poe2_equipment_slot


def normalize_slot_arg(slot: str) -> str:
    return normalize_poe2_equipment_slot(slot, command="uniques")


def rank_uniques(
    worker: Any,
    *,
    slot: str,
    stat: str | None = None,
    top: int = 15,
    eval_sanctified: bool = True,
    max_scan: int = 300,
) -> dict[str, Any]:
    """Keyword-free full-slot PoE2 unique ranking via Lua unique_rank_kernel."""
    canon_slot = normalize_slot_arg(slot)
    rpc_params: dict[str, Any] = {
        "slot": canon_slot,
        "top": max(1, min(int(top), 50)),
        "evalSanctified": bool(eval_sanctified),
        "maxScan": max(10, min(int(max_scan), 600)),
    }
    if stat:
        rpc_params["stat"] = str(stat).strip()
    with worker.lock:
        res = worker.call("rank_uniques_slot", rpc_params, timeout=60.0)
        info = worker.call("get_build_info", {}) or {}
    return attach_version_stamp(
        dict(res),
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


__all__ = ["normalize_slot_arg", "rank_uniques"]
