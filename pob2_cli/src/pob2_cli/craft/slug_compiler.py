# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Lazy per-slug ModsView compiler and pattern normalization helpers for `weights_loader` (L2)."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from .base_catalog import ELEMENT_LOCK_MAP
from .models import AffixSide, ModEntry, WeightConfidence

_GEN_TYPE_TO_SIDE: dict[int, AffixSide] = {
    1: AffixSide.PREFIX,
    2: AffixSide.SUFFIX,
    3: AffixSide.IMPLICIT,
    5: AffixSide.NONE,
}
_CASTER_WEAPON_SLUGS: frozenset[str] = frozenset({"Wands", "Staves"})
_NORM_KEY_RE = re.compile(r"[^a-z0-9]+")
_PAREN_RANGE_RE = re.compile(r"[+-]?\([^)]*\)")
_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_MULTI_WS_RE = re.compile(r"\s+")
_TRAILING_DIGITS_RE = re.compile(r"\d+_?$")


def norm_key(text: str) -> str:
    return _NORM_KEY_RE.sub("_", text.strip().lower()).strip("_")


@lru_cache(maxsize=8192)
def strip_numbers_to_pattern(text: str) -> str:
    s = _PAREN_RANGE_RE.sub("#", text)
    s = _NUMBER_RE.sub("#", s)
    return _MULTI_WS_RE.sub(" ", s).strip().lower()


def _infer_adds_tags(
    slug: str, mod_id: str, tags: tuple[str, ...], stat_text: str
) -> tuple[str, ...]:
    """Infer PoE2 caster-weapon single-element lock tags (`no_*_spell_mods`)."""
    if slug not in _CASTER_WEAPON_SLUGS:
        return ()
    if "caster" not in tags and "Spell" not in stat_text:
        return ()
    low_id = mod_id.lower()
    low_txt = stat_text.lower()
    if not (
        "spell" in low_txt
        or "damageprefixonweapon" in low_id
        or "damageprefixontwohandweapon" in low_id
        or "spellgemslevel" in low_id
    ):
        return ()
    present = [el for el in ELEMENT_LOCK_MAP if el in tags]
    if len(present) == 1:
        return ELEMENT_LOCK_MAP[present[0]]
    return ()


def load_optional_pob2_bases(base_map: dict[str, str]) -> None:
    """Optionally enrich base_name -> slug from PoB2 `Data/Bases/*.lua` via env var."""
    env_dir = os.environ.get("POB2_ENGINE_DIR") or os.environ.get("POB2_DATA_DIR")
    if not env_dir:
        return
    bases_dir = Path(env_dir) / "Data" / "Bases"
    if not bases_dir.is_dir():
        bases_dir = Path(env_dir) / "PoE2" / "Data" / "Bases"
    if not bases_dir.is_dir():
        return
    file_to_slug = {
        "wand.lua": "Wands",
        "sceptre.lua": "Sceptres",
        "staff.lua": "Staves",
        "bow.lua": "Bows",
        "crossbow.lua": "Crossbows",
        "quiver.lua": "Quivers",
        "amulet.lua": "Amulets",
        "ring.lua": "Rings",
        "belt.lua": "Belts",
        "spear.lua": "Spears",
        "flail.lua": "Flails",
        "dagger.lua": "Daggers",
        "claw.lua": "Claws",
    }
    name_re = re.compile(r'itemBases\["([^"]+)"\]\s*=')
    for fname, slug in file_to_slug.items():
        fp = bases_dir / fname
        if not fp.is_file():
            continue
        try:
            txt = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in name_re.finditer(txt):
            base_map.setdefault(m.group(1).lower(), slug)


