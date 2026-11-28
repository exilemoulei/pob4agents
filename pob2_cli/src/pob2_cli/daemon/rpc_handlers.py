# SPDX-License-Identifier: GPL-3.0-only
"""Single-slot, dual-slot, and extensible RPC action dispatchers for ``pob2_cli.daemon.DaemonManager``."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..analyzer import (
    audit_build,
    diagnose_build,
    extract_notes,
    extract_summary,
    inspect_config,
    inspect_items,
    inspect_skills,
    inspect_tree,
)
from ..bridge_client import BridgeRejectedError, BridgeWorker, sanitize_bridge_error
from ..bridge_protocol import decode_share_code_xml, validate_rpc_method
from ..comparator import compare_builds, rank_uniques
from ..errors import CrossGameHallucinationError, DaemonRequestError
from ..game_profile import GAME_ID
from ..guardrails import normalize_poe2_equipment_slot
from ..mutators import diff_against_source, run_what_if
from ..snapshot import history_of, load_build, redo_build, undo_build
from ..snapshot.helpers import clean_points_dict
from .read_cache import compute_source_hash, is_live_source
from .rpc_rebalance import REBALANCE_SLOT_HANDLERS
from .runtime import normalize_slot_name

if TYPE_CHECKING:
    from .slots import DaemonManager

SlotHandler = Callable[["DaemonManager", dict[str, Any], BridgeWorker], dict[str, Any]]


def _h_load(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    try:
        load_res = load_build(
            w,
            source=params.get("source"),
            xml_code=params.get("xml_code"),
            char_name=params.get("char_name"),
            league=params.get("league"),
            realm=params.get("realm"),
        )
    except CrossGameHallucinationError:
        raise
    src_hash = compute_source_hash(params)
    slot_nm = normalize_slot_name(params.get("target") or getattr(w, "slot_name", None))
    read_cache = getattr(m, "_read_cache", None)
    if read_cache is not None:
        read_cache.record_slot_load(slot_nm, history_of(w).rev, src_hash, load_res, live=is_live_source(params))
    if src_hash and isinstance(load_res, dict):
        load_res["sourceHash"] = src_hash
    return m._attach_warning_if_degraded(load_res)


def _h_export(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    with w.lock:
        code_res = w.call("export_code", {})
        code_str = str(code_res.get("code") or "")
        xml_str = None
        if params.get("include_xml"):
            xml_res = w.call("save_xml", {})
            xml_str = xml_res.get("xml") or decode_share_code_xml(code_str)
        return {
            "game": GAME_ID,
            "code": code_str,
            "xml": xml_str,
            "pobDir": str(m.pob_dir) if m.pob_dir else None,
        }


def _h_undo(_m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return undo_build(w)


def _h_redo(_m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return redo_build(w)


def _h_summary(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(extract_summary(w))


def _h_notes(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(extract_notes(w))


def _h_audit(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    overcap = int(params["overcap"]) if params.get("overcap") is not None else 15
    chaos_floor = int(params["chaos_floor"]) if params.get("chaos_floor") is not None else 0
    return m._attach_warning_if_degraded(audit_build(w, overcap_target=overcap, chaos_floor=chaos_floor))


def _h_diagnose(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    overcap = int(params["overcap"]) if params.get("overcap") is not None else 15
    chaos_floor = int(params["chaos_floor"]) if params.get("chaos_floor") is not None else 0
    inc_np = bool(params.get("node_power", False))
    max_depth = int(params["max_depth"]) if params.get("max_depth") is not None else 4
    return m._attach_warning_if_degraded(
        diagnose_build(w, overcap_target=overcap, chaos_floor=chaos_floor, include_node_power=inc_np, max_depth=max_depth)
    )


def _h_uniques(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(
        rank_uniques(
            w,
            slot=str(params.get("slot") or ""),
            stat=params.get("stat"),
            top=int(params["top"]) if params.get("top") is not None else 15,
            eval_sanctified=bool(params.get("eval_sanctified", True)),
            max_scan=int(params["max_scan"]) if params.get("max_scan") is not None else 300,
        )
    )


def _h_compare(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(
        compare_builds(
            w,
            ref_source=params.get("ref_source") or params.get("source"),
            ref_xml_code=params.get("ref_xml_code") or params.get("xml_code"),
        )
    )


def _h_what_if(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(
        run_what_if(w, params, apply=bool(params.get("apply", False)))
    )


def _h_diff(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(
        diff_against_source(w, source=params.get("source"), xml_code=params.get("xml_code"))
    )


def _h_stats(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("get_stats", {}))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_build_info(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("get_build_info", {}))
    if isinstance(res.get("points"), dict):
        res["points"] = clean_points_dict(res["points"])
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_tree_state(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("get_tree_state", {}))
    if isinstance(res.get("points"), dict):
        res["points"] = clean_points_dict(res["points"])
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_tree(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(inspect_tree(w))


def _h_items(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    inc_raw = bool(params.get("include_raw") or params.get("raw"))
    return m._attach_warning_if_degraded(inspect_items(w, slot=params.get("slot"), include_raw=inc_raw))


def _h_skills(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(inspect_skills(w))


def _h_config_list(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return m._attach_warning_if_degraded(inspect_config(w))


def _h_weapon_set_state(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("get_weapon_set_state", {}))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_node_power(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    rpc_params: dict[str, Any] = {}
    if params.get("stat"):
        rpc_params["stat"] = str(params["stat"])
    if params.get("max_depth") is not None:
        rpc_params["maxDepth"] = int(params["max_depth"])
    elif params.get("maxDepth") is not None:
        rpc_params["maxDepth"] = int(params["maxDepth"])
    raw_ids = params.get("nodeIds") if params.get("nodeIds") is not None else params.get("node_ids")
    if isinstance(raw_ids, (list, tuple)) and raw_ids:
        rpc_params["nodeIds"] = [int(x) for x in raw_ids]
    if params.get("includePathPower") is not None:
        rpc_params["includePathPower"] = bool(params["includePathPower"])
    res = dict(w.call("node_power", rpc_params, timeout=45.0))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_probe(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("agent_probe_build", {}, timeout=30.0))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_diagnostics(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.get_engine_diagnostics())
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_new_build(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("new_build", {}))
    code = str(w.call("export_code", {}).get("code") or "")
    history_of(w).load_build(code, "new_build")
    w.has_build = True
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_rpc(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    method = str(params.get("method") or "").strip()
    if not method:
        raise DaemonRequestError("Action 'rpc' requires non-empty 'method'.")
    validate_rpc_method(method, w.caps)
    rpc_params = params.get("params")
    if rpc_params is not None and not isinstance(rpc_params, dict):
        raise DaemonRequestError("Action 'rpc' field 'params' must be a JSON object.")
    timeout = float(params["timeout"]) if params.get("timeout") is not None else None
    res = dict(w.call(method, rpc_params or {}, timeout=timeout))
    res.setdefault("game", GAME_ID)
    return m._attach_warning_if_degraded(res)


REQUIRE_LOADED_BUILD_ACTIONS: frozenset[str] = frozenset(
    {
        "summary",
        "diagnose",
        "audit",
        "items",
        "skills",
        "tree",
        "tree_state",
        "config_list",
        "what_if",
        "diff",
        "compare",
        "uniques",
        "notes",
        "export",
        "undo",
        "redo",
        "stats",
        "build_info",
        "weapon_set_state",
        "node_power",
        "probe",
        "fix_gaps",
        "rank_instill",
        "rune_rank",
    }
)


def action_requires_loaded_build(action: str, params: dict[str, Any] | None = None) -> bool:
    if action in REQUIRE_LOADED_BUILD_ACTIONS:
        return True
    if action == "spirit":
        return (params or {}).get("budget") is None
    return False


def empty_slot_error(slot: str) -> DaemonRequestError:
    return DaemonRequestError(
        f"Slot '{slot}' has no build loaded; run `pob2 load <source> --target {slot}` first."
    )


def worker_has_loaded_build(worker: Any) -> bool:
    return bool(getattr(worker, "has_build", False) or getattr(worker, "last_good_code", None))


def dispatch_dual_slot_action(
    manager: DaemonManager,
    action: str,
    params: dict[str, Any],
    mw: BridgeWorker,
    rw: BridgeWorker,
) -> dict[str, Any]:
    if action == "compare":
        if not worker_has_loaded_build(mw):
            raise empty_slot_error(mw.slot_name)
        if not worker_has_loaded_build(rw):
            raise empty_slot_error(rw.slot_name)
        try:
            res = compare_builds(
                mw,
                ref_worker=rw,
                ref_source=params.get("ref_source") or params.get("source"),
                ref_xml_code=params.get("ref_xml_code") or params.get("xml_code"),
            )
        except BridgeRejectedError as exc:
            if exc.kind == "request":
                raise DaemonRequestError(sanitize_bridge_error(str(exc))) from exc
            raise
        res.setdefault("rev", history_of(mw).rev)
        res.setdefault("targetSlot", mw.slot_name)
        return manager._attach_warning_if_degraded(res)
    raise ValueError(f"Unknown PoE2 dual-slot action '{action}'.")


_SLOT_ACTION_HANDLERS: dict[str, SlotHandler] = {
    "load": _h_load,
    "export": _h_export,
    "undo": _h_undo,
    "redo": _h_redo,
    "summary": _h_summary,
    "notes": _h_notes,
    "audit": _h_audit,
    "diagnose": _h_diagnose,
    "uniques": _h_uniques,
    "compare": _h_compare,
    "what_if": _h_what_if,
    "diff": _h_diff,
    "stats": _h_stats,
    "build_info": _h_build_info,
    "tree_state": _h_tree_state,
    "tree": _h_tree,
    "items": _h_items,
    "skills": _h_skills,
    "config_list": _h_config_list,
    "weapon_set_state": _h_weapon_set_state,
    "node_power": _h_node_power,
    "probe": _h_probe,
    "diagnostics": _h_diagnostics,
    "new_build": _h_new_build,
    "rpc": _h_rpc,
    **REBALANCE_SLOT_HANDLERS,
}


def register_slot_handler(action: str, handler: SlotHandler) -> None:
    """Register an additional daemon slot RPC action (for extension layers)."""
    _SLOT_ACTION_HANDLERS[action] = handler


def prevalidate_slot_action_params(action: str, params: dict[str, Any] | None) -> None:
    """Validate equipment `--slot` arguments before checking whether the target build slot is loaded."""
    if not params:
        return
    if action in ("uniques", "what_if", "items", "rune_rank"):
        raw_slot = params.get("slot")
        if raw_slot is not None and str(raw_slot).strip():
            cmd_label = "what-if" if action == "what_if" else action.replace("_", " ")
            params["slot"] = normalize_poe2_equipment_slot(str(raw_slot), command=cmd_label)
    if action == "what_if":
        raw_unequip = params.get("unequip_slot")
        if raw_unequip is not None and str(raw_unequip).strip():
            params["unequip_slot"] = normalize_poe2_equipment_slot(str(raw_unequip), command="what-if")


def dispatch_slot_action(
    manager: DaemonManager,
    action: str,
    params: dict[str, Any],
    w: BridgeWorker,
) -> dict[str, Any]:
    handler = _SLOT_ACTION_HANDLERS.get(action)
    if handler is None:
        raise ValueError(f"Unknown PoE2 daemon action '{action}'.")
    prevalidate_slot_action_params(action, params)
    if action_requires_loaded_build(action, params) and not worker_has_loaded_build(w):
        slot_nm = normalize_slot_name(params.get("target") or getattr(w, "slot_name", None))
        raise empty_slot_error(slot_nm)
    try:
        return handler(manager, params, w)
    except BridgeRejectedError as exc:
        if exc.kind == "request":
            raise DaemonRequestError(sanitize_bridge_error(str(exc))) from exc
        raise
