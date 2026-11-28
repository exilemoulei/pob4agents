# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Lever alias resolution, roll-bound parsing, and fuzzy target query matching for `ModPoolEngine`."""

from __future__ import annotations

import re
from typing import Any

from .base_catalog import REMOVED_LEVER_MESSAGES, ZH_TARGET_ALIASES
from .models import (
    AffixSide,
    LeverEntry,
    ModEntry,
    RemovedLeverError,
    TargetSpec,
    UnknownTargetError,
)
from .pool_outcomes import rank_target_suggestions
from .weights_loader import CraftDataBundle, norm_key

LEVER_ALIASES: dict[str, str] = {
    "scour": "orb_of_scouring",
    "scouring": "orb_of_scouring",
    "重铸石": "orb_of_scouring",
    "重鑄石": "orb_of_scouring",
    "alt": "orb_of_alteration",
    "alteration": "orb_of_alteration",
    "改造石": "orb_of_alteration",
    "重组器": "recombinator",
    "重組器": "recombinator",
    "重组预兆": "omen_of_recombination",
    "同质化预兆": "omen_of_homogenising_exaltation",
    "同质化崇高预兆": "omen_of_homogenising_exaltation",
    "同质化富豪预兆": "omen_of_homogenising_coronation",
    "transmute": "orb_of_transmutation",
    "transmutation": "orb_of_transmutation",
    "greater_transmute": "greater_orb_of_transmutation",
    "perfect_transmute": "perfect_orb_of_transmutation",
    "augment": "orb_of_augmentation",
    "augmentation": "orb_of_augmentation",
    "greater_augment": "greater_orb_of_augmentation",
    "perfect_augment": "perfect_orb_of_augmentation",
    "regal": "regal_orb",
    "greater_regal": "greater_regal_orb",
    "perfect_regal": "perfect_regal_orb",
    "exalt": "exalted_orb",
    "exalted": "exalted_orb",
    "greater_exalt": "greater_exalted_orb",
    "greater_exalted": "greater_exalted_orb",
    "perfect_exalt": "perfect_exalted_orb",
    "perfect_exalted": "perfect_exalted_orb",
    "chaos": "chaos_orb",
    "greater_chaos": "greater_chaos_orb",
    "perfect_chaos": "perfect_chaos_orb",
    "annul": "orb_of_annulment",
    "annulment": "orb_of_annulment",
    "alchemy": "orb_of_alchemy",
    "fracture": "fracturing_orb",
    "fracturing": "fracturing_orb",
    "whittling": "omen_of_whittling",
    "削切预兆": "omen_of_whittling",
}


def extract_primary_roll_bounds(stat_text: str) -> tuple[float, float] | None:
    """Extract `(min_abs_val, max_abs_val)` from the primary roll line of *stat_text*."""
    first = stat_text.splitlines()[0] if stat_text else ""
    rm = re.search(r"\(([+-]?\d+(?:\.\d+)?)\s*-\s*([+-]?\d+(?:\.\d+)?)\)", first)
    if rm:
        a, b = abs(float(rm.group(1))), abs(float(rm.group(2)))
        return min(a, b), max(a, b)
    nm = re.search(r"[+-]?(\d+(?:\.\d+)?)", first)
    if nm:
        v = abs(float(nm.group(1)))
        return v, v
    return None


