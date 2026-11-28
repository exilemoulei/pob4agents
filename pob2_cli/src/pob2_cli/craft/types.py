# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Core crafting enums, error types, value-range parser, and catalog entries (L2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..errors import DaemonRequestError

_MOD_PAREN_RANGE_RE = re.compile(r"([+-]?)\(\s*([+-]?\d+(?:\.\d+)?)\s*-\s*([+-]?\d+(?:\.\d+)?)\s*\)")
_MOD_ADDS_FIXED_RE = re.compile(r"\badds\s+([+-]?\d+(?:\.\d+)?)\s+to\s+([+-]?\d+(?:\.\d+)?)\b", re.IGNORECASE)
_MOD_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])([+-]?\d+(?:\.\d+)?)")

MARK_OF_ABYSS_MOD_ID = "MarkOfTheAbyssalLord"

CRAFTED_SOURCE_POOLS: frozenset[str] = frozenset(
    {"essence", "perfect_essence", "alloy", "liquid", "crafted"}
)


def _clean_num(val: float) -> int | float:
    return int(val) if val.is_integer() else round(val, 4)


def extract_mod_value_range(stat_text: str) -> list[Any] | None:
    """Extract `[min, max]` (or `[[min1, max1], ...]` for multi-stat mods) from *stat_text*."""
    if not stat_text:
        return None
    paren_hits = list(_MOD_PAREN_RANGE_RE.finditer(stat_text))
    if paren_hits:
        pairs: list[list[int | float]] = []
        for m in paren_hits:
            outer_neg = m.group(1) == "-"
            ra, rb = m.group(2), m.group(3)
            fa = -float(ra) if (outer_neg and not ra.startswith(("-", "+"))) else float(ra)
            fb = -float(rb) if (outer_neg and not rb.startswith(("-", "+"))) else float(rb)
            lo, hi = min(fa, fb), max(fa, fb)
            pairs.append([_clean_num(lo), _clean_num(hi)])
        return pairs[0] if len(pairs) == 1 else pairs

    fixed_pairs: list[list[int | float]] = []
    for raw_ln in stat_text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        m_adds = _MOD_ADDS_FIXED_RE.search(ln)
        if m_adds:
            va, vb = _clean_num(float(m_adds.group(1))), _clean_num(float(m_adds.group(2)))
            fixed_pairs.extend(([va, va], [vb, vb]))
            continue
        m_num = _MOD_NUM_RE.search(ln)
        if m_num:
            vn = _clean_num(float(m_num.group(1)))
            fixed_pairs.append([vn, vn])
    if not fixed_pairs:
        return None
    return fixed_pairs[0] if len(fixed_pairs) == 1 else fixed_pairs


class AffixSide(StrEnum):
    PREFIX = "prefix"
    SUFFIX = "suffix"
    IMPLICIT = "implicit"
    ENCHANT = "enchant"
    RUNE = "rune"
    NONE = "none"


class ItemRarity(StrEnum):
    NORMAL = "NORMAL"
    MAGIC = "MAGIC"
    RARE = "RARE"
    UNIQUE = "UNIQUE"


class ModDomain(StrEnum):
    EXPLICIT = "explicit"
    IMPLICIT = "implicit"
    CRAFTED = "crafted"
    FRACTURED = "fractured"
    DESECRATED = "desecrated"
    ENCHANT = "enchant"
    RUNE = "rune"
    CORRUPTED = "corrupted"
    MUTATED = "mutated"


class WeightConfidence(StrEnum):
    """Four-tier honest weight confidence scale for PoE2 0.5.5 crafting probabilities."""

    EXACT_DETERMINISTIC = "exact_deterministic"
    VERIFIED_COMMUNITY = "verified_community"
    EMPIRICAL_ESTIMATE = "empirical_estimate"
    UNMEASURED = "unmeasured"


_CONFIDENCE_RANK: dict[WeightConfidence, int] = {
    WeightConfidence.EXACT_DETERMINISTIC: 3,
    WeightConfidence.VERIFIED_COMMUNITY: 2,
    WeightConfidence.EMPIRICAL_ESTIMATE: 1,
    WeightConfidence.UNMEASURED: 0,
}


def lowest_confidence(values: list[WeightConfidence]) -> WeightConfidence:
    if not values:
        return WeightConfidence.UNMEASURED
    return min(values, key=lambda c: _CONFIDENCE_RANK[c])


