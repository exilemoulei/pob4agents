# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 0.5.5 Runes, Soul Cores, Idols, LiquidEmotions bridge, and Pareto Instillation Ranker (L2)."""

from __future__ import annotations

from .catalog import (
    EmotionEntry,
    InstillRecipe,
    RuneEntry,
    RuneInstillCatalog,
    RuneSlotEffect,
    SpiritSupportEntry,
    load_rune_instill_catalog,
)
from .instill_bridge import (
    DEFAULT_CATALYST_PRICES_EX,
    DEFAULT_EMOTION_PRICES_EX,
    DEFAULT_RUNE_PRICES_EX,
    format_ordered_recipe,
    is_excluded_jewel_emotion,
    legacy_to_live_emotion,
    live_to_legacy_emotion,
    normalize_emotion_name,
    normalize_emotion_with_warning,
    price_instill_recipe,
    resolve_emotion_price_ex,
    resolve_rune_price_ex,
)
from .instill_ranker import rank_instillations
from .pareto_ranker import (
    ParetoRankingReport,
    RankedCandidate,
    compute_pareto_frontier,
)
from .rune_ranker import rank_runes_for_slot
from .socket_rules import (
    VALID_MODRUNE_SLOT_KEYS,
    SocketParityResult,
    apply_anoint_to_amulet_raw,
    apply_runes_to_item_raw,
    eligible_runes_for_item,
    extract_existing_anoint,
    parse_socketed_augment_override,
    parse_socketed_effect_modifiers,
    resolve_item_augment_types,
    resolve_rune_effects_for_item,
    strip_anoint_from_amulet_raw,
    strip_replaceable_runes_from_item_raw,
)

__all__ = [
    "DEFAULT_CATALYST_PRICES_EX",
    "DEFAULT_EMOTION_PRICES_EX",
    "DEFAULT_RUNE_PRICES_EX",
    "VALID_MODRUNE_SLOT_KEYS",
    "EmotionEntry",
    "InstillRecipe",
    "ParetoRankingReport",
    "RankedCandidate",
    "RuneEntry",
    "RuneInstillCatalog",
    "RuneSlotEffect",
    "SocketParityResult",
    "SpiritSupportEntry",
    "apply_anoint_to_amulet_raw",
    "apply_runes_to_item_raw",
    "compute_pareto_frontier",
    "eligible_runes_for_item",
    "extract_existing_anoint",
    "format_ordered_recipe",
    "is_excluded_jewel_emotion",
    "legacy_to_live_emotion",
    "live_to_legacy_emotion",
    "load_rune_instill_catalog",
    "normalize_emotion_name",
    "normalize_emotion_with_warning",
    "parse_socketed_augment_override",
    "parse_socketed_effect_modifiers",
    "price_instill_recipe",
    "rank_instillations",
    "rank_runes_for_slot",
    "resolve_emotion_price_ex",
    "resolve_item_augment_types",
    "resolve_rune_effects_for_item",
    "resolve_rune_price_ex",
    "strip_anoint_from_amulet_raw",
    "strip_replaceable_runes_from_item_raw",
]
