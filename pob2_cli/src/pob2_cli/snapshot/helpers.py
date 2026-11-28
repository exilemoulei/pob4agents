"""PoE2 stat catalog, numeric formatting, escape stripping, and batch RPC helpers for ``pob2_cli.snapshot``."""

from __future__ import annotations

import math
import re
from typing import Any

_POB_ESCAPE_RE = re.compile(r"\^(?:x[0-9A-Fa-f]{6}|\d)")

CORE_STAT_KEYS: list[tuple[str, str, str]] = [
    ("FullDPS", "Full DPS", ""),
    ("CombinedDPS", "Combined DPS", ""),
    ("TotalDPS", "Hit DPS", ""),
    ("TotalDotDPS", "DoT DPS", ""),
    ("MinionCombinedDPS", "Minion Combined DPS", ""),
    ("MinionTotalDPS", "Minion Hit DPS", ""),
    ("AverageDamage", "Average Hit", ""),
    ("Speed", "Attack/Cast Rate", "/s"),
    ("CritChance", "Crit Chance", "%"),
    ("CritMultiplier", "Crit Multiplier", "x"),
    ("HitChance", "Hit Chance", "%"),
    ("ManaCost", "Mana Cost", ""),
    ("TotalEHP", "Effective HP (TotalEHP)", ""),
    ("Life", "Maximum Life", ""),
    ("LifeUnreserved", "Unreserved Life", ""),
    ("Mana", "Maximum Mana", ""),
    ("ManaUnreserved", "Unreserved Mana", ""),
    ("Spirit", "Maximum Spirit", ""),
    ("SpiritUnreserved", "Unreserved Spirit", ""),
    ("EnergyShield", "Energy Shield", ""),
    ("Armour", "Armour", ""),
    ("Evasion", "Evasion", ""),
    ("DeflectionChance", "Deflection Chance", "%"),
    ("EffectiveBlockChance", "Block Chance", "%"),
    ("PhysicalMaximumHitTaken", "Phys Max Hit", ""),
    ("FireMaximumHitTaken", "Fire Max Hit", ""),
    ("ColdMaximumHitTaken", "Cold Max Hit", ""),
    ("LightningMaximumHitTaken", "Lightning Max Hit", ""),
    ("ChaosMaximumHitTaken", "Chaos Max Hit", ""),
    ("FireResist", "Fire Resist", "%"),
    ("ColdResist", "Cold Resist", "%"),
    ("LightningResist", "Lightning Resist", "%"),
    ("ChaosResist", "Chaos Resist", "%"),
    ("LifeRegenRecovery", "Net Life Regen", "/s"),
    ("ManaRegenRecovery", "Net Mana Regen", "/s"),
    ("Str", "Strength", ""),
    ("Dex", "Dexterity", ""),
    ("Int", "Intelligence", ""),
]

_OFFENCE_SUMMARY_KEYS = (
    "FullDPS",
    "CombinedDPS",
    "TotalDPS",
    "TotalDotDPS",
    "MinionCombinedDPS",
    "MinionTotalDPS",
    "AverageDamage",
    "Speed",
    "CritChance",
    "CritMultiplier",
    "HitChance",
    "ManaCost",
)

_DEFENCE_SUMMARY_KEYS = (
    "TotalEHP",
    "Life",
    "LifeUnreserved",
    "Mana",
    "ManaUnreserved",
    "Spirit",
    "SpiritUnreserved",
    "EnergyShield",
    "Armour",
    "Evasion",
    "DeflectionChance",
    "EffectiveBlockChance",
    "PhysicalMaximumHitTaken",
    "FireMaximumHitTaken",
    "ColdMaximumHitTaken",
    "LightningMaximumHitTaken",
    "ChaosMaximumHitTaken",
    "FireResist",
    "ColdResist",
    "LightningResist",
    "ChaosResist",
    "LifeRegenRecovery",
    "ManaRegenRecovery",
)


def strip_pob_escapes(text: Any) -> str:
    """Strip PoB colour escape codes (`^7`, `^xRRGGBB`) from a string."""
    if not isinstance(text, str):
        return ""
    return _POB_ESCAPE_RE.sub("", text).strip()


