# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 0.5.5 zero-engine crafting solver, pool probability engine, and item inspector."""

from __future__ import annotations

from typing import Any

from .models import (
    ActionEval,
    AffixSide,
    BaseItemEntry,
    CraftedSlotLimitError,
    CraftingMod,
    CraftingState,
    CraftPlan,
    ItemRarity,
    LeverEntry,
    ModDomain,
    ModEntry,
    PolicyStep,
    PoolEvalResult,
    RemovedLeverError,
    TargetSpec,
    UnknownBaseError,
    UnknownTargetError,
    WeightConfidence,
    WhittlingReport,
    WhittlingTarget,
)

_INSPECTOR_EXPORTS: frozenset[str] = frozenset({"InspectedMod", "ItemInspectionReport", "inspect_item_text"})
_PRICE_BOOK_EXPORTS: frozenset[str] = frozenset(
    {
        "DEFAULT_CRAFTING_PRICES_EX",
        "PriceBook",
        "fetch_live_price_book",
        "parse_ggg_cx_snapshot",
        "parse_poeninja_exchange_payload",
    }
)


def __getattr__(name: str) -> Any:
    if name in _INSPECTOR_EXPORTS:
        from . import item_inspector as _mod
    elif name == "CraftMdpSolver":
        from . import mdp_solver as _mod
    elif name in ("ModPoolEngine", "exact_three_choice_unveil_prob"):
        from . import pool_engine as _mod
    elif name in ("compute_next_step_outcomes", "filter_pool_with_family_exemption"):
        from . import pool_outcomes as _mod
    elif name in _PRICE_BOOK_EXPORTS:
        from . import price_book as _mod
    elif name in ("EXPECTED_GAME_PATCH", "load_craft_data"):
        from . import weights_loader as _mod
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    val = getattr(_mod, name)
    globals()[name] = val
    return val


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "DEFAULT_CRAFTING_PRICES_EX",
    "EXPECTED_GAME_PATCH",
    "ActionEval",
    "AffixSide",
    "BaseItemEntry",
    "CraftMdpSolver",
    "CraftPlan",
    "CraftedSlotLimitError",
    "CraftingMod",
    "CraftingState",
    "InspectedMod",
    "ItemInspectionReport",
    "ItemRarity",
    "LeverEntry",
    "ModDomain",
    "ModEntry",
    "ModPoolEngine",
    "PolicyStep",
    "PoolEvalResult",
    "PriceBook",
    "RemovedLeverError",
    "TargetSpec",
    "UnknownBaseError",
    "UnknownTargetError",
    "WeightConfidence",
    "WhittlingReport",
    "WhittlingTarget",
    "compute_next_step_outcomes",
    "exact_three_choice_unveil_prob",
    "fetch_live_price_book",
    "filter_pool_with_family_exemption",
    "inspect_item_text",
    "load_craft_data",
    "parse_ggg_cx_snapshot",
    "parse_poeninja_exchange_payload",
]
