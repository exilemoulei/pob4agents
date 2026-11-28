# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Action probability evaluation helpers for `ModPoolEngine`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import (
    MARK_OF_ABYSS_MOD_ID,
    AffixSide,
    CraftingState,
    ItemRarity,
    LeverEntry,
    ModEntry,
    PoolEvalResult,
    WeightConfidence,
    lowest_confidence,
)
from .pool_transitions import (
    eval_bone_action,
    exact_three_choice_unveil_prob,
    sample_transition_impl,
)
from .pool_transitions import (
    parse_omens as _parse_omens,
)

if TYPE_CHECKING:
    from .pool_engine import ModPoolEngine

CRAFTED_LEVER_CATEGORIES: frozenset[str] = frozenset(
    {"essence", "perfect_or_corrupted_essence", "alloy", "liquid_emotion"}
)


def _pool_conf(elig: list[tuple[ModEntry, int]], boss_con: str | None = None) -> WeightConfidence:
    if boss_con is not None:
        return WeightConfidence.EXACT_DETERMINISTIC
    if not elig:
        return WeightConfidence.UNMEASURED
    return lowest_confidence([m.weight_confidence for m, _ in elig])


def _eval_add_orb(
    eng: ModPoolEngine,
    state: CraftingState,
    lever: LeverEntry,
    omens: tuple[LeverEntry, ...],
    target_ids: set[str],
    side_con: AffixSide | None,
    has_cat: bool,
    extra_mods: bool,
    floor: int,
) -> PoolEvalResult:
    req_rar = (
        ItemRarity.NORMAL
        if "Transmutation" in lever.name
        else (ItemRarity.MAGIC if ("Augmentation" in lever.name or "Regal" in lever.name) else ItemRarity.RARE)
    )
    next_rar = (
        ItemRarity.MAGIC
        if ("Transmutation" in lever.name or "Augmentation" in lever.name)
        else ItemRarity.RARE
    )
    if state.rarity != req_rar:
        return PoolEvalResult(
            lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, f"Requires {req_rar.value} item"
        )
    elig = eng.eligible_pool(
        state, target_rarity=next_rar, side_constraint=side_con, min_mod_level=floor, use_catalyst=has_cat
    )
    tot_w = sum(w for _, w in elig)
    hits = tuple(m for m, _ in elig if m.mod_id in target_ids)
    hit_w = sum(w for m, w in elig if m.mod_id in target_ids)
    p1 = (hit_w / tot_w) if tot_w > 0 else 0.0
    if extra_mods and p1 < 1.0 and tot_w > 0:
        p1 = 1.0 - (1.0 - p1) ** 2
    return PoolEvalResult(
        lever, omens, p1, 0.0, tot_w, hit_w, hits, _pool_conf(elig),
        f"floor={floor}" + (f", side={side_con.value}" if side_con else ""),
    )


def _eval_chaos_orb(
    eng: ModPoolEngine,
    state: CraftingState,
    lever: LeverEntry,
    omens: tuple[LeverEntry, ...],
    target_ids: set[str],
    protected: set[str],
    side_con: AffixSide | None,
    has_whittling: bool,
    floor: int,
) -> PoolEvalResult:
    if state.rarity != ItemRarity.RARE:
        return PoolEvalResult(
            lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "Requires Rare item"
        )
    w_rep = eng.analyze_whittling(state, protected, side_constraint=side_con) if has_whittling else None
    rem = eng.removable_indices(state, side_constraint=side_con, whittling=has_whittling)
    if not rem:
        return PoolEvalResult(
            lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "No removable mods", w_rep
        )
    total_p = collat = 0.0
    last_tw = last_hw = 0
    matched: dict[str, ModEntry] = {}
    confs: list[WeightConfidence] = []
    for idx in rem:
        p_rem = 1.0 / len(rem)
        if state.mods[idx].mod_id in protected:
            collat += p_rem
            continue
        sub = state.clone()
        sub.mods.pop(idx)
        elig = eng.eligible_pool(sub, target_rarity=ItemRarity.RARE, min_mod_level=floor)
        confs.append(_pool_conf(elig))
        tw = sum(w for _, w in elig)
        hw = sum(w for m, w in elig if m.mod_id in target_ids)
        for m, _ in elig:
            if m.mod_id in target_ids:
                matched[m.mod_id] = m
        last_tw, last_hw = tw, hw
        if tw > 0:
            total_p += p_rem * (hw / tw)
    note = w_rep.summary if w_rep else f"floor={floor}"
    return PoolEvalResult(
        lever, omens, total_p, collat, last_tw, last_hw, tuple(matched.values()),
        lowest_confidence(confs) if confs else WeightConfidence.EXACT_DETERMINISTIC, note, w_rep,
    )


