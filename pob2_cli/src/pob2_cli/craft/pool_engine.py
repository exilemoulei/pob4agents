# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Exact PoE2 0.5.5 crafting modifier pool engine with 1-Crafted-mod & Whittling guards."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .models import (
    MARK_OF_ABYSS_MOD_ID,
    AffixSide,
    BaseItemEntry,
    CraftingState,
    ItemRarity,
    LeverEntry,
    ModEntry,
    PoolEvalResult,
    TargetSpec,
    WeightConfidence,
    WhittlingReport,
    WhittlingTarget,
)
from .pool_actions import (
    evaluate_action_impl,
    exact_three_choice_unveil_prob,
    sample_transition_impl,
)
from .pool_outcomes import filter_pool_with_family_exemption
from .target_resolver import (
    extract_primary_roll_bounds,
    resolve_lever_entry,
    resolve_target_spec,
    suggest_target_queries,
)
from .weights_loader import CraftDataBundle, load_craft_data, resolve_base_entry

__all__ = [
    "ModPoolEngine",
    "exact_three_choice_unveil_prob",
    "extract_primary_roll_bounds",
]


class ModPoolEngine:
    """Zero-SQLite exact PoE2 0.5.5 crafting pool engine with 1-Crafted-mod & Whittling guards."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.bundle: CraftDataBundle = load_craft_data(data_dir)

    @property
    def patch(self) -> str:
        return self.bundle.patch

    def list_levers(
        self, category: str | None = None, *, active_only: bool = True
    ) -> list[LeverEntry]:
        out = list(self.bundle.levers)
        if category is not None:
            out = [lv for lv in out if lv.category == category]
        if active_only:
            out = [lv for lv in out if lv.is_active_drop_in_patch]
        return out

    def resolve_lever(self, name_or_id: str, *, allow_legacy: bool = False) -> LeverEntry:
        return resolve_lever_entry(self.bundle, name_or_id, allow_legacy=allow_legacy)

    def resolve_base(self, base_name_or_slug: str) -> BaseItemEntry:
        return resolve_base_entry(self.bundle, base_name_or_slug)

    def get_mod_tiers(self, base_slug: str, source_pool: str | None = None) -> list[ModEntry]:
        mods = self.bundle.mods_by_slug.get(base_slug, ())
        if source_pool is None:
            return list(mods)
        return [m for m in mods if m.source_pool == source_pool]

    def resolve_target(
        self,
        base_slug: str,
        target: str | dict[str, Any] | TargetSpec,
        ilvl: int = 82,
    ) -> TargetSpec:
        return resolve_target_spec(self.get_mod_tiers(base_slug), base_slug, target, ilvl=ilvl)

    def suggest_targets(
        self,
        base_slug: str,
        query: str,
        *,
        ilvl: int = 82,
        limit: int = 6,
    ) -> list[str]:
        """Return Top-*limit* closest valid `--want` query strings from *base_slug*'s modifier pool."""
        return suggest_target_queries(self.get_mod_tiers(base_slug), query, ilvl=ilvl, limit=limit)

    def _is_tag_eligible(self, mod: ModEntry, effective_tags: set[str]) -> bool:
        if "caster" in mod.mod_tags or "Spell" in mod.stat_text:
            for el in ("fire", "cold", "lightning", "chaos", "physical"):
                if el in mod.mod_tags and f"no_{el}_spell_mods" in effective_tags:
                    return False
        return True

    def eligible_pool(
        self,
        state: CraftingState,
        *,
        source_pool: str = "normal",
        target_rarity: ItemRarity | None = None,
        side_constraint: AffixSide | None = None,
        min_mod_level: int = 1,
        boss_tag: str | None = None,
        use_catalyst: bool = False,
    ) -> list[tuple[ModEntry, int]]:
        eff_rar = target_rarity or state.rarity
        open_p = state.has_open_prefix(eff_rar)
        open_s = state.has_open_suffix(eff_rar)
        if side_constraint == AffixSide.PREFIX:
            open_s = False
        elif side_constraint == AffixSide.SUFFIX:
            open_p = False
        if not open_p and not open_s:
            return []

        blocked = state.blocked_families()
        eff_tags = state.effective_tags()
        pools = ["normal", "desecrated"] if source_pool == "desecrated_combined" else [source_pool]
        out: list[tuple[ModEntry, int]] = []
        for pname in pools:
            pre_filtered: list[ModEntry] = []
            for mod in self.get_mod_tiers(state.base.poe2db_slug, pname):
                if mod.affix_side == AffixSide.PREFIX and not open_p:
                    continue
                if mod.affix_side == AffixSide.SUFFIX and not open_s:
                    continue
                if mod.affix_side not in (AffixSide.PREFIX, AffixSide.SUFFIX):
                    continue
                if boss_tag is not None and mod.boss_tag != boss_tag:
                    continue
                if any(f in blocked for f in mod.families):
                    continue
                if not self._is_tag_eligible(mod, eff_tags):
                    continue
                w_base = 1000 if (boss_tag is not None and pname == "desecrated") else mod.weight
                if w_base <= 0:
                    continue
                pre_filtered.append(mod)
            for mod in filter_pool_with_family_exemption(pre_filtered, ilvl=state.ilvl, min_mod_level=min_mod_level):
                w = 1000 if (boss_tag is not None and pname == "desecrated") else mod.weight
                if use_catalyst and state.catalyst_tag and state.quality > 0 and state.catalyst_tag in mod.mod_tags:
                    w = round(w * (1.0 + state.quality / 20.0))
                out.append((mod, w))
        return out

    def lookup_forced_mod_entry(self, base: BaseItemEntry, lever: LeverEntry) -> ModEntry | None:
        if lever.name == "Essence of the Abyss":
            return ModEntry(
                mod_id=MARK_OF_ABYSS_MOD_ID,
                mod_type=MARK_OF_ABYSS_MOD_ID,
                family_id=MARK_OF_ABYSS_MOD_ID,
                families=(MARK_OF_ABYSS_MOD_ID,),
                base_group_slug=base.poe2db_slug,
                source_pool="perfect_essence",
                affix_side=AffixSide.SUFFIX,
                affix_name="of the Abyss",
                internal_rank_asc=1,
                display_tier=1,
                required_level=1,
                weight=0,
                weight_confidence=WeightConfidence.EXACT_DETERMINISTIC,
                currency_name=lever.name,
                removes_mod=True,
                stat_text="Bears the Mark of the Abyssal Lord",
            )
        mod_id = lever.forced_mods.get(base.poe2db_slug)
        if not mod_id:
            return None
        for m in self.get_mod_tiers(base.poe2db_slug):
            if m.mod_id == mod_id and m.currency_name == lever.name:
                return m
        return next((m for m in self.get_mod_tiers(base.poe2db_slug) if m.mod_id == mod_id), None)

    def removable_indices(
        self,
        state: CraftingState,
        *,
        side_constraint: AffixSide | None = None,
        only_desecrated: bool = False,
        whittling: bool = False,
    ) -> list[int]:
        idxs = [
            i
            for i, m in enumerate(state.mods)
            if not m.fractured
            and (side_constraint is None or m.affix_side == side_constraint)
            and (not only_desecrated or m.is_desecrated)
        ]
        if whittling and idxs:
            min_lvl = min(state.mods[i].required_level for i in idxs)
            idxs = [i for i in idxs if state.mods[i].required_level == min_lvl]
        return idxs

    def analyze_whittling(
        self,
        state: CraftingState,
        protected_mod_ids: set[str] | None = None,
        *,
        side_constraint: AffixSide | None = None,
    ) -> WhittlingReport:
        """Exact Omen of Whittling lowest `required_level` targeting & collateral risk analyzer."""
        protected = protected_mod_ids or set()
        rem = self.removable_indices(state, side_constraint=side_constraint, whittling=True)
        if not rem:
            return WhittlingReport(None, (), 0.0, False, False, "无可削切的非破裂词缀")
        min_lvl = state.mods[rem[0]].required_level
        p_each = 1.0 / len(rem)
        targets: list[WhittlingTarget] = []
        collat = 0.0
        for idx in rem:
            m = state.mods[idx]
            is_prot = m.mod_id in protected
            if is_prot:
                collat += p_each
            targets.append(
                WhittlingTarget(
                    mod_id=m.mod_id,
                    stat_text=m.stat_text.splitlines()[0],
                    affix_side=m.affix_side,
                    display_tier=m.display_tier,
                    required_level=m.required_level,
                    is_protected=is_prot,
                    remove_probability=p_each,
                )
            )
        is_tied = len(targets) > 1
        alert = collat > 0.0
        names = ", ".join(
            f"{t.stat_text} [T{t.display_tier}, req_lvl={t.required_level}"
            + (" · 受保护!" if t.is_protected else "")
            + "]"
            for t in targets
        )
        prefix = "[高危误伤预警] " if alert else ""
        tie_tag = f"（{len(targets)} 条同级平局随机）" if is_tied else ""
        return WhittlingReport(
            min_lvl, tuple(targets), collat, is_tied, alert,
            f"{prefix}削切预兆锁定最低需求等级 req_lvl={min_lvl}{tie_tag}: {names}",
        )

    def evaluate_action(
        self,
        state: CraftingState,
        target_mod_ids: set[str],
        lever_name: str,
        omens: list[str] | None = None,
        *,
        protected_mod_ids: set[str] | None = None,
    ) -> PoolEvalResult:
        return evaluate_action_impl(self, state, target_mod_ids, lever_name, omens, protected_mod_ids)

    def sample_transition(
        self,
        state: CraftingState,
        lever_name: str,
        omens: list[str] | tuple[str, ...] | None,
        desired_mod_ids: set[str],
        rng: random.Random,
    ) -> CraftingState:
        return sample_transition_impl(self, state, lever_name, omens, desired_mod_ids, rng)
