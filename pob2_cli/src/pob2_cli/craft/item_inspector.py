# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Trilingual (zh-CN / zh-TW / EN) PoE2 item affix inspector and 0.5.5 health check (L2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .clipboard_parser import (
    detect_domain_and_strip,
    extract_bounds,
    extract_bounds_tuple,
    extract_values,
    parse_clipboard_sections,
    translate_mod_line,
)
from .models import (
    AffixSide,
    CraftingMod,
    CraftingState,
    ItemRarity,
    ModDomain,
    ModEntry,
    UnknownBaseError,
    WeightConfidence,
    WhittlingReport,
)
from .pool_engine import ModPoolEngine
from .weights_loader import strip_numbers_to_pattern
from .zh_lookup import load_en_base_whitelist

# Backwards-compatible internal aliases
_extract_values = extract_values
_extract_bounds = extract_bounds
_parse_clipboard_sections = parse_clipboard_sections


@dataclass(frozen=True, slots=True)
class InspectedMod:
    raw_text: str
    mod_id: str
    affix_side: AffixSide
    source_pool: str
    family_id: str
    display_tier: int
    required_level: int
    is_crafted: bool
    fractured: bool
    is_desecrated: bool
    resolved: bool
    weight_confidence: WeightConfidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "rawText": self.raw_text,
            "modId": self.mod_id,
            "affixSide": self.affix_side.value,
            "sourcePool": self.source_pool,
            "familyId": self.family_id,
            "displayTier": self.display_tier,
            "requiredLevel": self.required_level,
            "isCrafted": self.is_crafted,
            "fractured": self.fractured,
            "isDesecrated": self.is_desecrated,
            "resolved": self.resolved,
            "weightConfidence": self.weight_confidence.value,
        }


