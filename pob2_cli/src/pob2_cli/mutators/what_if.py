# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 CoW transactional what-if mutator & build diff engine (L2)."""

from __future__ import annotations

from typing import Any

from ..analyzer.version_stamp import attach_version_stamp
from ..bridge_protocol import encode_xml_share_code
from ..errors import DaemonRequestError
from ..game_profile import GAME_ID
from ..guardrails import (
    GuardrailScope,
    check_input,
    check_input_item_raw,
    normalize_poe2_equipment_slot,
)
from ..importers.share_urls import resolve_poe2_build_source
from ..snapshot import BuildScope, BuildTransaction, capture_digest, diff_digest, run_mutation


def _normalize_slot_name(raw_slot: str | None) -> str:
    return normalize_poe2_equipment_slot(raw_slot, command="what-if", allow_empty=True)


def _slot_equipped_id(slots: Any, slot_name: str) -> int | None:
    if not isinstance(slots, list):
        return None
    for s in slots:
        if isinstance(s, dict) and str(s.get("name") or "") == slot_name:
            sel = s.get("selItemId") if "selItemId" in s else s.get("itemId")
            return int(sel) if isinstance(sel, int) else None
    return None


def _apply_tree_node_op(worker: Any, spec: dict[str, Any], *, apply_flag: bool) -> dict[str, Any]:
    nid = int(spec["tree_node"])
    anoint_preview = bool(spec.get("anoint_preview") or spec.get("isolated_node"))
    if anoint_preview and apply_flag:
        raise DaemonRequestError(
            "Cannot combine --apply with --anoint-preview/--isolated-node: a disconnected node will be dropped on XML reload. "
            "Omit --apply to preview isolated single-node impact, or allocate a connected path / instill an amulet."
        )

    ts_before = worker.call("get_tree_state", {}) or {}
    before_alloc = {int(x) for x in (ts_before.get("allocatedNodes") or []) if isinstance(x, int)}
    pts_before_raw = (ts_before.get("points") or {}).get("used")
    pts_before = int(pts_before_raw) if isinstance(pts_before_raw, int) else len(before_alloc)

    hover = worker.call("node_hover", {"id": nid}) or {}
    isolated: dict[str, Any] | None = None
    isolated_warn: str | None = None
    try:
        isolated = worker.call("eval_isolated_node", {"id": nid})
    except Exception as exc:
        isolated = None
        isolated_warn = f"[what-if-degraded] eval_isolated_node failed for node {nid} ({exc})."

    node_name = str((isolated or {}).get("name") or nid)
    if anoint_preview:
        tog = worker.call("toggle_isolated_node", {"id": nid}) or {}
        op_dir = str(tog.get("op") or ("remove" if nid in before_alloc else "add"))
        delta_cnt = -1 if op_dir == "remove" else 1
        path_cost = hover.get("cost")
        travel_skipped = max(0, int(path_cost) - 1) if isinstance(path_cost, int) else None
        if op_dir == "add":
            if travel_skipped is not None:
                warn = (
                    f"Anoint/isolated preview for node {nid} ({node_name}): evaluated as 1 node without "
                    f"{travel_skipped} intermediate travel node(s); disconnected from tree unless instilled on amulet."
                )
            else:
                warn = (
                    f"Anoint/isolated preview for node {nid} ({node_name}): evaluated in isolation "
                    "(node has no connected path from current tree; disconnected unless instilled on amulet)."
                )
        else:
            warn = f"Isolated removal preview for node {nid} ({node_name}): evaluated without cascading dependent nodes."
        op_out: dict[str, Any] = {
            "op": "tree_click",
            "isolatedMode": True,
            "nodeId": nid,
            "nodeName": node_name,
            "pointsUsedBefore": pts_before,
            "pointsUsedAfter": pts_before + delta_cnt,
            "nodesDeltaCount": delta_cnt,
            "allocatedPathNodes": [],
            "connectivityWarning": warn,
        }
        if isinstance(isolated, dict):
            op_out["isolatedNodeEffect"] = {
                "nodeId": nid,
                "nodeName": node_name,
                "op": isolated.get("op"),
                "isReachable": isolated.get("isReachable"),
                "pathDist": isolated.get("pathDist"),
                "delta": isolated.get("delta"),
            }
        elif isolated_warn:
            op_out["degradedWarning"] = isolated_warn
        return op_out

    click_res = worker.call("tree_click", {"id": nid}) or {}
    ts_after = click_res if isinstance(click_res.get("allocatedNodes"), list) else (worker.call("get_tree_state", {}) or {})
    after_alloc = {int(x) for x in (ts_after.get("allocatedNodes") or []) if isinstance(x, int)}
    pts_after_raw = (ts_after.get("points") or {}).get("used")
    pts_after = int(pts_after_raw) if isinstance(pts_after_raw, int) else len(after_alloc)

    added = sorted(after_alloc - before_alloc)
    removed = sorted(before_alloc - after_alloc)
    nodes_delta = len(after_alloc) - len(before_alloc)
    allocated_path_nodes = [x for x in added if x != nid]
    deallocated_dep_nodes = [x for x in removed if x != nid]

    conn_warn: str | None = None
    if click_res.get("blocked"):
        conn_warn = f"Node {nid} ({node_name}) was blocked by engine rule: {click_res.get('blocked')}."
    elif click_res.get("needsConfirm"):
        conn_warn = (
            f"Node {nid} ({node_name}) requires class/ascendancy confirmation ({click_res.get('needsConfirm')}) "
            "and was not allocated."
        )
    elif click_res.get("needsAttribute"):
        conn_warn = (
            f"Node {nid} ({node_name}) is a +5 Attribute node; pass `--attribute-node {nid} --attribute str|dex|int`."
        )
    elif nodes_delta == 0:
        conn_warn = (
            f"Node {nid} ({node_name}) is unreachable/disconnected from the current tree and was not allocated; "
            "use `--anoint-preview` to evaluate its isolated single-node effect."
        )
    elif hover.get("cost") is None and nid in added:
        conn_warn = (
            f"Node {nid} ({node_name}) was force-allocated without a connected path from class start and will be dropped on XML reload."
        )
    elif nodes_delta > 1:
        conn_warn = (
            f"Allocating node {nid} ({node_name}) auto-allocated {len(allocated_path_nodes)} intermediate travel node(s) "
            f"({nodes_delta} total points: {pts_before} -> {pts_after}). Top-level diff reflects all {nodes_delta} nodes; "
            "see `isolatedNodeEffect` or pass `--anoint-preview` for single-node impact."
        )
    elif nodes_delta < -1:
        conn_warn = (
            f"Deallocating node {nid} ({node_name}) cascaded {len(deallocated_dep_nodes)} dependent node(s) "
            f"({abs(nodes_delta)} total points: {pts_before} -> {pts_after})."
        )

    op_entry: dict[str, Any] = {
        "op": "tree_click",
        "nodeId": nid,
        "nodeName": node_name,
        "pointsUsedBefore": pts_before,
        "pointsUsedAfter": pts_after,
        "nodesDeltaCount": nodes_delta,
        "allocatedPathNodes": allocated_path_nodes,
    }
    if deallocated_dep_nodes:
        op_entry["deallocatedDependentNodes"] = deallocated_dep_nodes
    if isinstance(isolated, dict) and (abs(nodes_delta) > 1 or nodes_delta == 0 or conn_warn):
        op_entry["isolatedNodeEffect"] = {
            "nodeId": nid,
            "nodeName": node_name,
            "op": isolated.get("op"),
            "isReachable": isolated.get("isReachable"),
            "pathDist": isolated.get("pathDist"),
            "delta": isolated.get("delta"),
        }
    if conn_warn:
        op_entry["connectivityWarning"] = conn_warn
    if isolated_warn:
        op_entry["degradedWarning"] = isolated_warn
    return op_entry


