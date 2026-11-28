# SPDX-License-Identifier: GPL-3.0-only
"""Lightweight English-only 1,768 PoE2 base whitelist & PoE1 base hallucination guard (L2)."""

from __future__ import annotations

import difflib
import gzip
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import CrossGameHallucinationError
from ..han_fold import fold_han

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

FALLBACK_BASE_SUGGESTIONS: tuple[str, ...] = (
    "Vile Robe",
    "Attuned Wand",
    "Solar Amulet",
    "Breach Ring",
    "Grounding Charm",
)

EN_BASE_PREFIXES: tuple[str, ...] = (
    "Superior ",
    "Exceptional ",
    "Runeforged ",
    "Runemastered ",
    "Expert ",
    "Advanced ",
    "Synthesised ",
)
EN_BASE_PREFIXES_LOW: tuple[tuple[str, int], ...] = tuple(
    (p.lower(), len(p)) for p in EN_BASE_PREFIXES
)

# Unambiguous PoE1-only item bases / uniques that MUST trigger CrossGameHallucinationError.
POE1_ONLY_BASES: dict[str, str] = {
    "viridian jewel": "PoE1 jewel base; PoE2 uses Ruby, Emerald, Sapphire, Diamond, and Time-Lost Jewels.",
    "crimson jewel": "PoE1 jewel base; PoE2 uses Ruby, Emerald, Sapphire, Diamond, and Time-Lost Jewels.",
    "cobalt jewel": "PoE1 jewel base; PoE2 uses Ruby, Emerald, Sapphire, Diamond, and Time-Lost Jewels.",
    "prismatic jewel": "PoE1 jewel base; PoE2 uses Ruby, Emerald, Sapphire, Diamond, and Time-Lost Jewels.",
    "murderous eye jewel": "PoE1 Abyss jewel base; Abyss jewels do not exist in PoE2.",
    "searching eye jewel": "PoE1 Abyss jewel base; Abyss jewels do not exist in PoE2.",
    "hypnotic eye jewel": "PoE1 Abyss jewel base; Abyss jewels do not exist in PoE2.",
    "ghastly eye jewel": "PoE1 Abyss jewel base; Abyss jewels do not exist in PoE2.",
    "cluster jewel": "PoE1 Cluster Jewels do not exist in PoE2.",
    "small cluster jewel": "PoE1 Cluster Jewels do not exist in PoE2.",
    "medium cluster jewel": "PoE1 Cluster Jewels do not exist in PoE2.",
    "large cluster jewel": "PoE1 Cluster Jewels do not exist in PoE2.",
    "vaal regalia": "PoE1 ES Body Armour base; PoE2 uses Vile Robe / Ancestral Robe / Altar Robe (`Body_Armours_int`).",
    "astral plate": "PoE1 Armour Body base; PoE2 uses Conquered Plate / Ornate Plate (`Body_Armours_str`).",
    "stygian vise": "PoE1 Abyssal Socket belt; PoE2 belts have Charm slots (`Utility Belt`, `Heavy Belt`).",
    "mageblood": "PoE1 unique Utility Flask belt; PoE2 has no Utility Flasks.",
    "hubris circlet": "PoE1 ES Helmet base; PoE2 uses Kamasan Tiara / Ancestral Tiara (`Helmets_int`).",
    "sorcerer boots": "PoE1 ES Boots base; PoE2 uses Sekhema Sandals / Silken Slippers (`Boots_int`).",
    "sorcerer gloves": "PoE1 ES Gloves base; PoE2 uses Sirenscale Gloves / Sombre Gloves (`Gloves_int`).",
    "spiked gloves": "PoE1 Atlas gloves base; not in PoE2.",
    "fingerless silk gloves": "PoE1 Atlas gloves base; not in PoE2.",
    "gripped gloves": "PoE1 Atlas gloves base; not in PoE2.",
    "two-toned boots": "PoE1 Atlas boots base; not in PoE2.",
    "opal ring": "PoE1 Atlas ring base; PoE2 uses Breach Ring / Amethyst Ring / Prismatic Ring.",
    "steel ring": "PoE1 Atlas ring base; not in PoE2.",
    "vermillion ring": "PoE1 Atlas ring base; not in PoE2.",
    "crystal belt": "PoE1 Atlas belt base; not in PoE2.",
    "marble amulet": "PoE1 Atlas amulet base; PoE2 uses Solar Amulet / Stellar Amulet.",
    "blue pearl amulet": "PoE1 Atlas amulet base; not in PoE2.",
    "bone helmet": "PoE1 Atlas helmet base; not in PoE2.",
    "titanium spirit shield": "PoE1 Spirit Shield base; PoE2 uses Foci (`Foci`) instead of Spirit Shields.",
    "archon kite shield": "PoE1 Kite Shield base; PoE2 uses Crest Shields (`Shields_str_int`).",
    "imbued wand": "PoE1 wand base; PoE2 uses Attuned Wand / Siphoning Wand / Volatile Wand.",
    "profane wand": "PoE1 wand base; PoE2 uses Attuned Wand / Withered Wand.",
    "opal wand": "PoE1 wand base; PoE2 uses Attuned Wand / Galvanic Wand.",
    "thicket bow": "PoE1 bow base; PoE2 uses Dualstring Bow / Gemini Bow / Obliterator Bow.",
    "imperial bow": "PoE1 bow base; PoE2 uses Dualstring Bow / Gemini Bow.",
    "spine bow": "PoE1 bow base; PoE2 uses Dualstring Bow / Gemini Bow.",
    "jeweled foil": "PoE1 thrusting sword base; not in PoE2.",
    "jewelled foil": "PoE1 thrusting sword base; not in PoE2.",
    "quicksilver flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "granite flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "jade flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "quartz flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "diamond flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "silver flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "amethyst flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "bismuth flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "basalt flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "sulphur flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "stibnite flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "progenesis": "PoE1 unique Amethyst Flask; not in PoE2.",
    "瓦尔法衣": "PoE1 ES 胸甲底材（Vaal Regalia），PoE2 对应先祖长袍 / 邪恶长袍（Vile Robe）。",
    "星芒战铠": "PoE1 护甲胸甲底材（Astral Plate），PoE2 不存在。",
    "冥河腰带": "PoE1 深渊插槽腰带（Stygian Vise），PoE2 不存在。",
    "魔血": "PoE1 传奇功能药剂腰带（Mageblood），PoE2 无功能药剂。",
    "星团珠宝": "PoE1 星团珠宝（Cluster Jewel），PoE2 不存在。",
    "水银药剂": "PoE1 功能药剂（Quicksilver Flask），PoE2 仅保留血/蓝药与腰带咒符。",
    "坚岩药剂": "PoE1 功能药剂（Granite Flask），PoE2 仅保留血/蓝药与腰带咒符。",
}

