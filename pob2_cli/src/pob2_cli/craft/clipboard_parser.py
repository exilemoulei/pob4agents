# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Trilingual (zh-CN / zh-TW / EN) PoE2 item clipboard tokenizer, header guard, and CN stat bridge (L2)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from .models import AffixSide, ItemRarity, ModDomain
from .zh_lookup import has_cjk, load_craft_l10n, load_en_base_whitelist

if TYPE_CHECKING:
    from ..importers.wegame_translate import WeGameTranslator

_RICH_LINK_RE = re.compile(r"\[[^\]|]+\|([^\]]+)\]|\[([^\]]+)\]")
_RANGE_OR_NUM_RE = re.compile(
    r"\((?P<lo>[+-]?\d+(?:\.\d+)?)\s*-\s*(?P<hi>[+-]?\d+(?:\.\d+)?)\)"
    r"|(?P<num>[+-]?\d+(?:\.\d+)?)"
)
_CTRL_ALT_HEADER_RE = re.compile(
    r"\{\s*(Prefix|Suffix)\s+Modifier\s+\"([^\"]+)\"\s+\(Tier:\s*(\d+)\)",
    re.IGNORECASE,
)
_HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_POB_META_TAG_RE = re.compile(
    r"\{(?:mutated|unscalable|custom|range:[^}]+|tags:[^}]+|variant:[^}]+)\}",
    re.IGNORECASE,
)
_STAT_LINE_RE = re.compile(
    r"^(?:\{|\[?[+-]?\d|adds\s+|gain\s+|grants\s+|leech\s+|used\s+when\s+|has\s+\d|"
    r"socketed\s+|trigger\s+|bears\s+the\s+mark|allocates\s+|cannot\s+|can\s+|"
    r"enemies\s+|minions\s+|companions\s+|attacks\s+|spells\s+|projectiles\s+|"
    r"your\s+|you\s+|skills\s+|recover\s+|possessed\s+by\s+|energy\s+shield\s+)",
    re.IGNORECASE,
)
_POE1_ILLEGAL_LINE_RE = re.compile(
    r"prefixes cannot be changed|suffixes cannot be changed|can have up to 3 crafted modifiers|"
    r"cannot roll attack modifiers|cannot roll caster modifiers|前缀无法被改变|后缀无法被改变|"
    r"最多可以有\s*3\s*个工艺词缀",
    re.IGNORECASE,
)

_ZH_RARITY: dict[str, ItemRarity] = {
    "普通": ItemRarity.NORMAL,
    "魔法": ItemRarity.MAGIC,
    "稀有": ItemRarity.RARE,
    "传奇": ItemRarity.UNIQUE,
    "傳奇": ItemRarity.UNIQUE,
    "暗金": ItemRarity.UNIQUE,
    "normal": ItemRarity.NORMAL,
    "magic": ItemRarity.MAGIC,
    "rare": ItemRarity.RARE,
    "unique": ItemRarity.UNIQUE,
}

_CATALYST_ZH_MAP: dict[str, str] = {
    "火焰": "fire",
    "冰霜": "cold",
    "闪电": "lightning",
    "閃電": "lightning",
    "混沌": "chaos",
    "物理": "physical",
    "生命": "life",
    "魔力": "mana",
    "防御": "defences",
    "防禦": "defences",
    "攻击": "attack",
    "攻擊": "attack",
    "施法": "caster",
    "属性": "attribute",
    "屬性": "attribute",
    "速度": "speed",
}

_IGNORED_META_KEYS: frozenset[str] = frozenset(
    {
        "itemclass", "物品类别", "物品類別", "sockets", "插槽", "levelreq", "level",
        "str", "dex", "int", "armour", "evasion", "evasionrating", "energyshield",
        "ward", "spirit", "uniqueid", "rune", "charmslots", "requirements", "requires",
        "catalyst", "catalystquality", "limitedto", "radius", "blockchance", "block",
        "physicaldamage", "elementaldamage", "chaosdamage", "criticalhitchance",
        "attackspersecond", "weaponrange", "duration", "charges", "uses",
        "物理伤害", "元素伤害", "混沌伤害", "护甲", "闪避值", "能量护盾", "需求", "等级",
        "需求等级", "需求等級", "暴击几率", "每秒攻击次数", "武器范围", "精魂", "插槽数",
    }
)


def clean_rich_markup(text: str) -> str:
    return _RICH_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)


def extract_values(text: str) -> list[float]:
    vals: list[float] = []
    for m in _RANGE_OR_NUM_RE.finditer(text):
        if m.group("num") is not None:
            vals.append(abs(float(m.group("num"))))
    return vals


