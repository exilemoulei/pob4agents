# SPDX-License-Identifier: GPL-3.0-only
"""Compressed-state MDP representation and Bellman transition synthesis for PoE2 0.5.5 crafting."""

from __future__ import annotations

from dataclasses import replace

from .mdp_cache import (
    MdpAction,
    MdpState,
    PoolStatsCache,
    bone_family_for_slug,
    merge_dist,
    state_to_key,
)
from .models import (
    AffixSide,
    BaseItemEntry,
    ItemRarity,
    TargetSpec,
    WeightConfidence,
)
from .pool_engine import ModPoolEngine
from .price_book import PriceBook


def _lever_cost(
    engine: ModPoolEngine, pb: PriceBook, lever_name: str, omens: tuple[str, ...] = ()
) -> float | None:
    lv = engine.resolve_lever(lever_name)
    if pb.is_excluded(lv):
        return None
    om_entries = tuple(engine.resolve_lever(o) for o in omens)
    if any(pb.is_excluded(o) for o in om_entries):
        return None
    try:
        return pb.action_cost(lv, om_entries)
    except ValueError:
        return None


def _add_dist(
    st: MdpState,
    targets: tuple[TargetSpec, ...],
    hp: dict[int, float],
    mp: float,
    ms: float,
    *,
    next_rarity: ItemRarity | None = None,
    set_desecrated: bool = False,
    clear_mark: bool = False,
) -> list[tuple[MdpState, float]]:
    rar = next_rarity or st.rarity
    des = True if set_desecrated else st.has_desecrated
    mrk = False if clear_mark else st.has_abyss_mark
    dist: list[tuple[MdpState, float]] = []
    for idx, p in hp.items():
        is_p = targets[idx].affix_side == AffixSide.PREFIX
        dist.append((replace(st, rarity=rar, hit_mask=st.hit_mask | (1 << idx), n_pref=st.n_pref + int(is_p), n_suff=st.n_suff + int(not is_p), has_desecrated=des, has_abyss_mark=mrk), p))
    if mp > 0:
        dist.append((replace(st, rarity=rar, n_pref=st.n_pref + 1, has_desecrated=des, has_abyss_mark=mrk), mp))
    if ms > 0:
        dist.append((replace(st, rarity=rar, n_suff=st.n_suff + 1, has_desecrated=des, has_abyss_mark=mrk), ms))
    return dist


