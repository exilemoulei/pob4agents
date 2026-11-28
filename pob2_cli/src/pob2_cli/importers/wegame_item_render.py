# SPDX-License-Identifier: GPL-3.0-only
"""WeGame equipment/jewel item-text renderer with 8 mod tags and Shaman-bonded stripping (L1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import DaemonRequestError
from .wegame_translate import (
    WeGameTranslator,
    clean_wegame_text,
    has_shaman_bonded_marker,
    parse_leading_int,
    strip_shaman_bonded_prefix,
)

SPECIAL_MOD_FIELDS: tuple[tuple[str, str], ...] = (
    ("craftedMods", "crafted"),
    ("fracturedMods", "fractured"),
    ("desecratedMods", "desecrated"),
    ("mutatedMods", "mutated"),
)
_RARITY_MAP: dict[int, str] = {0: "NORMAL", 1: "MAGIC", 3: "UNIQUE"}
_DEFENCE_PROP_TYPES: dict[int, str] = {16: "Armour", 17: "Evasion", 18: "Energy Shield"}


@dataclass
class ParsedMod:
    lines: list[str]
    flags: dict[str, bool]

    def render_lines(self, field_name: str) -> list[str]:
        tags: list[str] = []
        if field_name == "enchantMods":
            tags.append("{enchant}")
        elif field_name == "runeMods":
            tags.append("{enchant}{rune}")
        for key in ("fractured", "crafted", "desecrated", "mutated"):
            if self.flags.get(key):
                tags.append(f"{{{key}}}")
        prefix = "".join(tags)
        return [f"{prefix}{ln}" for ln in self.lines]


def extract_item_mods(
    item: dict[str, Any],
    field_name: str,
    translator: WeGameTranslator,
    warnings: set[str],
    *,
    is_shaman: bool,
) -> list[ParsedMod]:
    """Extract mods from a WeGame array supporting mixed `dict | str` elements."""
    raw_list = item.get(field_name)
    if not isinstance(raw_list, list):
        return []
    out: list[ParsedMod] = []
    for entry in raw_list:
        if isinstance(entry, str):
            raw_desc = entry
            flag_src: dict[str, Any] = {}
        elif isinstance(entry, dict):
            raw_desc = str(entry.get("description") or "")
            raw_flags = entry.get("flags")
            flag_src = raw_flags if isinstance(raw_flags, dict) else entry
        else:
            continue
        parsed_lines: list[str] = []
        for raw_ln in raw_desc.splitlines():
            raw_ln = raw_ln.strip()
            if not raw_ln:
                continue
            if has_shaman_bonded_marker(raw_ln):
                if not is_shaman:
                    warnings.add("Shaman-only Bonded rune modifier stripped for non-Shaman character.")
                    continue
                inner = strip_shaman_bonded_prefix(clean_wegame_text(raw_ln))
                en_inner, matched = translator.translate_stat_line(inner)
                if not matched:
                    warnings.add(f"Untranslated mod preserved: {inner}")
                parsed_lines.append(f"Bonded: {en_inner}")
                continue
            en_ln, matched = translator.translate_stat_line(raw_ln)
            if not matched:
                warnings.add(f"Untranslated mod preserved: {en_ln}")
            if en_ln:
                parsed_lines.append(en_ln)
        if not parsed_lines:
            continue
        flags = {flag: bool(flag_src[flag]) for _, flag in SPECIAL_MOD_FIELDS if isinstance(flag_src.get(flag), bool)}
        out.append(ParsedMod(lines=parsed_lines, flags=flags))
    return out


def collect_explicit_mods(
    item: dict[str, Any],
    translator: WeGameTranslator,
    warnings: set[str],
    *,
    is_shaman: bool,
) -> list[ParsedMod]:
    """Merge explicitMods + crafted/fractured/desecrated/mutatedMods with 1-to-1 dedup."""
    mods = extract_item_mods(item, "explicitMods", translator, warnings, is_shaman=is_shaman)
    explicit_count = len(mods)
    for field_name, flag in SPECIAL_MOD_FIELDS:
        earlier_count = len(mods)
        matched_indices: set[int] = set()
        for entry in extract_item_mods(item, field_name, translator, warnings, is_shaman=is_shaman):
            entry.flags[flag] = True

            def _is_compatible(
                idx: int,
                existing: ParsedMod,
                cand: ParsedMod = entry,
                used: set[int] = matched_indices,
            ) -> bool:
                if idx in used or existing.lines != cand.lines:
                    return False
                return all(existing.flags.get(k, v) == v for k, v in cand.flags.items())

            dup_idx: int | None = None
            for idx in range(earlier_count):
                existing = mods[idx]
                if _is_compatible(idx, existing) and any(v and existing.flags.get(k) for k, v in entry.flags.items()):
                    dup_idx = idx
                    break
            if dup_idx is None:
                for idx in range(explicit_count):
                    existing = mods[idx]
                    if _is_compatible(idx, existing) and not any(existing.flags.values()):
                        dup_idx = idx
                        break
            if dup_idx is not None:
                matched_indices.add(dup_idx)
                mods[dup_idx].flags.update(entry.flags)
            else:
                mods.append(entry)
    return mods


def _format_requirement_lines(item: dict[str, Any]) -> list[str]:
    reqs = item.get("requirements")
    if not isinstance(reqs, list):
        return []
    label_map = {
        "Level": "LevelReq", "等级": "LevelReq", "等級": "LevelReq",
        "Str": "Str", "Strength": "Str", "力量": "Str",
        "Dex": "Dex", "Dexterity": "Dex", "敏捷": "Dex",
        "Int": "Int", "Intelligence": "Int", "智慧": "Int", "智力": "Int",
    }
    lines: list[str] = []
    for req in reqs:
        if not isinstance(req, dict):
            continue
        name = clean_wegame_text(str(req.get("name") or "")).rstrip(":：").strip()
        vals = req.get("values")
        raw_val = vals[0][0] if isinstance(vals, list) and vals and isinstance(vals[0], list) and vals[0] else None
        lbl = label_map.get(name)
        num = parse_leading_int(str(raw_val)) if raw_val is not None else None
        if lbl and num is not None:
            lines.append(f"{lbl}: {num}")
    return lines


def render_wegame_item_text(
    item: dict[str, Any],
    translator: WeGameTranslator,
    warnings: set[str],
    *,
    is_shaman: bool,
) -> str:
    """Render a WeGame equipment or jewel dict into native PoB2 `<Item>` body text."""
    raw_base = str(item.get("baseType") or item.get("typeLine") or "").strip()
    base, base_ok = translator.translate_item_name(raw_base, is_unique=False)
    if not base:
        raise DaemonRequestError("WeGame item is missing baseType/typeLine.")
    if not base_ok:
        warnings.add(f"Untranslated item base preserved: {base}")

    frame_type = int(item.get("frameType", 2)) if isinstance(item.get("frameType"), int) else 2
    rarity = _RARITY_MAP.get(frame_type, "RARE")
    lines = [f"Rarity: {rarity}"]
    if rarity in ("RARE", "UNIQUE"):
        raw_name = str(item.get("name") or "").strip()
        disp_name, name_ok = translator.translate_item_name(raw_name, is_unique=(rarity == "UNIQUE"))
        if rarity == "UNIQUE" and raw_name and not name_ok:
            warnings.add(f"Untranslated unique name preserved: {disp_name}")
        lines.append(disp_name or base)
    lines.append(base)

    props = item.get("properties") if isinstance(item.get("properties"), list) else []
    quality_written = False
    for prop in props:
        if not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        vals = prop.get("values")
        val0 = str(vals[0][0]) if isinstance(vals, list) and vals and isinstance(vals[0], list) and vals[0] else ""
        if ptype == 6 and not quality_written:
            q_line = translator.format_quality_line(str(prop.get("name") or ""), val0)
            if q_line:
                lines.append(q_line)
                quality_written = True
        elif isinstance(ptype, int) and ptype in _DEFENCE_PROP_TYPES and val0:
            lines.append(f"{_DEFENCE_PROP_TYPES[ptype]}: {clean_wegame_text(val0)}")

    if isinstance(item.get("ilvl"), int):
        lines.append(f"Item Level: {item['ilvl']}")
    lines.extend(_format_requirement_lines(item))

    sockets = item.get("sockets") if isinstance(item.get("sockets"), list) else []
    socketed_items = item.get("socketedItems") if isinstance(item.get("socketedItems"), list) else []
    if sockets:
        kinds = ["J" if isinstance(s, dict) and s.get("type") == "jewel" else "S" for s in sockets]
        lines.append(f"Sockets: {' '.join(kinds)}")
        for idx, sock in enumerate(sockets):
            if isinstance(sock, dict) and sock.get("type") == "jewel":
                warnings.add("Equipment-socketed jewels require manual review.")
                continue
            rune_obj = next((r for r in socketed_items if isinstance(r, dict) and r.get("socket") == idx), None)
            if rune_obj is not None:
                raw_rune = str(rune_obj.get("baseType") or rune_obj.get("typeLine") or "")
                en_rune, rune_ok = translator.translate_rune(raw_rune)
                if not rune_ok:
                    warnings.add(f"Untranslated rune preserved: {en_rune}")
                if en_rune:
                    lines.append(f"Rune: {en_rune}")

    enchant_lines = [
        ln for m in extract_item_mods(item, "enchantMods", translator, warnings, is_shaman=is_shaman)
        for ln in m.render_lines("enchantMods")
    ]
    rune_lines = [
        ln for m in extract_item_mods(item, "runeMods", translator, warnings, is_shaman=is_shaman)
        for ln in m.render_lines("runeMods")
    ]
    implicit_lines = [
        ln for m in extract_item_mods(item, "implicitMods", translator, warnings, is_shaman=is_shaman)
        for ln in m.render_lines("implicitMods")
    ]
    all_implicits = enchant_lines + rune_lines + implicit_lines
    lines.append(f"Implicits: {len(all_implicits)}")
    lines.extend(all_implicits)

    for m in collect_explicit_mods(item, translator, warnings, is_shaman=is_shaman):
        lines.extend(m.render_lines("explicitMods"))

    if item.get("corrupted") is True:
        lines.append("Corrupted")
    return "\n".join(lines)


__all__ = [
    "SPECIAL_MOD_FIELDS",
    "ParsedMod",
    "collect_explicit_mods",
    "extract_item_mods",
    "render_wegame_item_text",
]
