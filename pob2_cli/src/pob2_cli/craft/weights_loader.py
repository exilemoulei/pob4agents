# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Zero-SQLite loader for bundled PoE2 0.5.5 ModsView weights, overrides, and base resolution."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..han_fold import fold_han
from .base_catalog import (
    SPECIAL_BASE_SPECS,
    ZH_BASE_ALIASES,
    build_static_levers,
)
from .en_bases import (
    check_poe1_base_hallucination,
    has_cjk,
    load_en_base_whitelist,
)
from .models import AffixSide, BaseItemEntry, LeverEntry, ModEntry, UnknownBaseError
from .slug_compiler import (
    LazyModsBySlug,
    collect_forced_levers_from_raw,
    load_optional_pob2_bases,
    norm_key,
    strip_numbers_to_pattern,
)
from .zh_lookup import load_craft_l10n

EXPECTED_GAME_PATCH = "0.5.5"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _slot_limits_for_slug(slug: str) -> tuple[int, int]:
    if "Flask" in slug or slug == "Charms":
        return 1, 1
    if any(k in slug for k in ("Tablet", "Ruby", "Emerald", "Sapphire", "Diamond", "Relic")):
        return 2, 2
    return 3, 3


@dataclass(slots=True)
class CraftDataBundle:
    patch: str
    patch_mismatch_warning: str | None
    overrides: dict[str, Any]
    mods_by_slug: dict[str, tuple[ModEntry, ...]]
    measured_slugs: frozenset[str]
    removed_levers: frozenset[str]
    dormant_poe1_orbs: frozenset[str]
    data_dir: Path | None = None
    _raw_classes: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _slug_low_map: dict[str, str] = field(default_factory=dict, repr=False)
    _levers_cache: tuple[LeverEntry, ...] | None = field(default=None, repr=False)
    _levers_by_key_cache: dict[str, LeverEntry] | None = field(default=None, repr=False)
    _base_name_to_slug_cache: dict[str, str] | None = field(default=None, repr=False)
    _global_pat_cache: frozenset[str] | None = field(default=None, repr=False)
    _inspector_slug_cache: dict[str, tuple[dict[str, tuple[ModEntry, ...]], frozenset[str]]] = field(
        default_factory=dict, repr=False
    )

    def _ensure_levers(self) -> None:
        if self._levers_cache is not None and self._levers_by_key_cache is not None:
            return
        forced_by_lever = collect_forced_levers_from_raw(self._raw_classes)
        levers = build_static_levers()
        seen_ids = {lv.lever_id for lv in levers}
        for lname, (cat, forced_map) in sorted(forced_by_lever.items()):
            lid = norm_key(lname)
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            applies = (
                "magic->rare"
                if cat == "essence"
                else (
                    "rare_jewel->remove_1_add_1_forced"
                    if cat == "liquid_emotion"
                    else "rare->remove_1_add_1_forced"
                )
            )
            levers.append(
                LeverEntry(
                    lever_id=lid,
                    name=lname,
                    category=cat,
                    is_active_drop_in_patch=True,
                    applies_to_rarities=applies,
                    forced_mods=forced_map,
                )
            )
        by_key: dict[str, LeverEntry] = {}
        for lv in levers:
            by_key[lv.lever_id] = lv
            by_key[norm_key(lv.name)] = lv
        self._levers_cache = tuple(levers)
        self._levers_by_key_cache = by_key

    @property
    def levers(self) -> tuple[LeverEntry, ...]:
        self._ensure_levers()
        assert self._levers_cache is not None
        return self._levers_cache

    @property
    def levers_by_key(self) -> dict[str, LeverEntry]:
        self._ensure_levers()
        assert self._levers_by_key_cache is not None
        return self._levers_by_key_cache

    @property
    def base_name_to_slug(self) -> dict[str, str]:
        if self._base_name_to_slug_cache is None:
            base_map: dict[str, str] = {k: v[1] for k, v in SPECIAL_BASE_SPECS.items()}
            load_optional_pob2_bases(base_map)
            self._base_name_to_slug_cache = base_map
        return self._base_name_to_slug_cache

    def get_inspector_slug_index(
        self, slug: str
    ) -> tuple[dict[str, tuple[ModEntry, ...]], frozenset[str]]:
        cached = self._inspector_slug_cache.get(slug)
        if cached is not None:
            return cached
        all_tiers = self.mods_by_slug.get(slug, ())
        buckets: dict[str, list[ModEntry]] = {}
        hybrid_lines: set[str] = set()
        for m in all_tiers:
            if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX):
                buckets.setdefault(strip_numbers_to_pattern(m.stat_text), []).append(m)
                if "\n" in m.stat_text:
                    for ln in m.stat_text.splitlines():
                        ln = ln.strip()
                        if ln:
                            hybrid_lines.add(strip_numbers_to_pattern(ln))
        pat_map = {k: tuple(v) for k, v in buckets.items()}
        res = (pat_map, frozenset(hybrid_lines))
        self._inspector_slug_cache[slug] = res
        return res

    def get_global_mod_patterns(self) -> frozenset[str]:
        if self._global_pat_cache is not None:
            return self._global_pat_cache
        raw_texts: set[str] = set()
        if self._raw_classes:
            for info in self._raw_classes.values():
                pools = info.get("pools") or {}
                for pname in ("normal", "desecrated", "essence", "perfect_essence", "liquid"):
                    for item in pools.get(pname) or ():
                        if item.get("gen_type_id") in (1, 2):
                            st = item.get("stat_text")
                            if st:
                                raw_texts.add(str(st))
        else:
            for mods in self.mods_by_slug.values():
                for m in mods:
                    if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX) and m.stat_text:
                        raw_texts.add(m.stat_text)
        pats: set[str] = set()
        for st in raw_texts:
            pats.add(strip_numbers_to_pattern(st))
            if "\n" in st:
                for ln in st.splitlines():
                    ln = ln.strip()
                    if ln:
                        pats.add(strip_numbers_to_pattern(ln))
        self._global_pat_cache = frozenset(pats)
        return self._global_pat_cache


