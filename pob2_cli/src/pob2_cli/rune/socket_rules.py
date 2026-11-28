# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 Item.lua 21-slot socket parity, override, level, Shaman Bonded, and dual-write rendering rules (L2).

Strict source parity with `PathOfBuilding-PoE2` `PoE2/Classes/Item.lua`:
  - Line 981: `socketedAugmentTypeOverride` ("This item gains bonuses from socketed items as though it was a(n) ...")
  - Lines 1610-1692: `UpdateRunes` & `ApplySocketedRuneDisplayScalars` (`effectModifier`)
  - Lines 1786-1800: `GetSocketedAugmentTypes` (21-slot parity: Wand/Staff/Sceptre -> caster != weapon,
    Quarterstaff -> weapon != caster, Buckler -> armour+buckler != shield)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .anoint_raw import (
    apply_anoint_to_amulet_raw,
    extract_existing_anoint,
    strip_anoint_from_amulet_raw,
)
from .catalog import RuneEntry, RuneInstillCatalog, RuneSlotEffect, load_rune_instill_catalog

_OVERRIDE_RE = re.compile(
    r"^this item gains bonuses from socketed items as though it was a? ?(.+)$",
    re.IGNORECASE,
)
_AUGMENT_EFFECT_RE = re.compile(r"(\d+)% increased effect of socketed items\b", re.IGNORECASE)
_RUNE_EFFECT_RE = re.compile(r"(\d+)% increased effect of socketed runes\b", re.IGNORECASE)
_SOUL_CORE_EFFECT_RE = re.compile(r"(\d+)% increased effect of socketed soul cores\b", re.IGNORECASE)
_IMPLICITS_RE = re.compile(r"^Implicits:\s*(\d+)\s*$", re.IGNORECASE)
_SOCKETS_RE = re.compile(r"^Sockets:\s*(.+)$", re.IGNORECASE)
_RUNE_HEADER_RE = re.compile(r"^Rune:\s*(.+)$", re.IGNORECASE)

# Canonical 21 ModRunes.lua slot keys.
VALID_MODRUNE_SLOT_KEYS: frozenset[str] = frozenset(
    {
        "armour", "body armour", "boots", "bow", "buckler", "caster", "crossbow",
        "focus", "gloves", "helmet", "martial weapon wand or staff", "one hand mace",
        "quarterstaff", "sceptre", "shield", "spear", "staff", "talisman",
        "two hand mace", "wand", "weapon",
    }
)

_CASTER_TYPES: frozenset[str] = frozenset({"wand", "staff", "sceptre"})
_MARTIAL_WEAPON_TYPES: frozenset[str] = frozenset(
    {
        "bow", "crossbow", "quarterstaff", "warstaff", "one hand mace", "two hand mace",
        "spear", "talisman", "one hand sword", "two hand sword", "one hand axe",
        "two hand axe", "claw", "dagger", "flail", "weapon",
    }
)
_ARMOUR_TYPES: frozenset[str] = frozenset({"helmet", "body armour", "gloves", "boots", "shield", "buckler", "focus", "armour"})


@dataclass(frozen=True, slots=True)
class SocketParityResult:
    """Resolved augment slot keys and legality metadata for an item."""

    base_type: str | None  # "weapon" | "armour" | "caster"
    specific_type: str  # e.g. "wand", "quarterstaff", "buckler", "helmet", ...
    override_type: str | None
    accepted_keys: tuple[str, ...]
    rune_socket_count: int
    jewel_socket_count: int
    is_mirrored: bool
    is_corrupted: bool
    is_sanctified: bool
    effect_item_pct: float
    effect_rune_pct: float
    effect_soul_core_pct: float
    existing_runes: tuple[str, ...]
    locked_socket_indices: tuple[int, ...]

    def effect_multiplier(self, kind: str) -> float:
        """Return total socket effect multiplier matching Item.lua:1680-1687."""
        bonus = self.effect_item_pct
        k = (kind or "").lower()
        if k == "soulcore":
            bonus += self.effect_soul_core_pct
        elif k == "rune":
            bonus += self.effect_rune_pct
        return 1.0 + bonus / 100.0


def parse_socketed_augment_override(raw_text: str) -> str | None:
    """Extract unique `socketedAugmentTypeOverride` (Item.lua:981)."""
    for line in (raw_text or "").splitlines():
        cleaned = re.sub(r"^\{[^}]+\}", "", line.strip()).strip().lower()
        m = _OVERRIDE_RE.match(cleaned)
        if m:
            val = m.group(1).strip().rstrip(".")
            if val.startswith("an "):
                val = val[3:].strip()
            elif val.startswith("a "):
                val = val[2:].strip()
            return val
    return None


