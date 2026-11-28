# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Omen flag parsing, Abyssal bone 3-choice unveil math, and stochastic transitions for `ModPoolEngine`."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from .models import (
    AffixSide,
    CraftingMod,
    CraftingState,
    ItemRarity,
    LeverEntry,
    ModEntry,
    PoolEvalResult,
    WeightConfidence,
)

if TYPE_CHECKING:
    from .pool_engine import ModPoolEngine


def parse_omens(
    omen_entries: tuple[LeverEntry, ...],
) -> tuple[AffixSide | None, str | None, bool, bool, bool, bool, bool]:
    """Extract side/boss constraints and active omen flags from *omen_entries*."""
    side_con: AffixSide | None = None
    boss_con: str | None = None
    has_whittling = has_light = has_echoes = has_cat = extra_mods = False
    for om in omen_entries:
        if om.omen_side_constraint is not None:
            side_con = om.omen_side_constraint
        if om.omen_boss_constraint is not None:
            boss_con = om.omen_boss_constraint
        if om.lever_id == "omen_of_whittling":
            has_whittling = True
        elif om.lever_id == "omen_of_light":
            has_light = True
        elif om.lever_id == "omen_of_abyssal_echoes":
            has_echoes = True
        elif om.lever_id == "omen_of_catalysing_exaltation":
            has_cat = True
        elif om.lever_id == "omen_of_greater_exaltation":
            extra_mods = True
    return side_con, boss_con, has_whittling, has_light, has_echoes, has_cat, extra_mods


def exact_three_choice_unveil_prob(
    eligible: list[tuple[ModEntry, int]],
    target_mod_ids: set[str],
) -> float:
    """Exact probability that >=1 of 3 without-replacement weighted draws hits `target_mod_ids`."""
    total_w = sum(w for _, w in eligible if w > 0)
    if total_w <= 0:
        return 0.0
    hit_w = sum(w for m, w in eligible if w > 0 and m.mod_id in target_mod_ids)
    if hit_w <= 0:
        return 0.0
    misses = [w for m, w in eligible if w > 0 and m.mod_id not in target_mod_ids]
    if len(misses) < 3:
        return 1.0

    miss_w_sum = total_w - hit_w
    p_all_three_miss = 0.0
    n_miss = len(misses)
    for i in range(n_miss):
        wi = misses[i]
        rem1 = total_w - wi
        if rem1 <= 0:
            continue
        p1 = wi / total_w
        inner = 0.0
        for j in range(n_miss):
            if i == j:
                continue
            wj = misses[j]
            rem2 = rem1 - wj
            if rem2 <= 0:
                continue
            rem_miss_w = miss_w_sum - wi - wj
            if rem_miss_w > 0:
                inner += (wj / rem1) * (rem_miss_w / rem2)
        p_all_three_miss += p1 * inner
    return max(0.0, min(1.0, 1.0 - p_all_three_miss))