def _rare_surgery_actions(
    st: MdpState,
    start_key: MdpState,
    base: BaseItemEntry,
    ilvl: int,
    targets: tuple[TargetSpec, ...],
    cache: PoolStatsCache,
    pb: PriceBook,
) -> list[MdpAction]:
    eng = cache.engine
    actions: list[MdpAction] = []
    max_p, max_s = base.max_prefixes, base.max_suffixes
    hit_p = sum(1 for i, t in enumerate(targets) if (st.hit_mask & (1 << i)) and t.affix_side == AffixSide.PREFIX)
    hit_s = sum(1 for i, t in enumerate(targets) if (st.hit_mask & (1 << i)) and t.affix_side == AffixSide.SUFFIX)
    junk_p, junk_s = max(0, st.n_pref - hit_p), max(0, st.n_suff - hit_s)
    total_mods = st.n_pref + st.n_suff
    open_p, open_s = st.n_pref < max_p, st.n_suff < max_s

    # Annul (+ Sinistral/Dextral)
    if total_mods > 0:
        for omens, side_only in (
            ((), None),
            (("Omen of Sinistral Annulment",), AffixSide.PREFIX),
            (("Omen of Dextral Annulment",), AffixSide.SUFFIX),
        ):
            rem_p = st.n_pref if side_only != AffixSide.SUFFIX else 0
            rem_s = st.n_suff if side_only != AffixSide.PREFIX else 0
            rem_tot = rem_p + rem_s - (1 if st.has_fractured_target and side_only is None else 0)
            if rem_tot <= 0 or (c := _lever_cost(eng, pb, "Orb of Annulment", omens)) is None:
                continue
            dist: list[tuple[MdpState, float]] = []
            if rem_p > 0 and junk_p > 0:
                dist.append((replace(st, n_pref=st.n_pref - 1), junk_p / rem_tot))
            if rem_s > 0 and junk_s > 0:
                dist.append((replace(st, n_suff=st.n_suff - 1), junk_s / rem_tot))
            for idx, t in enumerate(targets):
                if not (st.hit_mask & (1 << idx)) or (side_only and t.affix_side != side_only):
                    continue
                if st.has_fractured_target and idx == 0:
                    continue
                dp = 1 if t.affix_side == AffixSide.PREFIX else 0
                dist.append((replace(st, hit_mask=st.hit_mask & ~(1 << idx), n_pref=st.n_pref - dp, n_suff=st.n_suff - (1 - dp)), 1.0 / rem_tot))
            if dist:
                actions.append(MdpAction("lever", "Orb of Annulment", omens, c, merge_dist(dist), WeightConfidence.EXACT_DETERMINISTIC))

    # Chaos + Whittling
    if junk_p + junk_s > 0:
        p_jp = junk_p / (junk_p + junk_s)
        for lname, floor in (("Chaos Orb", 1), ("Greater Chaos Orb", 35), ("Perfect Chaos Orb", 50)):
            if (c := _lever_cost(eng, pb, lname, ("Omen of Whittling",))) is None:
                continue
            dist = []
            conf = WeightConfidence.VERIFIED_COMMUNITY
            for rem_p_flag, ws in ((True, p_jp), (False, 1.0 - p_jp)):
                if ws <= 0:
                    continue
                sub = replace(st, n_pref=st.n_pref - int(rem_p_flag), n_suff=st.n_suff - int(not rem_p_flag))
                hp, mp, ms, conf = cache.normal_add_probs(sub.n_pref < max_p, sub.n_suff < max_s, floor, st.hit_mask)
                for ns, p in _add_dist(sub, targets, hp, mp, ms):
                    dist.append((ns, ws * p))
            if dist:
                actions.append(MdpAction("lever", lname, ("Omen of Whittling",), c, merge_dist(dist), conf))

    # Perfect Essence / Alloy (STRICTLY gated by `not st.has_crafted` per 0.5.0+)
    if not st.has_crafted and total_mods > 0:
        for idx, plist in cache.matching_perf_essences.items():
            if st.hit_mask & (1 << idx):
                continue
            is_p = targets[idx].affix_side == AffixSide.PREFIX
            for lv in plist[:2]:
                for omens, side_only in (
                    ((), None),
                    (("Omen of Sinistral Crystallisation",), AffixSide.PREFIX),
                    (("Omen of Dextral Crystallisation",), AffixSide.SUFFIX),
                ):
                    if (c := _lever_cost(eng, pb, lv.name, omens)) is None:
                        continue
                    rem_p = st.n_pref if side_only != AffixSide.SUFFIX else 0
                    rem_s = st.n_suff if side_only != AffixSide.PREFIX else 0
                    rem_tot = rem_p + rem_s
                    if rem_tot <= 0:
                        continue
                    dist = []
                    if rem_p > 0 and junk_p > 0 and ((is_p and st.n_pref - 1 < max_p) or (not is_p and st.n_suff < max_s)):
                        dist.append((replace(st, hit_mask=st.hit_mask | (1 << idx), n_pref=st.n_pref - 1 + int(is_p), n_suff=st.n_suff + int(not is_p), has_crafted=True), junk_p / rem_tot))
                    if rem_s > 0 and junk_s > 0 and ((not is_p and st.n_suff - 1 < max_s) or (is_p and st.n_pref < max_p)):
                        dist.append((replace(st, hit_mask=st.hit_mask | (1 << idx), n_pref=st.n_pref + int(is_p), n_suff=st.n_suff - 1 + int(not is_p), has_crafted=True), junk_s / rem_tot))
                    for h_i, ht in enumerate(targets):
                        if not (st.hit_mask & (1 << h_i)) or (side_only and ht.affix_side != side_only):
                            continue
                        dp = 1 if ht.affix_side == AffixSide.PREFIX else 0
                        np2, ns2 = st.n_pref - dp + int(is_p), st.n_suff - (1 - dp) + int(not is_p)
                        if np2 <= max_p and ns2 <= max_s:
                            dist.append((replace(st, hit_mask=(st.hit_mask & ~(1 << h_i)) | (1 << idx), n_pref=np2, n_suff=ns2, has_crafted=True), 1.0 / rem_tot))
                    if dist:
                        actions.append(MdpAction("lever", lv.name, omens, c, merge_dist(dist), WeightConfidence.EXACT_DETERMINISTIC))

    # Abyssal Desecration Bone (independent cap 1)
    bfam = bone_family_for_slug(base.poe2db_slug)
    if bfam and not st.has_desecrated and (open_p or open_s):
        bname = f"Ancient {bfam}" if ilvl >= 40 else f"Preserved {bfam}"
        floor = 40 if "Ancient" in bname else 1
        for omens, op, os_, ech in (
            ((), open_p, open_s, False),
            (("Omen of Sinistral Necromancy",), open_p, False, False),
            (("Omen of Dextral Necromancy",), False, open_s, False),
            (("Omen of Abyssal Echoes",), open_p, open_s, True),
        ):
            if not (op or os_) or (c := _lever_cost(eng, pb, bname, omens)) is None:
                continue
            hp, mp, ms, conf = cache.desecrate_probs(op, os_, floor, st.hit_mask, None, ech)
            dist = _add_dist(st, targets, hp, mp if open_p else 0.0, ms if open_s else 0.0, set_desecrated=True, clear_mark=True)
            if dist:
                actions.append(MdpAction("lever", bname, omens, c, merge_dist(dist), conf))

    if total_mods == 4 and not st.has_fractured_target and st.hit_mask != 0:
        if (c := _lever_cost(eng, pb, "Fracturing Orb")) is not None:
            p_lock = (hit_p + hit_s) / 4.0
            actions.append(
                MdpAction("lever", "Fracturing Orb", (), c, merge_dist([(replace(st, has_fractured_target=True), p_lock), (start_key, 1.0 - p_lock)]), WeightConfidence.EXACT_DETERMINISTIC)
            )
    return actions


