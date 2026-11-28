# SPDX-License-Identifier: GPL-3.0-only
"""Fast live PoB2 item-swap evaluator (`item_tooltip` ~15ms/item with single-CoW fallback) (L2)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..snapshot import BuildScope, BuildTransaction

_POB_COLOR_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")
_DELTA_LINE_RE = re.compile(r"^([+-][\d,]+(?:\.\d+)?)\s+(.+?)(?:\s*\(([+-]?[\d.]+)%\))?$")
_PER_DENOM_RE = re.compile(r"\bper\s+\d[\d,.]*")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_LVL_RE = re.compile(r"\+(\d+)\s+to\s+level\s+of")
_RAGE_RE = re.compile(r"\+(\d+)\s+to\s+maximum\s+rage")
_ADDS_RE = re.compile(r"adds\s+(\d+)\s+to\s+(\d+)")
_NUM_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")

_NON_DPS_SKIP = (
    "defend with", "damage taken", "taken from", "hits against you", "against critical hits",
    "reduced damage", "less damage", "reduced attack speed", "reduced cast speed",
    "reduced critical", "reduced accuracy", "mana cost", "life cost", "attribute requirement",
    "item rarity", "light radius", "flask", "charm", "block", "recoup", "regeneration",
    "leech", "thorns", "ailment threshold", "stun threshold", "movement speed",
    "cannot be ", "chance to avoid", "curse effect on you", "armour", "evasion", "energy shield",
    "resistance", "maximum life", "maximum mana",
)
_CONDITIONAL_PHRASES = (
    "against enemies that are", "enemies that are on full life", "on low life",
    "while surrounded", "against demons", "against beasts", "against humanoids",
    "against constructs", "against heavy stunned", "if you have been",
    "while affected by", "while you have no", "while you have an ally",
    "in your presence", "against bleeding", "against poisoned", "against ignited",
    "against frozen", "against shocked", "against electrocuted", "against pinned",
    "against immobilised", "if you've dealt a critical", "if you have used",
    "if you've killed", "while stationary", "while moving",
    "while channelling", "while on low", "for each", "per power charge",
    "per frenzy charge", "per endurance charge", "while dual wielding",
)
_SPELL_SKIP_WORDS = (
    "attack", "melee", "weapon", "bow", "crossbow", "spear", "mace", "quarterstaff",
    "quarterstaves", "axe", "sword", "claw", "dagger", "flail", "talisman", "unarmed",
    "accuracy", "impale", "bleed", "rage", "warcry", "slam", "strike", "companion",
    "minion", "totem", "grenade", "ballista", "banner", "shapeshift", "shield",
)
_ATTACK_SKIP_WORDS = (
    "spell", "cast speed", "wand", "staves", "staff", "curse", "minion", "totem",
    "trap", "mine", "offering", "companion",
)
_WEAPON_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("crossbow", "crossbow"), ("bow", "bow"), ("spear", "spear"), ("mace", "mace"),
    ("quarterstaff", "quarterstaff"), ("quarterstaves", "quarterstaff"),
    ("axe", "axe"), ("sword", "sword"), ("claw", "claw"), ("dagger", "dagger"),
    ("flail", "flail"), ("talisman", "talisman"), ("unarmed", "unarmed"),
    ("shapeshift", "shapeshift"), ("two handed", "two_handed"), ("one handed", "one_handed"),
)
_DOT_WORDS = ("bleed", "ignite", "poison", "ailment", "damage over time", "damaging ailment")
_ELEMENTS = ("physical", "fire", "cold", "lightning", "chaos", "elemental")


@dataclass(frozen=True, slots=True)
class BuildProfile:
    """Lightweight offensive/weapon/element fingerprint read from live PoB2 (<3ms)."""

    is_attack: bool
    is_spell: bool
    weapon_tags: frozenset[str]
    elem_share: dict[str, float]
    crit_scale: float
    uses_rage: bool
    uses_dot: bool
    uses_area: bool
    uses_minion: bool
    low_accuracy: bool


def _weapon_type_tags(wtype: str) -> set[str]:
    low = (wtype or "").strip().lower()
    tags: set[str] = set()
    for kw in ("crossbow", "bow", "spear", "mace", "quarterstaff", "axe", "sword", "claw", "dagger", "flail", "talisman", "wand", "staff", "sceptre", "shield", "focus", "quiver"):
        if kw in low:
            tags.add(kw)
    if "talisman" in low:
        tags.add("shapeshift")
    if "two handed" in low or low in ("bow", "crossbow", "quarterstaff", "staff", "talisman"):
        tags.add("two_handed")
    elif low and low not in ("shield", "focus", "quiver"):
        tags.add("one_handed")
    return tags


def extract_build_profile(worker: Any, *, warnings: list[str] | None = None) -> BuildProfile | None:
    """Extract live build offensive profile in <3ms from `get_stats` + `list_items`."""
    try:
        stats = worker.call("get_stats", {}).get("stats") or {}
    except Exception as exc:
        if warnings is not None:
            warnings.append(
                f"[live-eval-degraded] get_stats failed during build profile extraction ({exc}); fell back to generic stat weights."
            )
        return None
    is_attack = float(stats.get("MainHandTotalDPS") or stats.get("MainHandAverageHit") or 0.0) > 0.0
    is_spell = not is_attack
    pref = "MainHand" if is_attack else ""
    raw_el = {
        el: float(stats.get(f"{pref}{el.title()}HitAverage") or stats.get(f"{el.title()}HitAverage") or 0.0)
        for el in ("physical", "fire", "cold", "lightning", "chaos")
    }
    el_sum = sum(v for v in raw_el.values() if v > 0.0)
    if el_sum > 0.0:
        elem_share = {k: max(0.0, v) / el_sum for k, v in raw_el.items()}
    else:
        elem_share = {k: 1.0 for k in raw_el}
    elem_share["elemental"] = min(1.0, elem_share["fire"] + elem_share["cold"] + elem_share["lightning"])

    crit_ch = float(stats.get(f"{pref}CritChance") or stats.get("CritChance") or 0.0)
    crit_scale = 0.0 if crit_ch <= 0.0 else (0.5 + min(2.0, crit_ch / 20.0))
    uses_rage = float(stats.get("MaximumRage") or 0.0) > 0.0
    tot_dps = float(stats.get("CombinedDPS") or stats.get("TotalDPS") or 0.0)
    dot_dps = float(stats.get("TotalDotDPS") or stats.get("IgniteDPS") or stats.get("BleedDPS") or stats.get("PoisonDPS") or 0.0)
    uses_dot = (dot_dps / tot_dps > 0.12) if tot_dps > 0.0 else False
    uses_area = float(stats.get("AreaOfEffectRadius") or 0.0) > 0.0
    uses_minion = float(stats.get("MinionTotalDPS") or 0.0) > 0.0
    hit_ch = float(stats.get(f"{pref}HitChance") or stats.get("HitChance") or 100.0)
    low_accuracy = is_attack and hit_ch < 98.0

    wtags: set[str] = set()
    try:
        listing = worker.call("list_items", {})
        by_id = {int(it["id"]): it for it in (listing.get("items") or ()) if isinstance(it, dict) and "id" in it}
        for sl in listing.get("slots") or ():
            if not isinstance(sl, dict):
                continue
            sname = str(sl.get("name") or "")
            if sname in ("Weapon 1", "Weapon 2"):
                sid = int(sl.get("selItemId") or sl.get("selectedId") or sl.get("itemId") or 0)
                if sid in by_id:
                    wtags.update(_weapon_type_tags(str(by_id[sid].get("type") or "")))
    except Exception as exc:
        if warnings is not None:
            warnings.append(
                f"[live-eval-degraded] list_items failed during weapon tag detection ({exc}); fell back to weapon-agnostic profile."
            )
    if not wtags and is_attack:
        wtags.add("unarmed")
    return BuildProfile(
        is_attack=is_attack, is_spell=is_spell, weapon_tags=frozenset(wtags),
        elem_share=elem_share, crit_scale=crit_scale, uses_rage=uses_rage,
        uses_dot=uses_dot, uses_area=uses_area, uses_minion=uses_minion, low_accuracy=low_accuracy,
    )


def score_stat_lines(lines: tuple[str, ...], metric: str, profile: BuildProfile | None = None) -> float:
    """Fast deterministic stat-line scorer (offline heuristic or build-aware live shortlist)."""
    m_low = (metric or "CombinedDPS").lower()
    score = 0.0
    is_def = any(k in m_low for k in ("ehp", "life", "res", "def", "armour", "evasion", "shield"))
    is_spirit = "spirit" in m_low
    for ln in lines:
        low = ln.lower()
        cleaned = _PER_DENOM_RE.sub("", low)
        if is_def:
            if any(k in low for k in ("resistance", "maximum life", "energy shield", "armour", "evasion", "deflection", "block")):
                nums = [float(x) for x in _NUM_RE.findall(cleaned)]
                mag = sum(abs(n) for n in nums) / max(1, len(nums)) if nums else 10.0
                score += mag * (1.8 if "all elemental" in low else 1.0)
            continue
        if is_spirit:
            if "spirit" in low:
                nums = [float(x) for x in _NUM_RE.findall(cleaned)]
                score += (sum(abs(n) for n in nums) / max(1, len(nums)) if nums else 10.0) * 5.0
            continue

        # DPS scoring: reject purely defensive/negative lines (allow enemy resistance / penetration / armour break)
        enemy_debuff = any(k in low for k in ("penetrates", "exposure", "enemy", "armour break", "broken armour"))
        if not enemy_debuff and any(k in low for k in _NON_DPS_SKIP):
            continue

        # Extract meaningful stat magnitude (ignore `per 400` denominators and raw flat +250 Accuracy)
        m_lvl = _LVL_RE.search(cleaned)
        m_rage = _RAGE_RE.search(cleaned)
        m_add = _ADDS_RE.search(cleaned)
        pcts = [float(x) for x in _PCT_RE.findall(cleaned)]
        if m_lvl:
            base = float(m_lvl.group(1)) * 45.0
        elif m_rage:
            if profile is not None and not profile.uses_rage:
                continue
            base = float(m_rage.group(1)) * 12.0
        elif m_add:
            base = (float(m_add.group(1)) + float(m_add.group(2))) * 0.3
        elif pcts:
            base = sum(pcts) / len(pcts)
        elif "accuracy" in low and (profile is None or profile.low_accuracy):
            nums = [float(x) for x in _NUM_RE.findall(cleaned)]
            base = min(20.0, (nums[0] * 0.05) if nums else 0.0)
        else:
            continue

        # Must carry an offensive stat keyword
        if any(k in low for k in ("attack speed", "cast speed", "skill speed")):
            mult = 2.6
        elif any(k in low for k in ("more ", "as extra", "penetrates", "exposure", "faster")):
            mult = 3.2
        elif "critical damage" in low or "critical strike multiplier" in low:
            mult = 1.1 * (profile.crit_scale if profile is not None else 1.0)
        elif "critical" in low:
            mult = 0.45 * (profile.crit_scale if profile is not None else 1.0)
        elif any(k in low for k in ("damage", "level of", "rage", "wither", "shock", "impale", "ailment", "bleed", "ignite", "poison", "accuracy")):
            mult = 1.0
        else:
            continue

        if mult <= 0.0:
            continue

        if profile is not None:
            if profile.is_spell:
                if any(re.search(rf"\b{w}s?\b", low) for w in _SPELL_SKIP_WORDS):
                    continue
                if "spell" in low or "cast speed" in low:
                    mult *= 1.35
            else:
                if not profile.uses_minion and any(re.search(rf"\b{w}s?\b", low) for w in _ATTACK_SKIP_WORDS):
                    continue
                skip_w = False
                for kw, tag in _WEAPON_KEYWORDS:
                    if re.search(rf"\b{kw}s?\b", low):
                        if tag not in profile.weapon_tags:
                            skip_w = True
                            break
                        mult *= 1.25
                if skip_w:
                    continue
                if "attack" in low:
                    mult *= 1.15
            if not profile.uses_dot and any(w in low for w in _DOT_WORDS):
                mult *= 0.1
            matched_els = [el for el in _ELEMENTS if re.search(rf"\b{el}\b", low)]
            if matched_els:
                el_w = max(profile.elem_share.get(el, 0.0) for el in matched_els)
                if el_w < 0.05:
                    continue
                mult *= 0.4 + 0.8 * el_w
            if "area damage" in low and not profile.uses_area:
                mult *= 0.15

        if any(p in low for p in _CONDITIONAL_PHRASES):
            mult *= 0.12

        score += base * mult
    return score


def _strip_color(text: str) -> str:
    return _POB_COLOR_RE.sub("", text or "").strip()


def parse_tooltip_metric_delta(tooltip_res: dict[str, Any], metric: str) -> tuple[float, bool]:
    """Parse exact stat delta from PoB2 `item_tooltip(..., compare=True)` lines.

    Returns `(delta_value, metric_supported)`. When `metric_supported` is True and the stat did not
    appear in the diff block, delta is legitimately `0.0`.
    """
    lines = tooltip_res.get("lines")
    if not isinstance(lines, list) or not lines:
        return 0.0, False

    m_low = (metric or "CombinedDPS").strip().lower()
    if m_low in ("combineddps", "totaldps", "fulldps", "dps"):
        kind = "full_dps" if m_low == "fulldps" else "dps"
    elif "ehp" in m_low:
        kind = "ehp"
    elif "spirit" in m_low:
        kind = "spirit"
    else:
        return 0.0, False

    # Locate the comparison header ("Equipping this item..." or "(replacing ...)")
    diff_start = 0
    for i, row in enumerate(lines):
        raw = _strip_color(str(row.get("raw") or row.get("text") or "") if isinstance(row, dict) else str(row))
        low = raw.lower()
        if "equipping this" in low or low.startswith("(replacing "):
            diff_start = i + 1

    best_prio = -1
    best_val = 0.0
    for row in lines[diff_start:]:
        raw = _strip_color(str(row.get("raw") or row.get("text") or "") if isinstance(row, dict) else str(row))
        dm = _DELTA_LINE_RE.match(raw)
        if not dm:
            continue
        val = float(dm.group(1).replace(",", ""))
        label = dm.group(2).strip().lower()
        prio = -1
        if kind == "full_dps":
            if label == "full dps":
                prio = 4
            elif label.startswith(("total dps inc.", "combined dps")):
                prio = 3
            elif label == "total dps":
                prio = 2
            elif label == "hit dps":
                prio = 1
        elif kind == "dps":
            if label.startswith(("total dps inc.", "combined dps")):
                prio = 4
            elif label == "total dps":
                prio = 3
            elif label == "hit dps":
                prio = 2
            elif label == "full dps":
                prio = 1
        elif kind == "ehp":
            if "effective hit pool" in label or label == "total ehp":
                prio = 3
        elif kind == "spirit":
            if label in ("total spirit", "spirit"):
                prio = 3
            elif label == "unreserved spirit":
                prio = 2
        if prio > best_prio:
            best_prio = prio
            best_val = val
    return best_val, True


def get_allocated_node_ids(worker: Any, *, warnings: list[str] | None = None) -> set[int]:
    """Return currently allocated passive tree node IDs (<1ms)."""
    try:
        ts = worker.call("get_tree_state", {})
        return {int(nid) for nid in (ts.get("allocatedNodes") or ())}
    except Exception as exc:
        if warnings is not None:
            warnings.append(
                f"[live-eval-degraded] get_tree_state failed ({exc}); allocated-node filter skipped."
            )
        return set()


def read_metric_value(worker: Any, metric: str) -> float:
    stats = worker.call("get_stats", {}).get("stats") or {}
    if metric in stats:
        return float(stats[metric] or 0.0)
    for k, v in stats.items():
        if k.lower() == metric.lower():
            return float(v or 0.0)
    return float(stats.get("CombinedDPS") or stats.get("TotalDPS") or 0.0)


def eval_item_raw_deltas_live(
    worker: Any,
    slot_name: str,
    raw_items: list[str],
    metric: str,
    baseline: float,
    *,
    warnings: list[str] | None = None,
) -> list[float]:
    """Evaluate exact metric deltas vs current equipped *slot_name* for each raw item text.

    Uses zero-mutation `item_tooltip(..., compare=True)` (~13ms/item) when supported, falling back
    to a single outer `BuildTransaction` with `tx.restore(rebuild=False)` if needed.
    """
    if not raw_items:
        return []

    # Probe first item with zero-mutation item_tooltip
    deltas: list[float] = []
    use_tooltip = True
    try:
        tt0 = worker.call("item_tooltip", {"raw": raw_items[0], "slotName": slot_name, "compare": True})
        if isinstance(tt0, dict):
            d0, ok0 = parse_tooltip_metric_delta(tt0, metric)
            if ok0:
                deltas.append(d0)
            else:
                use_tooltip = False
        else:
            use_tooltip = False
            if warnings is not None:
                warnings.append(
                    f"[live-eval-degraded] item_tooltip returned non-dict payload on `{slot_name}`; fell back to BuildTransaction CoW."
                )
    except Exception as exc:
        use_tooltip = False
        if warnings is not None:
            warnings.append(
                f"[live-eval-degraded] item_tooltip probe failed on `{slot_name}` ({exc}); fell back to BuildTransaction CoW."
            )

    if use_tooltip:
        try:
            for raw in raw_items[1:]:
                tt = worker.call("item_tooltip", {"raw": raw, "slotName": slot_name, "compare": True})
                d, _ok = parse_tooltip_metric_delta(tt if isinstance(tt, dict) else {}, metric)
                deltas.append(d)
            return deltas
        except Exception as exc:
            if warnings is not None:
                warnings.append(
                    f"[live-eval-degraded] item_tooltip batch failed mid-run on `{slot_name}` ({exc}); fell back to BuildTransaction CoW."
                )

    # Single outer BuildTransaction fallback (1 snapshot + 1 export_code total, rebuild=False between probes)
    out: list[float] = []
    with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False) as tx:
        for idx, raw in enumerate(raw_items):
            if idx > 0:
                tx.restore(rebuild=False)
            worker.call("add_item", {"raw": raw, "slotName": slot_name})
            after = read_metric_value(worker, metric)
            out.append(after - baseline)
    return out


__all__ = [
    "BuildProfile",
    "eval_item_raw_deltas_live",
    "extract_build_profile",
    "get_allocated_node_ids",
    "parse_tooltip_metric_delta",
    "read_metric_value",
    "score_stat_lines",
]