def translate_zh_target(query: str) -> str:
    """Expand Chinese crafting target aliases inside *query* to English search tokens."""
    out = query
    for zh, en in sorted(ZH_TARGET_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if zh in out:
            out = out.replace(zh, f" {en} ")
    return " ".join(out.split())


def resolve_lever_entry(
    bundle: CraftDataBundle,
    name_or_id: str,
    *,
    allow_legacy: bool = False,
) -> LeverEntry:
    """Resolve a PoE2 0.5.5 crafting currency/omen/essence/bone lever with legacy-trap guardrails."""
    raw = name_or_id.strip()
    key = LEVER_ALIASES.get(raw) or LEVER_ALIASES.get(norm_key(raw), norm_key(raw))
    if "homogenizing" in key:
        key = key.replace("homogenizing", "homogenising")

    if not allow_legacy:
        if key in REMOVED_LEVER_MESSAGES:
            reason, alt = REMOVED_LEVER_MESSAGES[key]
            raise RemovedLeverError(raw, reason, alt)
        if "recombinat" in key or "重组" in raw or "重組" in raw:
            reason, alt = REMOVED_LEVER_MESSAGES["recombinator"]
            raise RemovedLeverError(raw, reason, alt)
        if "homogenis" in key or "同质化" in raw or "同質化" in raw:
            reason, alt = REMOVED_LEVER_MESSAGES["omen_of_homogenising_exaltation"]
            raise RemovedLeverError(raw, reason, alt)
        if key in bundle.dormant_poe1_orbs:
            raise RemovedLeverError(
                raw,
                f"{raw!r} 属于 PoE1 遗留通货，在 PoE2 0.5.5 中不存在。",
                "改用 PoE2 0.5.5 合法通货（三阶基础通货、精华/合金、深渊骨头、预兆、破裂石）。",
            )

    entry = bundle.levers_by_key.get(key)
    if entry is None:
        for cand_k, cand in bundle.levers_by_key.items():
            if key and key in cand_k:
                entry = cand
                break
    if entry is None:
        raise ValueError(f"Unknown PoE2 0.5.5 crafting lever: {name_or_id!r}")
    if not entry.is_active_drop_in_patch and not allow_legacy:
        reason, alt = REMOVED_LEVER_MESSAGES.get(
            entry.lever_id,
            (f"{entry.name!r} 已在 PoE2 0.4/0.5 移除。", "请改用 0.5.5 合法通货/预兆组合。"),
        )
        raise RemovedLeverError(entry.name, reason, alt)
    return entry


def suggest_target_queries(
    mod_tiers: list[ModEntry],
    query: str,
    *,
    ilvl: int = 82,
    limit: int = 6,
) -> list[str]:
    """Return Top-*limit* closest valid `--want` query strings from *mod_tiers*."""
    cands = [
        m
        for m in mod_tiers
        if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX) and m.required_level <= ilvl
    ]
    if not cands:
        cands = [m for m in mod_tiers if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX)]
    return rank_target_suggestions(cands, translate_zh_target(query or ""), limit=limit)


def _fuzzy_match_ladder(
    candidates: list[ModEntry],
    mod_tiers: list[ModEntry],
    q_low: str,
    requested_val: float | None,
    base_slug: str,
    query: str,
) -> list[ModEntry]:
    raw_tokens = [
        tok.lstrip("+")
        for tok in re.split(r"\s+", re.sub(r"[^a-z0-9+%]+", " ", q_low))
        if tok.lstrip("+")
    ]
    word_tokens = [t for t in raw_tokens if not re.fullmatch(r"\d+%?", t)]
    grouped: dict[tuple[str, str], list[ModEntry]] = {}
    for m in candidates:
        grouped.setdefault((m.source_pool, m.mod_type), []).append(m)
    scored: list[tuple[int, str, str]] = []
    for (spool, mtype), mlist in grouped.items():
        sample = " ".join(f"{x.stat_text} {x.affix_name} {x.mod_type} {x.boss_tag or ''}" for x in mlist).lower()
        tok_ok = bool(raw_tokens) and all(t in sample for t in raw_tokens)
        word_ok = (
            not tok_ok
            and requested_val is not None
            and len(word_tokens) >= 2
            and all(t in sample for t in word_tokens)
            and any(
                (b := extract_primary_roll_bounds(x.stat_text)) is not None and b[0] <= requested_val <= b[1]
                for x in mlist
            )
        )
        if q_low in sample or tok_ok or word_ok:
            pool_bonus = 100 if spool == "normal" else (60 if spool == "desecrated" else 40)
            exact_sub = 50 if any(q_low in x.stat_text.lower() for x in mlist) else 0
            scored.append((pool_bonus + exact_sub - min(len(x.stat_text) for x in mlist), spool, mtype))
    if not scored:
        sugs = tuple(suggest_target_queries(mod_tiers, query, limit=6))
        raise UnknownTargetError(query, base_slug, sugs)
    scored.sort(reverse=True)
    best_pool, best_type = scored[0][1], scored[0][2]
    return [m for m in candidates if m.source_pool == best_pool and m.mod_type == best_type]


def _include_special_pool_equivalents(
    chosen: list[ModEntry],
    candidates: list[ModEntry],
    *,
    ilvl: int,
    requested_val: float | None,
) -> None:
    top = chosen[0]
    bounds = [b[0] for m in chosen if (b := extract_primary_roll_bounds(m.stat_text)) is not None]
    floor_val = requested_val if requested_val is not None else (min(bounds) if bounds else None)
    chosen_ids = {m.mod_id for m in chosen}
    for cand in candidates:
        if cand.mod_id in chosen_ids or cand.required_level > ilvl:
            continue
        if cand.affix_side != top.affix_side or cand.family_id != top.family_id:
            continue
        if cand.source_pool not in ("essence", "perfect_essence", "alloy", "desecrated"):
            continue
        cb = extract_primary_roll_bounds(cand.stat_text)
        if floor_val is not None and cb is not None and cb[1] >= floor_val:
            chosen.append(cand)
            chosen_ids.add(cand.mod_id)