@lru_cache(maxsize=2)
def _load_craft_data_cached(resolved_dir: Path) -> CraftDataBundle:
    """Load bundled ModsView weights & overrides once per process with lazy per-slug compilation."""
    d = resolved_dir
    mv = json.loads(gzip.decompress((d / "modsview_weights.json.gz").read_bytes()))
    ov_path = d / "weights_overrides.json"
    overrides = json.loads(ov_path.read_bytes()) if ov_path.is_file() else {}

    patch = str(mv.get("patch") or overrides.get("patch") or "unknown")
    mismatch_warn: str | None = None
    if patch != EXPECTED_GAME_PATCH:
        mismatch_warn = (
            f"[待复核] 词缀权重包补丁版本 ({patch}) 与预期 {EXPECTED_GAME_PATCH} 不一致，需复核。"
        )

    raw_classes: dict[str, dict[str, Any]] = mv.get("classes") or {}
    measured_slugs = frozenset(
        slug for slug, info in raw_classes.items() if info.get("has_measured_normal_weights")
    )
    slug_low_map = {slug.lower(): slug for slug in raw_classes}

    return CraftDataBundle(
        patch=patch,
        patch_mismatch_warning=mismatch_warn,
        overrides=overrides,
        mods_by_slug=LazyModsBySlug(raw_classes, overrides),
        measured_slugs=measured_slugs,
        removed_levers=frozenset(norm_key(x) for x in overrides.get("removed_levers_in_patch", [])),
        dormant_poe1_orbs=frozenset(
            norm_key(x) for x in overrides.get("dormant_poe1_currencies_blacklist", [])
        ),
        data_dir=d,
        _raw_classes=raw_classes,
        _slug_low_map=slug_low_map,
    )


def load_craft_data(data_dir: Path | None = None) -> CraftDataBundle:
    """Load bundled ModsView weights & overrides once per process with lazy per-slug compilation."""
    return _load_craft_data_cached((data_dir or _DATA_DIR).resolve())


load_craft_data.cache_clear = _load_craft_data_cached.cache_clear  # type: ignore[attr-defined]
load_craft_data.cache_info = _load_craft_data_cached.cache_info  # type: ignore[attr-defined]


def _make_base_entry(
    bundle: CraftDataBundle,
    *,
    name: str,
    slug: str,
    entry_id: str | None = None,
    drop_level: int = 1,
    tags: tuple[str, ...] = ("default",),
) -> BaseItemEntry:
    max_p, max_s = _slot_limits_for_slug(slug)
    return BaseItemEntry(
        id=entry_id or f"Base/{slug}/{norm_key(name)}",
        name=name,
        item_class=slug,
        poe2db_slug=slug,
        drop_level=drop_level,
        max_prefixes=max_p,
        max_suffixes=max_s,
        tags=tags,
        has_measured_weights=slug in bundle.measured_slugs,
    )


