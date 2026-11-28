# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 build summary, diagnose, notes, items, skills, tree, and config analyzers (L2)."""

from __future__ import annotations

from typing import Any

from ..game_profile import GAME_ID
from ..guardrails import normalize_poe2_equipment_slot
from ..importers.post_load_fixups import fetch_clean_notes
from ..snapshot import capture_digest, compact_digest_summary
from ..snapshot.helpers import clean_points_dict
from .deep_audit import DEFAULT_OVERCAP_TARGET, run_deep_audit
from .version_stamp import attach_version_stamp


def extract_summary(worker: Any) -> dict[str, Any]:
    """Return compact PoE2 build summary stamped with PoB2 version, patch, and diagnostics."""
    digest = capture_digest(worker)
    out = compact_digest_summary(digest)
    info = digest.get("buildInfo") or {}
    degraded: list[str] = []
    try:
        out["engineDiagnostics"] = worker.get_engine_diagnostics()
    except Exception as exc:
        degraded.append(f"[summary-degraded] get_engine_diagnostics failed ({exc}).")
    try:
        out["weaponSetState"] = worker.call("get_weapon_set_state", {})
    except Exception as exc:
        degraded.append(f"[summary-degraded] get_weapon_set_state failed ({exc}).")
    if degraded:
        out["warnings"] = [*(out.get("warnings") or []), *degraded]
    return attach_version_stamp(
        out,
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


def extract_notes(worker: Any) -> dict[str, Any]:
    """Return author Notes stripped of PoB colour codes (`^xRRGGBB`, `^0..^9`)."""
    notes = fetch_clean_notes(worker)
    info = worker.call("get_build_info", {}) or {}
    return attach_version_stamp(
        {"game": GAME_ID, **notes},
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


def audit_build(
    worker: Any,
    *,
    overcap_target: int = DEFAULT_OVERCAP_TARGET,
    chaos_floor: int = 0,
) -> dict[str, Any]:
    """Run S4/T3.4 deep audit + 4-dim honest dehydration + item-101 resistance audit."""
    return run_deep_audit(worker, overcap_target=overcap_target, chaos_floor=chaos_floor)


def diagnose_build(
    worker: Any,
    *,
    overcap_target: int = DEFAULT_OVERCAP_TARGET,
    chaos_floor: int = 0,
    include_node_power: bool = False,
    max_depth: int = 4,
) -> dict[str, Any]:
    """One-shot comprehensive PoE2 diagnosis: summary + deep audit + author notes + optional node power."""
    summary = extract_summary(worker)
    audit = run_deep_audit(worker, overcap_target=overcap_target, chaos_floor=chaos_floor)
    notes = fetch_clean_notes(worker)
    node_power: dict[str, Any] | None = None
    diag_warnings: list[str] = list(audit.get("warnings") or [])
    if include_node_power:
        try:
            np_raw = worker.call("node_power", {"maxDepth": max_depth}, timeout=45.0)
            node_power = {
                "stat": np_raw.get("stat"),
                "classId": np_raw.get("classId"),
                "className": np_raw.get("className"),
                "weaponSet": np_raw.get("weaponSet"),
                "topNodes": [r for r in (np_raw.get("report") or []) if not r.get("allocated")][:10],
            }
        except Exception as exc:
            node_power = None
            diag_warnings.append(f"[diagnose-degraded] node_power failed ({exc}); omitted passive power ranking.")

    payload: dict[str, Any] = {
        "game": GAME_ID,
        "summary": summary,
        "audit": audit,
        "notes": notes,
        "unsimulatedRedCards": audit.get("unsimulatedRedCards") or [],
        "findings": audit.get("findings") or [],
    }
    if diag_warnings:
        payload["warnings"] = diag_warnings
    if node_power is not None:
        payload["nodePower"] = node_power
    return attach_version_stamp(
        payload,
        pob2_version=audit.get("pob2Version"),
        tree_version=(audit.get("versionStamp") or {}).get("treeVersion"),
    )


def inspect_items(worker: Any, *, slot: str | None = None, include_raw: bool = False) -> dict[str, Any]:
    canon_slot = normalize_poe2_equipment_slot(slot, command="items") if (slot and slot.strip()) else None
    res = worker.call("list_items", {}) or {}
    items = list(res.get("items") or [])
    slots = list(res.get("slots") or [])
    raw_text: str | None = None
    if canon_slot:
        sl_low = canon_slot.lower()
        slots = [s for s in slots if isinstance(s, dict) and str(s.get("name") or "").lower() == sl_low]
        slot_ids = {
            int(s.get("selItemId") or s.get("selectedId") or s.get("itemId") or 0)
            for s in slots
            if isinstance(s, dict)
        }
        slot_ids.discard(0)
        items = [it for it in items if isinstance(it, dict) and int(it.get("id") or 0) in slot_ids]
        if include_raw and items:
            first_id = int(items[0].get("id") or 0)
            raw_text = str(items[0].get("raw") or "")
            if not raw_text and first_id > 0:
                raw_text = str((worker.call("item_raw", {"id": first_id}) or {}).get("raw") or "")
    info = worker.call("get_build_info", {}) or {}
    out_payload: dict[str, Any] = {
        "game": GAME_ID,
        "slots": slots,
        "items": items,
        "activeItemSetId": res.get("activeItemSetId"),
    }
    if canon_slot:
        out_payload["slot"] = canon_slot
    if include_raw:
        out_payload["raw"] = raw_text
    return attach_version_stamp(
        out_payload,
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


def inspect_skills(worker: Any) -> dict[str, Any]:
    res = worker.call("list_skills", {}) or {}
    ws = worker.call("get_weapon_set_state", {}) or {}
    info = worker.call("get_build_info", {}) or {}
    return attach_version_stamp(
        {
            "game": GAME_ID,
            "mainSocketGroup": res.get("mainSocketGroup"),
            "groups": res.get("groups") or [],
            "skillWeaponSets": ws.get("skillWeaponSets") or [],
        },
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


def inspect_tree(worker: Any) -> dict[str, Any]:
    state = worker.call("get_tree_state", {}) or {}
    ws = worker.call("get_weapon_set_state", {}) or {}
    info = worker.call("get_build_info", {}) or {}
    return attach_version_stamp(
        {
            "game": GAME_ID,
            "classId": info.get("classId"),
            "className": info.get("className"),
            "ascendClassName": info.get("ascendClassName"),
            "points": clean_points_dict(info.get("points")),
            "allocatedCount": len(state.get("allocatedNodes") or []),
            "allocatedNodes": state.get("allocatedNodes") or [],
            "overrides": state.get("overrides") or {},
            "weaponSetState": ws,
        },
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


def inspect_config(worker: Any) -> dict[str, Any]:
    cfg = worker.call("list_config", {}) or {}
    info = worker.call("get_build_info", {}) or {}
    return attach_version_stamp(
        {"game": GAME_ID, **cfg},
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


__all__ = [
    "audit_build",
    "diagnose_build",
    "extract_notes",
    "extract_summary",
    "inspect_config",
    "inspect_items",
    "inspect_skills",
    "inspect_tree",
]