def eval_bone_action(
    eng: ModPoolEngine,
    state: CraftingState,
    lever: LeverEntry,
    omens: tuple[LeverEntry, ...],
    target_ids: set[str],
    protected: set[str],
    side_con: AffixSide | None,
    boss_con: str | None,
    has_echoes: bool,
    floor: int,
) -> PoolEvalResult:
    """Evaluate exact Abyssal Bone desecrate unveil probability (Mark overwrite or full-affix removal)."""
    if state.rarity != ItemRarity.RARE:
        return PoolEvalResult(
            lever, omens, 0.0, 0.0, 0, 0, (), WeightConfidence.EXACT_DETERMINISTIC, "Requires Rare item"
        )
    if state.has_desecrated:
        return PoolEvalResult(
            lever,
            omens,
            0.0,
            0.0,
            0,
            0,
            (),
            WeightConfidence.EXACT_DETERMINISTIC,
            "Item already has 1 Desecrated mod (use Annul + Omen of Light first)",
        )
    pool_mode = "desecrated" if boss_con else "desecrated_combined"
    conf = WeightConfidence.EXACT_DETERMINISTIC if boss_con else WeightConfidence.EMPIRICAL_ESTIMATE

    def _unveil(sub_st: CraftingState) -> tuple[float, int, int, tuple[ModEntry, ...]]:
        elig = eng.eligible_pool(
            sub_st,
            source_pool=pool_mode,
            target_rarity=ItemRarity.RARE,
            side_constraint=side_con,
            min_mod_level=floor,
            boss_tag=boss_con,
        )
        p3 = exact_three_choice_unveil_prob(elig, target_ids)
        if has_echoes:
            p3 = 1.0 - (1.0 - p3) ** 2
        tw = sum(w for _, w in elig)
        hw = sum(w for m, w in elig if m.mod_id in target_ids)
        return p3, tw, hw, tuple(m for m, _ in elig if m.mod_id in target_ids)

    if state.has_abyss_mark:
        sub = state.clone()
        sub.mods = [m for m in sub.mods if not m.is_abyss_mark]
        p_hit, tw, hw, mt = _unveil(sub)
        return PoolEvalResult(
            lever,
            omens,
            p_hit,
            0.0,
            tw,
            hw,
            mt,
            conf,
            "Replaces Mark of the Abyssal Lord + 3-choice unveil" + (" + Echoes" if has_echoes else ""),
        )
    needs_rem = len(state.mods) >= (state.base.max_prefixes + state.base.max_suffixes)
    if side_con == AffixSide.PREFIX and not state.has_open_prefix(ItemRarity.RARE):
        needs_rem = True
    elif side_con == AffixSide.SUFFIX and not state.has_open_suffix(ItemRarity.RARE):
        needs_rem = True
    if needs_rem:
        rem = eng.removable_indices(state, side_constraint=side_con)
        if not rem:
            return PoolEvalResult(lever, omens, 0.0, 0.0, 0, 0, (), conf, "No removable mods")
        p_acc = collat = 0.0
        tw = hw = 0
        mt: tuple[ModEntry, ...] = ()
        for idx in rem:
            if state.mods[idx].mod_id in protected:
                collat += 1.0 / len(rem)
                continue
            sub = state.clone()
            sub.mods.pop(idx)
            p3, tw, hw, mt = _unveil(sub)
            p_acc += (1.0 / len(rem)) * p3
        return PoolEvalResult(
            lever, omens, p_acc, collat, tw, hw, mt, conf, f"Full-affix overwrite + 3-choice unveil (floor={floor})"
        )
    p_hit, tw, hw, mt = _unveil(state)
    return PoolEvalResult(lever, omens, p_hit, 0.0, tw, hw, mt, conf, f"3-choice unveil (floor={floor})")


def _weighted_pick(pool: list[tuple[ModEntry, int]], rng: random.Random) -> ModEntry | None:
    tot = sum(w for _, w in pool)
    if tot <= 0:
        return None
    r = rng.uniform(0.0, tot)
    acc = 0.0
    for m, w in pool:
        acc += w
        if r <= acc:
            return m
    return pool[-1][0]


def _sample_bone_transition(
    eng: ModPoolEngine,
    nxt: CraftingState,
    *,
    side_con: AffixSide | None,
    boss_con: str | None,
    has_echoes: bool,
    floor: int,
    desired_mod_ids: set[str],
    rng: random.Random,
) -> CraftingState:
    if nxt.has_abyss_mark:
        nxt.mods = [m for m in nxt.mods if not m.is_abyss_mark]
    elif len(nxt.mods) >= (nxt.base.max_prefixes + nxt.base.max_suffixes):
        rem = eng.removable_indices(nxt, side_constraint=side_con)
        if rem:
            nxt.mods.pop(rng.choice(rem))
    pool_mode = "desecrated" if boss_con else "desecrated_combined"
    elig = eng.eligible_pool(
        nxt, source_pool=pool_mode, side_constraint=side_con, min_mod_level=floor, boss_tag=boss_con
    )
    picks: list[ModEntry] = []
    for _attempt in range(2 if has_echoes else 1):
        loc = list(elig)
        picks = []
        for _ in range(min(3, len(loc))):
            c = _weighted_pick(loc, rng)
            if c is None:
                break
            picks.append(c)
            loc = [(m, w) for m, w in loc if m.mod_id != c.mod_id]
        if any(c.mod_id in desired_mod_ids for c in picks):
            break
    sel = next((c for c in picks if c.mod_id in desired_mod_ids), picks[0] if picks else None)
    if sel is not None:
        nxt.mods.append(CraftingMod.from_entry(sel, is_desecrated=True, is_crafted=False))
    return nxt