@lru_cache(maxsize=4096)
def extract_bounds_tuple(stat_text: str) -> tuple[tuple[float, float], ...]:
    bounds: list[tuple[float, float]] = []
    for m in _RANGE_OR_NUM_RE.finditer(stat_text):
        if m.group("lo") is not None and m.group("hi") is not None:
            a, b = abs(float(m.group("lo"))), abs(float(m.group("hi")))
            bounds.append((min(a, b), max(a, b)))
        elif m.group("num") is not None:
            v = abs(float(m.group("num")))
            bounds.append((v, v))
    return tuple(bounds)


def extract_bounds(stat_text: str) -> list[tuple[float, float]]:
    return list(extract_bounds_tuple(stat_text))


def detect_domain_and_strip(raw_line: str) -> tuple[str, ModDomain, bool, bool, bool]:
    """Return `(cleaned_text, domain, fractured, is_crafted, is_desecrated)`."""
    s = clean_rich_markup(raw_line).strip()
    fractured = is_crafted = is_desecrated = False
    domain = ModDomain.EXPLICIT

    for tag in ("{fractured}", "(fractured)", "(破裂)", "（破裂）"):
        if tag in s.lower() or tag in s:
            fractured = True
            s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE).strip()
    for tag in ("{crafted}", "(crafted)", "(工艺)", "（工艺）", "(工藝)", "（工藝）"):
        if tag in s.lower() or tag in s:
            is_crafted = True
            s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE).strip()
    for tag in ("{desecrated}", "(desecrated)", "(亵渎)", "（亵渎）", "(褻瀆)", "（褻瀆）"):
        if tag in s.lower() or tag in s:
            is_desecrated = True
            domain = ModDomain.DESECRATED
            s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE).strip()
    for tag, dom in (
        ("{implicit}", ModDomain.IMPLICIT),
        ("(implicit)", ModDomain.IMPLICIT),
        ("(基底)", ModDomain.IMPLICIT),
        ("(固定词缀)", ModDomain.IMPLICIT),
        ("(固定詞綴)", ModDomain.IMPLICIT),
        ("{enchant}", ModDomain.ENCHANT),
        ("(enchant)", ModDomain.ENCHANT),
        ("(附魔)", ModDomain.ENCHANT),
        ("{rune}", ModDomain.RUNE),
        ("(rune)", ModDomain.RUNE),
        ("(符文)", ModDomain.RUNE),
    ):
        if tag in s.lower() or tag in s:
            domain = dom
            s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE).strip()
    if fractured and domain == ModDomain.EXPLICIT:
        domain = ModDomain.FRACTURED
    elif is_crafted and domain == ModDomain.EXPLICIT:
        domain = ModDomain.CRAFTED
    s = _POB_META_TAG_RE.sub("", s).strip()
    if s.lower().startswith(("grants skill:", "给予技能:", "給予技能:")):
        domain = ModDomain.IMPLICIT
    return s.strip(), domain, fractured, is_crafted, is_desecrated


def parse_catalyst_tag(key_raw: str) -> str | None:
    m = re.search(r"[（(]([^）)]+)[）)]", key_raw)
    if not m:
        return None
    inner = m.group(1).strip().lower()
    for zh, en in _CATALYST_ZH_MAP.items():
        if zh in inner:
            return en
    word = inner.replace("modifiers", "").replace("词缀", "").replace("詞綴", "").strip()
    return word.split()[0] if word else None


def looks_like_stat_line(clean_ln: str) -> bool:
    return bool(_STAT_LINE_RE.match(clean_ln)) or any(ch.isdigit() or ch == "%" for ch in clean_ln)


@lru_cache(maxsize=1)
def _cached_wegame_translator() -> WeGameTranslator:
    from ..importers.wegame_translate import WeGameTranslator

    return WeGameTranslator()


def translate_mod_line(clean_mod: str) -> tuple[str, bool]:
    """Translate a CN/TW stat line into PoE2 EN via bundled+local stats.json templates and WeGameTranslator."""
    if not has_cjk(clean_mod):
        return clean_mod, True
    en, ok = load_craft_l10n().translate_zh_stat_line(clean_mod)
    if ok:
        return en, True
    en2, ok2 = _cached_wegame_translator().translate_stat_line(clean_mod)
    if ok2:
        return en2, True
    return clean_mod, False


@dataclass(slots=True)
class ParsedClipboard:
    rarity: ItemRarity
    ilvl: int
    quality: int
    catalyst_tag: str | None
    corrupted: bool
    sanctified: bool
    header_names: list[str]
    implicits: list[str]
    raw_mod_lines: list[tuple[str, AffixSide | None, str | None, int | None]]
    legality_issues: list[str]


