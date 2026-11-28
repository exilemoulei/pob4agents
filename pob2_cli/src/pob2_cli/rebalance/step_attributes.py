# SPDX-License-Identifier: GPL-3.0-only
"""Stage 1: Zero-buy gold-respec attribute node reassignment (+5 Str/Dex/Int on allocMode==0) (L2)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..rune.catalog import RuneInstillCatalog, load_rune_instill_catalog
from .net_gap import StatSnapshot

# PoB2 tree_attribute indices: 1=Strength, 2=Dexterity, 3=Intelligence
ATTR_NAME_TO_IDX: dict[str, int] = {"str": 1, "dex": 2, "int": 3}
IDX_TO_ATTR_NAME: dict[int, str] = {1: "str", 2: "dex", 3: "int"}
ATTRIBUTE_NODE_STEP = 5


@dataclass(frozen=True, slots=True)
class AttributeNodeInfo:
    """Allocated +5 Attribute choice node on the passive tree."""

    node_id: int
    current_attr: str  # "str" | "dex" | "int"
    alloc_mode: int  # 0 = common path (safe across WS1+WS2), 1 = WS1 only, 2 = WS2 only
    in_radius_jewel: bool = False


@dataclass(frozen=True, slots=True)
class AttributeSwapAction:
    """Single +5 Str/Dex/Int gold-respec swap."""

    node_id: int
    from_attr: str
    to_attr: str
    delta: int = ATTRIBUTE_NODE_STEP

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "from": self.from_attr,
            "to": self.to_attr,
            "delta": self.delta,
        }


@dataclass(frozen=True)
class Stage1AttributePlan:
    """Result of Stage 1 gold-respec attribute swapping."""

    swaps: tuple[AttributeSwapAction, ...]
    gold_cost: int
    remaining_deficits: dict[str, int]
    skipped_ws_only_count: int
    skipped_radius_sensitive_count: int
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": 1,
            "name": "gold_respec_attributes",
            "swapCount": len(self.swaps),
            "swaps": [s.to_dict() for s in self.swaps],
            "goldCost": self.gold_cost,
            "remainingDeficits": dict(self.remaining_deficits),
            "skippedWeaponSetNodes": self.skipped_ws_only_count,
            "skippedRadiusSensitiveNodes": self.skipped_radius_sensitive_count,
            "warnings": list(self.warnings),
        }


def collect_live_attribute_nodes(
    worker: Any,
    *,
    protected_jewel_nodes: tuple[int, ...] = (),
    radius_threshold: float = 1600.0,
) -> list[AttributeNodeInfo]:
    """Read allocated `+5` attribute nodes and flag WS-specific or radius-jewel-sensitive nodes."""
    tdata = worker.call("tree_data", {})
    tstate = worker.call("get_tree_state", {})
    ws_state = worker.call("get_weapon_set_state", {})

    nodes_map = tdata.get("nodes") or {}
    overrides = ws_state.get("hashOverrides") or tstate.get("overrides") or {}
    node_modes = ws_state.get("nodeModes") or {}
    ws1_set = set(ws_state.get("weaponSet1Nodes") or ())
    ws2_set = set(ws_state.get("weaponSet2Nodes") or ())

    jewel_coords: list[tuple[float, float]] = []
    for jnid in protected_jewel_nodes:
        jnd = nodes_map.get(str(jnid)) or {}
        if isinstance(jnd.get("x"), (int, float)) and isinstance(jnd.get("y"), (int, float)):
            jewel_coords.append((float(jnd["x"]), float(jnd["y"])))

    out: list[AttributeNodeInfo] = []
    for raw_nid in tstate.get("allocatedNodes") or ():
        nid = int(raw_nid)
        nd = nodes_map.get(str(nid)) or {}
        if not nd.get("attribute"):
            continue
        raw_ov = overrides.get(str(nid))
        cur_attr = _parse_override_attr(raw_ov)
        if cur_attr is None:
            continue
        if str(nid) in node_modes:
            mode = int(node_modes[str(nid)])
        elif nid in ws1_set:
            mode = 1
        elif nid in ws2_set:
            mode = 2
        else:
            mode = 0

        in_rad = False
        if jewel_coords and isinstance(nd.get("x"), (int, float)) and isinstance(nd.get("y"), (int, float)):
            nx, ny = float(nd["x"]), float(nd["y"])
            for jx, jy in jewel_coords:
                if math.hypot(nx - jx, ny - jy) <= radius_threshold:
                    in_rad = True
                    break
        out.append(AttributeNodeInfo(node_id=nid, current_attr=cur_attr, alloc_mode=mode, in_radius_jewel=in_rad))
    return out


def _parse_override_attr(raw_ov: Any) -> str | None:
    if isinstance(raw_ov, int) and raw_ov in IDX_TO_ATTR_NAME:
        return IDX_TO_ATTR_NAME[raw_ov]
    if isinstance(raw_ov, str):
        low = raw_ov.strip().lower()
        if low in ("1", "str", "strength"):
            return "str"
        if low in ("2", "dex", "dexterity"):
            return "dex"
        if low in ("3", "int", "intelligence"):
            return "int"
    if isinstance(raw_ov, dict):
        name = str(raw_ov.get("name") or raw_ov.get("attr") or "").lower()
        if "str" in name:
            return "str"
        if "dex" in name:
            return "dex"
        if "int" in name:
            return "int"
        if int(raw_ov.get("grantedStrength") or 0) > 0:
            return "str"
        if int(raw_ov.get("grantedDexterity") or 0) > 0:
            return "dex"
        if int(raw_ov.get("grantedIntelligence") or 0) > 0:
            return "int"
    return None


def solve_attribute_respec(
    snapshot: StatSnapshot,
    attr_nodes: list[AttributeNodeInfo],
    *,
    catalog: RuneInstillCatalog | None = None,
) -> Stage1AttributePlan:
    """Solve minimum gold-respec swaps among `allocMode == 0` non-radius-sensitive +5 Attribute nodes.

    Strictly respects WS1+WS2 worst-case requirements (including support gem +5 reqs and Giant's Blood 3x Str).
    """
    cat = catalog or load_rune_instill_catalog()
    deficits = dict(snapshot.effective_attr_deficits())
    surpluses = dict(snapshot.effective_attr_surpluses())
    warnings: list[str] = []

    if snapshot.giants_blood and deficits["str"] > 0:
        warnings.append("Giant's Blood active (3x martial weapon Str requirement included in ReqStr floor).")

    skipped_ws = sum(1 for n in attr_nodes if n.alloc_mode != 0)
    skipped_rad = sum(1 for n in attr_nodes if n.alloc_mode == 0 and n.in_radius_jewel)

    swappable_by_attr: dict[str, list[AttributeNodeInfo]] = {"str": [], "dex": [], "int": []}
    for nd in sorted(attr_nodes, key=lambda n: n.node_id):
        if nd.alloc_mode == 0 and not nd.in_radius_jewel and nd.current_attr in swappable_by_attr:
            swappable_by_attr[nd.current_attr].append(nd)

    swaps: list[AttributeSwapAction] = []
    # Greedily donate +5 nodes from highest-surplus attribute (>= 5 surplus across WS1 & WS2) to needed attributes.
    while any(v > 0 for v in deficits.values()):
        target_attr = max(deficits, key=lambda k: deficits[k])
        if deficits[target_attr] <= 0:
            break
        donors = [
            a
            for a in ("str", "dex", "int")
            if a != target_attr and surpluses[a] >= ATTRIBUTE_NODE_STEP and swappable_by_attr[a]
        ]
        if not donors:
            break
        donor = max(donors, key=lambda a: (surpluses[a], len(swappable_by_attr[a])))
        node = swappable_by_attr[donor].pop(0)
        swaps.append(AttributeSwapAction(node_id=node.node_id, from_attr=donor, to_attr=target_attr))
        surpluses[donor] -= ATTRIBUTE_NODE_STEP
        deficits[target_attr] = max(0, deficits[target_attr] - ATTRIBUTE_NODE_STEP)

    gold_cost = cat.gold_respec_cost(snapshot.level, nodes=len(swaps))
    return Stage1AttributePlan(
        swaps=tuple(swaps),
        gold_cost=gold_cost,
        remaining_deficits=deficits,
        skipped_ws_only_count=skipped_ws,
        skipped_radius_sensitive_count=skipped_rad,
        warnings=tuple(warnings),
    )


def apply_attribute_swaps_live(worker: Any, plan: Stage1AttributePlan) -> None:
    """Apply planned Stage 1 attribute swaps to a live `BridgeWorker` (call inside BuildTransaction)."""
    for sw in plan.swaps:
        worker.call("tree_attribute", {"id": sw.node_id, "attribute": ATTR_NAME_TO_IDX[sw.to_attr]})


__all__ = [
    "ATTRIBUTE_NODE_STEP",
    "ATTR_NAME_TO_IDX",
    "IDX_TO_ATTR_NAME",
    "AttributeNodeInfo",
    "AttributeSwapAction",
    "Stage1AttributePlan",
    "apply_attribute_swaps_live",
    "collect_live_attribute_nodes",
    "solve_attribute_respec",
]
