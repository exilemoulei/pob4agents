# SPDX-License-Identifier: GPL-3.0-only
"""Asymmetric PoB2 `X Resistance is ±N%` item line scanner and worker detector (L1)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Any

_POB_COLOR_RE = re.compile(r"\^(?:x[0-9A-Fa-f]{6}|[0-9])")
_LEADING_BRACE_TAGS_RE = re.compile(r"^(?:\{[^}]*\}\s*)+")
_VARIANT_TAG_RE = re.compile(r"\{variant:([0-9,\s]+)\}", re.IGNORECASE)
_SELECTED_VARIANT_RE = re.compile(r"^Selected\s+(?:Alt\s+)?Variant:\s*(\d+)\s*$", re.IGNORECASE)
_RES_IS_LINE_RE = re.compile(r"\b(Fire|Cold|Lightning|Chaos)\s+Resistance\s+is\s+([+-]?\d+)%", re.IGNORECASE)
_ITEM_HEADER_PREFIXES = (
    "Rarity:",
    "Unique ID:",
    "Item Level:",
    "Quality:",
    "Sockets:",
    "Rune:",
    "LevelReq:",
    "Implicits:",
    "Variant:",
    "Selected Variant:",
    "Has Alt Variant:",
    "Selected Alt Variant:",
    "League:",
    "Radius:",
    "Limited to:",
)


def scan_res_override_lines_in_items(
    equipped_items: Sequence[tuple[str, str, str]],
) -> dict[str, Any]:
    """Scan `(slot_name, item_name, raw_text)` tuples for PoB2 asymmetric `X Resistance is ±N%` lines."""
    matches: list[dict[str, Any]] = []
    seen_elems: list[str] = []
    seen_slots: list[str] = []

    for slot_name, item_name, raw_text in equipped_items:
        if not raw_text or "resistance is" not in raw_text.lower():
            continue
        lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        selected_variants: set[int] = set()
        for ln in lines:
            mv = _SELECTED_VARIANT_RE.match(ln.strip())
            if mv:
                selected_variants.add(int(mv.group(1)))

        for ln in lines:
            stripped = _POB_COLOR_RE.sub("", ln).strip()
            if not stripped or stripped.startswith("<") or stripped.startswith(_ITEM_HEADER_PREFIXES):
                continue
            vt = _VARIANT_TAG_RE.search(stripped)
            if vt and selected_variants:
                v_nums = {int(x.strip()) for x in vt.group(1).split(",") if x.strip().isdigit()}
                if v_nums and not (v_nums & selected_variants):
                    continue
            clean_ln = _LEADING_BRACE_TAGS_RE.sub("", stripped).strip()
            m_res = _RES_IS_LINE_RE.search(clean_ln)
            if not m_res:
                continue
            elem = m_res.group(1).capitalize()
            raw_num = m_res.group(2)
            val = int(raw_num)
            effect = "ignored" if raw_num.startswith("+") else "override"
            matches.append(
                {
                    "slot": slot_name,
                    "itemName": item_name,
                    "element": elem,
                    "value": val,
                    "rawValue": f"{raw_num}%",
                    "line": stripped,
                    "cleanLine": clean_ln,
                    "poB2Effect": effect,
                }
            )
            if elem not in seen_elems:
                seen_elems.append(elem)
            if slot_name and slot_name not in seen_slots:
                seen_slots.append(slot_name)

    if not matches:
        return {"matches": [], "affectedElements": [], "affectedSlots": [], "warnings": []}

    details = "; ".join(
        f"{m['slot']}: `{m['line']}` ({'overrides total to ' + m['rawValue'] if m['poB2Effect'] == 'override' else 'ignored by PoB2 (+0%)'})"
        for m in matches
    )
    elems_str = "/".join(seen_elems)
    slots_str = ", ".join(seen_slots)
    warn = (
        f"[res-override-line] Equipped item(s) in {slots_str} contain asymmetric PoB2 resistance line(s) "
        f"affecting {elems_str} ({details}). PoB2 treats `X Resistance is -N%` as a total-resistance override "
        f"and ignores positive `X Resistance is +N%` lines; verify {elems_str} Resistance against the in-game character sheet."
    )
    return {
        "matches": matches,
        "affectedElements": seen_elems,
        "affectedSlots": seen_slots,
        "warnings": [warn],
    }


def detect_worker_res_override_lines(worker: Any) -> dict[str, Any]:
    """Inspect equipped items on ``worker`` for `X Resistance is ±N%` lines."""
    equipped: list[tuple[str, str, str]] = []
    try:
        listing = worker.call("list_items", {}) or {}
    except (RuntimeError, OSError, ValueError):
        listing = {}

    raw_slots = listing.get("slots") or ()
    items_list = listing.get("items") or ()
    if raw_slots and items_list:
        by_id = {int(it["id"]): it for it in items_list if isinstance(it, dict) and "id" in it}
        for sl in raw_slots:
            if not isinstance(sl, dict):
                continue
            sname = str(sl.get("name") or "").strip()
            sel_id = int(sl.get("selItemId") or sl.get("selectedId") or sl.get("itemId") or 0)
            if not sname or sel_id <= 0 or sel_id not in by_id:
                continue
            meta = by_id[sel_id]
            raw_txt = str(meta.get("raw") or "")
            if not raw_txt:
                try:
                    raw_txt = str((worker.call("item_raw", {"id": sel_id}) or {}).get("raw") or "")
                except (RuntimeError, OSError, ValueError):
                    raw_txt = ""
            if raw_txt:
                equipped.append((sname, str(meta.get("name") or meta.get("baseName") or ""), raw_txt))
        return scan_res_override_lines_in_items(equipped)

    try:
        xml_text = (worker.call("save_xml", {}) or {}).get("xml")
    except (RuntimeError, OSError, ValueError):
        xml_text = None
    if isinstance(xml_text, str) and "Resistance is" in xml_text:
        try:
            root = ET.fromstring(xml_text)
            items_el = root.find("Items")
            if items_el is not None:
                slot_by_id: dict[int, str] = {}
                for slot_el in items_el.iter("Slot"):
                    sname = (slot_el.attrib.get("name") or "").strip()
                    iid = int(slot_el.attrib.get("itemId") or 0)
                    if sname and iid > 0 and iid not in slot_by_id:
                        slot_by_id[iid] = sname
                for item_el in items_el.findall("Item"):
                    iid = int(item_el.attrib.get("id") or 0)
                    if iid in slot_by_id and item_el.text:
                        equipped.append((slot_by_id[iid], f"Item #{iid}", item_el.text))
        except (ET.ParseError, ValueError):
            pass
    return scan_res_override_lines_in_items(equipped)


__all__ = ["detect_worker_res_override_lines", "scan_res_override_lines_in_items"]
