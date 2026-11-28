# SPDX-License-Identifier: GPL-3.0-only
"""Step 0 bare net-gap analyzer for PoE2 5-stage zero-buy rebalance solver (L2).

Obeys the core PoE2 resistance contract:
  - Hard Floor: each elemental resistance >= its own MaxRes (`Res >= MaxRes`), Chaos >= target,
    and Str/Dex/Int >= ReqStr/ReqDex/ReqInt across BOTH Weapon Set 1 and Weapon Set 2.
  - Overcap Buffer: soft bonus goal (`+overcap_target`, default +20%) against curses/exposure/minus-res maps.
  - NEVER equalizes Fire/Cold/Lightning or penalizes healthy overcap surplus.
  - NEVER counts hit-triggered `Charm 1..3` modifiers as permanent resistance or overcap buffer.
"""

from __future__ import annotations

import re
from typing import Any

from ..craft.item_inspector import inspect_item_text
from ..importers.post_load_fixups import scan_res_override_lines_in_items
from ..rune.catalog import RuneInstillCatalog, load_rune_instill_catalog
from ..rune.socket_rules import (
    resolve_item_augment_types,
    strip_replaceable_runes_from_item_raw,
)
from ..snapshot import BuildScope, BuildTransaction
from .gap_models import (
    EquippedSlotAudit,
    NetGapAudit,
    StatSnapshot,
    stat_snapshot_from_pob_stats,
)

_CHARM_SLOTS: frozenset[str] = frozenset({"Charm 1", "Charm 2", "Charm 3"})
_FLASK_SLOTS: frozenset[str] = frozenset({"Flask 1", "Flask 2"})
_JEWELLERY_BELT_SLOTS: frozenset[str] = frozenset({"Amulet", "Ring 1", "Ring 2", "Ring 3", "Belt"})
_GIANTS_BLOOD_RE = re.compile(r"giant'?s\s+blood", re.IGNORECASE)
_RADIUS_JEWEL_RE = re.compile(
    r"(?:from nothing|controlled metamorphosis|against the darkness|time-lost|undying hate|heroic tragedy|radius:)",
    re.IGNORECASE,
)


def audit_equipped_slot(
    *,
    slot_name: str,
    item_id: int,
    name: str,
    base_name: str,
    rarity: str,
    item_type: str,
    raw_text: str,
    is_shaman: bool = False,
    catalog: RuneInstillCatalog | None = None,
    warnings: list[str] | None = None,
) -> EquippedSlotAudit:
    """Audit a single equipped slot for editable rune sockets, Crafted slot availability, and catalysts."""
    cat = catalog or load_rune_instill_catalog()
    parity = resolve_item_augment_types(item_type, raw_text=raw_text, base_name=base_name, catalog=cat)
    stripped_raw, stripped, kept = strip_replaceable_runes_from_item_raw(
        raw_text, item_type=item_type, base_name=base_name, is_shaman=is_shaman, catalog=cat
    )
    editable_sockets = 0 if parity.is_mirrored else max(0, parity.rune_socket_count - len(kept))

    has_open_crafted = False
    open_pre = open_suf = 0
    if rarity.upper() == "RARE" and not (parity.is_corrupted or parity.is_sanctified or parity.is_mirrored):
        try:
            insp = inspect_item_text(raw_text)
            open_pre = insp.open_prefixes
            open_suf = insp.open_suffixes
            has_open_crafted = (insp.crafted_mod_count == 0) and (open_pre + open_suf > 0)
        except Exception as exc:
            has_open_crafted = False
            if warnings is not None:
                warnings.append(f"[net-gap-degraded] Crafted-slot inspection skipped on `{slot_name}` ({exc}).")

    catalyst_eligible = (
        slot_name in _JEWELLERY_BELT_SLOTS
        and not parity.is_corrupted
        and not parity.is_sanctified
        and not parity.is_mirrored
    )
    is_weapon_slot = slot_name in ("Weapon 1", "Weapon 2", "Weapon 1 Swap", "Weapon 2 Swap")
    return EquippedSlotAudit(
        slot_name=slot_name,
        item_id=item_id,
        name=name,
        base_name=base_name,
        rarity=rarity.upper(),
        item_type=item_type,
        raw_text=raw_text,
        stripped_raw_text=stripped_raw,
        parity=parity,
        editable_socket_count=editable_sockets,
        stripped_runes=tuple(stripped),
        preserved_bound_runes=tuple(kept),
        has_open_crafted_slot=has_open_crafted,
        open_prefix_count=open_pre,
        open_suffix_count=open_suf,
        catalyst_eligible=catalyst_eligible,
        is_weapon_slot=is_weapon_slot,
    )


