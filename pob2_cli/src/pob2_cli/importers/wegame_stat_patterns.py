# SPDX-License-Identifier: GPL-3.0-only
"""Rich-text cleaner, marker helpers, and built-in CN/TW -> EN regex stat patterns for WeGame imports (L1)."""

from __future__ import annotations

import re
from collections.abc import Callable

from ..han_fold import fold_han

HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CATALYST_PAREN_RE = re.compile(r"^品质\s*[（(]([^）)]+)[）)]$")

_ROLL_RANGE_RE = re.compile(r" \(([0-9.+\-]+)\)")
_LEADING_INT_RE = re.compile(r"^\s*\+?(\d+)")
_ROMAN_FULLWIDTH = str.maketrans({"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V"})
_SHAMAN_PREFIX_RE = re.compile(r"^(?:羁绊|羈絆|Bonded)\s*[:：]\s*", re.IGNORECASE)
_PROGRESS_COUNTER_RE = re.compile(r"(?:BrokenFace|毁面首领|毀面首領|Broken Boss Faces)", re.IGNORECASE)


def _sign(val: str) -> str:
    return val if val.startswith(("+", "-")) else f"+{val}"


def norm_wegame_key(text: str) -> str:
    """Fold Traditional/Simplified Han characters and fullwidth Roman numerals."""
    return fold_han(text.translate(_ROMAN_FULLWIDTH)).strip()


def has_shaman_bonded_marker(raw: str) -> bool:
    """Return True if *raw* represents a Shaman-only Bonded rune modifier line."""
    text = (raw or "").strip()
    if "ShamanOnlyMods" in text:
        return True
    cleaned = clean_wegame_text(text)
    return bool(_SHAMAN_PREFIX_RE.match(cleaned)) or cleaned.startswith(("羁绊", "羈絆"))


def strip_shaman_bonded_prefix(cleaned: str) -> str:
    """Strip leading `羁绊：` / `Bonded:` prefix so the inner stat can be translated."""
    return _SHAMAN_PREFIX_RE.sub("", cleaned.strip())


def is_progress_counter_stat(raw: str) -> bool:
    """Return True for non-stat campaign progress counters in quest_stats (e.g. Broken Boss Faces)."""
    return bool(_PROGRESS_COUNTER_RE.search(raw or ""))


def clean_wegame_text(raw: str) -> str:
    """Strip GGG `[Key|Display]` markup and informational jewel `(min-max)` roll ranges."""
    if not raw:
        return ""
    out_parts: list[str] = []
    rest = raw
    while True:
        start = rest.find("[")
        if start < 0:
            out_parts.append(rest)
            break
        out_parts.append(rest[:start])
        end = rest.find("]", start)
        if end < 0:
            out_parts.append(rest[start:])
            break
        inner = rest[start + 1 : end]
        out_parts.append(inner.rsplit("|", 1)[-1])
        rest = rest[end + 1 :]
    text = "".join(out_parts)

    def _strip_range(match: re.Match[str]) -> str:
        inner = match.group(1)
        return "" if "-" in inner else match.group(0)

    return _ROLL_RANGE_RE.sub(_strip_range, text).strip()


def parse_leading_int(raw: str | None) -> int | None:
    """Parse leading integer from values like `20(最高等级)`, `+20%`, or `19`."""
    if not raw:
        return None
    m = _LEADING_INT_RE.match(str(raw).strip())
    return int(m.group(1)) if m else None


