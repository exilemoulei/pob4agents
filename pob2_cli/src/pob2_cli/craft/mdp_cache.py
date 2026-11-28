# SPDX-License-Identifier: GPL-3.0-only
"""Compressed MDP state/action types and precomputed mod-pool probability cache (L2)."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    AffixSide,
    BaseItemEntry,
    CraftingState,
    ItemRarity,
    LeverEntry,
    TargetSpec,
    WeightConfidence,
    lowest_confidence,
)
from .pool_actions import exact_three_choice_unveil_prob
from .pool_engine import ModPoolEngine
from .pool_outcomes import filter_pool_with_family_exemption
from .price_book import PriceBook


@dataclass(frozen=True, slots=True)
class MdpState:
    rarity: ItemRarity
    hit_mask: int
    n_pref: int
    n_suff: int
    has_crafted: bool
    has_desecrated: bool
    has_fractured_target: bool
    has_abyss_mark: bool


@dataclass(frozen=True, slots=True)
class MdpAction:
    kind: str  # "lever" | "restart"
    lever_name: str
    omens: tuple[str, ...]
    cost_ex: float
    transitions: tuple[tuple[MdpState, float], ...]
    confidence: WeightConfidence
    note: str = ""


def bone_family_for_slug(slug: str) -> str | None:
    if slug in ("Amulets", "Rings", "Belts"):
        return "Collarbone"
    if any(
        slug.startswith(p)
        for p in ("Body_Armours", "Helmets", "Gloves", "Boots", "Shields", "Bucklers", "Foci")
    ):
        return "Rib"
    if slug in (
        "Claws", "Daggers", "Wands", "One_Hand_Swords", "One_Hand_Axes", "One_Hand_Maces",
        "Sceptres", "Spears", "Flails", "Bows", "Staves", "Two_Hand_Swords", "Two_Hand_Axes",
        "Two_Hand_Maces", "Quarterstaves", "Crossbows", "Talismans", "Quivers",
    ):
        return "Jawbone"
    return None


def state_to_key(state: CraftingState, targets: tuple[TargetSpec, ...]) -> MdpState:
    mask = 0
    has_frac_target = False
    for idx, t in enumerate(targets):
        hit_mod = next((m for m in state.mods if m.mod_id in t.matching_mod_ids), None)
        if hit_mod is not None:
            mask |= 1 << idx
            if hit_mod.fractured:
                has_frac_target = True
    return MdpState(
        rarity=state.rarity,
        hit_mask=mask,
        n_pref=min(state.prefix_count, state.base.max_prefixes),
        n_suff=min(state.suffix_count, state.base.max_suffixes),
        has_crafted=state.has_crafted_mod,
        has_desecrated=state.has_desecrated,
        has_fractured_target=has_frac_target,
        has_abyss_mark=state.has_abyss_mark,
    )


def merge_dist(items: list[tuple[MdpState, float]]) -> tuple[tuple[MdpState, float], ...]:
    acc: dict[MdpState, float] = {}
    for st, p in items:
        if p > 1e-12:
            acc[st] = acc.get(st, 0.0) + p
    tot = sum(acc.values())
    return tuple((st, p / tot) for st, p in acc.items()) if tot > 0 else ()


class PoolStatsCache:
    """Precomputed target hit probabilities per `(open_p, open_s, floor, hit_mask)`."""

    def __init__(
        self,
        engine: ModPoolEngine,
        base: BaseItemEntry,
        ilvl: int,
        targets: tuple[TargetSpec, ...],
        price_book: PriceBook,
    ) -> None:
        self.engine = engine
        self.base = base
        self.ilvl = ilvl
        self.targets = targets
        self.price_book = price_book
        self._normal_cache: dict[
            tuple[bool, bool, int, int], tuple[dict[int, float], float, float, WeightConfidence]
        ] = {}
        self.matching_essences: dict[int, list[LeverEntry]] = {}
        self.matching_perf_essences: dict[int, list[LeverEntry]] = {}
        slug = base.poe2db_slug
        for lv in engine.list_levers(active_only=True):
            if price_book.is_excluded(lv):
                continue
            mid = lv.forced_mods.get(slug)
            if not mid:
                continue
            for idx, t in enumerate(targets):
                if mid in t.matching_mod_ids:
                    if lv.category == "essence":
                        self.matching_essences.setdefault(idx, []).append(lv)
                    elif lv.category in ("perfect_or_corrupted_essence", "alloy", "liquid_emotion"):
                        self.matching_perf_essences.setdefault(idx, []).append(lv)

    def normal_add_probs(
        self, open_p: bool, open_s: bool, floor: int, hit_mask: int
    ) -> tuple[dict[int, float], float, float, WeightConfidence]:
        key = (open_p, open_s, floor, hit_mask)
        if key in self._normal_cache:
            return self._normal_cache[key]
        blocked = {self.targets[i].family_id for i in range(len(self.targets)) if hit_mask & (1 << i)}
        eff_tags = set(self.base.tags) | {"default"}
        pre_mods = [
            m
            for m in self.engine.get_mod_tiers(self.base.poe2db_slug, "normal")
            if ((m.affix_side == AffixSide.PREFIX and open_p) or (m.affix_side == AffixSide.SUFFIX and open_s))
            and not any(f in blocked for f in m.families)
            and m.weight > 0
            and self.engine._is_tag_eligible(m, eff_tags)
        ]
        mods = filter_pool_with_family_exemption(pre_mods, ilvl=self.ilvl, min_mod_level=floor)
        tot_w = sum(m.weight for m in mods)
        if tot_w <= 0:
            res: tuple[dict[int, float], float, float, WeightConfidence] = ({}, 0.0, 0.0, WeightConfidence.UNMEASURED)
            self._normal_cache[key] = res
            return res
        conf = lowest_confidence([m.weight_confidence for m in mods])
        hit_probs: dict[int, float] = {}
        hit_w_p = hit_w_s = 0
        pref_w = sum(m.weight for m in mods if m.affix_side == AffixSide.PREFIX)
        suff_w = tot_w - pref_w
        for i, t in enumerate(self.targets):
            if hit_mask & (1 << i):
                continue
            w_i = sum(m.weight for m in mods if m.mod_id in t.matching_mod_ids)
            if w_i > 0:
                hit_probs[i] = w_i / tot_w
                if t.affix_side == AffixSide.PREFIX:
                    hit_w_p += w_i
                else:
                    hit_w_s += w_i
        res = (hit_probs, max(0.0, (pref_w - hit_w_p) / tot_w), max(0.0, (suff_w - hit_w_s) / tot_w), conf)
        self._normal_cache[key] = res
        return res

    def desecrate_probs(
        self, open_p: bool, open_s: bool, floor: int, hit_mask: int, boss_tag: str | None, echoes: bool
    ) -> tuple[dict[int, float], float, float, WeightConfidence]:
        blocked = {self.targets[i].family_id for i in range(len(self.targets)) if hit_mask & (1 << i)}
        pools = ["desecrated"] if boss_tag else ["normal", "desecrated"]
        elig = []
        for pname in pools:
            pre_des = [
                m
                for m in self.engine.get_mod_tiers(self.base.poe2db_slug, pname)
                if (boss_tag is None or m.boss_tag == boss_tag)
                and ((m.affix_side == AffixSide.PREFIX and open_p) or (m.affix_side == AffixSide.SUFFIX and open_s))
                and not any(f in blocked for f in m.families)
                and (1000 if (boss_tag and pname == "desecrated") else m.weight) > 0
            ]
            for m in filter_pool_with_family_exemption(pre_des, ilvl=self.ilvl, min_mod_level=floor):
                w = 1000 if (boss_tag and pname == "desecrated") else m.weight
                elig.append((m, w))
        conf = WeightConfidence.EXACT_DETERMINISTIC if boss_tag else WeightConfidence.EMPIRICAL_ESTIMATE
        if not elig:
            return {}, 0.0, 0.0, conf
        hit_probs: dict[int, float] = {}
        rem_prob = 1.0
        for i, t in enumerate(self.targets):
            if hit_mask & (1 << i):
                continue
            p3 = exact_three_choice_unveil_prob(elig, set(t.matching_mod_ids))
            if echoes:
                p3 = 1.0 - (1.0 - p3) ** 2
            if p3 > 0:
                eff = min(rem_prob, p3)
                hit_probs[i] = eff
                rem_prob = max(0.0, rem_prob - eff)
        tot_w = sum(w for _, w in elig)
        frac_p = (sum(w for m, w in elig if m.affix_side == AffixSide.PREFIX) / tot_w) if tot_w > 0 else 0.5
        return hit_probs, rem_prob * frac_p, rem_prob * (1.0 - frac_p), conf


__all__ = [
    "MdpAction",
    "MdpState",
    "PoolStatsCache",
    "bone_family_for_slug",
    "merge_dist",
    "state_to_key",
]