def resolve_target_spec(
    mod_tiers: list[ModEntry],
    base_slug: str,
    target: str | dict[str, Any] | TargetSpec,
    ilvl: int = 82,
) -> TargetSpec:
    """Resolve a string/dict crafting target query against *mod_tiers* for *base_slug*."""
    if isinstance(target, TargetSpec):
        return target
    max_tier: int | None = 1
    explicit_tier = False
    pool_filter: str | None = None
    if isinstance(target, dict):
        raw_query = str(target.get("query") or target.get("mod") or target.get("mod_id") or "").strip()
        query = raw_query
        explicit_tier = "max_display_tier" in target or "tier" in target
        max_tier = target.get("max_display_tier", target.get("tier", 1))
        pool_filter = target.get("source_pool")
        if explicit_tier and max_tier is not None and not re.match(r"^[Tt](?:ier\s*)?\d+", raw_query):
            raw_query = f"T{max_tier} {raw_query}".strip()
    else:
        raw_query = target.strip()
        query = translate_zh_target(raw_query)
        tm = re.match(r"^[Tt](?:ier\s*)?(\d+)\+?\s+(.+)$", query)
        if tm:
            explicit_tier = True
            max_tier = int(tm.group(1))
            query = tm.group(2).strip()

    requested_val: float | None = None
    if not explicit_tier:
        vm = re.search(r"(?<![a-zA-Z])[+-]?(\d+(?:\.\d+)?)", query)
        if vm:
            requested_val = float(vm.group(1))

    candidates = [
        m
        for m in mod_tiers
        if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX)
        and (pool_filter is None or m.source_pool == pool_filter)
    ]
    if not candidates:
        raise UnknownTargetError(
            query,
            base_slug,
            (),
            detail=f"No prefix/suffix modifiers found for base_slug={base_slug!r}.",
        )

    q_low = query.lower()
    exact_id = [
        m
        for m in candidates
        if m.mod_id.lower() == q_low or m.mod_id.rstrip("_").lower() == q_low.rstrip("_")
    ]
    if exact_id:
        hit = exact_id[0]
        same = [
            m
            for m in candidates
            if m.source_pool == hit.source_pool
            and m.mod_type == hit.mod_type
            and m.display_tier <= hit.display_tier
            and m.required_level <= ilvl
        ] or [hit]
        return TargetSpec(
            label=f"{hit.stat_text} (T{hit.display_tier}+)",
            affix_side=hit.affix_side,
            mod_type=hit.mod_type,
            family_id=hit.family_id,
            matching_mod_ids=frozenset(m.mod_id for m in same),
            matching_entries=tuple(same),
            max_display_tier=hit.display_tier,
        )

    by_type = [m for m in candidates if m.mod_type.lower() == q_low or m.family_id.lower() == q_low]
    if by_type:
        normal_only = [m for m in by_type if m.source_pool == "normal"]
        if normal_only:
            first_t = normal_only[0].mod_type
            by_type = [m for m in normal_only if m.mod_type == first_t]
    else:
        by_type = _fuzzy_match_ladder(candidates, mod_tiers, q_low, requested_val, base_slug, raw_query)

    ilvl_ok = [m for m in by_type if m.required_level <= ilvl]
    if not ilvl_ok:
        sugs = tuple(suggest_target_queries(mod_tiers, raw_query, ilvl=ilvl, limit=6))
        min_req = min(m.required_level for m in by_type)
        raise UnknownTargetError(
            raw_query,
            base_slug,
            sugs,
            detail=f"Target {raw_query!r} on `{base_slug}` requires ilvl >= {min_req} (got ilvl={ilvl}).",
        )
    ilvl_ok.sort(key=lambda m: m.display_tier)
    eff_max_t = max(ilvl_ok[0].display_tier, max_tier) if max_tier is not None else ilvl_ok[-1].display_tier
    chosen = [m for m in ilvl_ok if m.display_tier <= eff_max_t]
    if requested_val is not None and not explicit_tier:
        val_hits = [
            m
            for m in ilvl_ok
            if (b := extract_primary_roll_bounds(m.stat_text)) is not None and b[1] >= requested_val
        ]
        if val_hits:
            chosen = val_hits
            eff_max_t = max(m.display_tier for m in chosen)

    top = chosen[0]
    if pool_filter is None:
        _include_special_pool_equivalents(chosen, candidates, ilvl=ilvl, requested_val=requested_val)

    return TargetSpec(
        label=f"{top.stat_text} (<=T{eff_max_t})",
        affix_side=top.affix_side,
        mod_type=top.mod_type,
        family_id=top.family_id,
        matching_mod_ids=frozenset(m.mod_id for m in chosen),
        matching_entries=tuple(chosen),
        max_display_tier=eff_max_t,
    )


__all__ = [
    "LEVER_ALIASES",
    "extract_primary_roll_bounds",
    "resolve_lever_entry",
    "resolve_target_spec",
    "suggest_target_queries",
    "translate_zh_target",
]