def _eval_crafted_lever(
    eng: ModPoolEngine,
    state: CraftingState,
    lever: LeverEntry,
    omens: tuple[LeverEntry, ...],
    target_ids: set[str],
    protected: set[str],
    side_con: AffixSide | None,
) -> PoolEvalResult:
    is_abyss_mark = lever.name == "Essence of the Abyss"
    # PoE2 0.5.0+ P0 HARD LIMIT: max 1 Crafted mod per item; fractured Crafted permanently locks slot
    if not is_abyss_mark and state.has_crafted_mod:
        detail = (
            "（该 Crafted 词缀已被 Fracturing Orb 破裂锁死，永久占用唯一 Crafted 名额）"
            if state.has_fractured_crafted_mod
            else "（须先剥离现有 Crafted 词缀才可再次使用精华/合金/涂灌）"
        )
        return PoolEvalResult(
            lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC,
            f"[0.5.0+ 硬上限拦截] 单件装备最多持有 1 条 Crafted 保底词缀{detail}",
        )
    forced = eng.lookup_forced_mod_entry(state.base, lever)
    if forced is None:
        return PoolEvalResult(
            lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "Not applicable to base"
        )
    if lever.category == "essence":
        if state.rarity != ItemRarity.MAGIC:
            return PoolEvalResult(
                lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "Requires Magic item"
            )
        if any(f in state.blocked_families() for f in forced.families):
            return PoolEvalResult(
                lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "Mod family already present"
            )
        hit = 1.0 if forced.mod_id in target_ids else 0.0
        return PoolEvalResult(
            lever, omens, hit, 0.0, 1, int(hit), (forced,) if hit else (),
            WeightConfidence.EXACT_DETERMINISTIC, f"Deterministic Magic->Rare Crafted: {forced.stat_text}",
        )
    if state.rarity != ItemRarity.RARE:
        return PoolEvalResult(
            lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "Requires Rare item"
        )
    rem = eng.removable_indices(state, side_constraint=side_con)
    if not rem:
        return PoolEvalResult(
            lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "No removable mods"
        )
    valid_hits = collat = 0
    for idx in rem:
        if state.mods[idx].mod_id in protected:
            collat += 1
            continue
        sub = state.clone()
        sub.mods.pop(idx)
        if forced.affix_side == AffixSide.PREFIX and not sub.has_open_prefix(ItemRarity.RARE):
            continue
        if forced.affix_side == AffixSide.SUFFIX and not sub.has_open_suffix(ItemRarity.RARE):
            continue
        if any(f in sub.blocked_families() for f in forced.families):
            continue
        if forced.mod_id in target_ids or forced.mod_id == MARK_OF_ABYSS_MOD_ID:
            valid_hits += 1
    return PoolEvalResult(
        lever, omens, valid_hits / len(rem), collat / len(rem), len(rem), valid_hits,
        (forced,) if valid_hits > 0 else (), WeightConfidence.EXACT_DETERMINISTIC,
        f"Removes 1 ({side_con.value if side_con else 'any'}) + forces {forced.stat_text}",
    )