_POE1_CJK_KEYS: tuple[str, ...] = tuple(k for k in POE1_ONLY_BASES if not k.isascii())
_POE1_ASCII_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(
        re.escape(k)
        for k in sorted((k for k in POE1_ONLY_BASES if k.isascii()), key=len, reverse=True)
    )
    + r")(?![a-z0-9])"
)


def has_cjk(text: str | None) -> bool:
    """Fast check returning True only when *text* contains CJK Unified Ideographs."""
    if not text or text.isascii():
        return False
    return bool(_HAS_CJK_RE.search(text))


def check_poe1_base_hallucination(raw_base: str) -> None:
    """Raise `CrossGameHallucinationError` if *raw_base* names a PoE1-only base or unique."""
    s = (raw_base or "").strip()
    if not s:
        return
    low = s.lower()
    hit_key: str | None = None
    if low in POE1_ONLY_BASES:
        hit_key = low
    elif s.isascii():
        m = _POE1_ASCII_RE.search(low)
        if m is not None:
            hit_key = m.group(0)
    else:
        folded = fold_han(s)
        if folded in POE1_ONLY_BASES:
            hit_key = folded
        else:
            for k in _POE1_CJK_KEYS:
                if k in folded:
                    hit_key = k
                    break
            if hit_key is None:
                m = _POE1_ASCII_RE.search(low)
                if m is not None:
                    hit_key = m.group(0)
    if hit_key is not None:
        raise CrossGameHallucinationError(
            game="poe2",
            detected_game="poe1",
            trigger=s,
            error=f"`{s}` is a PoE1-only base/item and does not exist in PoE2 0.5.5. {POE1_ONLY_BASES[hit_key]}",
            remediation="Use a valid PoE2 0.5.5 base item (e.g. `Vile Robe`, `Attuned Wand`, `Emerald`, `Breach Ring`, `Solar Amulet`) or run `pob` for PoE1.",
            equivalent_command='pob2 craft odds --base "Vile Robe" --want "T1 Energy Shield"',
            rule_id="CRAFT_POE1_BASE",
        )


