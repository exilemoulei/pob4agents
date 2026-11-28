# SPDX-License-Identifier: GPL-3.0-only
"""Pareto cost-efficiency ranker for PoE2 287 runes, soul cores, and idols (L2)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..craft.price_book import PriceBook
from .catalog import RuneEntry, RuneInstillCatalog, load_rune_instill_catalog
from .instill_bridge import resolve_rune_price_ex
from .live_eval import eval_item_raw_deltas_live, read_metric_value
from .pareto_ranker import (
    ParetoRankingReport,
    RankedCandidate,
    compute_pareto_frontier,
    find_equipped_slot_item,
    offline_stat_score,
)
from .socket_rules import (
    apply_runes_to_item_raw,
    eligible_runes_for_item,
    resolve_item_augment_types,
)


def _select_live_rune_shortlist(
    eligible: list[tuple[RuneEntry, tuple[str, ...]]],
    parity: Any,
    metric: str,
    price_book: PriceBook | None,
    top_live_eval: int,
) -> list[tuple[RuneEntry, tuple[str, ...]]]:
    """Pre-screen legal runes via fast offline score union efficiency (score/cost_ex) to keep live CoW <3s."""
    if top_live_eval <= 0 or len(eligible) <= top_live_eval:
        return eligible
    scored: list[tuple[float, float, float, RuneEntry, tuple[str, ...]]] = []
    for rune, mods in eligible:
        cost_ex, _src = resolve_rune_price_ex(rune, price_book=price_book)
        mult = parity.effect_multiplier(rune.kind)
        sc = offline_stat_score(mods, metric) * mult
        eff = sc / max(0.05, cost_ex)
        scored.append((sc, eff, cost_ex, rune, mods))

    half = max(1, top_live_eval // 2)
    by_gain = sorted(scored, key=lambda x: (-x[0], x[2], x[3].name))[:half]
    by_eff = sorted(scored, key=lambda x: (-x[1], -x[0], x[3].name))[:top_live_eval]
    by_cheap = sorted((x for x in scored if x[0] > 0.0), key=lambda x: (x[2], -x[0], x[3].name))[:4]

    chosen: list[tuple[RuneEntry, tuple[str, ...]]] = []
    seen: set[str] = set()
    for _sc, _eff, _cost, rune, mods in (*by_gain, *by_eff, *by_cheap):
        if rune.name not in seen:
            seen.add(rune.name)
            chosen.append((rune, mods))
            if len(chosen) >= top_live_eval:
                break
    return chosen


def _eval_runes_live(
    worker: Any,
    *,
    eligible: list[tuple[RuneEntry, tuple[str, ...]]],
    parity: Any,
    slot_name: str,
    raw_item_text: str,
    item_type: str,
    sub_type: str | None,
    base_name: str,
    char_level: int,
    is_shaman: bool,
    metric: str,
    top_live_eval: int,
    price_book: PriceBook | None,
    catalog: RuneInstillCatalog,
    warnings: list[str],
) -> list[RankedCandidate]:
    shortlist = _select_live_rune_shortlist(eligible, parity, metric, price_book, top_live_eval)
    baseline = read_metric_value(worker, metric)
    valid_pairs: list[tuple[RuneEntry, tuple[str, ...], str]] = []
    for rune, mods in shortlist:
        try:
            mutated_raw = apply_runes_to_item_raw(
                raw_item_text,
                [rune.name],
                item_type=item_type,
                sub_type=sub_type,
                base_name=base_name,
                char_level=char_level,
                is_shaman=is_shaman,
                allow_overwrite_bound=True,
                catalog=catalog,
            )
        except ValueError as exc:
            warnings.append(f"[rune-rank-skipped] Skipped `{rune.name}` ({exc}).")
            continue
        valid_pairs.append((rune, mods, mutated_raw))
    deltas = eval_item_raw_deltas_live(
        worker, slot_name, [mraw for _, _, mraw in valid_pairs], metric, baseline, warnings=warnings
    )
    candidates: list[RankedCandidate] = []
    for (rune, mods, _mraw), gain in zip(valid_pairs, deltas, strict=False):
        cost_ex, src = resolve_rune_price_ex(rune, price_book=price_book)
        gain_pct = (gain / baseline * 100.0) if baseline > 0 else 0.0
        candidates.append(
            RankedCandidate(
                name=rune.name,
                zh_cn=rune.zh_cn,
                cost_ex=cost_ex,
                gain=gain,
                gain_pct=gain_pct,
                efficiency_per_ex=gain / cost_ex,
                price_source=src,
                details={
                    "kind": rune.kind,
                    "reqLevel": rune.req_level,
                    "isSocketBound": rune.is_socket_bound,
                    "effectiveMods": list(mods),
                    "effectMultiplier": round(parity.effect_multiplier(rune.kind), 3),
                },
            )
        )
    return candidates


def rank_runes_for_slot(
    *,
    item_type: str = "",
    slot_name: str = "Body Armour",
    raw_item_text: str = "",
    sub_type: str | None = None,
    base_name: str = "",
    char_level: int = 100,
    is_shaman: bool = False,
    metric: str = "CombinedDPS",
    max_budget_ex: float | None = None,
    limit: int = 15,
    top_live_eval: int = 28,
    include_socket_bound: bool = False,
    price_book: PriceBook | None = None,
    worker: Any | None = None,
    evaluator: Callable[[RuneEntry, tuple[str, ...]], float] | None = None,
    catalog: RuneInstillCatalog | None = None,
) -> ParetoRankingReport:
    """Rank legal PoE2 runes/soul cores/idols for an item/slot with BiS anchor + Pareto frontier."""
    cat = catalog or load_rune_instill_catalog()
    warnings: list[str] = []

    if worker is not None and not raw_item_text:
        _item_id, raw_item_text, meta = find_equipped_slot_item(worker, slot_name)
        item_type = item_type or str(meta.get("type") or "")
        base_name = base_name or str(meta.get("baseName") or "")

    parity = resolve_item_augment_types(
        item_type, sub_type=sub_type, raw_text=raw_item_text, base_name=base_name, catalog=cat
    )
    if parity.is_mirrored:
        return ParetoRankingReport(
            kind="rune",
            metric=metric,
            evaluated_count=0,
            bis_anchor=None,
            budget_champion=None,
            pareto_frontier=(),
            top_candidates=(),
            warnings=("Target item is Mirrored; socketed runes cannot be changed.",),
            eval_mode="live_cow" if worker is not None else "offline_heuristic",
            slot=slot_name,
            item_type=item_type or None,
        )

    eligible = eligible_runes_for_item(
        parity,
        char_level=char_level,
        is_shaman=is_shaman,
        include_socket_bound=include_socket_bound,
        catalog=cat,
    )
    if max_budget_ex is not None:
        eligible = [
            (r, m) for r, m in eligible if resolve_rune_price_ex(r, price_book=price_book)[0] <= max_budget_ex
        ]

    total_eligible = len(eligible)
    eff_mode = "live_cow" if worker is not None else "offline_heuristic"
    candidates: list[RankedCandidate] | None = None
    if worker is not None:
        try:
            candidates = _eval_runes_live(
                worker,
                eligible=eligible,
                parity=parity,
                slot_name=slot_name,
                raw_item_text=raw_item_text,
                item_type=item_type,
                sub_type=sub_type,
                base_name=base_name,
                char_level=char_level,
                is_shaman=is_shaman,
                metric=metric,
                top_live_eval=top_live_eval,
                price_book=price_book,
                catalog=cat,
                warnings=warnings,
            )
        except Exception as exc:
            warnings.append(
                f"[live-eval-degraded] Live CoW rune evaluation failed on `{slot_name}` ({exc}); fell back to offline heuristic scoring."
            )
            eff_mode = "offline_heuristic"
            candidates = None

    if candidates is None:
        candidates = []
        for rune, mods in eligible:
            cost_ex, src = resolve_rune_price_ex(rune, price_book=price_book)
            mult = parity.effect_multiplier(rune.kind)
            raw_gain = evaluator(rune, mods) if evaluator is not None else (offline_stat_score(mods, metric) * mult)
            candidates.append(
                RankedCandidate(
                    name=rune.name,
                    zh_cn=rune.zh_cn,
                    cost_ex=cost_ex,
                    gain=raw_gain,
                    gain_pct=raw_gain,
                    efficiency_per_ex=raw_gain / cost_ex,
                    price_source=src,
                    details={
                        "kind": rune.kind,
                        "reqLevel": rune.req_level,
                        "isSocketBound": rune.is_socket_bound,
                        "effectiveMods": list(mods),
                        "effectMultiplier": round(mult, 3),
                    },
                )
            )

    bis, champ, frontier, ranked = compute_pareto_frontier(candidates)
    return ParetoRankingReport(
        kind="rune",
        metric=metric,
        evaluated_count=len(candidates),
        bis_anchor=bis,
        budget_champion=champ,
        pareto_frontier=tuple(frontier),
        top_candidates=tuple(ranked[: max(1, limit)]),
        warnings=tuple(warnings),
        eval_mode=eff_mode,
        shortlisted_from=total_eligible if eff_mode == "live_cow" else None,
        slot=slot_name,
        item_type=item_type or None,
    )


__all__ = ["rank_runes_for_slot"]