def _apply_item_equip_op(worker: Any, spec: dict[str, Any]) -> dict[str, Any]:
    raw_item = str(spec["item_text"]).strip()
    check_input_item_raw(raw_item, expected_game="poe2", emit_stderr=False)
    slot_name = _normalize_slot_name(spec.get("slot"))
    add_params: dict[str, Any] = {"raw": raw_item}
    if slot_name:
        add_params["slotName"] = slot_name

    add_res = worker.call("add_item", add_params) or {}
    item_obj = add_res.get("item") if isinstance(add_res.get("item"), dict) else {}
    item_id = item_obj.get("id") if item_obj.get("id") is not None else add_res.get("id")
    if item_id is None:
        raise DaemonRequestError(
            "Failed to add item for what-if: PoB2 bridge did not return an item ID (verify PoE2 item text and base type)."
        )

    if slot_name:
        equipped_id = _slot_equipped_id(add_res.get("slots"), slot_name)
        if equipped_id != item_id:
            eq_res = worker.call("equip_item", {"id": item_id, "slotName": slot_name}) or {}
            eq_slots = eq_res.get("slots")
            if isinstance(eq_slots, list) and _slot_equipped_id(eq_slots, slot_name) != item_id:
                raise DaemonRequestError(f"Item #{item_id} failed to equip into slot '{slot_name}'.")

    used_in = item_obj.get("usedIn") if isinstance(item_obj.get("usedIn"), dict) else {}
    eff_slot = slot_name or str(used_in.get("slot") or item_obj.get("primarySlot") or "")
    entry: dict[str, Any] = {"op": "equip_item", "slot": eff_slot, "itemId": item_id}
    if item_obj.get("name"):
        entry["itemName"] = item_obj["name"]
    return entry


