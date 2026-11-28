# SPDX-License-Identifier: GPL-3.0-only
"""Static stat vector and negative side-effect analysis for socketable augments (L2)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .gap_models import StatSnapshot

STAT_KEYS: tuple[str, ...] = ("str", "dex", "int", "fire", "cold", "lightning", "chaos", "spirit")

_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\+(\d+)%\s+to\s+Fire\s+Resistance", re.IGNORECASE), ("fire",)),
    (re.compile(r"\+(\d+)%\s+to\s+Cold\s+Resistance", re.IGNORECASE), ("cold",)),
    (re.compile(r"\+(\d+)%\s+to\s+Lightning\s+Resistance", re.IGNORECASE), ("lightning",)),
    (re.compile(r"\+(\d+)%\s+to\s+Chaos\s+Resistance", re.IGNORECASE), ("chaos",)),
    (re.compile(r"\+(\d+)%\s+to\s+all\s+Elemental\s+Resistances", re.IGNORECASE), ("fire", "cold", "lightning")),
    (re.compile(r"\+(\d+)\s+to\s+Strength\b", re.IGNORECASE), ("str",)),
    (re.compile(r"\+(\d+)\s+to\s+Dexterity\b", re.IGNORECASE), ("dex",)),
    (re.compile(r"\+(\d+)\s+to\s+Intelligence\b", re.IGNORECASE), ("int",)),
    (re.compile(r"\+(\d+)\s+to\s+all\s+Attributes\b", re.IGNORECASE), ("str", "dex", "int")),
    (re.compile(r"\+(\d+)\s+to\s+Spirit\b", re.IGNORECASE), ("spirit",)),
)

_NEG_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"(?:^|:\s*)-(\d+)%\s+to\s+Fire\s+Resistance", re.IGNORECASE), ("fire",)),
    (re.compile(r"(?:^|:\s*)-(\d+)%\s+to\s+Cold\s+Resistance", re.IGNORECASE), ("cold",)),
    (re.compile(r"(?:^|:\s*)-(\d+)%\s+to\s+Lightning\s+Resistance", re.IGNORECASE), ("lightning",)),
    (re.compile(r"(?:^|:\s*)-(\d+)%\s+to\s+Chaos\s+Resistance", re.IGNORECASE), ("chaos",)),
    (re.compile(r"(?:^|:\s*)-(\d+)%\s+to\s+all\s+Elemental\s+Resistances", re.IGNORECASE), ("fire", "cold", "lightning")),
    (re.compile(r"(?:^|:\s*)-(\d+)\s+to\s+Strength\b", re.IGNORECASE), ("str",)),
    (re.compile(r"(?:^|:\s*)-(\d+)\s+to\s+Dexterity\b", re.IGNORECASE), ("dex",)),
    (re.compile(r"(?:^|:\s*)-(\d+)\s+to\s+Intelligence\b", re.IGNORECASE), ("int",)),
    (re.compile(r"(?:^|:\s*)-(\d+)\s+to\s+all\s+Attributes\b", re.IGNORECASE), ("str", "dex", "int")),
    (re.compile(r"(?:^|:\s*)-(\d+)\s+to\s+Spirit\b(?!\s+per\b)", re.IGNORECASE), ("spirit",)),
)

_SPIRIT_PER_LEVEL_NEG_RE = re.compile(r"(?:^|:\s*)-(\d+)\s+to\s+Spirit\s+per\s+(\d+)\s+Levels\b", re.IGNORECASE)
_NEG_MAX_RES_RE = re.compile(
    r"(?:(?:^|:\s*)-\s*(\d+)%\s+to|(\d+)%\s+(?:reduced|less))\s+(?:all\s+)?Maximum\s+"
    r"(Elemental|Fire|Cold|Lightning|Chaos)?\s*Resistances?\b",
    re.IGNORECASE,
)
_INC_ATTR_REQ_RE = re.compile(
    r"(\d+)%?\s+(?:increased|additional|to)\s+(?:all\s+)?(?:Attribute|Strength|Dexterity|Intelligence)\s+Requirements\b",
    re.IGNORECASE,
)
_POOL_PENALTY_RE = re.compile(
    r"(?:(\d+)%\s+(?:reduced|less)\s+maximum\s+(?:Life|Energy\s+Shield|Mana|Effective\s+Hit\s+Pool)\b"
    r"|(?:^|:\s*)-\s*(\d+)\s+to\s+maximum\s+(?:Life|Energy\s+Shield|Mana)\b"
    r"|Lose\s+(\d+)%\s+of\s+maximum\s+(?:Life|Energy\s+Shield|Mana)\b)",
    re.IGNORECASE,
)
_SPIRIT_PENALTY_RE = re.compile(
    r"(\d+)%\s+(?:reduced|less)\s+(?:Spirit\b|(?:[A-Za-z]+\s+){0,3}Reservation\s+Efficiency\b)",
    re.IGNORECASE,
)
_DPS_PENALTY_RE = re.compile(
    r"(?:(\d+)%\s+less\s+(?:(?:Attack|Spell|Elemental|Physical|Chaos|Global)\s+)?Damage\b"
    r"|You\s+have\s+no\s+Critical\s+Damage\s+Bonus\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AugmentSideEffects:
    """Negative side-effect profile for a candidate socketable augment."""

    max_res_deltas: dict[str, int]
    attr_req_increase: int
    penalty_ex: float
    penalty_notes: tuple[str, ...]

    @property
    def reduces_max_res(self) -> bool:
        return any(v < 0 for v in self.max_res_deltas.values())

    def format_max_res_deltas(self) -> str:
        return ", ".join(f"{k}Max {v:+d}%" for k, v in self.max_res_deltas.items() if v < 0)


def inspect_augment_side_effects(mods: tuple[str, ...], *, multiplier: float = 1.0) -> AugmentSideEffects:
    """Inspect candidate rune/soul core/idol modifier lines for negative maxRes, req, pool, or Spirit side effects."""
    max_res_deltas: dict[str, int] = {}
    attr_req_inc = 0
    penalty_ex = 0.0
    notes: list[str] = []

    for ln in mods:
        clean = ln.strip()
        low = clean.lower()
        for m_mr in _NEG_MAX_RES_RE.finditer(clean):
            raw_v = int(m_mr.group(1) or m_mr.group(2) or 0)
            if raw_v <= 0:
                continue
            drop = max(1, round(raw_v * multiplier))
            elem_tok = (m_mr.group(3) or "elemental").lower()
            targets = ("fire", "cold", "lightning") if elem_tok == "elemental" else (elem_tok,)
            for tk in targets:
                max_res_deltas[tk] = max_res_deltas.get(tk, 0) - drop
            notes.append(clean)

        is_beneficial_def = (
            "on you" in low
            or "against you" in low
            or "damage taken" in low
            or low.startswith("take ")
            or "enemies " in low
            or "delay" in low
        )
        if is_beneficial_def:
            continue

        m_req = _INC_ATTR_REQ_RE.search(clean)
        if m_req and "reduced" not in low and "less" not in low:
            attr_req_inc += max(1, round(int(m_req.group(1)) * multiplier))
            penalty_ex += 150.0
            notes.append(clean)
        if _POOL_PENALTY_RE.search(clean):
            penalty_ex += 180.0
            notes.append(clean)
        if _SPIRIT_PENALTY_RE.search(clean):
            penalty_ex += 180.0
            notes.append(clean)
        if _DPS_PENALTY_RE.search(clean):
            penalty_ex += 120.0
            notes.append(clean)

    return AugmentSideEffects(
        max_res_deltas=max_res_deltas,
        attr_req_increase=attr_req_inc,
        penalty_ex=penalty_ex,
        penalty_notes=tuple(notes),
    )


def format_max_res_drop_warning(
    rune_name: str,
    baseline: StatSnapshot,
    after: StatSnapshot,
    *,
    slot_name: str = "",
) -> str | None:
    """Format `[rune-reduces-max-res]` warning if any elemental/chaos MaxRes dropped below *baseline*."""
    drops: list[str] = []
    for label, b_val, a_val in (
        ("fireMax", baseline.fire_max, after.fire_max),
        ("coldMax", baseline.cold_max, after.cold_max),
        ("lightningMax", baseline.lightning_max, after.lightning_max),
        ("chaosMax", baseline.chaos_max, after.chaos_max),
    ):
        if a_val < b_val - 1e-6:
            drops.append(f"{label} {b_val:.0f}%->{a_val:.0f}% ({a_val - b_val:+.0f}%)")
    if not drops:
        return None
    slot_tag = f" ({slot_name})" if slot_name else ""
    return f"[rune-reduces-max-res] Rejected `{rune_name}`{slot_tag}: lowers maximum resistance ({', '.join(drops)})."


def parse_augment_stat_vector(
    mods: tuple[str, ...],
    *,
    multiplier: float = 1.0,
    char_level: int = 90,
) -> dict[str, int]:
    """Extract net static `(str, dex, int, fire, cold, lightning, chaos, spirit)` gains scaled by *multiplier*."""
    vec = {k: 0 for k in STAT_KEYS}
    for ln in mods:
        low = ln.lower()
        if "while " in low or "if you " in low or "enemies " in low or "minions " in low:
            continue
        m_sp_lvl = _SPIRIT_PER_LEVEL_NEG_RE.search(ln)
        if m_sp_lvl:
            per_val = int(m_sp_lvl.group(1))
            per_lvls = max(1, int(m_sp_lvl.group(2)))
            vec["spirit"] -= int((max(1, char_level) // per_lvls) * per_val * multiplier)
            continue
        for pat, keys in _NEG_PATTERNS:
            for m in pat.finditer(ln):
                val = int(int(m.group(1)) * multiplier)
                for k in keys:
                    vec[k] -= val
        if ln.strip().startswith("-"):
            continue
        for pat, keys in _PATTERNS:
            for m in pat.finditer(ln):
                val = int(int(m.group(1)) * multiplier)
                for k in keys:
                    vec[k] += val
    return vec


__all__ = [
    "STAT_KEYS",
    "AugmentSideEffects",
    "format_max_res_drop_warning",
    "inspect_augment_side_effects",
    "parse_augment_stat_vector",
]