# Compact built-in regex stat translators for offline / synthetic operation.
# When local Data/poe2/{zh-rCN,zh-rTW}/stats.json is present, exact & template lookup runs first.
BUILTIN_STAT_PATTERNS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (re.compile(r"^([+-]?\d+)\s*(?:最大生命|点最大生命)$"), lambda m: f"{_sign(m.group(1))} to maximum Life"),
    (re.compile(r"^最大生命\s*([+-]?\d+)$"), lambda m: f"{_sign(m.group(1))} to maximum Life"),
    (re.compile(r"^最大生命提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased maximum Life"),
    (re.compile(r"^([+-]?\d+)\s*(?:最大魔力|点最大魔力)$"), lambda m: f"{_sign(m.group(1))} to maximum Mana"),
    (re.compile(r"^最大魔力\s*([+-]?\d+)$"), lambda m: f"{_sign(m.group(1))} to maximum Mana"),
    (re.compile(r"^最大魔力提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased maximum Mana"),
    (re.compile(r"^([+-]?\d+)\s*(?:精魂|灵魂)$"), lambda m: f"{_sign(m.group(1))} to Spirit"),
    (re.compile(r"^(?:精魂|灵魂)\s*([+-]?\d+)$"), lambda m: f"{_sign(m.group(1))} to Spirit"),
    (re.compile(r"^([+-]?\d+)%\s*火焰抗性$"), lambda m: f"{_sign(m.group(1))}% to Fire Resistance"),
    (re.compile(r"^火焰抗性\s*([+-]?\d+)%$"), lambda m: f"{_sign(m.group(1))}% to Fire Resistance"),
    (re.compile(r"^([+-]?\d+)%\s*(?:冰霜|冰冷)抗性$"), lambda m: f"{_sign(m.group(1))}% to Cold Resistance"),
    (re.compile(r"^(?:冰霜|冰冷)抗性\s*([+-]?\d+)%$"), lambda m: f"{_sign(m.group(1))}% to Cold Resistance"),
    (re.compile(r"^([+-]?\d+)%\s*闪电抗性$"), lambda m: f"{_sign(m.group(1))}% to Lightning Resistance"),
    (re.compile(r"^闪电抗性\s*([+-]?\d+)%$"), lambda m: f"{_sign(m.group(1))}% to Lightning Resistance"),
    (re.compile(r"^([+-]?\d+)%\s*混沌抗性$"), lambda m: f"{_sign(m.group(1))}% to Chaos Resistance"),
    (re.compile(r"^混沌抗性\s*([+-]?\d+)%$"), lambda m: f"{_sign(m.group(1))}% to Chaos Resistance"),
    (re.compile(r"^([+-]?\d+)%\s*所有元素抗性$"), lambda m: f"{_sign(m.group(1))}% to all Elemental Resistances"),
    (re.compile(r"^所有元素抗性\s*([+-]?\d+)%$"), lambda m: f"{_sign(m.group(1))}% to all Elemental Resistances"),
    (re.compile(r"^([+-]?\d+)\s*力量$"), lambda m: f"{_sign(m.group(1))} to Strength"),
    (re.compile(r"^力量\s*([+-]?\d+)$"), lambda m: f"{_sign(m.group(1))} to Strength"),
    (re.compile(r"^([+-]?\d+)\s*敏捷$"), lambda m: f"{_sign(m.group(1))} to Dexterity"),
    (re.compile(r"^敏捷\s*([+-]?\d+)$"), lambda m: f"{_sign(m.group(1))} to Dexterity"),
    (re.compile(r"^([+-]?\d+)\s*(?:智慧|智力)$"), lambda m: f"{_sign(m.group(1))} to Intelligence"),
    (re.compile(r"^(?:智慧|智力)\s*([+-]?\d+)$"), lambda m: f"{_sign(m.group(1))} to Intelligence"),
    (re.compile(r"^([+-]?\d+)\s*所有属性$"), lambda m: f"{_sign(m.group(1))} to all Attributes"),
    (re.compile(r"^所有属性\s*([+-]?\d+)$"), lambda m: f"{_sign(m.group(1))} to all Attributes"),
    (re.compile(r"^(?:咒符|护符)栏\s*\+(\d+)$"), lambda m: f"+{m.group(1)} Charm Slot"),
    (re.compile(r"^拥有\s*(\d+)\s*个(?:咒符|护符)栏$"), lambda m: f"Has {m.group(1)} Charm Slot"),
    (re.compile(r"^移动速度提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Movement Speed"),
    (re.compile(r"^攻击速度提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Attack Speed"),
    (re.compile(r"^施法速度提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Cast Speed"),
    (re.compile(r"^法术伤害提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Spell Damage"),
    (re.compile(r"^攻击伤害提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Attack Damage"),
    (re.compile(r"^物理伤害提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Physical Damage"),
    (re.compile(r"^护甲提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Armour"),
    (re.compile(r"^闪避值提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Evasion Rating"),
    (re.compile(r"^能量护盾提高\s*(\d+)%$"), lambda m: f"{m.group(1)}% increased Energy Shield"),
    (
        re.compile(r"^全局护甲、闪避值和能量护盾提高\s*(\d+)%$"),
        lambda m: f"{m.group(1)}% increased Global Armour, Evasion and Energy Shield",
    ),
    (re.compile(r"^所有攻击技能等级\s*\+(\d+)$"), lambda m: f"+{m.group(1)} to Level of all Attack Skills"),
    (re.compile(r"^所有法术技能等级\s*\+(\d+)$"), lambda m: f"+{m.group(1)} to Level of all Spell Skills"),
    (re.compile(r"^所有投射物技能等级\s*\+(\d+)$"), lambda m: f"+{m.group(1)} to Level of all Projectile Skills"),
    (re.compile(r"^所有近战技能等级\s*\+(\d+)$"), lambda m: f"+{m.group(1)} to Level of all Melee Skills"),
    (
        re.compile(r"^所有闪电法术技能等级\s*\+(\d+)$"),
        lambda m: f"+{m.group(1)} to Level of all Lightning Spell Skills",
    ),
    (
        re.compile(r"^攻击附加\s*(\d+)\s*[-–至到]\s*(\d+)\s*(?:点)?物理伤害$"),
        lambda m: f"Adds {m.group(1)} to {m.group(2)} Physical Damage to Attacks",
    ),
    (
        re.compile(r"^攻击附加\s*(\d+)\s*[-–至到]\s*(\d+)\s*(?:点)?火焰伤害$"),
        lambda m: f"Adds {m.group(1)} to {m.group(2)} Fire Damage to Attacks",
    ),
    (
        re.compile(r"^攻击附加\s*(\d+)\s*[-–至到]\s*(\d+)\s*(?:点)?(?:冰霜|冰冷)伤害$"),
        lambda m: f"Adds {m.group(1)} to {m.group(2)} Cold Damage to Attacks",
    ),
    (
        re.compile(r"^攻击附加\s*(\d+)\s*[-–至到]\s*(\d+)\s*(?:点)?闪电伤害$"),
        lambda m: f"Adds {m.group(1)} to {m.group(2)} Lightning Damage to Attacks",
    ),
    (
        re.compile(r"^附加\s*(\d+)\s*[-–至到]\s*(\d+)\s*(?:点)?物理伤害$"),
        lambda m: f"Adds {m.group(1)} to {m.group(2)} Physical Damage",
    ),
    (
        re.compile(r"^护甲、闪避值和能量护盾提高\s*(\d+)%$"),
        lambda m: f"{m.group(1)}% increased Armour, Evasion and Energy Shield",
    ),
    (
        re.compile(r"^眩晕门槛提高\s*(\d+)%$"),
        lambda m: f"{m.group(1)}% increased Stun Threshold",
    ),
    (
        re.compile(r"^元素异常状态门槛提高\s*(\d+)%$"),
        lambda m: f"{m.group(1)}% increased Elemental Ailment Threshold",
    ),
    (
        re.compile(r"^魔力回复率提高\s*(\d+)%$"),
        lambda m: f"{m.group(1)}% increased Mana Regeneration Rate",
    ),
    (
        re.compile(r"^当你被感电时使用$"),
        lambda _m: "Used when you become Shocked",
    ),
    (
        re.compile(r"^当你中毒时使用$"),
        lambda _m: "Used when you become Poisoned",
    ),
)


def match_builtin_stat_pattern(folded_key: str) -> str | None:
    """Match a folded Chinese modifier line against built-in PoE2 stat patterns."""
    for pattern, formatter in BUILTIN_STAT_PATTERNS:
        m = pattern.match(folded_key)
        if m is not None:
            return str(formatter(m))
    return None


__all__ = [
    "BUILTIN_STAT_PATTERNS",
    "CATALYST_PAREN_RE",
    "HAS_CJK_RE",
    "clean_wegame_text",
    "has_shaman_bonded_marker",
    "is_progress_counter_stat",
    "match_builtin_stat_pattern",
    "norm_wegame_key",
    "parse_leading_int",
    "strip_shaman_bonded_prefix",
]
