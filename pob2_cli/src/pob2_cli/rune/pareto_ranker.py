# SPDX-License-Identifier: GPL-3.0-only
"""Shared Pareto frontier models, non-dominated skyline solver, and slot lookup helpers (L2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .live_eval import BuildProfile, score_stat_lines


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """Single evaluated rune or instillation candidate with cost, gain, and Pareto flags."""

    name: str
    zh_cn: str
    cost_ex: float
    gain: float
    gain_pct: float
    efficiency_per_ex: float
    price_source: str
    details: dict[str, Any] = field(default_factory=dict)
    is_pareto: bool = False
    is_bis_anchor: bool = False
    is_budget_champion: bool = False
    pct_of_bis: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "zhCn": self.zh_cn,
            "costEx": round(self.cost_ex, 3),
            "gain": round(self.gain, 4),
            "gainPct": round(self.gain_pct, 3),
            "efficiencyPerEx": round(self.efficiency_per_ex, 4),
            "pctOfBis": round(self.pct_of_bis, 2),
            "isPareto": self.is_pareto,
            "isBisAnchor": self.is_bis_anchor,
            "isBudgetChampion": self.is_budget_champion,
            "priceSource": self.price_source,
            **self.details,
        }


@dataclass(frozen=True)
class ParetoRankingReport:
    """BiS anchor + non-dominated Pareto frontier + full ranked list."""

    kind: str  # "rune" | "instill"
    metric: str
    evaluated_count: int
    bis_anchor: RankedCandidate | None
    budget_champion: RankedCandidate | None
    pareto_frontier: tuple[RankedCandidate, ...]
    top_candidates: tuple[RankedCandidate, ...]
    warnings: tuple[str, ...] = ()
    eval_mode: str = "offline_heuristic"
    shortlisted_from: int | None = None
    slot: str | None = None
    item_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "game": "poe2",
            "patch": "0.5.5",
            "kind": self.kind,
            "metric": self.metric,
            "evalMode": self.eval_mode,
            "evaluatedCount": self.evaluated_count,
            "bisAnchor": self.bis_anchor.to_dict() if self.bis_anchor else None,
            "budgetChampion": self.budget_champion.to_dict() if self.budget_champion else None,
            "paretoFrontier": [c.to_dict() for c in self.pareto_frontier],
            "topCandidates": [c.to_dict() for c in self.top_candidates],
            "warnings": list(self.warnings),
        }
        if self.slot is not None:
            out["slot"] = self.slot
        if self.item_type is not None:
            out["itemType"] = self.item_type
        if self.shortlisted_from is not None:
            out["shortlistedFrom"] = self.shortlisted_from
        return out


def compute_pareto_frontier(
    candidates: list[RankedCandidate],
    *,
    budget_sweet_spot_min_pct_bis: float = 75.0,
) -> tuple[RankedCandidate | None, RankedCandidate | None, list[RankedCandidate], list[RankedCandidate]]:
    """Compute BiS anchor, non-dominated `(cost_ex asc, gain desc)` Pareto frontier, and budget champion."""
    positive = [c for c in candidates if c.gain > 0.0 and c.cost_ex > 0.0]
    if not positive:
        ordered = sorted(candidates, key=lambda c: (-c.gain, c.cost_ex, c.name))
        return None, None, [], ordered

    bis = max(positive, key=lambda c: (c.gain, -c.cost_ex, c.name))
    bis_gain = bis.gain

    # Sort ascending by cost, descending by gain; keep strictly improving running-max gain.
    by_cost = sorted(positive, key=lambda c: (c.cost_ex, -c.gain, c.name))
    frontier_names: set[str] = set()
    best_gain = -1.0
    for cand in by_cost:
        if cand.gain > best_gain + 1e-12:
            frontier_names.add(cand.name)
            best_gain = cand.gain

    # Pick budget champion: cheapest Pareto point reaching >=75% of BiS (or max efficiency if none cheaper than BiS)
    sweet_candidates = [
        c for c in by_cost if c.name in frontier_names and (c.gain / bis_gain) * 100.0 >= budget_sweet_spot_min_pct_bis
    ]
    if sweet_candidates:
        champ_name = sweet_candidates[0].name
    else:
        champ_name = max(
            (c for c in by_cost if c.name in frontier_names),
            key=lambda c: (c.efficiency_per_ex, c.gain),
        ).name

    annotated: list[RankedCandidate] = []
    for c in candidates:
        pct_bis = (c.gain / bis_gain * 100.0) if bis_gain > 0 else 0.0
        annotated.append(
            RankedCandidate(
                name=c.name,
                zh_cn=c.zh_cn,
                cost_ex=c.cost_ex,
                gain=c.gain,
                gain_pct=c.gain_pct,
                efficiency_per_ex=c.gain / c.cost_ex if c.cost_ex > 0 else 0.0,
                price_source=c.price_source,
                details=c.details,
                is_pareto=(c.name in frontier_names),
                is_bis_anchor=(c.name == bis.name),
                is_budget_champion=(c.name == champ_name),
                pct_of_bis=pct_bis,
            )
        )

    bis_out = next(c for c in annotated if c.is_bis_anchor)
    champ_out = next(c for c in annotated if c.is_budget_champion)
    frontier_out = sorted((c for c in annotated if c.is_pareto), key=lambda c: (c.cost_ex, -c.gain))
    ranked_out = sorted(annotated, key=lambda c: (-c.gain, c.cost_ex, c.name))
    return bis_out, champ_out, frontier_out, ranked_out


def offline_stat_score(lines: tuple[str, ...], metric: str, profile: BuildProfile | None = None) -> float:
    """Deterministic stat-line scorer (offline heuristic or build-aware live shortlist)."""
    return score_stat_lines(lines, metric, profile=profile)


def find_equipped_slot_item(worker: Any, slot_name: str) -> tuple[int, str, dict[str, Any]]:
    """Return `(item_id, raw_item_text, item_meta)` for an equipped PoB2 item slot."""
    listing = worker.call("list_items", {})
    items_by_id = {int(it["id"]): it for it in (listing.get("items") or ()) if isinstance(it, dict) and "id" in it}
    slot_low = slot_name.strip().lower()
    for sl in listing.get("slots") or ():
        if not isinstance(sl, dict):
            continue
        if str(sl.get("name") or "").strip().lower() == slot_low:
            sel_id = int(sl.get("selItemId") or sl.get("selectedId") or sl.get("itemId") or 0)
            if sel_id > 0 and sel_id in items_by_id:
                raw = str(worker.call("item_raw", {"id": sel_id}).get("raw") or "")
                return sel_id, raw, items_by_id[sel_id]
    raise ValueError(f"No equipped item found in slot `{slot_name}`.")


__all__ = [
    "ParetoRankingReport",
    "RankedCandidate",
    "compute_pareto_frontier",
    "find_equipped_slot_item",
    "offline_stat_score",
]
