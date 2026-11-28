# SPDX-License-Identifier: GPL-3.0-only
"""Currency mod-family floor exemption, full next-step outcome distribution, and target similarity ranking (L2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import (
    AffixSide,
    CraftingState,
    ItemRarity,
    LeverEntry,
    ModEntry,
    TargetSpec,
    WeightConfidence,
    extract_mod_value_range,
    lowest_confidence,
)
from .pool_actions import (
    CRAFTED_LEVER_CATEGORIES,
    _parse_omens,
    exact_three_choice_unveil_prob,
)
from .pool_suggestions import clean_mod_phrase as _clean_mod_phrase
from .pool_suggestions import rank_target_suggestions, render_outcomes_md
from .weights_loader import strip_numbers_to_pattern

if TYPE_CHECKING:
    from .pool_engine import ModPoolEngine


def filter_pool_with_family_exemption(
    candidates: list[ModEntry],
    *,
    ilvl: int,
    min_mod_level: int,
) -> list[ModEntry]:
    """Apply `ilvl` cap + official GGG / Craft of Exile 2 `Greater`/`Perfect` mod-family floor exemption.

    Rule:
      - Filter `mod.required_level <= ilvl` first.
      - If a mod family/ladder's highest `ilvl`-eligible tier has `max_req >= min_mod_level`,
        prune lower tiers with `required_level < min_mod_level`.
      - If a mod family/ladder's highest `ilvl`-eligible tier has `max_req < min_mod_level`,
        that family is EXEMPTED rather than dropped and retains its highest available tier (`required_level == max_req`).
    """
    ilvl_ok = [m for m in candidates if m.required_level <= ilvl]
    if min_mod_level <= 1 or not ilvl_ok:
        return ilvl_ok

    max_req_by_ladder: dict[tuple[str, AffixSide, str, str], int] = {}
    for m in ilvl_ok:
        key = (m.source_pool, m.affix_side, m.family_id, strip_numbers_to_pattern(m.stat_text))
        prev = max_req_by_ladder.get(key, -1)
        if m.required_level > prev:
            max_req_by_ladder[key] = m.required_level

    out: list[ModEntry] = []
    for m in ilvl_ok:
        key = (m.source_pool, m.affix_side, m.family_id, strip_numbers_to_pattern(m.stat_text))
        max_req = max_req_by_ladder[key]
        if max_req >= min_mod_level:
            if m.required_level >= min_mod_level:
                out.append(m)
        elif m.required_level == max_req:
            out.append(m)
    return out


def default_rarity_for_lever(lever: LeverEntry | None) -> ItemRarity:
    """Infer sensible default item rarity for `craft odds` when `--text` is omitted."""
    if lever is None:
        return ItemRarity.NORMAL
    nm = lever.name
    if "Transmutation" in nm or nm == "Orb of Alchemy":
        return ItemRarity.NORMAL
    if "Augmentation" in nm or "Regal" in nm or lever.category == "essence":
        return ItemRarity.MAGIC
    return ItemRarity.RARE


def _default_next_lever_name(state: CraftingState) -> str:
    if state.rarity == ItemRarity.NORMAL:
        return "Orb of Transmutation"
    if state.rarity == ItemRarity.MAGIC:
        if state.has_open_prefix(ItemRarity.MAGIC) or state.has_open_suffix(ItemRarity.MAGIC):
            return "Orb of Augmentation"
        return "Regal Orb"
    if state.rarity == ItemRarity.RARE:
        return "Exalted Orb"
    raise ValueError(f"Cannot compute crafting outcomes on {state.rarity.value} item.")


def _group_weighted_outcomes(
    weighted: list[tuple[ModEntry, float]],
    *,
    floor: int,
    unveil_pool: list[tuple[ModEntry, int]] | None = None,
    has_echoes: bool = False,
) -> tuple[list[dict[str, Any]], float]:
    tot_w = sum(w for _, w in weighted if w > 0)
    if tot_w <= 0:
        return [], 0.0

    groups: dict[tuple[str, AffixSide, str, str], list[tuple[ModEntry, float]]] = {}
    for m, w in weighted:
        if w <= 0:
            continue
        key = (m.source_pool, m.affix_side, m.family_id, strip_numbers_to_pattern(m.stat_text))
        groups.setdefault(key, []).append((m, w))

    outcomes: list[dict[str, Any]] = []
    for (spool, side, fam_id, pat), members in groups.items():
        members.sort(key=lambda x: (x[0].display_tier, -x[0].required_level, x[0].mod_id))
        fam_w = sum(w for _, w in members)
        prob = fam_w / tot_w
        tiers_num = [m.display_tier for m, _ in members]
        reqs = [m.required_level for m, _ in members]
        min_t, max_t = min(tiers_num), max(tiers_num)
        min_req, max_req = min(reqs), max(reqs)
        rep = members[0][0]
        tiers_detail = [
            {
                "modId": m.mod_id,
                "tier": f"T{m.display_tier}",
                "ilvlReq": m.required_level,
                "valueRange": extract_mod_value_range(m.stat_text),
                "statText": m.stat_text,
                "weight": round(w, 4) if isinstance(w, float) and not w.is_integer() else round(w),
                "probability": round(w / tot_w, 8),
            }
            for m, w in members
        ]
        fam_w_fmt = sum(t["weight"] for t in tiers_detail)
        if isinstance(fam_w_fmt, float) and fam_w_fmt.is_integer():
            fam_w_fmt = int(fam_w_fmt)
        else:
            fam_w_fmt = round(fam_w_fmt, 4) if isinstance(fam_w_fmt, float) else fam_w_fmt
        entry: dict[str, Any] = {
            "familyId": fam_id,
            "modType": rep.mod_type,
            "name": _clean_mod_phrase(rep.stat_text, fam_id),
            "statPattern": pat or rep.stat_text,
            "slot": side.value,
            "sourcePool": spool,
            "weight": fam_w_fmt,
            "probability": round(prob, 8),
            "tierCount": len(members),
            "tierRange": f"T{min_t}-T{max_t}" if min_t != max_t else f"T{min_t}",
            "reqLevelRange": [min_req, max_req],
            "exemptedBelowFloor": bool(floor > 1 and max_req < floor),
            "tiers": tiers_detail,
            "availableTiers": [
                {
                    "modId": m.mod_id,
                    "tier": m.display_tier,
                    "requiredLevel": m.required_level,
                    "valueRange": extract_mod_value_range(m.stat_text),
                    "weight": round(w, 4) if isinstance(w, float) and not w.is_integer() else round(w),
                    "probability": round(w / tot_w, 8),
                    "statText": m.stat_text,
                }
                for m, w in members
            ],
        }
        if unveil_pool is not None:
            fam_ids = {m.mod_id for m, _ in members}
            p3 = exact_three_choice_unveil_prob(unveil_pool, fam_ids)
            if has_echoes:
                p3 = 1.0 - (1.0 - p3) ** 2
            entry["unveilHitProbability"] = round(p3, 8)
        outcomes.append(entry)

    outcomes.sort(key=lambda o: (-o["probability"], o["slot"], o["familyId"], o["name"]))
    return outcomes, tot_w


def compute_next_step_outcomes(
    eng: ModPoolEngine,
    state: CraftingState,
    *,
    lever_name: str | None = None,
    omens: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Compute complete next-step `ModFamily` weight & probability distribution for `craft odds --outcomes`."""
    if state.corrupted or state.sanctified:
        raise ValueError("Item is Corrupted or Sanctified; no further currency outcomes are legal.")

    eff_lever_name = lever_name or _default_next_lever_name(state)
    lever = eng.resolve_lever(eff_lever_name)
    omen_entries = tuple(eng.resolve_lever(om) for om in (omens or ()))
    if lever.max_item_level is not None and state.ilvl > lever.max_item_level:
        raise ValueError(f"{lever.name} has max item level {lever.max_item_level}, got ilvl={state.ilvl}")

    side_con, boss_con, has_whittle, _has_light, has_echoes, has_cat, _extra = _parse_omens(omen_entries)
    floor = lever.min_mod_level_floor or 1

    if lever.category in CRAFTED_LEVER_CATEGORIES:
        forced = eng.lookup_forced_mod_entry(state.base, lever)
        if forced is None:
            raise ValueError(f"{lever.name} has no forced modifier on `{state.base.poe2db_slug}`.")
        if lever.name != "Essence of the Abyss" and state.has_crafted_mod:
            raise ValueError(f"{lever.name} blocked: item already has 1 Crafted modifier (0.5.0+ limit).")
        outcomes, tot_w = _group_weighted_outcomes([(forced, 1.0)], floor=1)
        conf = WeightConfidence.EXACT_DETERMINISTIC
    elif lever.category == "bone":
        if state.rarity != ItemRarity.RARE:
            raise ValueError(f"{lever.name} requires a RARE item (got {state.rarity.value}).")
        if state.has_desecrated:
            raise ValueError("Item already has 1 Desecrated modifier.")
        pool_mode = "desecrated" if boss_con else "desecrated_combined"
        elig = eng.eligible_pool(
            state,
            source_pool=pool_mode,
            target_rarity=ItemRarity.RARE,
            side_constraint=side_con,
            min_mod_level=floor,
            boss_tag=boss_con,
        )
        outcomes, tot_w = _group_weighted_outcomes(
            [(m, float(w)) for m, w in elig],
            floor=floor,
            unveil_pool=elig,
            has_echoes=has_echoes,
        )
        conf = WeightConfidence.EXACT_DETERMINISTIC if boss_con else WeightConfidence.EMPIRICAL_ESTIMATE
    elif "Chaos Orb" in lever.name:
        if state.rarity != ItemRarity.RARE:
            raise ValueError(f"{lever.name} requires a RARE item (got {state.rarity.value}).")
        rem = eng.removable_indices(state, side_constraint=side_con, whittling=has_whittle)
        if not rem:
            elig = eng.eligible_pool(state, target_rarity=ItemRarity.RARE, side_constraint=side_con, min_mod_level=floor)
            outcomes, tot_w = _group_weighted_outcomes([(m, float(w)) for m, w in elig], floor=floor)
            conf = lowest_confidence([m.weight_confidence for m, _ in elig]) if elig else WeightConfidence.UNMEASURED
        else:
            acc_prob: dict[str, tuple[ModEntry, float]] = {}
            confs: list[WeightConfidence] = []
            avg_tw = 0.0
            for idx in rem:
                sub = state.clone()
                sub.mods.pop(idx)
                elig = eng.eligible_pool(sub, target_rarity=ItemRarity.RARE, min_mod_level=floor)
                tw = sum(w for _, w in elig)
                avg_tw += tw / len(rem)
                if elig:
                    confs.append(lowest_confidence([m.weight_confidence for m, _ in elig]))
                if tw > 0:
                    p_branch = 1.0 / len(rem)
                    for m, w in elig:
                        prev_m, prev_w = acc_prob.get(m.mod_id, (m, 0.0))
                        acc_prob[m.mod_id] = (prev_m, prev_w + p_branch * (w / tw))
            scaled = [(m, p * max(1.0, avg_tw)) for m, p in acc_prob.values()]
            outcomes, tot_w = _group_weighted_outcomes(scaled, floor=floor)
            conf = lowest_confidence(confs) if confs else WeightConfidence.UNMEASURED
    else:
        req_rar = (
            ItemRarity.NORMAL
            if ("Transmutation" in lever.name or lever.name == "Orb of Alchemy")
            else (ItemRarity.MAGIC if ("Augmentation" in lever.name or "Regal" in lever.name) else ItemRarity.RARE)
        )
        next_rar = (
            ItemRarity.MAGIC
            if ("Transmutation" in lever.name or "Augmentation" in lever.name)
            else ItemRarity.RARE
        )
        if state.rarity != req_rar:
            raise ValueError(f"{lever.name} requires {req_rar.value} item (got {state.rarity.value}).")
        elig = eng.eligible_pool(
            state,
            target_rarity=next_rar,
            side_constraint=side_con,
            min_mod_level=floor,
            use_catalyst=has_cat,
        )
        outcomes, tot_w = _group_weighted_outcomes([(m, float(w)) for m, w in elig], floor=floor)
        conf = lowest_confidence([m.weight_confidence for m, _ in elig]) if elig else WeightConfidence.UNMEASURED

    prob_sum = round(sum(o["probability"] for o in outcomes), 6)
    tot_w_int = round(tot_w, 4) if isinstance(tot_w, float) and not tot_w.is_integer() else round(tot_w)
    return {
        "game": "poe2",
        "patch": eng.patch,
        "base": state.base.name,
        "baseSlug": state.base.poe2db_slug,
        "ilvl": state.ilvl,
        "rarity": state.rarity.value,
        "lever": lever.name,
        "omens": [o.name for o in omen_entries],
        "minModLevelFloor": floor,
        "totalPoolWeight": tot_w_int,
        "familyCount": len(outcomes),
        "probabilitySum": prob_sum,
        "weightConfidence": conf.value,
        "outcomes": outcomes,
    }