def compile_slug_mods(
    slug: str,
    info: dict[str, Any],
    overrides: dict[str, Any],
    forced_by_lever: dict[str, tuple[str, dict[str, str]]] | None = None,
) -> tuple[ModEntry, ...]:
    has_measured = bool(info.get("has_measured_normal_weights", False))
    defaults = overrides.get("defaults", {})
    desec_default_w = int(defaults.get("desecrated_unomen_weight", 2500))
    unmeasured_default_w = int(defaults.get("unmeasured_normal_pool_weight", 1000))
    mod_overrides: dict[str, Any] = overrides.get("mod_weight_overrides", {})
    pools = info.get("pools", {})

    ladders: dict[tuple[str, AffixSide, str, str], list[tuple[int, dict[str, Any]]]] = {}
    for pname in ("normal", "desecrated", "essence", "perfect_essence", "liquid"):
        for idx, item in enumerate(pools.get(pname, [])):
            side = _GEN_TYPE_TO_SIDE.get(int(item.get("gen_type_id") or 0), AffixSide.NONE)
            if side not in (AffixSide.PREFIX, AffixSide.SUFFIX):
                continue
            fams = tuple(str(f) for f in (item.get("families") or []) if f) or ("Unknown",)
            pat = strip_numbers_to_pattern(str(item.get("stat_text") or ""))
            eff_pool = "alloy" if (pname == "perfect_essence" and item.get("is_alloy")) else pname
            ladders.setdefault((eff_pool, side, fams[0], pat), []).append((idx, item))

    entries: list[ModEntry] = []
    for (eff_pool, side, fam0, _pat), members in ladders.items():
        members.sort(key=lambda x: (int(x[1].get("level") or 1), str(x[1].get("mod_id") or ""), x[0]))
        total_t = len(members)
        for rank0, (idx, item) in enumerate(members):
            level = int(item.get("level") or 1)
            raw_id = item.get("mod_id")
            mod_id = str(raw_id) if raw_id else f"{slug}_{eff_pool}_{fam0}_{side.value}_L{level}_{idx}"
            fams = tuple(str(f) for f in (item.get("families") or []) if f) or (fam0,)
            tags = tuple(str(t) for t in (item.get("tags") or []) if t)
            stat_text = str(item.get("stat_text") or "")
            raw_w = int(item.get("drop_chance") or 0)
            boss_tag = str(item["boss_tag"]) if item.get("boss_tag") else None
            name_field = str(item.get("name") or "")
            is_alloy = bool(item.get("is_alloy")) or eff_pool == "alloy"
            removes_mod = bool(item.get("removes_mod")) or eff_pool in (
                "perfect_essence",
                "alloy",
                "liquid",
            )
            currency_name: str | None = None

            if eff_pool in ("essence", "perfect_essence", "alloy", "liquid"):
                currency_name = name_field
                weight = 0
                confidence = WeightConfidence.EXACT_DETERMINISTIC
                if forced_by_lever is not None and currency_name:
                    cat = (
                        "alloy"
                        if is_alloy
                        else (
                            "liquid_emotion"
                            if eff_pool == "liquid"
                            else (
                                "perfect_or_corrupted_essence"
                                if eff_pool == "perfect_essence"
                                else "essence"
                            )
                        )
                    )
                    _, fdict = forced_by_lever.setdefault(currency_name, (cat, {}))
                    fdict.setdefault(slug, mod_id)
            elif eff_pool == "desecrated":
                weight = raw_w if raw_w > 1 else desec_default_w
                confidence = WeightConfidence.EMPIRICAL_ESTIMATE
            else:
                ov = mod_overrides.get(f"{slug}:{mod_id}")
                if ov and isinstance(ov, dict):
                    weight = int(ov.get("weight", unmeasured_default_w))
                    confidence = WeightConfidence.EMPIRICAL_ESTIMATE
                elif has_measured:
                    weight = raw_w
                    confidence = WeightConfidence.VERIFIED_COMMUNITY
                else:
                    weight = raw_w if raw_w > 1 else unmeasured_default_w
                    confidence = WeightConfidence.UNMEASURED

            mod_type = _TRAILING_DIGITS_RE.sub("", mod_id) or fam0
            entries.append(
                ModEntry(
                    mod_id=mod_id,
                    mod_type=mod_type,
                    family_id=fam0,
                    families=fams,
                    base_group_slug=slug,
                    source_pool=eff_pool,
                    affix_side=side,
                    affix_name="" if currency_name else name_field,
                    internal_rank_asc=rank0 + 1,
                    display_tier=total_t - rank0,
                    required_level=level,
                    weight=weight,
                    weight_confidence=confidence,
                    boss_tag=boss_tag,
                    currency_name=currency_name,
                    is_alloy=is_alloy,
                    removes_mod=removes_mod,
                    adds_tags=_infer_adds_tags(slug, mod_id, tags, stat_text),
                    mod_tags=tags,
                    stat_text=stat_text,
                )
            )
    return tuple(entries)


