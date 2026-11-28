# SPDX-License-Identifier: GPL-3.0-only
"""Exact multi-choice dynamic programming solvers and dataclasses for PoE2 Spirit knapsack (L2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SpiritOption:
    """One discrete Spirit reservation choice (either a deficit cut or a forward-packing pick)."""

    option_id: str
    choice_group: str  # Mutually exclusive group (e.g. same gem downgrade vs disable)
    kind: str  # "support_downgrade" | "support_disable" | "minion_count" | "aura_toggle" | "pack_item"
    label: str
    spirit_delta: int  # Positive = Spirit freed (rescue) or Spirit cost (forward packing)
    delta_dps: float
    delta_ehp: float
    loss_score: float = 0.0  # Lower is better for deficit rescue
    value_score: float = 0.0  # Higher is better for forward packing
    rpc_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.option_id,
            "group": self.choice_group,
            "kind": self.kind,
            "label": self.label,
            "spirit": self.spirit_delta,
            "deltaDps": round(self.delta_dps, 2),
            "deltaEhp": round(self.delta_ehp, 2),
            "dpsPerSpirit": round(abs(self.delta_dps) / max(1, self.spirit_delta), 2),
            "ehpPerSpirit": round(abs(self.delta_ehp) / max(1, self.spirit_delta), 2),
            "lossScore": round(self.loss_score, 4),
            "valueScore": round(self.value_score, 4),
        }


@dataclass(frozen=True)
class SpiritKnapsackPlan:
    """Output of either deficit-rescue or forward-packing discrete Spirit knapsack."""

    mode: str  # "rescue" | "pack"
    target_spirit: int
    achieved_spirit: int
    remaining_deficit: int
    selected: tuple[SpiritOption, ...]
    all_evaluated: tuple[SpiritOption, ...]
    missing_campaign_spirit: tuple[dict[str, Any], ...] = ()
    azcapa_hint: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": 4,
            "name": "spirit_knapsack",
            "mode": self.mode,
            "targetSpirit": self.target_spirit,
            "achievedSpirit": self.achieved_spirit,
            "remainingDeficit": self.remaining_deficit,
            "selected": [o.to_dict() for o in self.selected],
            "evaluatedCount": len(self.all_evaluated),
            "evaluated": [o.to_dict() for o in self.all_evaluated],
            "missingCampaignSpiritQuests": list(self.missing_campaign_spirit),
            "azcapaHint": self.azcapa_hint,
            "warnings": list(self.warnings),
        }


def solve_deficit_knapsack(
    options: list[SpiritOption],
    deficit: int,
) -> tuple[list[SpiritOption], int]:
    """Exact multi-choice DP: pick <=1 option per `choice_group` freeing `>= deficit` minimizing loss & overshoot."""
    if deficit <= 0 or not options:
        return [], 0
    grouped: dict[str, list[SpiritOption]] = {}
    for opt in options:
        if opt.spirit_delta > 0:
            grouped.setdefault(opt.choice_group, []).append(opt)
    if not grouped:
        return [], 0

    cap = int(deficit)
    inf = float("inf")
    overshoot_w = 0.005
    dp: list[tuple[float, float, int, int, tuple[SpiritOption, ...]]] = [(inf, inf, 0, 0, ())] * (cap + 1)
    dp[0] = (0.0, 0.0, 0, 0, ())

    for group_key in sorted(grouped):
        group_opts = grouped[group_key]
        nxt = list(dp)
        for cur_s in range(cap):
            _cur_obj, cur_loss, cur_freed, cur_cnt, cur_pick = dp[cur_s]
            if cur_loss == inf:
                continue
            for opt in group_opts:
                new_freed = cur_freed + opt.spirit_delta
                ns = min(cap, new_freed)
                cand_loss = cur_loss + max(0.0, opt.loss_score)
                overshoot = max(0, new_freed - cap) if ns == cap else 0
                cand_obj = cand_loss + overshoot * overshoot_w
                cand_cnt = cur_cnt + 1
                prev_obj, prev_loss, prev_freed, prev_cnt, _ = nxt[ns]
                if (cand_obj, new_freed, cand_cnt) < (prev_obj - 1e-12, prev_freed, prev_cnt) or (
                    abs(cand_obj - prev_obj) <= 1e-12
                    and (new_freed, cand_loss, cand_cnt) < (prev_freed, prev_loss, prev_cnt)
                ):
                    nxt[ns] = (cand_obj, cand_loss, new_freed, cand_cnt, (*cur_pick, opt))
        dp = nxt

    if dp[cap][1] < inf:
        chosen = list(dp[cap][4])
        return chosen, sum(o.spirit_delta for o in chosen)

    for s in range(cap - 1, 0, -1):
        if dp[s][1] < inf:
            chosen = list(dp[s][4])
            return chosen, sum(o.spirit_delta for o in chosen)
    return [], 0


def solve_forward_spirit_knapsack(
    options: list[SpiritOption],
    budget: int,
) -> tuple[list[SpiritOption], int]:
    """Exact multi-choice 0/1 knapsack maximizing `value_score` subject to `sum(spirit_delta) <= budget`."""
    if budget <= 0 or not options:
        return [], 0
    grouped: dict[str, list[SpiritOption]] = {}
    for opt in options:
        if 0 < opt.spirit_delta <= budget and opt.value_score > 0.0:
            grouped.setdefault(opt.choice_group, []).append(opt)
    if not grouped:
        return [], 0

    cap = int(budget)
    dp: list[tuple[float, tuple[SpiritOption, ...]]] = [(-1.0, ())] * (cap + 1)
    dp[0] = (0.0, ())

    for group_key in sorted(grouped):
        group_opts = grouped[group_key]
        nxt = list(dp)
        for cur_s in range(cap + 1):
            cur_val, cur_pick = dp[cur_s]
            if cur_val < 0.0:
                continue
            for opt in group_opts:
                ns = cur_s + opt.spirit_delta
                if ns <= cap:
                    cand_val = cur_val + opt.value_score
                    if cand_val > nxt[ns][0] + 1e-12:
                        nxt[ns] = (cand_val, (*cur_pick, opt))
        dp = nxt

    best_s = max(range(cap + 1), key=lambda s: (dp[s][0], -s))
    if dp[best_s][0] <= 0.0:
        return [], 0
    chosen = list(dp[best_s][1])
    return chosen, sum(o.spirit_delta for o in chosen)


__all__ = [
    "SpiritKnapsackPlan",
    "SpiritOption",
    "solve_deficit_knapsack",
    "solve_forward_spirit_knapsack",
]