class RemovedLeverError(ValueError):
    """Raised when a PoE1 orb or PoE2 0.4/0.5 removed crafting lever is requested."""

    def __init__(self, lever_name: str, reason: str, alternatives: str) -> None:
        self.lever_name = lever_name
        self.reason = reason
        self.alternatives = alternatives
        super().__init__(f"{lever_name}: {reason} 0.5.5 合法替代: {alternatives}")


class CraftedSlotLimitError(ValueError):
    """Raised when violating PoE2 0.5.0+ single Crafted modifier per item limit."""


class UnknownBaseError(DaemonRequestError, ValueError):
    """Raised when an item base or class slug is not in the PoE2 0.5.5 whitelist."""

    error_kind: str = "unknown_base"

    def __init__(self, query: str, suggestions: tuple[str, ...] = ()) -> None:
        self.query = query
        self.suggestions = tuple(suggestions)
        sug_txt = f" Closest valid PoE2 bases: {', '.join(self.suggestions)}" if self.suggestions else ""
        super().__init__(
            f"[POE2-UNKNOWN-BASE] Unknown PoE2 0.5.5 base item or class slug: {query!r}.{sug_txt}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": "poe2",
            "ok": False,
            "errorKind": self.error_kind,
            "query": self.query,
            "error": str(self),
            "suggestions": list(self.suggestions),
        }


class UnknownTargetError(DaemonRequestError, ValueError):
    """Raised when a `--want` modifier query cannot be resolved in the base's 0.5.5 pool."""

    error_kind: str = "unknown_target"

    def __init__(self, query: str, base_slug: str, suggestions: tuple[str, ...] = (), *, detail: str = "") -> None:
        self.query = query
        self.base_slug = base_slug
        self.suggestions = tuple(suggestions)
        msg = detail or f"Could not match modifier target {query!r} on `{base_slug}`."
        sug_txt = f" Valid examples for `{base_slug}`: {', '.join(self.suggestions)}" if self.suggestions else ""
        super().__init__(f"[POE2-UNKNOWN-TARGET] {msg}{sug_txt}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": "poe2",
            "ok": False,
            "errorKind": self.error_kind,
            "query": self.query,
            "baseSlug": self.base_slug,
            "error": str(self),
            "suggestions": list(self.suggestions),
        }


@dataclass(frozen=True, slots=True)
class BaseItemEntry:
    id: str
    name: str
    item_class: str
    poe2db_slug: str
    drop_level: int
    max_prefixes: int
    max_suffixes: int
    tags: tuple[str, ...] = ("default",)
    implicits: tuple[str, ...] = ()
    has_measured_weights: bool = True


@dataclass(frozen=True, slots=True)
class ModEntry:
    mod_id: str
    mod_type: str
    family_id: str
    families: tuple[str, ...]
    base_group_slug: str
    source_pool: str
    affix_side: AffixSide
    affix_name: str
    internal_rank_asc: int
    display_tier: int
    required_level: int
    weight: int
    weight_confidence: WeightConfidence
    boss_tag: str | None = None
    currency_name: str | None = None
    is_alloy: bool = False
    removes_mod: bool = False
    adds_tags: tuple[str, ...] = ()
    mod_tags: tuple[str, ...] = ()
    stat_text: str = ""

    @property
    def is_crafted_pool(self) -> bool:
        if self.mod_id == MARK_OF_ABYSS_MOD_ID or "Mark of the Abyssal Lord" in self.stat_text:
            return False
        return self.source_pool in CRAFTED_SOURCE_POOLS or self.is_alloy


@dataclass(frozen=True, slots=True)
class LeverEntry:
    lever_id: str
    name: str
    category: str
    is_active_drop_in_patch: bool
    applies_to_rarities: str
    target_classes: tuple[str, ...] = ()
    max_item_level: int | None = None
    min_mod_level_floor: int | None = None
    omen_side_constraint: AffixSide | None = None
    omen_boss_constraint: str | None = None
    forced_mods: dict[str, str] = field(default_factory=dict)
    description: str = ""


__all__ = [
    "CRAFTED_SOURCE_POOLS",
    "MARK_OF_ABYSS_MOD_ID",
    "AffixSide",
    "BaseItemEntry",
    "CraftedSlotLimitError",
    "ItemRarity",
    "LeverEntry",
    "ModDomain",
    "ModEntry",
    "RemovedLeverError",
    "UnknownBaseError",
    "UnknownTargetError",
    "WeightConfidence",
    "extract_mod_value_range",
    "lowest_confidence",
]