def analyze_live_net_gap(
    worker: Any,
    *,
    catalog: RuneInstillCatalog | None = None,
) -> NetGapAudit:
    """Run Step 0 on a live `BridgeWorker`: deactivate Charms, inspect WS1/WS2, strip replaceable runes in CoW."""
    cat = catalog or load_rune_instill_catalog()
    warnings: list[str] = []
    info = worker.call("get_build_info", {})
    level = int(info.get("level") or 90)
    asc = str(info.get("ascendClassName") or info.get("ascendancyName") or "")
    is_shaman = asc.lower() == "shaman"

    listing = worker.call("list_items", {})
    items_by_id = {int(it["id"]): it for it in (listing.get("items") or ()) if isinstance(it, dict) and "id" in it}
    raw_slots = listing.get("slots") or ()

    # Detect Giant's Blood & radius jewels on tree
    tstate = worker.call("get_tree_state", {})
    tdata = worker.call("tree_data", {})
    nodes_map = tdata.get("nodes") or {}
    giants_blood = False
    for nid in tstate.get("allocatedNodes") or ():
        nd = nodes_map.get(str(nid)) or {}
        if _GIANTS_BLOOD_RE.search(str(nd.get("name") or "")):
            giants_blood = True

    radius_jewel_nodes: list[int] = []
    audited_slots: list[EquippedSlotAudit] = []
    has_active_charm = False
    has_ws2_equip = False

    for sl in raw_slots:
        if not isinstance(sl, dict):
            continue
        sname = str(sl.get("name") or "")
        sel_id = int(sl.get("selItemId") or sl.get("selectedId") or 0)
        if sel_id <= 0 or sel_id not in items_by_id:
            continue
        meta = items_by_id[sel_id]
        raw_txt = str(worker.call("item_raw", {"id": sel_id}).get("raw") or "")
        if sname in _CHARM_SLOTS:
            if sl.get("active"):
                has_active_charm = True
            continue
        if sname in _FLASK_SLOTS:
            continue
        if sl.get("nodeId"):
            if _RADIUS_JEWEL_RE.search(raw_txt):
                radius_jewel_nodes.append(int(sl["nodeId"]))
            continue
        if sname in ("Weapon 1 Swap", "Weapon 2 Swap"):
            has_ws2_equip = True
        audited_slots.append(
            audit_equipped_slot(
                slot_name=sname,
                item_id=sel_id,
                name=str(meta.get("name") or ""),
                base_name=str(meta.get("baseName") or ""),
                rarity=str(meta.get("rarity") or "RARE"),
                item_type=str(meta.get("type") or ""),
                raw_text=raw_txt,
                is_shaman=is_shaman,
                catalog=cat,
                warnings=warnings,
            )
        )

    if has_active_charm:
        warnings.append("Hit-triggered Charm(s) deactivated for permanent resistance & overcap calculation.")
    res_ov = scan_res_override_lines_in_items(
        [(s.slot_name, s.name or s.base_name, s.raw_text) for s in audited_slots]
    )
    warnings.extend(res_ov["warnings"])

    # Capture live `currentInGame` baseline BEFORE deactivating hit-triggered Charms.
    # Permanent campaign quest rewards (`quest*`) are NEVER stripped in any snapshot!
    ingame_ws1 = worker.call("get_stats", {}).get("stats") or {}
    ingame_snap = stat_snapshot_from_pob_stats(
        ingame_ws1, level=level, ascendancy=asc, giants_blood=giants_blood
    )

    # Measure permanent baseline (with Charms explicitly turned off) and bare net gap (replaceable runes stripped)
    with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False):
        if has_active_charm:
            for cname in sorted(_CHARM_SLOTS):
                try:
                    worker.call("set_slot_active", {"slotName": cname, "active": False})
                except Exception as exc:
                    warnings.append(f"[rebalance-degraded] Could not deactivate `{cname}` ({exc}).")
        ws1_stats = worker.call("get_stats", {}).get("stats") or {}
        ws2_stats: dict[str, Any] | None = None
        if has_ws2_equip:
            worker.call("set_weapon_swap", {"on": True})
            ws2_stats = worker.call("get_stats", {}).get("stats") or {}
            worker.call("set_weapon_swap", {"on": False})
        current_snap = stat_snapshot_from_pob_stats(
            ws1_stats, level=level, ascendancy=asc, giants_blood=giants_blood, ws2_stats=ws2_stats
        )

        # Strip ONLY replaceable socket runes (preserving all gear, permanent campaign quests, Mirrored & isSocketBound)
        any_stripped = False
        for sl_audit in audited_slots:
            if sl_audit.stripped_runes:
                worker.call("add_item", {"raw": sl_audit.stripped_raw_text, "slotName": sl_audit.slot_name})
                any_stripped = True
        if any_stripped:
            bare_ws1 = worker.call("get_stats", {}).get("stats") or {}
            bare_ws2: dict[str, Any] | None = None
            if has_ws2_equip:
                worker.call("set_weapon_swap", {"on": True})
                bare_ws2 = worker.call("get_stats", {}).get("stats") or {}
                worker.call("set_weapon_swap", {"on": False})
            bare_snap = stat_snapshot_from_pob_stats(
                bare_ws1, level=level, ascendancy=asc, giants_blood=giants_blood, ws2_stats=bare_ws2
            )
        else:
            bare_snap = current_snap

    crafted_hints = [
        {
            "slot": s.slot_name,
            "item": s.name,
            "openPrefixes": s.open_prefix_count,
            "openSuffixes": s.open_suffix_count,
            "note": "0.5.0+ single Crafted mod slot is unused (Essence/Alloy/Distill legal without overwriting).",
        }
        for s in audited_slots
        if s.has_open_crafted_slot
    ]

    return NetGapAudit(
        current=current_snap,
        bare_net=bare_snap,
        slots=tuple(audited_slots),
        protected_radius_jewel_nodes=tuple(sorted(set(radius_jewel_nodes))),
        charm_resists_excluded=True,
        warnings=tuple(warnings),
        crafted_slot_hints=tuple(crafted_hints),
        current_in_game=ingame_snap,
    )


__all__ = [
    "EquippedSlotAudit",
    "NetGapAudit",
    "StatSnapshot",
    "analyze_live_net_gap",
    "audit_equipped_slot",
    "stat_snapshot_from_pob_stats",
]