def _apply_mutation_ops(worker: Any, spec: dict[str, Any], *, apply_flag: bool = False) -> list[dict[str, Any]]:
    """Execute one or more atomic PoE2 mutations on ``worker`` inside a BuildTransaction."""
    applied_ops: list[dict[str, Any]] = []

    if spec.get("level") is not None:
        lv = int(spec["level"])
        worker.call("set_build_field", {"field": "level", "value": lv})
        applied_ops.append({"op": "level", "value": lv})

    if spec.get("weapon_swap") is not None:
        on = bool(spec["weapon_swap"])
        worker.call("set_weapon_swap", {"on": on})
        applied_ops.append({"op": "weapon_swap", "on": on})

    if spec.get("alloc_mode") is not None:
        mode = int(spec["alloc_mode"])
        worker.call("set_alloc_mode", {"mode": mode})
        applied_ops.append({"op": "alloc_mode", "mode": mode})

    if spec.get("tree_node") is not None:
        applied_ops.append(_apply_tree_node_op(worker, spec, apply_flag=apply_flag))

    if spec.get("attribute_node") is not None:
        nid = int(spec["attribute_node"])
        attr_raw = spec.get("attribute")
        attr_map = {"str": 1, "strength": 1, "1": 1, "dex": 2, "dexterity": 2, "2": 2, "int": 3, "intelligence": 3, "3": 3}
        attr_idx = attr_map.get(str(attr_raw or "").strip().lower())
        if attr_idx is None:
            raise DaemonRequestError(f"Invalid attribute {attr_raw!r}; expected str/dex/int or 1/2/3.")
        worker.call("tree_attribute", {"id": nid, "attribute": attr_idx})
        applied_ops.append({"op": "tree_attribute", "nodeId": nid, "attribute": attr_idx})

    if spec.get("item_text"):
        applied_ops.append(_apply_item_equip_op(worker, spec))
    elif spec.get("unequip_slot"):
        slot_name = _normalize_slot_name(spec["unequip_slot"])
        worker.call("unequip_slot", {"slotName": slot_name})
        applied_ops.append({"op": "unequip_slot", "slot": slot_name})

    if spec.get("gem_group") is not None or spec.get("set1") is not None or spec.get("set2") is not None:
        grp_idx = int(spec.get("gem_group") or 1)
        if grp_idx < 1:
            raise DaemonRequestError(f"Invalid --gem-group {grp_idx}; skill socket group index is 1-based (>= 1).")
        if spec.get("gem_index") is not None and any(
            spec.get(k) is not None for k in ("gem_level", "gem_quality", "gem_enabled")
        ):
            gem_idx = int(spec["gem_index"])
            if gem_idx < 1:
                raise DaemonRequestError(f"Invalid --gem-index {gem_idx}; gem index inside socket group is 1-based (>= 1).")
            gp: dict[str, Any] = {"group": grp_idx, "index": gem_idx}
            if spec.get("gem_level") is not None:
                gp["level"] = int(spec["gem_level"])
            if spec.get("gem_quality") is not None:
                gp["quality"] = int(spec["gem_quality"])
            if spec.get("gem_enabled") is not None:
                gp["enabled"] = bool(spec["gem_enabled"])
            worker.call("set_gem", gp)
            applied_ops.append({"op": "set_gem", **gp})
        if spec.get("set1") is not None or spec.get("set2") is not None:
            wsp: dict[str, Any] = {"index": grp_idx}
            if spec.get("gem_index") is not None:
                gem_idx = int(spec["gem_index"])
                if gem_idx < 1:
                    raise DaemonRequestError(
                        f"Invalid --gem-index {gem_idx}; gem index inside socket group is 1-based (>= 1)."
                    )
                wsp["gemIndex"] = gem_idx
            if spec.get("set1") is not None:
                wsp["set1"] = bool(spec["set1"])
            if spec.get("set2") is not None:
                wsp["set2"] = bool(spec["set2"])
            ws_res = worker.call("set_skill_weapon_set", wsp) or {}
            if isinstance(ws_res, dict) and ws_res.get("noOp") is True:
                raise DaemonRequestError(
                    f"[POE2-NO-OP] Skill group {grp_idx} weapon-set state did not change "
                    f"(requested set1={wsp.get('set1')}, set2={wsp.get('set2')}; "
                    f"current set1={ws_res.get('set1')}, set2={ws_res.get('set2')}, "
                    f"gem1EnableGlobal1={ws_res.get('gem1EnableGlobal1')}, "
                    f"gem1EnableGlobal2={ws_res.get('gem1EnableGlobal2')})."
                )
            op_rec: dict[str, Any] = {"op": "set_skill_weapon_set", **wsp}
            if isinstance(ws_res, dict):
                for k in ("set1", "set2", "gem1EnableGlobal1", "gem1EnableGlobal2", "changed"):
                    if k in ws_res:
                        op_rec[k] = ws_res[k]
            applied_ops.append(op_rec)

    if spec.get("config_key"):
        ckey = str(spec["config_key"]).strip()
        check_input(ckey, GuardrailScope.CONFIG_VAR, expected_game="poe2", emit_stderr=False)
        cval = spec.get("config_value")
        worker.call("set_config", {"var": ckey, "value": cval})
        applied_ops.append({"op": "set_config", "var": ckey, "value": cval})

    if isinstance(spec.get("config_vars"), dict):
        for raw_k, cval in spec["config_vars"].items():
            ckey = str(raw_k).strip()
            if not ckey:
                continue
            check_input(ckey, GuardrailScope.CONFIG_VAR, expected_game="poe2", emit_stderr=False)
            worker.call("set_config", {"var": ckey, "value": cval})
            applied_ops.append({"op": "set_config", "var": ckey, "value": cval})

    if spec.get("custom_mods") is not None:
        cmods = str(spec["custom_mods"])
        worker.call("set_custom_mods", {"text": cmods})
        applied_ops.append({"op": "set_custom_mods", "length": len(cmods)})

    if not applied_ops:
        raise DaemonRequestError(
            "No mutation specified. Pass one or more of: --level, --slot/--item-text, --unequip, "
            "--tree-node, --alloc-mode, --attribute-node/--attribute, --gem-group/--gem-index/--gem-level/--gem-quality, "
            "--set1/--set2, --config-key/--config-value, or --custom-mods."
        )
    return applied_ops