def sample_transition_impl(
    eng: ModPoolEngine,
    state: CraftingState,
    lever_name: str,
    omens: list[str] | tuple[str, ...] | None,
    desired_mod_ids: set[str],
    rng: random.Random,
) -> CraftingState:
    """Sample one concrete stochastic next `CraftingState` under *lever_name* + *omens*."""
    lever = eng.resolve_lever(lever_name)
    omen_entries = tuple(eng.resolve_lever(om) for om in (omens or ()))
    nxt = state.clone()
    if nxt.corrupted or nxt.sanctified:
        return nxt
    side_con, boss_con, has_whittle, has_light, has_echoes, has_cat, _ = parse_omens(omen_entries)
    floor = lever.min_mod_level_floor or 1

    if "Transmutation" in lever.name and nxt.rarity == ItemRarity.NORMAL:
        nxt.rarity = ItemRarity.MAGIC
        if p := _weighted_pick(eng.eligible_pool(nxt, min_mod_level=floor), rng):
            nxt.mods.append(CraftingMod.from_entry(p))
        return nxt
    if "Augmentation" in lever.name and nxt.rarity == ItemRarity.MAGIC:
        if p := _weighted_pick(eng.eligible_pool(nxt, min_mod_level=floor), rng):
            nxt.mods.append(CraftingMod.from_entry(p))
        return nxt
    if "Regal" in lever.name and nxt.rarity == ItemRarity.MAGIC:
        nxt.rarity = ItemRarity.RARE
        if p := _weighted_pick(eng.eligible_pool(nxt, side_constraint=side_con, min_mod_level=floor), rng):
            nxt.mods.append(CraftingMod.from_entry(p))
        return nxt
    if "Exalted" in lever.name and nxt.rarity == ItemRarity.RARE:
        p = _weighted_pick(
            eng.eligible_pool(nxt, side_constraint=side_con, min_mod_level=floor, use_catalyst=has_cat), rng
        )
        if has_cat:
            nxt.quality = 0
        if p:
            nxt.mods.append(CraftingMod.from_entry(p))
        return nxt
    if lever.name == "Orb of Alchemy" and nxt.rarity == ItemRarity.NORMAL:
        nxt.rarity = ItemRarity.RARE
        for _ in range(4):
            if p := _weighted_pick(eng.eligible_pool(nxt, side_constraint=side_con), rng):
                nxt.mods.append(CraftingMod.from_entry(p))
        return nxt
    if lever.name == "Orb of Annulment":
        rem = eng.removable_indices(nxt, side_constraint=side_con, only_desecrated=has_light)
        if rem:
            nxt.mods.pop(rng.choice(rem))
        return nxt
    if "Chaos" in lever.name and nxt.rarity == ItemRarity.RARE:
        rem = eng.removable_indices(nxt, side_constraint=side_con, whittling=has_whittle)
        if rem:
            nxt.mods.pop(rng.choice(rem))
            if p := _weighted_pick(eng.eligible_pool(nxt, min_mod_level=floor), rng):
                nxt.mods.append(CraftingMod.from_entry(p))
        return nxt
    if lever.category == "essence" and nxt.rarity == ItemRarity.MAGIC and not nxt.has_crafted_mod:
        nxt.rarity = ItemRarity.RARE
        forced = eng.lookup_forced_mod_entry(nxt.base, lever)
        if forced and not any(f in nxt.blocked_families() for f in forced.families):
            nxt.mods.append(CraftingMod.from_entry(forced, is_crafted=True))
        return nxt
    if lever.category in ("perfect_or_corrupted_essence", "alloy", "liquid_emotion") and nxt.rarity == ItemRarity.RARE:
        is_mark = lever.name == "Essence of the Abyss"
        if not is_mark and nxt.has_crafted_mod:
            return nxt
        forced = eng.lookup_forced_mod_entry(nxt.base, lever)
        rem = eng.removable_indices(nxt, side_constraint=side_con)
        if forced and rem:
            nxt.mods.pop(rng.choice(rem))
            if not any(f in nxt.blocked_families() for f in forced.families):
                nxt.mods.append(CraftingMod.from_entry(forced, is_crafted=(not is_mark)))
        return nxt
    if lever.name == "Fracturing Orb" and nxt.rarity == ItemRarity.RARE and len(nxt.mods) >= 4 and not nxt.has_fractured:
        idx = rng.randrange(len(nxt.mods))
        nxt.mods[idx] = nxt.mods[idx].with_fractured()
        return nxt
    if lever.category == "bone" and nxt.rarity == ItemRarity.RARE and not nxt.has_desecrated:
        return _sample_bone_transition(
            eng,
            nxt,
            side_con=side_con,
            boss_con=boss_con,
            has_echoes=has_echoes,
            floor=floor,
            desired_mod_ids=desired_mod_ids,
            rng=rng,
        )
    return nxt


__all__ = [
    "eval_bone_action",
    "exact_three_choice_unveil_prob",
    "parse_omens",
    "sample_transition_impl",
]
