# SPDX-License-Identifier: GPL-3.0-only
"""CN/TW base & stat skeleton O(1) reverse-lookup with lazy fallback to local Data/poe2 (L2)."""

from __future__ import annotations

import difflib
import gzip
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..han_fold import fold_han
from .en_bases import (
    EN_BASE_PREFIXES_LOW,
    FALLBACK_BASE_SUGGESTIONS,
    POE1_ONLY_BASES,
    EnBaseCatalog,
    check_poe1_base_hallucination,
    has_cjk,
    load_en_base_whitelist,
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_RICH_LINK_RE = re.compile(r"\[[^\]|]+\|([^\]]+)\]|\[([^\]]+)\]")
_NUM_OR_RANGE_RE = re.compile(
    r"[+-]?\(\s*[+-]?\d+(?:\.\d+)?\s*-\s*[+-]?\d+(?:\.\d+)?\s*\)|[+-]?\d+(?:\.\d+)?"
)
_SLOT_RE = re.compile(r"\{(\d+)\}")
_PLUS_SPACE_RE = re.compile(r"[+\s]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_LEADING_TO_RE = re.compile(r"^\d+(?:\.\d+)?%?\s+to\s+", re.IGNORECASE)
_ZH_PUNCT_TRANS = str.maketrans({"，": ",", "：": ":", "（": "(", "）": ")", "％": "%"})

_ZH_BASE_PREFIXES: tuple[str, ...] = (
    "精良的",
    "卓越的",
    "非凡的",
    "符文师匠",
    "符文锻造",
    "符文",
    "进阶",
    "专家",
    "高阶",
)


def _discover_l10n_dir() -> Path | None:
    for env_key in ("POB2_DATA_DIR", "POB2_L10N_DIR"):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            p = Path(raw)
            if (p / "zh-rCN").is_dir():
                return p
            if (p / "Data" / "poe2" / "zh-rCN").is_dir():
                return p / "Data" / "poe2"
    eng = os.environ.get("POB2_ENGINE_DIR", "").strip()
    if eng:
        cand = Path(eng).parent / "Data" / "poe2"
        if (cand / "zh-rCN").is_dir():
            return cand
    return None


def _zh_stat_pattern(text: str) -> tuple[str, list[str]]:
    """Normalize a Chinese stat line or template into `(compact_pattern, extracted_values)`."""
    cleaned = _RICH_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", text) if "[" in text else text
    cleaned = fold_han(cleaned).strip()
    vals: list[str] = []
    if "{" in cleaned and _SLOT_RE.search(cleaned):
        pat = _SLOT_RE.sub("#", cleaned)
    else:

        def _cap(m: re.Match[str]) -> str:
            vals.append(m.group(0))
            return "#"

        pat = _NUM_OR_RANGE_RE.sub(_cap, cleaned)
    pat = _PLUS_SPACE_RE.sub("", pat).lower().translate(_ZH_PUNCT_TRANS)
    return pat, vals


class CraftL10nCatalog:
    """Indexed 1,768 PoE2 base whitelist, CN/TW base reverse-map, and O(1) CN/TW stat skeleton map."""

    def __init__(self, data_dir: Path | None = None) -> None:
        d = data_dir or _DATA_DIR
        self.base_to_slug: dict[str, str] = {}
        self.base_low_to_canon: dict[str, str] = {}
        self.zh_to_base: dict[str, str] = {}
        self.stat_pat_to_en: dict[str, tuple[str, tuple[int, ...]]] = {}
        self._en_bases_by_len: tuple[tuple[str, str], ...] | None = None
        self._zh_bases_by_len: tuple[tuple[str, str], ...] | None = None
        self._optional_items_loaded = False
        self._optional_stats_loaded = False
        self._load_bundled(d)

    def _register_stat_template(self, zh_tmpl: str, en_tmpl: str, *, overwrite: bool = False) -> None:
        if not zh_tmpl or not en_tmpl:
            return
        slot_order = tuple(int(x) for x in _SLOT_RE.findall(zh_tmpl)) if "{" in zh_tmpl else ()
        pat, _ = _zh_stat_pattern(zh_tmpl)
        if not pat:
            return
        if overwrite or pat not in self.stat_pat_to_en:
            self.stat_pat_to_en[pat] = (en_tmpl, slot_order)

    def _load_bundled(self, data_dir: Path) -> None:
        fp = data_dir / "poe2_craft_l10n.json.gz"
        if not fp.is_file():
            return
        obj: dict[str, Any] = json.loads(gzip.decompress(fp.read_bytes()))
        for en_name, slug in (obj.get("base_to_slug") or {}).items():
            s_en = str(en_name)
            self.base_to_slug[s_en] = str(slug)
            self.base_low_to_canon[s_en.lower()] = s_en
        for zh_name, en_name in (obj.get("zh_to_base") or {}).items():
            if en_name in self.base_to_slug:
                self.zh_to_base[fold_han(str(zh_name)).strip()] = str(en_name)
        for zh_tmpl, en_tmpl in (obj.get("zh_stat_templates") or {}).items():
            self._register_stat_template(str(zh_tmpl), str(en_tmpl), overwrite=True)

    def _ensure_optional_items_loaded(self) -> None:
        if self._optional_items_loaded:
            return
        self._optional_items_loaded = True
        l10n = _discover_l10n_dir()
        if l10n is None:
            return
        changed = False
        for loc in ("zh-rTW", "zh-rCN"):
            items_fp = l10n / loc / "items.json"
            if not items_fp.is_file():
                continue
            is_cn = loc == "zh-rCN"
            try:
                entries = (json.loads(items_fp.read_bytes()) or {}).get("entries") or {}
                for en, zh in entries.items():
                    if en in self.base_to_slug and isinstance(zh, str) and zh:
                        k = fold_han(zh).strip()
                        if is_cn or k not in self.zh_to_base:
                            self.zh_to_base[k] = en
                            changed = True
            except (OSError, json.JSONDecodeError):
                pass
        if changed:
            self._zh_bases_by_len = None

    def _ensure_optional_stats_loaded(self) -> None:
        if self._optional_stats_loaded:
            return
        self._optional_stats_loaded = True
        l10n = _discover_l10n_dir()
        if l10n is None:
            return
        for loc in ("zh-rTW", "zh-rCN"):
            stats_fp = l10n / loc / "stats.json"
            if not stats_fp.is_file():
                continue
            try:
                entries = (json.loads(stats_fp.read_bytes()) or {}).get("entries") or {}
                for en, zh in entries.items():
                    if isinstance(en, str) and isinstance(zh, str) and zh and en and len(zh) <= 160 and "\n\n" not in zh:
                        self._register_stat_template(zh, en, overwrite=False)
            except (OSError, json.JSONDecodeError):
                pass

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

    def _get_zh_bases_by_len(self) -> tuple[tuple[str, str], ...]:
        if self._zh_bases_by_len is None:
            self._zh_bases_by_len = tuple(
                sorted(
                    ((k, v) for k, v in self.zh_to_base.items() if len(k) >= 2),
                    key=lambda x: len(x[0]),
                    reverse=True,
                )
            )
        return self._zh_bases_by_len

    def _lookup_exact_once(self, s: str) -> tuple[str, str] | None:
        low = s.lower()
        canon = self.base_low_to_canon.get(low)
        if canon is not None:
            return canon, self.base_to_slug[canon]
        if s.isascii():
            for pref_low, plen in EN_BASE_PREFIXES_LOW:
                if low.startswith(pref_low):
                    sub = low[plen:].strip()
                    canon = self.base_low_to_canon.get(sub)
                    if canon is not None:
                        return canon, self.base_to_slug[canon]
            return None
        folded = fold_han(s)
        canon = self.zh_to_base.get(folded)
        if canon is not None:
            return canon, self.base_to_slug[canon]
        for pref_low, plen in EN_BASE_PREFIXES_LOW:
            if low.startswith(pref_low):
                sub = low[plen:].strip()
                canon = self.base_low_to_canon.get(sub)
                if canon is not None:
                    return canon, self.base_to_slug[canon]
        for pref in _ZH_BASE_PREFIXES:
            if folded.startswith(pref):
                sub = folded[len(pref) :].strip()
                canon = self.zh_to_base.get(sub)
                if canon is not None:
                    return canon, self.base_to_slug[canon]
        return None

    def lookup_exact_base(self, raw: str) -> tuple[str, str] | None:
        """Return `(canonical_base_name, poe2db_slug)` if *raw* matches one of the 1,768 PoE2 bases."""
        s = (raw or "").strip()
        if not s:
            return None
        hit = self._lookup_exact_once(s)
        if hit is not None:
            return hit
        if has_cjk(s) and not self._optional_items_loaded:
            self._ensure_optional_items_loaded()
            return self._lookup_exact_once(s)
        return None

    def extract_magic_base(self, raw: str) -> tuple[str, str] | None:
        """Strip Magic prefix/suffix affixes around a valid PoE2 EN/ZH base."""
        s = (raw or "").strip()
        if not s:
            return None
        if has_cjk(s):
            folded = fold_han(s)
            for zh_key, canon in self._get_zh_bases_by_len():
                if zh_key in folded:
                    return canon, self.base_to_slug[canon]
            if not self._optional_items_loaded:
                self._ensure_optional_items_loaded()
                for zh_key, canon in self._get_zh_bases_by_len():
                    if zh_key in folded:
                        return canon, self.base_to_slug[canon]
            return None
        if " " not in s:
            return None
        padded = f" {s.lower()} "
        for en_low, canon in self._get_en_bases_by_len():
            if f" {en_low} " in padded:
                return canon, self.base_to_slug[canon]
        return None

    def suggest_bases(self, query: str, *, limit: int = 5) -> list[str]:
        """Return Top-*limit* closest valid PoE2 base names for an unknown base error."""
        q = (query or "").strip()
        if not q:
            return list(FALLBACK_BASE_SUGGESTIONS[:limit])
        if has_cjk(q):
            if not self._optional_items_loaded:
                self._ensure_optional_items_loaded()
            folded = fold_han(q)
            zh_hits = difflib.get_close_matches(folded, list(self.zh_to_base.keys()), n=limit, cutoff=0.25)
            out = [f"{z} ({self.zh_to_base[z]})" for z in zh_hits]
            if len(out) < limit:
                for fallback in FALLBACK_BASE_SUGGESTIONS:
                    if fallback not in out and len(out) < limit:
                        out.append(fallback)
            return out[:limit]
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

    def translate_zh_stat_line(self, raw_line: str) -> tuple[str, bool]:
        """Translate a Simplified/Traditional Chinese stat line into PoE2 EN mod text."""
        if not has_cjk(raw_line):
            return raw_line, True
        pat, vals = _zh_stat_pattern(raw_line)
        hit = self.stat_pat_to_en.get(pat)
        if hit is None and not self._optional_stats_loaded:
            self._ensure_optional_stats_loaded()
            hit = self.stat_pat_to_en.get(pat)
        if hit is None:
            return raw_line, False
        en_tmpl, slot_order = hit
        if not slot_order:
            return en_tmpl, True
        if len(vals) != len(slot_order):
            return raw_line, False
        val_by_slot = dict(zip(slot_order, vals, strict=False))

        def _sub(m: re.Match[str]) -> str:
            return val_by_slot.get(int(m.group(1)), "0")

        rendered = _SLOT_RE.sub(_sub, en_tmpl)
        if _LEADING_TO_RE.match(rendered):
            rendered = f"+{rendered}"
        return rendered, True


@lru_cache(maxsize=2)
def _load_craft_l10n_cached(resolved_dir: Path) -> CraftL10nCatalog:
    return CraftL10nCatalog(resolved_dir)


def load_craft_l10n(data_dir: Path | None = None) -> CraftL10nCatalog:
    """Load and cache the bundled PoE2 0.5.5 craft l10n & 1,768-base whitelist catalog."""
    return _load_craft_l10n_cached((data_dir or _DATA_DIR).resolve())


load_craft_l10n.cache_clear = _load_craft_l10n_cached.cache_clear  # type: ignore[attr-defined]
load_craft_l10n.cache_info = _load_craft_l10n_cached.cache_info  # type: ignore[attr-defined]


__all__ = [
    "POE1_ONLY_BASES",
    "CraftL10nCatalog",
    "EnBaseCatalog",
    "check_poe1_base_hallucination",
    "has_cjk",
    "load_craft_l10n",
    "load_en_base_whitelist",
]