def evaluate_action_impl(
    eng: ModPoolEngine,
    state: CraftingState,
    target_ids: set[str],
    lever_name: str,
    omens: list[str] | None = None,
    protected_mod_ids: set[str] | None = None,
) -> PoolEvalResult:
    lever = eng.resolve_lever(lever_name)
    omen_entries = tuple(eng.resolve_lever(om) for om in (omens or []))
    protected = protected_mod_ids or set()
    if state.corrupted or state.sanctified:
        return PoolEvalResult(
            lever, omen_entries, 0.0, 0.0, 0, 0, (),
            WeightConfidence.EXACT_DETERMINISTIC, "物品已腐化或神圣化，无法继续制作",
        )
    if lever.max_item_level is not None and state.ilvl > lever.max_item_level:
        raise ValueError(f"{lever.name} has max item level {lever.max_item_level}, got ilvl={state.ilvl}")
    side_con, boss_con, has_whittle, has_light, has_echoes, has_cat, extra = _parse_omens(omen_entries)
    floor = lever.min_mod_level_floor or 1

    if any(k in lever.name for k in ("Transmutation", "Augmentation", "Regal", "Exalted")):
        return _eval_add_orb(eng, state, lever, omen_entries, target_ids, side_con, has_cat, extra, floor)
    if lever.name == "Orb of Annulment":
        if state.rarity not in (ItemRarity.MAGIC, ItemRarity.RARE):
            return PoolEvalResult(
                lever, omen_entries, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "Requires Magic/Rare"
            )
        rem = eng.removable_indices(state, side_constraint=side_con, only_desecrated=has_light)
        if not rem:
            return PoolEvalResult(
                lever, omen_entries, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "No removable mods"
            )
        good = sum(1 for i in rem if state.mods[i].mod_id not in protected)
        collat = sum(1 for i in rem if state.mods[i].mod_id in protected) / len(rem)
        return PoolEvalResult(
            lever, omen_entries, good / len(rem), collat, len(rem), good, (),
            WeightConfidence.EXACT_DETERMINISTIC,
            "Removes 1 non-protected mod" + (" (Desecrated only)" if has_light else ""),
        )
    if "Chaos Orb" in lever.name:
        return _eval_chaos_orb(eng, state, lever, omen_entries, target_ids, protected, side_con, has_whittle, floor)
    if lever.category in CRAFTED_LEVER_CATEGORIES:
        return _eval_crafted_lever(eng, state, lever, omen_entries, target_ids, protected, side_con)
    if lever.name == "Fracturing Orb":
        if state.rarity != ItemRarity.RARE or len(state.mods) < 4 or state.has_fractured:
            return PoolEvalResult(
                lever, omen_entries, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC,
                "Requires unfractured Rare item with >= 4 mods",
            )
        k = len(state.mods)
        hits_n = sum(1 for m in state.mods if m.mod_id in target_ids)
        return PoolEvalResult(
            lever, omen_entries, hits_n / k, 0.0, k, hits_n, (), WeightConfidence.EXACT_DETERMINISTIC, f"1/{k} fracture lock"
        )
    if lever.category == "bone":
        return eval_bone_action(
            eng, state, lever, omen_entries, target_ids, protected, side_con, boss_con, has_echoes, floor
        )
    if lever.name == "Orb of Alchemy":
        if state.rarity != ItemRarity.NORMAL:
            return PoolEvalResult(
                lever, omen_entries, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "Requires Normal item"
            )
        elig = eng.eligible_pool(state, target_rarity=ItemRarity.RARE, side_constraint=side_con)
        tw = sum(w for _, w in elig)
        hw = sum(w for m, w in elig if m.mod_id in target_ids)
        ps = (hw / tw) if tw > 0 else 0.0
        return PoolEvalResult(
            lever, omen_entries, 1.0 - (1.0 - ps) ** (3 if side_con else 4), 0.0, tw, hw,
            tuple(m for m, _ in elig if m.mod_id in target_ids), _pool_conf(elig), "4-mod Rare opener",
        )
    return PoolEvalResult(lever, omen_entries, 0.0, 0.0, 0, 0, (), WeightConfidence.UNMEASURED, "Unsupported action")


__all__ = [
    "CRAFTED_LEVER_CATEGORIES",
    "_parse_omens",
    "evaluate_action_impl",
    "exact_three_choice_unveil_prob",
    "sample_transition_impl",
]