def run_what_if(
    worker: Any,
    mutation_spec: dict[str, Any],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Run a CoW what-if mutation (or persist when ``apply=True``) with 100% rollback verification."""
    res = run_mutation(
        worker,
        lambda w: _apply_mutation_ops(w, mutation_spec, apply_flag=apply),
        apply=apply,
        scope=BuildScope.ACTIVE_SETS,
    )
    info = worker.call("get_build_info", {}) or {}
    return attach_version_stamp(
        res,
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


def diff_against_source(
    worker: Any,
    *,
    source: str | None = None,
    xml_code: str | None = None,
) -> dict[str, Any]:
    """Diff the live slot build against another PoE2 build source without mutating the live slot."""
    resolved = resolve_poe2_build_source(source=source, xml_code=xml_code, data_dir=getattr(worker, "data_dir", None))
    target_code = resolved.code or encode_xml_share_code(resolved.xml)
    with BuildTransaction(worker, scope=BuildScope.REPLACES_BUILD, will_commit=False) as tx:
        before_digest = capture_digest(worker)
        worker.call("import_code", {"code": target_code, "mode": "replace"})
        after_digest = capture_digest(worker)
        diff = diff_digest(before_digest, after_digest)
        tx.rollback()
    info = before_digest.get("buildInfo") or {}
    return attach_version_stamp(
        {
            "game": GAME_ID,
            "targetSource": resolved.source_label,
            "targetSourceKind": resolved.source_kind,
            "before": {
                "primaryDpsKey": before_digest["primaryDpsKey"],
                "primaryDps": before_digest["primaryDps"],
                "totalEhp": before_digest["totalEhp"],
                "life": before_digest["life"],
                "spirit": before_digest.get("spirit", 0.0),
            },
            "after": {
                "primaryDpsKey": after_digest["primaryDpsKey"],
                "primaryDps": after_digest["primaryDps"],
                "totalEhp": after_digest["totalEhp"],
                "life": after_digest["life"],
                "spirit": after_digest.get("spirit", 0.0),
            },
            "diff": diff,
        },
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


__all__ = ["diff_against_source", "run_what_if"]