@dataclass(slots=True)
class ItemInspectionReport:
    patch: str
    rarity: ItemRarity
    item_name: str
    base_type: str
    base_slug: str
    ilvl: int
    quality: int
    catalyst_tag: str | None
    corrupted: bool
    sanctified: bool
    max_prefixes: int
    max_suffixes: int
    prefixes: list[InspectedMod] = field(default_factory=list)
    suffixes: list[InspectedMod] = field(default_factory=list)
    implicits: list[str] = field(default_factory=list)
    crafted_mod_count: int = 0
    desecrated_mod_count: int = 0
    fractured_crafted_locked: bool = False
    is_legal_0_5_5: bool = True
    legality_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    whittling_preview: WhittlingReport | None = None
    state: CraftingState | None = None

    @property
    def open_prefixes(self) -> int:
        return max(0, self.max_prefixes - len(self.prefixes))

    @property
    def open_suffixes(self) -> int:
        return max(0, self.max_suffixes - len(self.suffixes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": "poe2",
            "patch": self.patch,
            "rarity": self.rarity.value,
            "itemName": self.item_name,
            "baseType": self.base_type,
            "baseSlug": self.base_slug,
            "ilvl": self.ilvl,
            "quality": self.quality,
            "catalystTag": self.catalyst_tag,
            "corrupted": self.corrupted,
            "sanctified": self.sanctified,
            "slots": {
                "prefixes": len(self.prefixes),
                "maxPrefixes": self.max_prefixes,
                "openPrefixes": self.open_prefixes,
                "suffixes": len(self.suffixes),
                "maxSuffixes": self.max_suffixes,
                "openSuffixes": self.open_suffixes,
            },
            "craftedSlot": {
                "used": self.crafted_mod_count,
                "limit": 1,
                "fracturedLocked": self.fractured_crafted_locked,
                "canAddCrafted": self.crafted_mod_count == 0 and not self.corrupted and not self.sanctified,
            },
            "desecratedSlot": {
                "used": self.desecrated_mod_count,
                "limit": 1,
                "canDesecrate": self.desecrated_mod_count == 0 and not self.corrupted and not self.sanctified,
            },
            "isLegal055": self.is_legal_0_5_5,
            "legalityIssues": list(self.legality_issues),
            "warnings": list(self.warnings),
            "prefixes": [m.to_dict() for m in self.prefixes],
            "suffixes": [m.to_dict() for m in self.suffixes],
            "implicits": list(self.implicits),
            "whittlingPreview": self.whittling_preview.to_dict() if self.whittling_preview else None,
        }


def _pick_best_entry(
    candidates: list[ModEntry] | tuple[ModEntry, ...],
    *,
    ilvl: int,
    side_hint: AffixSide | None,
    tier_hint: int | None,
    name_hint: str | None,
    is_crafted: bool,
    is_desecrated: bool,
    vals: list[float],
    catalyst_scalar: float,
) -> ModEntry | None:
    filtered = [m for m in candidates if side_hint is None or m.affix_side == side_hint] or candidates
    if not filtered:
        return None
    best: ModEntry | None = None
    best_key: tuple[float, float, int] | None = None
    name_low = name_hint.lower() if name_hint else None
    for m in filtered:
        score = 0.0
        if is_desecrated:
            score += 140.0 if m.source_pool == "desecrated" else 20.0
        elif is_crafted:
            score += 140.0 if m.is_crafted_pool else 20.0
        else:
            score += 100.0 if m.source_pool == "normal" else (35.0 if m.is_crafted_pool else 15.0)
        score += 30.0 if m.required_level <= ilvl else -180.0
        if name_low and m.affix_name and m.affix_name.lower() == name_low:
            score += 400.0
        if tier_hint is not None:
            score += 300.0 if m.display_tier == tier_hint else -100.0 * abs(m.display_tier - tier_hint)
        bounds = extract_bounds_tuple(m.stat_text)
        dist = 0.0
        if vals and bounds:
            all_in = True
            for v, (lo, hi) in zip(vals, bounds, strict=False):
                d_raw = 0.0 if lo <= v <= hi else min(abs(v - lo), abs(v - hi))
                if catalyst_scalar > 1.0:
                    vu = v / catalyst_scalar
                    d_cat = 0.0 if lo <= vu <= hi else min(abs(vu - lo), abs(vu - hi))
                    d_best = min(d_raw, d_cat)
                else:
                    d_best = d_raw
                if d_best > 0.0:
                    all_in = False
                    dist += d_best
            score += 320.0 if all_in else -min(500.0, dist * 8.0)
        ck = (score, -dist, -m.display_tier)
        if best_key is None or ck > best_key:
            best_key = ck
            best = m
    return best


@lru_cache(maxsize=1)
def _default_engine() -> ModPoolEngine:
    return ModPoolEngine()


def _global_mod_patterns(eng: ModPoolEngine) -> frozenset[str]:
    return eng.bundle.get_global_mod_patterns()


def _hybrid_vals_plausible(
    cands: list[ModEntry] | tuple[ModEntry, ...], vals: list[float], cat_scalar: float
) -> bool:
    """Return True if *vals* are within plausible (up to 1.25x sanctified) bounds of any candidate hybrid mod."""
    if not vals or not cands:
        return True
    max_per_pos: list[float] = []
    for m in cands:
        for i, (_lo, hi) in enumerate(extract_bounds_tuple(m.stat_text)):
            if i >= len(max_per_pos):
                max_per_pos.append(hi)
            elif hi > max_per_pos[i]:
                max_per_pos[i] = hi
    if not max_per_pos:
        return True
    return all(v <= max_hi * cat_scalar * 1.25 + 1e-6 for v, max_hi in zip(vals, max_per_pos, strict=False))


def _check_out_of_range_warning(
    lookup_mod: str,
    cands: list[ModEntry] | tuple[ModEntry, ...],
    vals: list[float],
    cat_scalar: float,
    hybrid_subline_pats: frozenset[str],
) -> str | None:
    if not vals or not cands:
        return None
    max_per_pos: list[float] = []
    for m in cands:
        for i, (_lo, hi) in enumerate(extract_bounds_tuple(m.stat_text)):
            if i >= len(max_per_pos):
                max_per_pos.append(hi)
            elif hi > max_per_pos[i]:
                max_per_pos[i] = hi
    for v, max_hi in zip(vals, max_per_pos, strict=False):
        if v > max_hi * cat_scalar + 1e-6:
            has_hybrid = strip_numbers_to_pattern(lookup_mod) in hybrid_subline_pats
            hint = "composite pure+hybrid prefix or sanctified roll" if has_hybrid else "sanctified/corrupted or stacked roll"
            return f"[out-of-range] `{lookup_mod}` exceeds single-mod max {max_hi:g} ({hint})."
    return None


def inspect_item_text(
    raw_text: str,
    engine: ModPoolEngine | None = None,
    *,
    base_override: str | None = None,
    ilvl_override: int | None = None,
) -> ItemInspectionReport:
    """Parse PoE2 Ctrl+C / Ctrl+Alt+C / PoB2 item text and run full 0.5.5 affix & legality audit."""
    eng = engine if engine is not None else _default_engine()
    clip = parse_clipboard_sections(raw_text, ilvl_override)
    rarity, ilvl, quality, catalyst_tag = clip.rarity, clip.ilvl, clip.quality, clip.catalyst_tag
    corrupted, sanctified = clip.corrupted, clip.sanctified
    header_names, implicits = clip.header_names, clip.implicits
    raw_mod_lines, legality_issues = clip.raw_mod_lines, clip.legality_issues
    warnings: list[str] = []

    if base_override:
        base_str = base_override
        item_name = header_names[0] if header_names else base_override
    elif len(header_names) >= 2:
        item_name, base_str = header_names[0], header_names[1]
    elif header_names:
        item_name = base_str = header_names[0]
    else:
        raise UnknownBaseError("", tuple(load_en_base_whitelist(eng.bundle.data_dir).suggest_bases("", limit=5)))

    base = eng.resolve_base(base_str)
    pat_map, hybrid_subline_pats = eng.bundle.get_inspector_slug_index(base.poe2db_slug)

    cat_scalar = 1.0 + (quality / 100.0) if (catalyst_tag and quality > 0) else 1.0
    prefixes: list[InspectedMod] = []
    suffixes: list[InspectedMod] = []
    craft_mods: list[CraftingMod] = []

    idx = 0
    while idx < len(raw_mod_lines):
        ln_txt, s_hint, n_hint, t_hint = raw_mod_lines[idx]
        clean_mod, dom, frac, is_cr, is_des = detect_domain_and_strip(ln_txt)
        if dom in (ModDomain.IMPLICIT, ModDomain.ENCHANT, ModDomain.RUNE):
            implicits.append(ln_txt)
            idx += 1
            continue
        lookup_mod, zh_ok = translate_mod_line(clean_mod)
        chosen: ModEntry | None = None
        matched_cands: tuple[ModEntry, ...] = ()
        matched_vals: list[float] = []
        matched_lookup = lookup_mod
        consumed = 1

        if idx + 1 < len(raw_mod_lines):
            nxt_txt, _, _, _ = raw_mod_lines[idx + 1]
            nxt_clean, nxt_dom, nxt_frac, nxt_cr, nxt_des = detect_domain_and_strip(nxt_txt)
            if (nxt_dom, nxt_frac, nxt_cr, nxt_des) == (dom, frac, is_cr, is_des):
                nxt_lookup, _ = translate_mod_line(nxt_clean)
                combo_lookup = f"{lookup_mod}\n{nxt_lookup}"
                cands = pat_map.get(strip_numbers_to_pattern(combo_lookup), ())
                if cands:
                    cvals = extract_values(combo_lookup)
                    both_standalone = bool(
                        pat_map.get(strip_numbers_to_pattern(lookup_mod))
                        and pat_map.get(strip_numbers_to_pattern(nxt_lookup))
                    )
                    if not both_standalone or _hybrid_vals_plausible(cands, cvals, cat_scalar):
                        chosen = _pick_best_entry(
                            cands, ilvl=ilvl, side_hint=s_hint, tier_hint=t_hint, name_hint=n_hint,
                            is_crafted=is_cr, is_desecrated=is_des,
                            vals=cvals, catalyst_scalar=cat_scalar,
                        )
                        if chosen is not None:
                            clean_mod = f"{clean_mod}\n{nxt_clean}"
                            matched_lookup, matched_cands, matched_vals, consumed = combo_lookup, cands, cvals, 2

        if chosen is None:
            cands = pat_map.get(strip_numbers_to_pattern(lookup_mod), ())
            cvals = extract_values(lookup_mod)
            chosen = _pick_best_entry(
                cands, ilvl=ilvl, side_hint=s_hint, tier_hint=t_hint, name_hint=n_hint,
                is_crafted=is_cr, is_desecrated=is_des,
                vals=cvals, catalyst_scalar=cat_scalar,
            )
            if chosen is not None:
                matched_cands, matched_vals = cands, cvals

        if chosen is not None:
            oor_warn = _check_out_of_range_warning(
                matched_lookup, matched_cands, matched_vals, cat_scalar, hybrid_subline_pats
            )
            if oor_warn:
                warnings.append(oor_warn)
            eff_crafted = is_cr or chosen.is_crafted_pool
            eff_desec = is_des or (chosen.source_pool == "desecrated")
            cm = CraftingMod.from_entry(
                chosen, fractured=frac, is_crafted=eff_crafted, is_desecrated=eff_desec
            )
            im = InspectedMod(
                raw_text=clean_mod, mod_id=chosen.mod_id, affix_side=chosen.affix_side,
                source_pool=chosen.source_pool, family_id=chosen.family_id,
                display_tier=chosen.display_tier, required_level=chosen.required_level,
                is_crafted=eff_crafted, fractured=frac, is_desecrated=eff_desec,
                resolved=True, weight_confidence=chosen.weight_confidence,
            )
        else:
            side = s_hint or (AffixSide.PREFIX if len(prefixes) < base.max_prefixes else AffixSide.SUFFIX)
            syn = f"Unresolved_{idx}"
            cm = CraftingMod(
                mod_id=syn, mod_type=syn, family_id=syn, families=(syn,), affix_side=side,
                display_tier=t_hint or 1, required_level=1, stat_text=clean_mod,
                fractured=frac, is_crafted=is_cr, is_desecrated=is_des,
            )
            im = InspectedMod(
                raw_text=clean_mod, mod_id=syn, affix_side=side, source_pool="unknown",
                family_id=syn, display_tier=t_hint or 1, required_level=1,
                is_crafted=is_cr, fractured=frac, is_desecrated=is_des,
                resolved=False, weight_confidence=WeightConfidence.UNMEASURED,
            )
            if not zh_ok:
                warnings.append(f"[unrecognized] 未能翻译中文词缀（保留原文）: {clean_mod}")
            elif strip_numbers_to_pattern(lookup_mod) in _global_mod_patterns(eng):
                warnings.append(
                    f"[not-in-base-pool] 词缀不在 `{base.poe2db_slug}` 的 0.5.5 可制作词缀池中（保留原文）: {clean_mod}"
                )
            else:
                warnings.append(f"[unrecognized] 未能从 0.5.5 词缀池精确匹配（保留原文）: {clean_mod}")
        craft_mods.append(cm)
        (prefixes if im.affix_side == AffixSide.PREFIX else suffixes).append(im)
        idx += consumed

    state = CraftingState(
        base=base, ilvl=ilvl, rarity=rarity, mods=craft_mods,
        quality=quality, catalyst_tag=catalyst_tag, corrupted=corrupted, sanctified=sanctified,
    )
    max_p, max_s = state.max_slots_for_rarity()
    if rarity in (ItemRarity.MAGIC, ItemRarity.RARE) and not prefixes and not suffixes:
        warnings.append(
            f"[no-affix-lines] {rarity.value} item has header lines (`{base.name}`) but zero prefix/suffix affix lines; "
            "paste full Ctrl+C item text including explicit modifiers."
        )
    if state.crafted_mod_count > 1:
        legality_issues.append(
            f"[0.5.0+ 非法遗产/违例] 检测到 {state.crafted_mod_count} 条 Crafted 保底词缀"
            "（精华/合金/涂灌共享上限，0.5.0+ 单件最多 1 条）。"
        )
    if state.desecrated_mod_count > 1:
        legality_issues.append(
            f"[0.5.5 违例] 检测到 {state.desecrated_mod_count} 条 Desecrated 亵渎词缀（单件上限 1 条）。"
        )
    resolved_p = sum(1 for m in prefixes if m.resolved)
    resolved_s = sum(1 for m in suffixes if m.resolved)
    if resolved_p > max_p or resolved_s > max_s:
        legality_issues.append(
            f"词缀数量超槽位上限: 已解析前缀 {resolved_p}/{max_p}，已解析后缀 {resolved_s}/{max_s}。"
        )
    if state.has_fractured_crafted_mod:
        warnings.append(
            "注意: 破裂词缀属于 Crafted 槽位，按 PoE2 0.5.0+ 规则该装备已永久占满唯一 Crafted 名额，不可再点精华/合金。"
        )
    if corrupted or sanctified:
        warnings.append("物品已腐化或神圣化，制作通货已锁定（普通符文/灵核仍可覆盖插槽）。")

    whittle_preview = eng.analyze_whittling(state) if (rarity == ItemRarity.RARE and craft_mods) else None
    return ItemInspectionReport(
        patch=eng.patch, rarity=rarity, item_name=item_name, base_type=base.name,
        base_slug=base.poe2db_slug, ilvl=ilvl, quality=quality, catalyst_tag=catalyst_tag,
        corrupted=corrupted, sanctified=sanctified, max_prefixes=max_p, max_suffixes=max_s,
        prefixes=prefixes, suffixes=suffixes, implicits=implicits,
        crafted_mod_count=state.crafted_mod_count, desecrated_mod_count=state.desecrated_mod_count,
        fractured_crafted_locked=state.has_fractured_crafted_mod,
        is_legal_0_5_5=(len(legality_issues) == 0), legality_issues=legality_issues,
        warnings=warnings, whittling_preview=whittle_preview, state=state,
    )