def _resolve_canonical_or_special(
    bundle: CraftDataBundle, canon: str, slug: str
) -> BaseItemEntry:
    low_c = canon.lower()
    if low_c in SPECIAL_BASE_SPECS:
        sname, sslug, dlvl, tags = SPECIAL_BASE_SPECS[low_c]
        return _make_base_entry(bundle, name=sname, slug=sslug, drop_level=dlvl, tags=tags)
    return _make_base_entry(bundle, name=canon, slug=slug)


def _resolve_ascii_fallbacks(bundle: CraftDataBundle, raw_en: str) -> BaseItemEntry | None:
    stripped_en = raw_en
    for pref in ("Superior ", "Exceptional ", "Runeforged ", "Runemastered ", "Expert ", "Advanced "):
        if stripped_en.lower().startswith(pref.lower()):
            stripped_en = stripped_en[len(pref) :].strip()
    low = stripped_en.lower()
    if low in SPECIAL_BASE_SPECS:
        name, slug, dlvl, tags = SPECIAL_BASE_SPECS[low]
        return _make_base_entry(bundle, name=name, slug=slug, drop_level=dlvl, tags=tags)
    slug_probe = stripped_en.replace(" ", "_").lower()
    matched_slug = bundle._slug_low_map.get(slug_probe)
    if matched_slug is None and not bundle._slug_low_map:
        matched_slug = next((s for s in bundle.mods_by_slug if s.lower() == slug_probe), None)
    if matched_slug is not None:
        return _make_base_entry(bundle, name=stripped_en, slug=matched_slug, entry_id=f"Slug/{matched_slug}")
    if low in bundle.base_name_to_slug:
        return _make_base_entry(bundle, name=stripped_en, slug=bundle.base_name_to_slug[low])
    return None


def resolve_base_entry(bundle: CraftDataBundle, base_name_or_slug: str) -> BaseItemEntry:
    """Resolve an EN/CN/TW PoE2 0.5.5 base item or class slug strictly against the 1,768-base whitelist."""
    orig = (base_name_or_slug or "").strip()
    if not orig:
        en_wl = load_en_base_whitelist(bundle.data_dir)
        raise UnknownBaseError(orig, tuple(en_wl.suggest_bases("", limit=5)))

    # 1. Hard-block known PoE1-only bases/uniques before any lookup.
    check_poe1_base_hallucination(orig)

    # 2. Pure non-CJK path: never load poe2_craft_l10n.json.gz on English items.
    if not has_cjk(orig):
        en_wl = load_en_base_whitelist(bundle.data_dir)
        exact_en = en_wl.lookup_exact_base(orig)
        if exact_en is not None:
            return _resolve_canonical_or_special(bundle, exact_en[0], exact_en[1])
        fb = _resolve_ascii_fallbacks(bundle, orig)
        if fb is not None:
            return fb
        magic_en = en_wl.extract_magic_base(orig)
        if magic_en is not None:
            return _resolve_canonical_or_special(bundle, magic_en[0], magic_en[1])
        raise UnknownBaseError(base_name_or_slug, tuple(en_wl.suggest_bases(orig, limit=5)))

    # 3. CJK path: check fast static ZH aliases first, then bundled CN/TW craft l10n.
    raw = orig
    folded = fold_han(raw)
    if raw in ZH_BASE_ALIASES:
        raw = ZH_BASE_ALIASES[raw]
    elif folded in ZH_BASE_ALIASES:
        raw = ZH_BASE_ALIASES[folded]

    if not has_cjk(raw):
        en_wl = load_en_base_whitelist(bundle.data_dir)
        exact_alias = en_wl.lookup_exact_base(raw)
        if exact_alias is not None:
            return _resolve_canonical_or_special(bundle, exact_alias[0], exact_alias[1])
        fb_alias = _resolve_ascii_fallbacks(bundle, raw)
        if fb_alias is not None:
            return fb_alias

    l10n = load_craft_l10n(bundle.data_dir)
    exact_wl = l10n.lookup_exact_base(raw)
    if exact_wl is not None:
        return _resolve_canonical_or_special(bundle, exact_wl[0], exact_wl[1])

    fb_zh = _resolve_ascii_fallbacks(bundle, raw)
    if fb_zh is not None:
        return fb_zh

    magic_hit = l10n.extract_magic_base(orig)
    if magic_hit is not None:
        return _resolve_canonical_or_special(bundle, magic_hit[0], magic_hit[1])

    raise UnknownBaseError(base_name_or_slug, tuple(l10n.suggest_bases(orig, limit=5)))


__all__ = [
    "EXPECTED_GAME_PATCH",
    "CraftDataBundle",
    "LazyModsBySlug",
    "load_craft_data",
    "norm_key",
    "resolve_base_entry",
    "strip_numbers_to_pattern",
]
