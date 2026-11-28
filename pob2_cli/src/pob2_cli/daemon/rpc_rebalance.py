# SPDX-License-Identifier: GPL-3.0-only
"""Daemon RPC slot handlers for Wave 2 rebalance (`fix_gaps`, `spirit`) and rune/instill (`rune_rank`, `rank_instill`) (L3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..game_profile import GAME_ID
from ..rebalance import (
    SpiritKnapsackPlan,
    apply_spirit_plan_live,
    build_offline_pack_catalog_options,
    solve_fix_gaps_live,
    solve_forward_spirit_knapsack,
    solve_live_spirit_deficit,
    stat_snapshot_from_pob_stats,
)
from ..rune import rank_instillations, rank_runes_for_slot
from ..snapshot import BuildScope, BuildTransaction, history_of

if TYPE_CHECKING:
    from .slots import DaemonManager


def handle_fix_gaps(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    """Run 5-stage zero-buy rebalance on hot daemon slot; commits to SlotHistory when `apply=True`."""
    overcap = int(params["overcap"]) if params.get("overcap") is not None else 20
    chaos_target = int(params["chaos_target"]) if params.get("chaos_target") is not None else 0
    apply_flag = bool(params.get("apply", False))

    with w.lock:
        report = solve_fix_gaps_live(
            w,
            overcap_target=overcap,
            chaos_target=chaos_target,
            apply=apply_flag,
        )
        payload = report.to_dict()
        payload["evalMode"] = "live_cow"
        payload["applied"] = apply_flag
        if apply_flag:
            hist = history_of(w)
            payload["rev"] = hist.rev
            payload.update(hist.depths())
        return m._attach_warning_if_degraded(payload)


def handle_spirit(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    """Run discrete Spirit knapsack (forward `--budget` pack OR hot-slot CoW deficit rescue)."""
    raw_budget = params.get("budget")
    metric = str(params.get("metric") or "CombinedDPS")
    apply_flag = bool(params.get("apply", False))

    if raw_budget is not None:
        budget = int(raw_budget)
        if budget <= 0:
            raise DaemonRequestError("`budget` must be a positive integer.")
        opts = build_offline_pack_catalog_options(metric=metric)
        chosen, used = solve_forward_spirit_knapsack(opts, budget)
        plan = SpiritKnapsackPlan(
            mode="pack",
            target_spirit=budget,
            achieved_spirit=used,
            remaining_deficit=max(0, budget - used),
            selected=tuple(chosen),
            all_evaluated=tuple(opts),
        )
        out = plan.to_dict()
        out["game"] = GAME_ID
        out["evalMode"] = "catalog_knapsack"
        out["applied"] = False
        return m._attach_warning_if_degraded(out)

    with w.lock:
        plan = solve_live_spirit_deficit(w)
        out = plan.to_dict()
        out["game"] = GAME_ID
        out["evalMode"] = "live_cow"
        out["applied"] = False
        if apply_flag and plan.selected:
            with BuildTransaction(w, scope=BuildScope.ACTIVE_SETS, will_commit=True) as tx:
                apply_spirit_plan_live(w, plan)
                tx.commit(undoable=True)
            hist = history_of(w)
            after_st = stat_snapshot_from_pob_stats(w.call("get_stats", {}).get("stats") or {})
            out["applied"] = True
            out["afterSpiritUnreserved"] = round(after_st.spirit_unreserved, 1)
            out["rev"] = hist.rev
            out.update(hist.depths())
        return m._attach_warning_if_degraded(out)


def handle_rank_instill(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    """Pareto-rank 875 ordered amulet instillations via fast pre-scored live calc on hot daemon slot."""
    metric = str(params.get("metric") or "CombinedDPS")
    budget = float(params["budget"]) if params.get("budget") is not None else None
    limit = int(params["limit"]) if params.get("limit") is not None else 15
    top_live = int(params["top_live_eval"]) if params.get("top_live_eval") is not None else 20

    with w.lock:
        report = rank_instillations(
            metric=metric,
            max_budget_ex=budget,
            limit=limit,
            top_live_eval=top_live,
            worker=w,
        )
        if report.evaluated_count == 0 and report.warnings:
            raise DaemonRequestError("; ".join(report.warnings))
        return m._attach_warning_if_degraded(report.to_dict())


def handle_rune_rank(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    """Pareto-rank PoE2 runes/soul cores/idols for an equipped slot via live CoW (<3s shortlist)."""
    slot_name = str(params.get("slot") or "Weapon 1")
    item_type = str(params.get("item_type") or "")
    base_name = str(params.get("base_name") or "")
    metric = str(params.get("metric") or "CombinedDPS")
    budget = float(params["budget"]) if params.get("budget") is not None else None
    limit = int(params["limit"]) if params.get("limit") is not None else 12
    top_live = int(params["top_live_eval"]) if params.get("top_live_eval") is not None else 28

    with w.lock:
        info = w.call("get_build_info", {})
        char_level = int(params["level"]) if params.get("level") is not None else int(info.get("level") or 90)
        if params.get("shaman") is not None:
            is_shaman = bool(params["shaman"])
        else:
            asc = str(info.get("ascendClassName") or info.get("ascendancyName") or "")
            is_shaman = asc.lower() == "shaman"
        try:
            report = rank_runes_for_slot(
                item_type=item_type,
                slot_name=slot_name,
                base_name=base_name,
                char_level=char_level,
                is_shaman=is_shaman,
                metric=metric,
                max_budget_ex=budget,
                limit=limit,
                top_live_eval=top_live,
                worker=w,
            )
        except ValueError as exc:
            raise DaemonRequestError(str(exc)) from exc
        return m._attach_warning_if_degraded(report.to_dict())


REBALANCE_SLOT_HANDLERS = {
    "fix_gaps": handle_fix_gaps,
    "spirit": handle_spirit,
    "rank_instill": handle_rank_instill,
    "rune_rank": handle_rune_rank,
}

__all__ = [
    "REBALANCE_SLOT_HANDLERS",
    "handle_fix_gaps",
    "handle_rank_instill",
    "handle_rune_rank",
    "handle_spirit",
]
