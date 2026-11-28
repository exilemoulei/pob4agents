# SPDX-License-Identifier: GPL-3.0-only
"""Post-load PoE2 auto-fixups (#2401 false-negative Spirit, #2536 main skill / statSet, Notes strip) (L1)."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

from .res_override import detect_worker_res_override_lines, scan_res_override_lines_in_items

_LOG = logging.getLogger(__name__)

_POB_COLOR_RE = re.compile(r"\^(?:x[0-9A-Fa-f]{6}|[0-9])")
_BONDED_LINE_RE = re.compile(r"(?:^|\})Bonded:\s*", re.IGNORECASE)
_IMPLICITS_HDR_RE = re.compile(r"^Implicits:\s*(\d+)\s*$")


def strip_pob_color_codes(text: str | None) -> str:
    """Strip PoB colour escape codes (`^xRRGGBB`, `^0`..`^9`) while preserving line breaks."""
    if not isinstance(text, str) or not text:
        return ""
    cleaned = _POB_COLOR_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return cleaned.strip()


def _detect_and_fix_2401_spirit(worker: Any, *, auto_fix: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """Detect (#2401) false-negative Spirit caused by inactive weapon-set aura/persistent groups."""
    fixups: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        stats = (worker.call("get_stats", {}) or {}).get("stats") or {}
        ws_state = worker.call("get_weapon_set_state", {}) or {}
        skills = worker.call("list_skills", {}) or {}
    except (RuntimeError, OSError, ValueError) as exc:
        _LOG.debug("Post-load #2401 Spirit check skipped: %s", exc)
        return fixups, warnings

    spirit_total = float(stats.get("Spirit") or 0)
    spirit_unres = float(stats.get("SpiritUnreserved") if stats.get("SpiritUnreserved") is not None else spirit_total)
    if spirit_unres >= 0:
        return fixups, warnings

    active_ws = 2 if ws_state.get("useSecondWeaponSet") else 1
    ws_groups = ws_state.get("skillWeaponSets") or []
    groups = skills.get("groups") or []
    off_set_indices: list[dict[str, Any]] = []

    for idx, g_ws in enumerate(ws_groups, 1):
        if not isinstance(g_ws, dict) or not g_ws.get("enabled"):
            continue
        grp_info = groups[idx - 1] if idx - 1 < len(groups) else {}
        slot_name = str(grp_info.get("slot") or "")
        set1 = g_ws.get("set1")
        set2 = g_ws.get("set2")
        eg1 = g_ws.get("gem1EnableGlobal1")
        eg2 = g_ws.get("gem1EnableGlobal2")

        is_off_slot = (active_ws == 1 and "Swap" in slot_name) or (
            active_ws == 2 and slot_name in ("Weapon 1", "Weapon 2")
        )
        is_off_binding = (
            (active_ws == 1 and set1 is False and set2 is True)
            or (active_ws == 2 and set2 is False and set1 is True)
            or (active_ws == 1 and eg1 is False and eg2 is True)
            or (active_ws == 2 and eg2 is False and eg1 is True)
        )
        if is_off_slot or is_off_binding:
            off_set_indices.append(
                {
                    "index": idx,
                    "label": str(g_ws.get("label") or grp_info.get("label") or f"Group #{idx}"),
                    "slot": slot_name,
                    "reason": "off_weapon_set_slot" if is_off_slot else "off_weapon_set_binding",
                }
            )

    if not off_set_indices:
        return fixups, warnings

    disabled_groups: list[dict[str, Any]] = []
    after_unres = spirit_unres
    if auto_fix:
        for cand in off_set_indices:
            try:
                worker.call("set_group", {"index": cand["index"], "enabled": False})
                disabled_groups.append(cand)
            except (RuntimeError, OSError, ValueError) as exc:
                _LOG.debug("Failed to disable off-set group #%s: %s", cand["index"], exc)
                continue
        if disabled_groups:
            after_stats = (worker.call("get_stats", {}) or {}).get("stats") or {}
            after_unres = float(after_stats.get("SpiritUnreserved") or after_unres)

    fixups.append(
        {
            "issue": "#2401",
            "kind": "false_negative_spirit_offset_aura",
            "activeWeaponSet": active_ws,
            "spiritUnreservedBefore": spirit_unres,
            "spiritUnreservedAfter": after_unres,
            "detectedGroups": off_set_indices,
            "autoFixed": bool(disabled_groups),
            "disabledGroups": disabled_groups,
        }
    )
    if disabled_groups:
        warnings.append(
            f"[#2401] Disabled {len(disabled_groups)} inactive weapon-set aura/persistent group(s) "
            f"(SpiritUnreserved {spirit_unres:.0f} -> {after_unres:.0f})."
        )
    else:
        warnings.append(
            f"[#2401] Detected {len(off_set_indices)} inactive weapon-set aura/persistent group(s) "
            f"causing negative SpiritUnreserved ({spirit_unres:.0f})."
        )
    return fixups, warnings


def _detect_and_fix_2536_main_skill(worker: Any, *, auto_fix: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """Detect (#2536) 0-DPS main skill group or wrong statSetIndex (e.g. aura group #1 or Tempest Bell statSets[1])."""
    fixups: list[dict[str, Any]] = []
    warnings: list[str] = []
    caps = getattr(worker, "caps", None) or {}
    if not caps.get("deepAuditProbe"):
        return fixups, warnings
    try:
        res = worker.call("select_best_main_skill", {"apply": bool(auto_fix)}) or {}
    except (RuntimeError, OSError, ValueError) as exc:
        _LOG.debug("Post-load #2536 main-skill check skipped: %s", exc)
        return fixups, warnings
    if res.get("changed") or res.get("recommended"):
        fixups.append(
            {
                "issue": "#2536",
                "kind": "smart_main_skill_stat_set",
                "applied": bool(res.get("applied")),
                "beforeGroup": res.get("beforeGroup"),
                "afterGroup": res.get("afterGroup"),
                "beforeStatSet": res.get("beforeStatSet"),
                "afterStatSet": res.get("afterStatSet"),
                "skillName": res.get("skillName"),
                "statSetLabel": res.get("statSetLabel"),
                "beforeDps": res.get("beforeDps", 0),
                "afterDps": res.get("afterDps", 0),
            }
        )
        warnings.append(
            f"[#2536] Main skill calibrated to group #{res.get('afterGroup')} "
            f"`{res.get('skillName')}` (statSet={res.get('afterStatSet')}, "
            f"DPS {res.get('beforeDps', 0):.0f} -> {res.get('afterDps', 0):.0f})."
        )
    return fixups, warnings


def fetch_clean_notes(worker: Any) -> dict[str, Any]:
    """Read author Notes from PoB2 and strip PoB colour codes (`^xRRGGBB`, `^0..^9`)."""
    try:
        res = worker.call("get_notes", {}) or {}
    except (RuntimeError, OSError, ValueError) as exc:
        _LOG.debug("Could not fetch build notes: %s", exc)
        return {"text": "", "rawLength": 0, "hasNotes": False}
    raw_text = str(res.get("text") or "")
    clean = strip_pob_color_codes(raw_text)
    lines = [ln for ln in clean.splitlines() if ln.strip()]
    return {
        "text": clean,
        "preview": "\n".join(lines[:12]),
        "lineCount": len(lines),
        "rawLength": len(raw_text),
        "hasNotes": bool(clean),
    }


def _strip_bonded_lines_from_item_raw(raw: str) -> tuple[str, list[str]]:
    """Strip explicit ``Bonded:`` mod lines from raw item text and keep ``Implicits: N`` consistent."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    removed: list[str] = []
    out_lines: list[str] = []
    impl_idx: int | None = None
    impl_count: int | None = None
    impl_removed = 0
    seen_after_impl = 0

    for ln in lines:
        stripped = ln.strip()
        m_imp = _IMPLICITS_HDR_RE.match(stripped)
        if m_imp:
            impl_idx = len(out_lines)
            impl_count = int(m_imp.group(1))
            out_lines.append(ln)
            continue
        if _BONDED_LINE_RE.search(stripped):
            removed.append(stripped)
            if impl_count is not None and seen_after_impl < impl_count:
                impl_removed += 1
                seen_after_impl += 1
            continue
        if impl_count is not None and seen_after_impl < impl_count and stripped:
            seen_after_impl += 1
        out_lines.append(ln)

    if impl_idx is not None and impl_count is not None and impl_removed > 0:
        out_lines[impl_idx] = f"Implicits: {max(0, impl_count - impl_removed)}"
    return "\n".join(out_lines), removed


def _detect_and_sanitize_non_shaman_bonded_runes(
    worker: Any,
    *,
    auto_fix: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Detect (and strip when unbacked by Rune: header) inactive ``Bonded:`` lines on non-Shaman builds."""
    fixups: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        info = worker.call("get_build_info", {}) or {}
    except (RuntimeError, OSError, ValueError) as exc:
        _LOG.debug("Non-Shaman bonded check skipped (get_build_info): %s", exc)
        return fixups, warnings
    asc = str(info.get("ascendClassName") or "").strip()
    if asc.lower() == "shaman":
        return fixups, warnings

    try:
        xml_res = worker.call("save_xml", {}) or {}
    except (RuntimeError, OSError, ValueError) as exc:
        _LOG.debug("Non-Shaman bonded check skipped (save_xml): %s", exc)
        return fixups, warnings
    xml_text = xml_res.get("xml")
    if not isinstance(xml_text, str) or "Bonded:" not in xml_text:
        return fixups, warnings

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return fixups, warnings
    items_el = root.find("Items")
    if items_el is None:
        return fixups, warnings

    equipped_slot_by_id: dict[int, str] = {}
    for slot_el in items_el.iter("Slot"):
        sname = (slot_el.attrib.get("name") or "").strip()
        try:
            iid = int(slot_el.attrib.get("itemId") or 0)
        except ValueError:
            iid = 0
        if sname and iid > 0 and iid not in equipped_slot_by_id:
            equipped_slot_by_id[iid] = sname

    affected: list[dict[str, Any]] = []
    stripped_slots: list[str] = []
    for item_el in items_el.findall("Item"):
        try:
            iid = int(item_el.attrib.get("id") or 0)
        except ValueError:
            continue
        slot_name = equipped_slot_by_id.get(iid)
        if not slot_name:
            continue
        raw_txt = (item_el.text or "").strip()
        if "Bonded:" not in raw_txt:
            continue
        cleaned_raw, removed = _strip_bonded_lines_from_item_raw(raw_txt)
        if not removed:
            continue
        has_rune_header = any(ln.strip().startswith("Rune:") for ln in raw_txt.splitlines())
        sanitized = False
        if auto_fix and not has_rune_header:
            try:
                worker.call("add_item", {"raw": cleaned_raw, "slotName": slot_name})
                sanitized = True
                stripped_slots.append(slot_name)
            except Exception:
                sanitized = False
        affected.append(
            {
                "slot": slot_name,
                "itemId": iid,
                "bondedLines": removed,
                "hasRuneHeader": has_rune_header,
                "stripped": sanitized,
                "gatedByEngine": has_rune_header and not sanitized,
            }
        )

    if not affected:
        return fixups, warnings
    fixups.append(
        {
            "issue": "non_shaman_bonded_runes",
            "kind": "inactive_bonded_rune_lines",
            "ascendClassName": asc or "None",
            "affectedItems": affected,
            "strippedSlots": stripped_slots,
        }
    )
    warnings.append(
        f"[bonded-non-shaman] Non-Shaman build ({asc or 'None'}) has inactive `Bonded:` rune line(s) on "
        f"{len(affected)} equipped item(s) — Bonded modifiers require Shaman's 'Wisdom of the Maji' "
        f"(CanUseBondedModifiers){f'; stripped explicit lines on {stripped_slots}' if stripped_slots else ' (gated off by engine)'}."
    )
    return fixups, warnings


def apply_post_load_fixups(worker: Any, *, auto_fix: bool = True) -> dict[str, Any]:
    """Run #2401 Spirit check, #2536 main-skill/statSet calibration, non-Shaman Bonded check, res-override check, and Notes."""
    fixups: list[dict[str, Any]] = []
    warnings: list[str] = []

    f1, w1 = _detect_and_fix_2401_spirit(worker, auto_fix=auto_fix)
    fixups.extend(f1)
    warnings.extend(w1)

    f2, w2 = _detect_and_fix_2536_main_skill(worker, auto_fix=auto_fix)
    fixups.extend(f2)
    warnings.extend(w2)

    f3, w3 = _detect_and_sanitize_non_shaman_bonded_runes(worker, auto_fix=auto_fix)
    fixups.extend(f3)
    warnings.extend(w3)

    res_scan = detect_worker_res_override_lines(worker)
    if res_scan["matches"]:
        fixups.append(
            {
                "issue": "res_override_lines",
                "kind": "asymmetric_resistance_is_lines",
                "affectedElements": res_scan["affectedElements"],
                "affectedSlots": res_scan["affectedSlots"],
                "matches": res_scan["matches"],
            }
        )
        warnings.extend(res_scan["warnings"])

    notes = fetch_clean_notes(worker)
    return {"fixups": fixups, "warnings": warnings, "notes": notes}


__all__ = [
    "apply_post_load_fixups",
    "detect_worker_res_override_lines",
    "fetch_clean_notes",
    "scan_res_override_lines_in_items",
    "strip_pob_color_codes",
]
