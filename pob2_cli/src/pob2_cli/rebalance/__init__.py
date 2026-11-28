# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 0.5.5 5-stage zero-buy rebalance solver (`pob2 fix-gaps`) and discrete Spirit knapsack (`pob2 spirit`) (L2)."""

from __future__ import annotations

from .net_gap import (
    EquippedSlotAudit,
    NetGapAudit,
    StatSnapshot,
    analyze_live_net_gap,
    audit_equipped_slot,
    stat_snapshot_from_pob_stats,
)
from .solver import (
    FixGapsReport,
    Stage5PurchaseFallback,
    build_stage5_fallback,
    solve_fix_gaps_live,
    solve_fix_gaps_offline,
)
from .spirit_knapsack import (
    CAMPAIGN_SPIRIT_QUESTS,
    SpiritKnapsackPlan,
    SpiritOption,
    apply_spirit_plan_live,
    build_offline_pack_catalog_options,
    evaluate_live_spirit_options,
    solve_deficit_knapsack,
    solve_forward_spirit_knapsack,
    solve_live_spirit_deficit,
)
from .step_attributes import (
    ATTR_NAME_TO_IDX,
    ATTRIBUTE_NODE_STEP,
    IDX_TO_ATTR_NAME,
    AttributeNodeInfo,
    AttributeSwapAction,
    Stage1AttributePlan,
    apply_attribute_swaps_live,
    collect_live_attribute_nodes,
    solve_attribute_respec,
)
from .step_catalysts import (
    POE2_CATALYSTS,
    CatalystSpec,
    CatalystUpgradeAction,
    Stage3CatalystPlan,
    apply_catalyst_plan_live,
    apply_catalyst_to_item_raw,
    estimate_catalyst_gains,
    solve_catalyst_upgrades,
)
from .step_runes import (
    SocketAssignment,
    Stage2RunePlan,
    apply_rune_plan_live,
    parse_augment_stat_vector,
    solve_rune_allocation,
)

__all__ = [
    "ATTRIBUTE_NODE_STEP",
    "ATTR_NAME_TO_IDX",
    "CAMPAIGN_SPIRIT_QUESTS",
    "IDX_TO_ATTR_NAME",
    "POE2_CATALYSTS",
    "AttributeNodeInfo",
    "AttributeSwapAction",
    "CatalystSpec",
    "CatalystUpgradeAction",
    "EquippedSlotAudit",
    "FixGapsReport",
    "NetGapAudit",
    "SocketAssignment",
    "SpiritKnapsackPlan",
    "SpiritOption",
    "Stage1AttributePlan",
    "Stage2RunePlan",
    "Stage3CatalystPlan",
    "Stage5PurchaseFallback",
    "StatSnapshot",
    "analyze_live_net_gap",
    "apply_attribute_swaps_live",
    "apply_catalyst_plan_live",
    "apply_catalyst_to_item_raw",
    "apply_rune_plan_live",
    "apply_spirit_plan_live",
    "audit_equipped_slot",
    "build_offline_pack_catalog_options",
    "build_stage5_fallback",
    "collect_live_attribute_nodes",
    "estimate_catalyst_gains",
    "evaluate_live_spirit_options",
    "parse_augment_stat_vector",
    "solve_attribute_respec",
    "solve_catalyst_upgrades",
    "solve_deficit_knapsack",
    "solve_fix_gaps_live",
    "solve_fix_gaps_offline",
    "solve_forward_spirit_knapsack",
    "solve_live_spirit_deficit",
    "solve_rune_allocation",
    "stat_snapshot_from_pob_stats",
]