def parse_socketed_effect_modifiers(raw_text: str) -> tuple[float, float, float]:
    """Return `(item_pct, rune_pct, soul_core_pct)` effect bonuses from item text."""
    item_pct = rune_pct = core_pct = 0.0
    for line in (raw_text or "").splitlines():
        cleaned = re.sub(r"^\{[^}]+\}", "", line.strip())
        for m in _AUGMENT_EFFECT_RE.finditer(cleaned):
            item_pct += float(m.group(1))
        for m in _RUNE_EFFECT_RE.finditer(cleaned):
            rune_pct += float(m.group(1))
        for m in _SOUL_CORE_EFFECT_RE.finditer(cleaned):
            core_pct += float(m.group(1))
    return item_pct, rune_pct, core_pct


def resolve_item_augment_types(
    item_type: str,
    *,
    sub_type: str | None = None,
    raw_text: str = "",
    base_name: str = "",
    catalog: RuneInstillCatalog | None = None,
) -> SocketParityResult:
    """Resolve exact PoE2 `Item.lua:1786` augment slot keys + socket/protection metadata."""
    it_low = (item_type or "").strip().lower()
    sub_low = (sub_type or "").strip().lower()
    bn_low = (base_name or "").strip().lower()

    # Infer type from base_name or raw_text when caller only passes item_type="" or generic slot name
    if not it_low:
        it_low = _infer_item_type_from_text(raw_text, bn_low)

    if it_low in ("helm",):
        it_low = "helmet"
    elif it_low in ("body", "chest", "bodyarmour"):
        it_low = "body armour"

    override = parse_socketed_augment_override(raw_text)

    if override:
        base_type: str | None = "armour"
        specific_type = override
    elif it_low in _CASTER_TYPES:
        base_type = "caster"
        specific_type = it_low
    elif it_low in ("quarterstaff", "warstaff") or sub_low == "warstaff":
        base_type = "weapon"
        specific_type = "quarterstaff"
    elif it_low in _MARTIAL_WEAPON_TYPES:
        base_type = "weapon"
        specific_type = it_low
    elif it_low == "buckler" or (it_low == "shield" and (sub_low == "evasion" or "buckler" in bn_low)):
        base_type = "armour"
        specific_type = "buckler"
    elif it_low in _ARMOUR_TYPES:
        base_type = "armour"
        specific_type = it_low
    else:
        base_type = None
        specific_type = it_low

    keys: list[str] = []
    if base_type:
        keys.append(base_type)
    if specific_type and specific_type not in keys:
        keys.append(specific_type)
    # "martial weapon wand or staff" applies to martial weapons + wands + staves (NOT sceptres/armours)
    if base_type == "weapon" or specific_type in ("wand", "staff"):
        keys.append("martial weapon wand or staff")

    rune_sockets = 0
    jewel_sockets = 0
    existing_runes: list[str] = []
    is_mirrored = is_corrupted = is_sanctified = False
    for line in (raw_text or "").splitlines():
        st = line.strip()
        low = st.lower()
        if low == "mirrored":
            is_mirrored = True
        elif low == "corrupted":
            is_corrupted = True
        elif low == "sanctified":
            is_sanctified = True
        sm = _SOCKETS_RE.match(st)
        if sm:
            toks = [t.strip().upper() for t in re.split(r"[\s\-]+", sm.group(1)) if t.strip()]
            rune_sockets = sum(1 for t in toks if t == "S")
            jewel_sockets = sum(1 for t in toks if t == "J")
        rm = _RUNE_HEADER_RE.match(st)
        if rm:
            rname = rm.group(1).strip()
            if rname and rname.lower() != "none":
                existing_runes.append(rname)

    cat = catalog or load_rune_instill_catalog()
    locked: list[int] = []
    for idx, rname in enumerate(existing_runes):
        if rname in cat.socket_bound_names:
            locked.append(idx)

    eff_item, eff_rune, eff_core = parse_socketed_effect_modifiers(raw_text)
    return SocketParityResult(
        base_type=base_type,
        specific_type=specific_type,
        override_type=override,
        accepted_keys=tuple(keys),
        rune_socket_count=max(rune_sockets, len(existing_runes)),
        jewel_socket_count=jewel_sockets,
        is_mirrored=is_mirrored,
        is_corrupted=is_corrupted,
        is_sanctified=is_sanctified,
        effect_item_pct=eff_item,
        effect_rune_pct=eff_rune,
        effect_soul_core_pct=eff_core,
        existing_runes=tuple(existing_runes),
        locked_socket_indices=tuple(locked),
    )