def collect_forced_levers_from_raw(
    raw_classes: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, dict[str, str]]]:
    forced_by_lever: dict[str, tuple[str, dict[str, str]]] = {}
    for slug, info in raw_classes.items():
        pools = info.get("pools") or {}
        ladders: dict[tuple[str, AffixSide, str, str], list[tuple[int, dict[str, Any]]]] = {}
        for pname in ("essence", "perfect_essence", "liquid"):
            for idx, item in enumerate(pools.get(pname) or ()):
                side = _GEN_TYPE_TO_SIDE.get(int(item.get("gen_type_id") or 0), AffixSide.NONE)
                if side not in (AffixSide.PREFIX, AffixSide.SUFFIX):
                    continue
                fams = tuple(str(f) for f in (item.get("families") or ()) if f) or ("Unknown",)
                pat = strip_numbers_to_pattern(str(item.get("stat_text") or ""))
                eff_pool = "alloy" if (pname == "perfect_essence" and item.get("is_alloy")) else pname
                ladders.setdefault((eff_pool, side, fams[0], pat), []).append((idx, item))
        for (eff_pool, side, fam0, _pat), members in ladders.items():
            members.sort(key=lambda x: (int(x[1].get("level") or 1), str(x[1].get("mod_id") or ""), x[0]))
            for idx, item in members:
                currency_name = str(item.get("name") or "")
                if not currency_name:
                    continue
                level = int(item.get("level") or 1)
                raw_id = item.get("mod_id")
                mod_id = str(raw_id) if raw_id else f"{slug}_{eff_pool}_{fam0}_{side.value}_L{level}_{idx}"
                is_alloy = bool(item.get("is_alloy")) or eff_pool == "alloy"
                cat = (
                    "alloy"
                    if is_alloy
                    else (
                        "liquid_emotion"
                        if eff_pool == "liquid"
                        else (
                            "perfect_or_corrupted_essence"
                            if eff_pool == "perfect_essence"
                            else "essence"
                        )
                    )
                )
                _, fdict = forced_by_lever.setdefault(currency_name, (cat, {}))
                fdict.setdefault(slug, mod_id)
    return forced_by_lever


class LazyModsBySlug(dict[str, tuple[ModEntry, ...]]):
    """On-demand per-slug ModEntry compiler avoiding eager 81-class compilation on single-item inspect."""

    def __init__(self, raw_classes: dict[str, dict[str, Any]], overrides: dict[str, Any]) -> None:
        super().__init__()
        self._raw_classes = raw_classes
        self._overrides = overrides

    def _compile_one(self, slug: str) -> tuple[ModEntry, ...]:
        compiled = compile_slug_mods(slug, self._raw_classes[slug], self._overrides, None)
        super().__setitem__(slug, compiled)
        return compiled

    def _compile_all(self) -> None:
        for slug in self._raw_classes:
            if not super().__contains__(slug):
                self._compile_one(slug)

    def __contains__(self, key: object) -> bool:
        return key in self._raw_classes or super().__contains__(key)

    def __len__(self) -> int:
        return len(self._raw_classes)

    def __iter__(self) -> Iterator[str]:
        return iter(self._raw_classes)

    def keys(self) -> Any:
        return self._raw_classes.keys()

    def __getitem__(self, slug: str) -> tuple[ModEntry, ...]:
        if super().__contains__(slug):
            return super().__getitem__(slug)
        if slug in self._raw_classes:
            return self._compile_one(slug)
        raise KeyError(slug)

    def get(self, slug: str, default: Any = None) -> Any:  # type: ignore[override]
        if super().__contains__(slug):
            return super().__getitem__(slug)
        if slug in self._raw_classes:
            return self._compile_one(slug)
        return default

    def values(self) -> Any:
        self._compile_all()
        return super().values()

    def items(self) -> Any:
        self._compile_all()
        return super().items()


__all__ = [
    "LazyModsBySlug",
    "collect_forced_levers_from_raw",
    "compile_slug_mods",
    "load_optional_pob2_bases",
    "norm_key",
    "strip_numbers_to_pattern",
]