def actions_for_state(
    st: MdpState,
    start_key: MdpState,
    base: BaseItemEntry,
    ilvl: int,
    targets: tuple[TargetSpec, ...],
    cache: PoolStatsCache,
    pb: PriceBook,
    base_cost_ex: float,
) -> list[MdpAction]:
    """Generate legal PoE2 0.5.5 MDP actions & transition distributions for `st`."""
    eng = cache.engine
    actions: list[MdpAction] = []
    if st.hit_mask == (1 << len(targets)) - 1:
        return actions
    if st != start_key:
        actions.append(
            MdpAction("restart", "Restart (Fresh Base)", (), max(0.1, base_cost_ex), ((start_key, 1.0),), WeightConfidence.EXACT_DETERMINISTIC, "止损重开")
        )
    if st.rarity == ItemRarity.NORMAL:
        for lname, floor in (("Orb of Transmutation", 1), ("Greater Orb of Transmutation", 55), ("Perfect Orb of Transmutation", 70)):
            if (c := _lever_cost(eng, pb, lname)) is None:
                continue
            hp, mp, ms, conf = cache.normal_add_probs(True, True, floor, 0)
            if (dist := _add_dist(st, targets, hp, mp, ms, next_rarity=ItemRarity.MAGIC)):
                actions.append(MdpAction("lever", lname, (), c, merge_dist(dist), conf))
        return actions
    if st.rarity == ItemRarity.MAGIC:
        open_p, open_s = st.n_pref < 1, st.n_suff < 1
        if open_p or open_s:
            for lname, floor in (("Orb of Augmentation", 1), ("Greater Orb of Augmentation", 55), ("Perfect Orb of Augmentation", 70)):
                if (c := _lever_cost(eng, pb, lname)) is None:
                    continue
                hp, mp, ms, conf = cache.normal_add_probs(open_p, open_s, floor, st.hit_mask)
                if (dist := _add_dist(st, targets, hp, mp, ms)):
                    actions.append(MdpAction("lever", lname, (), c, merge_dist(dist), conf))
        for lname, floor in (("Regal Orb", 1), ("Greater Regal Orb", 35), ("Perfect Regal Orb", 50)):
            for omens, op, os_ in (((), True, True), (("Omen of Sinistral Coronation",), True, False), (("Omen of Dextral Coronation",), False, True)):
                if (c := _lever_cost(eng, pb, lname, omens)) is None:
                    continue
                hp, mp, ms, conf = cache.normal_add_probs(op, os_, floor, st.hit_mask)
                if (dist := _add_dist(st, targets, hp, mp, ms, next_rarity=ItemRarity.RARE)):
                    actions.append(MdpAction("lever", lname, omens, c, merge_dist(dist), conf))
        if not st.has_crafted:
            for idx, ess_list in cache.matching_essences.items():
                if st.hit_mask & (1 << idx):
                    continue
                for ess in ess_list[:2]:
                    if (c := _lever_cost(eng, pb, ess.name)) is None:
                        continue
                    is_p = targets[idx].affix_side == AffixSide.PREFIX
                    ns = replace(st, rarity=ItemRarity.RARE, hit_mask=st.hit_mask | (1 << idx), n_pref=st.n_pref + int(is_p), n_suff=st.n_suff + int(not is_p), has_crafted=True)
                    actions.append(MdpAction("lever", ess.name, (), c, ((ns, 1.0),), WeightConfidence.EXACT_DETERMINISTIC))
        return actions

    open_p, open_s = st.n_pref < base.max_prefixes, st.n_suff < base.max_suffixes
    if open_p or open_s:
        for lname, floor in (("Exalted Orb", 1), ("Greater Exalted Orb", 35), ("Perfect Exalted Orb", 50)):
            for omens, op, os_ in (((), open_p, open_s), (("Omen of Sinistral Exaltation",), open_p, False), (("Omen of Dextral Exaltation",), False, open_s)):
                if not (op or os_) or (c := _lever_cost(eng, pb, lname, omens)) is None:
                    continue
                hp, mp, ms, conf = cache.normal_add_probs(op, os_, floor, st.hit_mask)
                if (dist := _add_dist(st, targets, hp, mp if open_p else 0.0, ms if open_s else 0.0)):
                    actions.append(MdpAction("lever", lname, omens, c, merge_dist(dist), conf))
    actions.extend(_rare_surgery_actions(st, start_key, base, ilvl, targets, cache, pb))
    return actions


__all__ = [
    "MdpAction",
    "MdpState",
    "PoolStatsCache",
    "actions_for_state",
    "bone_family_for_slug",
    "merge_dist",
    "state_to_key",
]