def _infer_item_type_from_text(raw_text: str, base_low: str) -> str:
    blob = f"{base_low}\n{(raw_text or '').lower()}"
    for needle, mapped in (
        ("quarterstaff", "quarterstaff"),
        ("sceptre", "sceptre"),
        ("wand", "wand"),
        ("staff", "staff"),
        ("crossbow", "crossbow"),
        ("bow", "bow"),
        ("buckler", "buckler"),
        ("shield", "shield"),
        ("focus", "focus"),
        ("spear", "spear"),
        ("talisman", "talisman"),
        ("two hand mace", "two hand mace"),
        ("mace", "one hand mace"),
        ("helmet", "helmet"),
        ("helm", "helmet"),
        ("boots", "boots"),
        ("gloves", "gloves"),
        ("body armour", "body armour"),
        ("coat", "body armour"),
        ("robe", "body armour"),
        ("jacket", "body armour"),
        ("armour", "body armour"),
    ):
        if needle in blob:
            return mapped
    return ""


def resolve_rune_effects_for_item(
    rune: RuneEntry,
    parity: SocketParityResult,
    *,
    char_level: int = 100,
    is_shaman: bool = False,
) -> tuple[list[str], list[str]]:
    """Return `(effective_mod_lines, rejection_reasons)` under all 4 PoE2 Item.lua hard rules."""
    reasons: list[str] = []
    matched_slots: list[RuneSlotEffect] = []
    for key in parity.accepted_keys:
        eff = rune.slots.get(key)
        if eff is not None and eff not in matched_slots:
            matched_slots.append(eff)
    if not matched_slots:
        reasons.append(
            f"Rune `{rune.name}` has no legal effect for slot types {list(parity.accepted_keys)} "
            f"(supports {sorted(rune.slots.keys())})."
        )
        return [], reasons

    req_lvl = max(s.req_level for s in matched_slots)
    if req_lvl > char_level:
        reasons.append(
            f"Rune `{rune.name}` requires level {req_lvl} > character level {char_level} (would lock equipment)."
        )
        return [], reasons

    mods: list[str] = []
    for eff in matched_slots:
        mods.extend(eff.stats)
        if is_shaman:
            mods.extend(eff.bonded)
    return mods, []


def eligible_runes_for_item(
    parity: SocketParityResult,
    *,
    char_level: int = 100,
    is_shaman: bool = False,
    include_socket_bound: bool = False,
    catalog: RuneInstillCatalog | None = None,
) -> list[tuple[RuneEntry, tuple[str, ...]]]:
    """Return all legal `(RuneEntry, effective_mods)` pairs for *parity* obeying all 4 hard rules."""
    cat = catalog or load_rune_instill_catalog()
    out: list[tuple[RuneEntry, tuple[str, ...]]] = []
    for rune in cat.runes:
        if rune.is_socket_bound and not include_socket_bound:
            continue
        mods, reasons = resolve_rune_effects_for_item(rune, parity, char_level=char_level, is_shaman=is_shaman)
        if not reasons and mods:
            out.append((rune, tuple(mods)))
    return out


def strip_replaceable_runes_from_item_raw(
    raw_text: str,
    *,
    item_type: str = "",
    sub_type: str | None = None,
    base_name: str = "",
    is_shaman: bool = False,
    catalog: RuneInstillCatalog | None = None,
) -> tuple[str, list[str], list[str]]:
    """Strip replaceable runes from PoB2 item raw text while protecting Mirrored items & 13 isSocketBound runes.

    Returns `(updated_raw, stripped_rune_names, preserved_bound_runes)`.
    """
    cat = catalog or load_rune_instill_catalog()
    parity = resolve_item_augment_types(
        item_type, sub_type=sub_type, raw_text=raw_text, base_name=base_name, catalog=cat
    )
    if parity.is_mirrored:
        return raw_text, [], list(parity.existing_runes)

    kept_runes: list[str] = []
    stripped_runes: list[str] = []
    for rname in parity.existing_runes:
        if rname in cat.socket_bound_names:
            kept_runes.append(rname)
        else:
            stripped_runes.append(rname)
    if not stripped_runes:
        return raw_text, [], kept_runes

    rendered = apply_runes_to_item_raw(
        raw_text,
        kept_runes,
        item_type=item_type,
        sub_type=sub_type,
        base_name=base_name,
        char_level=100,
        is_shaman=is_shaman,
        allow_overwrite_bound=True,
        catalog=cat,
    )
    return rendered, stripped_runes, kept_runes