def compute_matched_tiers_for_state(
    eng: ModPoolEngine,
    state: CraftingState,
    targets: tuple[TargetSpec, ...],
    *,
    lever_name: str | None = None,
    omens: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Build per-tier roll/weight/probability details for `--want` targets against active/default pool."""
    wanted_ids = {mid for ts in targets for mid in ts.matching_mod_ids}
    weight_by_id: dict[str, tuple[int | float, float]] = {}
    try:
        dist = compute_next_step_outcomes(eng, state, lever_name=lever_name, omens=omens)
        for fam in dist.get("outcomes") or ():
            for t in fam.get("tiers") or ():
                mid = t.get("modId")
                if mid in wanted_ids:
                    weight_by_id[mid] = (t["weight"], t["probability"])
    except (ValueError, KeyError):
        weight_by_id = {}

    if not weight_by_id:
        eff_rar = ItemRarity.MAGIC if state.rarity == ItemRarity.NORMAL else state.rarity
        elig = eng.eligible_pool(state, target_rarity=eff_rar)
        tot_w = sum(w for _, w in elig)
        for m, w in elig:
            if m.mod_id in wanted_ids:
                weight_by_id[m.mod_id] = (w, round(w / tot_w, 8) if tot_w > 0 else 0.0)

    seen: set[str] = set()
    ordered_entries: list[ModEntry] = []
    for ts in targets:
        for m in sorted(ts.matching_entries, key=lambda x: (x.display_tier, -x.required_level, x.mod_id)):
            if m.mod_id not in seen:
                seen.add(m.mod_id)
                ordered_entries.append(m)
    ordered_entries.sort(key=lambda x: (x.display_tier, -x.required_level, x.mod_id))

    out: list[dict[str, Any]] = []
    for m in ordered_entries:
        w, prob = weight_by_id.get(m.mod_id, (m.weight, 0.0))
        out.append(
            {
                "modId": m.mod_id,
                "tier": f"T{m.display_tier}",
                "ilvlReq": m.required_level,
                "valueRange": extract_mod_value_range(m.stat_text),
                "statText": m.stat_text,
                "weight": w,
                "probability": prob,
            }
        )
    return out


__all__ = [
    "compute_matched_tiers_for_state",
    "compute_next_step_outcomes",
    "default_rarity_for_lever",
    "filter_pool_with_family_exemption",
    "rank_target_suggestions",
    "render_outcomes_md",
]