def clean_points_dict(pts: Any) -> dict[str, Any]:
    """Return a shallow copy of a PoB2 `points` dict with PoB color escapes stripped from text fields."""
    if not isinstance(pts, dict):
        return {}
    out = dict(pts)
    if isinstance(out.get("display"), str):
        out["display"] = strip_pob_escapes(out["display"])
    return out


def parse_numeric_or_none(val: Any) -> float | None:
    """Parse int/float or numeric string into float (supports inf/-inf/nan); reject booleans."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def fmt_num(val: Any) -> str:
    """Format a stat value into a compact human/JSON-safe string."""
    parsed = parse_numeric_or_none(val)
    if parsed is None:
        return str(val) if val is not None else "0"
    if math.isnan(parsed):
        return "nan"
    if math.isinf(parsed):
        return "inf" if parsed > 0 else "-inf"
    if abs(parsed) >= 1_000_000:
        return f"{parsed / 1_000_000:.2f}M"
    if abs(parsed) >= 10_000:
        return f"{parsed / 1_000:.1f}k"
    if parsed == math.floor(parsed):
        return str(int(parsed))
    return f"{parsed:.2f}".rstrip("0").rstrip(".")


def fmt_delta(before: Any, after: Any, unit: str = "") -> str:
    """Format numeric stat delta with leading sign and optional percentage."""
    bf = parse_numeric_or_none(before)
    af = parse_numeric_or_none(after)
    if bf is None or af is None or not (math.isfinite(bf) and math.isfinite(af)):
        return f"{fmt_num(before)}{unit} -> {fmt_num(after)}{unit}"
    diff = af - bf
    sign = "+" if diff >= 0 else ""
    pct_str = ""
    if abs(bf) > 1e-6:
        pct = diff / abs(bf) * 100.0
        pct_str = f" ({sign}{pct:.1f}%)"
    return f"{sign}{fmt_num(diff)}{unit}{pct_str}"


def call_batch(
    worker: Any,
    calls: list[tuple[str, dict[str, Any] | None]],
    *,
    timeout: float = 30.0,
    raise_on_error: bool = True,
) -> list[dict[str, Any]]:
    """Execute multiple RPCs in one pipelined batch when supported, else sequential ``worker.call``."""
    if not calls:
        return []
    batch_fn = getattr(worker, "call_batch", None)
    if callable(batch_fn):
        return batch_fn(calls, timeout=timeout, raise_on_error=raise_on_error)
    results: list[dict[str, Any]] = []
    for method, params in calls:
        if raise_on_error:
            results.append(worker.call(method, params or {}))
        else:
            try:
                results.append(worker.call(method, params or {}))
            except Exception as exc:
                results.append({"ok": False, "_error": str(exc), "error": str(exc)})
    return results


def stat_float(val: Any) -> float:
    parsed = parse_numeric_or_none(val)
    if parsed is None or math.isnan(parsed):
        return 0.0
    return parsed


def finite_float(val: Any, default: float = 0.0) -> float:
    parsed = parse_numeric_or_none(val)
    if parsed is None or not math.isfinite(parsed):
        return default
    return float(parsed)


def json_safe_stat(val: Any) -> float | str:
    if val is None:
        return 0.0
    parsed = parse_numeric_or_none(val)
    if parsed is None:
        return str(val) if isinstance(val, str) and val.strip() else 0.0
    if math.isfinite(parsed):
        return parsed
    return fmt_num(parsed)


def safe_min_ele_max_hit(stats: dict[str, Any]) -> float | str | None:
    vals: list[float] = []
    for key in ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken"):
        raw = stats.get(key)
        if raw is None:
            continue
        parsed = parse_numeric_or_none(raw)
        if parsed is None or math.isnan(parsed):
            continue
        vals.append(parsed)
    if not vals:
        return None
    m = min(vals)
    return m if math.isfinite(m) else fmt_num(m)


def extract_points_used(info: Any = None, stats: Any = None, pts: Any = None) -> int | None:
    raw: Any = pts.get("used") if isinstance(pts, dict) else None
    if raw is None and isinstance(info, dict) and isinstance(info.get("points"), dict):
        raw = info["points"].get("used")
    if raw is None and isinstance(stats, dict):
        raw = stats.get("pointsUsed")
    if raw is None or isinstance(raw, bool):
        return None
    parsed = parse_numeric_or_none(raw)
    return int(parsed) if (parsed is not None and math.isfinite(parsed)) else None