def apply_runes_to_item_raw(
    raw_text: str,
    rune_names: list[str],
    *,
    item_type: str = "",
    sub_type: str | None = None,
    base_name: str = "",
    char_level: int = 100,
    is_shaman: bool = False,
    allow_overwrite_bound: bool = False,
    catalog: RuneInstillCatalog | None = None,
) -> str:
    """Dual-write `Rune: <name>` headers AND `{enchant}{rune}<mod>` implicit lines onto PoB2 item text."""
    cat = catalog or load_rune_instill_catalog()
    parity = resolve_item_augment_types(
        item_type, sub_type=sub_type, raw_text=raw_text, base_name=base_name, catalog=cat
    )
    if parity.is_mirrored:
        raise ValueError("Cannot modify socketed runes on a Mirrored item.")
    if not allow_overwrite_bound and parity.locked_socket_indices:
        bound_list = [parity.existing_runes[i] for i in parity.locked_socket_indices]
        if any(b not in rune_names for b in bound_list):
            raise ValueError(f"Cannot overwrite isSocketBound augment(s): {bound_list}")
    if parity.rune_socket_count > 0 and len(rune_names) > parity.rune_socket_count:
        raise ValueError(
            f"Requested {len(rune_names)} rune(s) but item only has {parity.rune_socket_count} rune socket(s)."
        )

    new_rune_mod_lines: list[str] = []
    canonical_names: list[str] = []
    for rname in rune_names:
        entry = cat.lookup_rune(rname)
        if entry is None:
            raise ValueError(f"Unknown PoE2 rune/soul core/idol: {rname!r}")
        mods, reasons = resolve_rune_effects_for_item(entry, parity, char_level=char_level, is_shaman=is_shaman)
        if reasons:
            raise ValueError("; ".join(reasons))
        canonical_names.append(entry.name)
        for mline in mods:
            new_rune_mod_lines.append(f"{{enchant}}{{rune}}{mline}")

    lines = (raw_text or "").splitlines()
    # 1. Strip old Rune: headers and old {rune} implicit lines, locate Implicits: header
    out_header: list[str] = []
    implicit_count_idx: int | None = None
    orig_implicit_count = 0
    for ln in lines:
        if _RUNE_HEADER_RE.match(ln.strip()):
            continue
        out_header.append(ln)

    for i, ln in enumerate(out_header):
        im = _IMPLICITS_RE.match(ln.strip())
        if im:
            implicit_count_idx = i
            orig_implicit_count = int(im.group(1))
            break

    # Insert new `Rune:` header lines right after `Sockets:` (or before `Implicits:` / end of header)
    insert_hdr_at = next((i + 1 for i, ln in enumerate(out_header) if _SOCKETS_RE.match(ln.strip())), None)
    if insert_hdr_at is None:
        insert_hdr_at = implicit_count_idx if implicit_count_idx is not None else len(out_header)
    rune_hdr_lines = [f"Rune: {rn}" for rn in canonical_names]
    out_header[insert_hdr_at:insert_hdr_at] = rune_hdr_lines
    if implicit_count_idx is not None and insert_hdr_at <= implicit_count_idx:
        implicit_count_idx += len(rune_hdr_lines)

    if implicit_count_idx is None:
        # Insert Implicits section before trailing Corrupted/Sanctified/Mirrored lines if missing
        tail_idx = next(
            (i for i, ln in enumerate(out_header) if ln.strip().lower() in ("corrupted", "sanctified", "mirrored")),
            len(out_header),
        )
        out_header[tail_idx:tail_idx] = [f"Implicits: {len(new_rune_mod_lines)}", *new_rune_mod_lines]
        return "\n".join(out_header)

    imp_start = implicit_count_idx + 1
    imp_end = min(len(out_header), imp_start + orig_implicit_count)
    kept_implicits = [ln for ln in out_header[imp_start:imp_end] if "{rune}" not in ln.lower()]
    tail_after = [ln for ln in out_header[imp_end:] if "{rune}" not in ln.lower()]
    combined_implicits = new_rune_mod_lines + kept_implicits
    out_header[implicit_count_idx] = f"Implicits: {len(combined_implicits)}"
    out_header[imp_start:] = [*combined_implicits, *tail_after]
    return "\n".join(out_header)


__all__ = [
    "VALID_MODRUNE_SLOT_KEYS",
    "SocketParityResult",
    "apply_anoint_to_amulet_raw",
    "apply_runes_to_item_raw",
    "eligible_runes_for_item",
    "extract_existing_anoint",
    "parse_socketed_augment_override",
    "parse_socketed_effect_modifiers",
    "resolve_item_augment_types",
    "resolve_rune_effects_for_item",
    "strip_anoint_from_amulet_raw",
    "strip_replaceable_runes_from_item_raw",
]