def parse_clipboard_sections(raw_text: str, ilvl_override: int | None) -> ParsedClipboard:
    """Parse sectioned (`--------`) or sectionless PoB2/Ctrl+C item text without header-swallowing."""
    rarity = ItemRarity.RARE
    ilvl = ilvl_override or 82
    quality = 0
    catalyst_tag: str | None = None
    corrupted = sanctified = False
    header_names: list[str] = []
    implicits: list[str] = []
    raw_mod_lines: list[tuple[str, AffixSide | None, str | None, int | None]] = []
    legality_issues: list[str] = []

    sections = [
        [ln.strip() for ln in sec.splitlines() if ln.strip()]
        for sec in re.split(r"(?m)^-{4,}\s*$", raw_text.strip())
    ]
    sections = [s for s in sections if s]
    pending_side: AffixSide | None = None
    pending_name: str | None = None
    pending_tier: int | None = None
    in_pob_implicits = 0
    in_header = True

    for sec_idx, sec in enumerate(sections):
        if sec_idx > 0:
            in_header = False
        for ln in sec:
            clean_ln = clean_rich_markup(ln).strip()
            low = clean_ln.lower()
            hm = _CTRL_ALT_HEADER_RE.search(clean_ln)
            if hm:
                in_header = False
                pending_side = AffixSide.PREFIX if hm.group(1).lower() == "prefix" else AffixSide.SUFFIX
                pending_name = hm.group(2)
                pending_tier = int(hm.group(3))
                continue
            if _POE1_ILLEGAL_LINE_RE.search(clean_ln):
                in_header = False
                legality_issues.append(f"检测到 PoE1 工艺台元词缀（PoE2 0.5.5 不存在）: {clean_ln}")
                continue
            if low in ("corrupted", "已腐化", "已污染", "已汙染"):
                in_header = False
                corrupted = True
                continue
            if low in ("sanctified", "已神圣化", "已聖化"):
                in_header = False
                sanctified = True
                continue
            if low in ("mirrored", "已复制", "已複製", "unidentified", "未鉴定", "未鑑定"):
                in_header = False
                continue
            if ":" in clean_ln or "：" in clean_ln:
                k_raw, v_raw = clean_ln.replace("：", ":", 1).split(":", 1)
                k_comp = re.sub(r"\s+", "", k_raw).lower()
                v_str = v_raw.strip()
                if k_comp in ("rarity", "稀有度"):
                    rarity = _ZH_RARITY.get(v_str.lower(), _ZH_RARITY.get(v_str, ItemRarity.RARE))
                    continue
                if k_comp in ("itemclass", "物品类别", "物品類別"):
                    continue
                if k_comp in ("itemlevel", "物品等级", "物品等級"):
                    in_header = False
                    nm = re.search(r"\d+", v_str)
                    if nm and ilvl_override is None:
                        ilvl = int(nm.group(0))
                    continue
                if k_comp.startswith(("quality", "品质", "品質")):
                    in_header = False
                    nm = re.search(r"\d+", v_str)
                    if nm:
                        quality = int(nm.group(0))
                    cat = parse_catalyst_tag(k_raw)
                    if cat:
                        catalyst_tag = cat
                    continue
                if k_comp == "implicits":
                    in_header = False
                    nm = re.search(r"\d+", v_str)
                    in_pob_implicits = int(nm.group(0)) if nm else 0
                    continue
                if k_comp in _IGNORED_META_KEYS:
                    in_header = False
                    continue
            if in_header and sec_idx == 0 and in_pob_implicits == 0:
                if clean_ln.startswith(("{", "+", "-")):
                    in_header = False
                elif header_names and looks_like_stat_line(clean_ln):
                    in_header = False
                else:
                    header_names.append(clean_ln)
                    max_hdr = 1 if rarity == ItemRarity.NORMAL else 2
                    if len(header_names) >= max_hdr:
                        in_header = False
                    elif rarity == ItemRarity.MAGIC and len(header_names) == 1:
                        matched = (
                            load_craft_l10n().lookup_exact_base(clean_ln)
                            if has_cjk(clean_ln)
                            else load_en_base_whitelist().lookup_exact_base(clean_ln)
                        )
                        if matched is not None:
                            in_header = False
                    continue
            if in_pob_implicits > 0:
                in_pob_implicits -= 1
                implicits.append(clean_ln)
                continue
            raw_mod_lines.append((clean_ln, pending_side, pending_name, pending_tier))
            pending_side = pending_name = pending_tier = None

    return ParsedClipboard(
        rarity=rarity,
        ilvl=ilvl,
        quality=quality,
        catalyst_tag=catalyst_tag,
        corrupted=corrupted,
        sanctified=sanctified,
        header_names=header_names,
        implicits=implicits,
        raw_mod_lines=raw_mod_lines,
        legality_issues=legality_issues,
    )


__all__ = [
    "ParsedClipboard",
    "clean_rich_markup",
    "detect_domain_and_strip",
    "extract_bounds",
    "extract_bounds_tuple",
    "extract_values",
    "looks_like_stat_line",
    "parse_catalyst_tag",
    "parse_clipboard_sections",
    "translate_mod_line",
]
