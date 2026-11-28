# SPDX-License-Identifier: GPL-3.0-only
"""Pareto cost-efficiency ranker for PoE2 875 ordered amulet instillations (L2)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..craft.price_book import PriceBook
from .catalog import InstillRecipe, RuneInstillCatalog, load_rune_instill_catalog
from .instill_bridge import format_ordered_recipe, price_instill_recipe
from .live_eval import (
    eval_item_raw_deltas_live,
    extract_build_profile,
    get_allocated_node_ids,
    read_metric_value,
)
from .pareto_ranker import (
    ParetoRankingReport,
    RankedCandidate,
    compute_pareto_frontier,
    find_equipped_slot_item,
    offline_stat_score,
)
from .socket_rules import apply_anoint_to_amulet_raw, strip_anoint_from_amulet_raw


def _select_live_instill_shortlist(
    worker: Any,
    priced_pool: list[tuple[InstillRecipe, dict[str, Any]]],
    metric: str,
    top_live_eval: int,
    warnings: list[str] | None = None,
) -> list[tuple[InstillRecipe, dict[str, Any]]]:
    """Fast (<5ms) offline pre-scoring over `priced_pool` (skipping already-allocated nodes).

    Takes union of Top-12 offline stat score + Top-12 efficiency (score/cost_ex) + Top-4 budget picks,
    capped at `top_live_eval` (default 20), avoiding the 35s full-tree `node_power` scan.
    """
    alloc_ids = get_allocated_node_ids(worker, warnings=warnings)
    profile = extract_build_profile(worker, warnings=warnings)
    pool = [(inst, pinfo) for inst, pinfo in priced_pool if inst.node_id not in alloc_ids] or priced_pool
    k_limit = max(1, int(top_live_eval))
    if len(pool) <= k_limit:
        return list(pool)

    scored: list[tuple[float, float, float, InstillRecipe, dict[str, Any]]] = []
    for inst, pinfo in pool:
        cost_ex = max(0.05, float(pinfo["totalCostEx"]))
        sc = offline_stat_score(inst.stats, metric, profile=profile)
        scored.append((sc, sc / cost_ex, cost_ex, inst, pinfo))

    by_score = sorted(scored, key=lambda x: (-x[0], x[2], x[3].name))[:12]
    by_eff = sorted(scored, key=lambda x: (-x[1], -x[0], x[3].name))[:12]
    by_budget = sorted((x for x in scored if x[0] > 0.0), key=lambda x: (x[2], -x[0], x[3].name))[:4]
    by_all = sorted(scored, key=lambda x: (-x[0], x[2], x[3].name))

    chosen: list[tuple[InstillRecipe, dict[str, Any]]] = []
    seen: set[int] = set()
    for _sc, _eff, _cost, inst, pinfo in (*by_score, *by_eff, *by_budget, *by_all):
        if inst.node_id not in seen:
            seen.add(inst.node_id)
            chosen.append((inst, pinfo))
            if len(chosen) >= k_limit:
                break
    return chosen


def _eval_instills_live(
    worker: Any,
    *,
    am_raw: str,
    priced_pool: list[tuple[InstillRecipe, dict[str, Any]]],
    metric: str,
    top_live_eval: int,
    warnings: list[str],
) -> list[RankedCandidate]:
    prescored = _select_live_instill_shortlist(worker, priced_pool, metric, top_live_eval, warnings=warnings)
    equipped_baseline = read_metric_value(worker, metric)
    unanointed_raw, existing_anoint = strip_anoint_from_amulet_raw(am_raw)

    valid_pairs: list[tuple[InstillRecipe, dict[str, Any], str]] = []
    for inst, pinfo in prescored:
        try:
            mutated = apply_anoint_to_amulet_raw(am_raw, inst.name)
        except ValueError as exc:
            warnings.append(str(exc))
            break
        valid_pairs.append((inst, pinfo, mutated))

    batch_raws = [mraw for _, _, mraw in valid_pairs]
    if existing_anoint and valid_pairs:
        batch_raws = [unanointed_raw, *batch_raws]
    raw_deltas = eval_item_raw_deltas_live(
        worker, "Amulet", batch_raws, metric, equipped_baseline, warnings=warnings
    )
    if existing_anoint and raw_deltas:
        delta_unanointed = raw_deltas[0]
        cand_deltas = raw_deltas[1:]
        unanointed_baseline = equipped_baseline + delta_unanointed
        existing_contrib = -delta_unanointed
        existing_pct = (existing_contrib / unanointed_baseline * 100.0) if unanointed_baseline > 0 else 0.0
        warnings.append(
            f"Equipped amulet already allocates `{existing_anoint}` "
            f"({existing_contrib:+.1f} / {existing_pct:+.2f}% vs unanointed); "
            "Pareto ranking uses intrinsic `gainVsUnanointed` and reports net `gainVsEquipped` per candidate."
        )
    else:
        delta_unanointed = 0.0
        cand_deltas = raw_deltas
        unanointed_baseline = equipped_baseline

    candidates: list[RankedCandidate] = []
    for (inst, pinfo, _mraw), gain_vs_eq in zip(valid_pairs, cand_deltas, strict=False):
        gain_vs_un = gain_vs_eq - delta_unanointed
        gain_pct_eq = (gain_vs_eq / equipped_baseline * 100.0) if equipped_baseline > 0 else 0.0
        gain_pct_un = (gain_vs_un / unanointed_baseline * 100.0) if unanointed_baseline > 0 else 0.0
        cost_ex = float(pinfo["totalCostEx"])
        candidates.append(
            RankedCandidate(
                name=inst.name,
                zh_cn=inst.zh_cn,
                cost_ex=cost_ex,
                gain=gain_vs_un,
                gain_pct=gain_pct_un,
                efficiency_per_ex=gain_vs_un / cost_ex,
                price_source=",".join(pinfo["priceSources"]),
                details={
                    "nodeId": inst.node_id,
                    "orderedRecipe": list(inst.recipe),
                    "orderedDisplayEn": pinfo["orderedDisplayEn"],
                    "orderedDisplayZh": pinfo["orderedDisplayZh"],
                    "stats": list(inst.stats),
                    "existingAnoint": existing_anoint,
                    "gainVsEquipped": round(gain_vs_eq, 4),
                    "gainPctVsEquipped": round(gain_pct_eq, 3),
                    "gainVsUnanointed": round(gain_vs_un, 4),
                    "gainPctVsUnanointed": round(gain_pct_un, 3),
                },
            )
        )
    return candidates


def rank_instillations(
    *,
    metric: str = "CombinedDPS",
    max_budget_ex: float | None = None,
    limit: int = 15,
    top_live_eval: int = 20,
    price_book: PriceBook | None = None,
    worker: Any | None = None,
    evaluator: Callable[[InstillRecipe], float] | None = None,
    catalog: RuneInstillCatalog | None = None,
) -> ParetoRankingReport:
    """Pareto-rank PoE2 875 ordered amulet instillations with live LiquidEmotions pricing."""
    cat = catalog or load_rune_instill_catalog()
    warnings: list[str] = []

    priced_pool: list[tuple[InstillRecipe, dict[str, Any]]] = []
    for inst in cat.instills:
        pinfo = price_instill_recipe(inst, price_book=price_book, catalog=cat)
        if max_budget_ex is not None and pinfo["totalCostEx"] > max_budget_ex:
            continue
        priced_pool.append((inst, pinfo))

    eff_mode = "live_cow" if worker is not None else "offline_heuristic"
    candidates: list[RankedCandidate] | None = None
    if worker is not None:
        try:
            _am_id, am_raw, _meta = find_equipped_slot_item(worker, "Amulet")
        except ValueError as exc:
            return ParetoRankingReport(
                kind="instill",
                metric=metric,
                evaluated_count=0,
                bis_anchor=None,
                budget_champion=None,
                pareto_frontier=(),
                top_candidates=(),
                warnings=(str(exc),),
            )
        try:
            candidates = _eval_instills_live(
                worker,
                am_raw=am_raw,
                priced_pool=priced_pool,
                metric=metric,
                top_live_eval=top_live_eval,
                warnings=warnings,
            )
        except Exception as exc:
            warnings.append(
                f"[live-eval-degraded] Live CoW instillation evaluation failed ({exc}); fell back to offline heuristic scoring."
            )
            eff_mode = "offline_heuristic"
            candidates = None

    if candidates is None:
        candidates = []
        for inst, pinfo in priced_pool:
            cost_ex = float(pinfo["totalCostEx"])
            gain = evaluator(inst) if evaluator is not None else offline_stat_score(inst.stats, metric)
            candidates.append(
                RankedCandidate(
                    name=inst.name,
                    zh_cn=inst.zh_cn,
                    cost_ex=cost_ex,
                    gain=gain,
                    gain_pct=gain,
                    efficiency_per_ex=gain / cost_ex,
                    price_source=",".join(pinfo["priceSources"]),
                    details={
                        "nodeId": inst.node_id,
                        "orderedRecipe": list(inst.recipe),
                        "orderedDisplayEn": format_ordered_recipe(inst.recipe, lang="en", catalog=cat),
                        "orderedDisplayZh": format_ordered_recipe(inst.recipe, lang="zh-CN", catalog=cat),
                        "stats": list(inst.stats),
                    },
                )
            )

    bis, champ, frontier, ranked = compute_pareto_frontier(candidates)
    return ParetoRankingReport(
        kind="instill",
        metric=metric,
        evaluated_count=len(candidates),
        bis_anchor=bis,
        budget_champion=champ,
        pareto_frontier=tuple(frontier),
        top_candidates=tuple(ranked[: max(1, limit)]),
        warnings=tuple(warnings),
        eval_mode=eff_mode,
        shortlisted_from=len(priced_pool) if eff_mode == "live_cow" else None,
    )


__all__ = ["rank_instillations"]