class EnBaseCatalog:
    """Lightweight (~10 KB gz) English-only 1,768 PoE2 base whitelist without CJK l10n overhead."""

    def __init__(self, data_dir: Path | None = None) -> None:
        d = data_dir or _DATA_DIR
        self.base_to_slug: dict[str, str] = {}
        self.base_low_to_canon: dict[str, str] = {}
        self._en_bases_by_len: tuple[tuple[str, str], ...] | None = None
        en_fp = d / "poe2_en_bases.json.gz"
        if en_fp.is_file():
            obj: dict[str, Any] = json.loads(gzip.decompress(en_fp.read_bytes()))
            for en_name, slug in obj.items():
                s_en = str(en_name)
                self.base_to_slug[s_en] = str(slug)
                self.base_low_to_canon[s_en.lower()] = s_en
        else:
            l10n_fp = d / "poe2_craft_l10n.json.gz"
            if l10n_fp.is_file():
                full_obj: dict[str, Any] = json.loads(gzip.decompress(l10n_fp.read_bytes()))
                for en_name, slug in (full_obj.get("base_to_slug") or {}).items():
                    s_en = str(en_name)
                    self.base_to_slug[s_en] = str(slug)
                    self.base_low_to_canon[s_en.lower()] = s_en

    def _get_en_bases_by_len(self) -> tuple[tuple[str, str], ...]:
        if self._en_bases_by_len is None:
            self._en_bases_by_len = tuple(
                sorted(
                    ((k, v) for k, v in self.base_low_to_canon.items() if len(k) >= 4),
                    key=lambda x: len(x[0]),
                    reverse=True,
                )
            )
        return self._en_bases_by_len

    def lookup_exact_base(self, raw: str) -> tuple[str, str] | None:
        s = (raw or "").strip()
        if not s:
            return None
        low = s.lower()
        canon = self.base_low_to_canon.get(low)
        if canon is not None:
            return canon, self.base_to_slug[canon]
        for pref_low, plen in EN_BASE_PREFIXES_LOW:
            if low.startswith(pref_low):
                sub = low[plen:].strip()
                canon = self.base_low_to_canon.get(sub)
                if canon is not None:
                    return canon, self.base_to_slug[canon]
        return None

    def extract_magic_base(self, raw: str) -> tuple[str, str] | None:
        s = (raw or "").strip()
        if not s or " " not in s:
            return None
        padded = f" {s.lower()} "
        for en_low, canon in self._get_en_bases_by_len():
            if f" {en_low} " in padded:
                return canon, self.base_to_slug[canon]
        return None

    def suggest_bases(self, query: str, *, limit: int = 5) -> list[str]:
        q = (query or "").strip()
        if not q:
            return list(FALLBACK_BASE_SUGGESTIONS[:limit])
        q_low = q.lower()
        all_names = list(self.base_to_slug.keys())
        close = difflib.get_close_matches(q, all_names, n=limit, cutoff=0.35)
        if len(close) < limit:
            q_toks = [t for t in _NON_ALNUM_RE.split(q_low) if len(t) >= 3]
            if q_toks:
                tok_hits = [n for n in all_names if n not in close and any(t in n.lower() for t in q_toks)]
                close.extend(tok_hits[: limit - len(close)])
        if len(close) < limit:
            for fallback in FALLBACK_BASE_SUGGESTIONS:
                if fallback not in close and len(close) < limit:
                    close.append(fallback)
        return close[:limit]


@lru_cache(maxsize=2)
def _load_en_base_whitelist_cached(resolved_dir: Path) -> EnBaseCatalog:
    return EnBaseCatalog(resolved_dir)


def load_en_base_whitelist(data_dir: Path | None = None) -> EnBaseCatalog:
    """Load and cache the lightweight English-only 1,768 PoE2 base whitelist (~1.5ms cold)."""
    return _load_en_base_whitelist_cached((data_dir or _DATA_DIR).resolve())


load_en_base_whitelist.cache_clear = _load_en_base_whitelist_cached.cache_clear  # type: ignore[attr-defined]
load_en_base_whitelist.cache_info = _load_en_base_whitelist_cached.cache_info  # type: ignore[attr-defined]


__all__ = [
    "EN_BASE_PREFIXES",
    "EN_BASE_PREFIXES_LOW",
    "FALLBACK_BASE_SUGGESTIONS",
    "POE1_ONLY_BASES",
    "EnBaseCatalog",
    "check_poe1_base_hallucination",
    "has_cjk",
    "load_en_base_whitelist",
]
