# SPDX-License-Identifier: GPL-3.0-only
"""Rich-text cleaner and CN/TW -> PoB2 EN reverse translator for WeGame imports (L1).

Handles:
  - Stripping GGG keyword markup `[Key|Display Text]` -> `Display Text` and jewel roll ranges.
  - Detecting `[ShamanOnlyMods|羁绊]` / `Bonded:` lines and non-stat quest counters (`毁面首领`).
  - Reverse lookup priority: local `Data/poe2/zh-rCN` -> folded `zh-rTW` -> bundled 0.5.5
    ModRunes/Gems tables + 38-group `guardrails.zh_mistranslations` normalization.
  - Gem `(zh_name, is_support)` disambiguation preferring regular gems over item-granted variants.
  - Full-width & half-width catalyst quality (`品质（火焰词缀）`, `[Quality|品质]`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..guardrails import POE1_ZH_TO_POE2_ALIAS, ZH_MISTRANSLATION_TRAPS
from .wegame_stat_patterns import (
    CATALYST_PAREN_RE,
    HAS_CJK_RE,
    clean_wegame_text,
    has_shaman_bonded_marker,
    is_progress_counter_stat,
    match_builtin_stat_pattern,
    norm_wegame_key,
    parse_leading_int,
    strip_shaman_bonded_prefix,
)
from .wegame_tables import (
    BUNDLED_COMMON_BASES,
    BUNDLED_COMMON_UNIQUES,
    CATALYST_ZH_TO_EN,
    RUNE_CN_OVERRIDES,
    RUNE_CN_TO_TW_BRIDGES,
    load_bundled_runes_gems,
)


@dataclass(frozen=True)
class GemCandidate:
    """Single PoE2 skill/support gem metadata record."""

    name: str
    is_support: bool
    gem_id: str
    skill_id: str
    variant_id: str
    is_item_granted: bool = False


@dataclass(frozen=True)
class GemResolution:
    """Result of resolving a WeGame gem by `(zh_name, is_support)`."""

    candidate: GemCandidate | None
    ambiguous: bool = False
    warning: str | None = None


_norm_key = norm_wegame_key


def _discover_l10n_dir(explicit_dir: Path | None) -> Path | None:
    if explicit_dir and explicit_dir.is_dir():
        return explicit_dir
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


def _load_entries(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = obj.get("entries") if isinstance(obj, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {str(k): str(v) for k, v in entries.items() if isinstance(k, str) and isinstance(v, str)}


class WeGameTranslator:
    """CN/TW -> PoB2 EN reverse translator combining bundled 0.5.5 catalogs & optional Data/poe2."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        extra_gems: list[GemCandidate] | None = None,
        extra_gem_zh: dict[tuple[str, bool], list[GemCandidate]] | None = None,
    ) -> None:
        self.l10n_dir = _discover_l10n_dir(data_dir)
        self._runes_by_zh: dict[str, str] = {}
        self._items_by_zh: dict[str, str] = {}
        self._uniques_by_zh: dict[str, str] = {}
        self._stats_exact: dict[str, str] = {}
        self._gems_by_key: dict[tuple[str, bool], list[GemCandidate]] = {}
        self._known_en_runes: set[str] = set()
        self._valid_poe2_bases: frozenset[str] = frozenset()
        self._load_bundled()
        self._load_mistranslation_bridges()
        if self.l10n_dir is not None:
            self._load_l10n_dir(self.l10n_dir)
        if extra_gems:
            for cand in extra_gems:
                self._register_gem(cand.name, cand)
        if extra_gem_zh:
            for (zh_name, is_sup), cands in extra_gem_zh.items():
                key = (_norm_key(zh_name), is_sup)
                bucket = self._gems_by_key.setdefault(key, [])
                for c in cands:
                    if c not in bucket:
                        bucket.append(c)

    @property
    def valid_poe2_bases(self) -> frozenset[str]:
        """Return the set of all 0.5.5 canonical PoE2 itemBases names."""
        return self._valid_poe2_bases

    def _register_gem(self, label: str, cand: GemCandidate) -> None:
        if not label:
            return
        for k in (label.strip(), _norm_key(label)):
            if not k:
                continue
            bucket = self._gems_by_key.setdefault((k, cand.is_support), [])
            if cand not in bucket:
                bucket.append(cand)

    def _load_bundled(self) -> None:
        payload = load_bundled_runes_gems()
        raw_bases = payload.get("bases", ())
        self._valid_poe2_bases = frozenset(str(b) for b in raw_bases if isinstance(b, str) and b)

        for zh, en in BUNDLED_COMMON_BASES.items():
            if not self._valid_poe2_bases or en in self._valid_poe2_bases:
                self._items_by_zh[_norm_key(zh)] = en
        for zh, en in BUNDLED_COMMON_UNIQUES.items():
            self._uniques_by_zh[_norm_key(zh)] = en
        for zh, en in RUNE_CN_OVERRIDES.items():
            self._runes_by_zh[_norm_key(zh)] = en

        for row in payload.get("runes", ()):
            if len(row) < 3:
                continue
            en, zh_cn, zh_tw_f = str(row[0]), str(row[1]), str(row[2])
            self._known_en_runes.add(en)
            self._runes_by_zh.setdefault(en.lower(), en)
            if zh_tw_f:
                self._runes_by_zh.setdefault(_norm_key(zh_tw_f), en)
            if zh_cn:
                self._runes_by_zh[_norm_key(zh_cn)] = en

        for row in payload.get("gems", ()):
            if len(row) < 8:
                continue
            cand = GemCandidate(
                name=str(row[0]),
                is_support=bool(row[1]),
                gem_id=str(row[2]),
                skill_id=str(row[3]),
                variant_id=str(row[4]),
                is_item_granted=bool(row[5]),
            )
            self._register_gem(cand.name, cand)
            if row[7]:
                self._register_gem(str(row[7]), cand)
            if row[6]:
                self._register_gem(str(row[6]), cand)

    def _load_mistranslation_bridges(self) -> None:
        # Link T2.1 38-group CN mistranslation table without injecting non-PoE2-base names.
        for poe1_zh, (en_canon, poe2_zh) in POE1_ZH_TO_POE2_ALIAS.items():
            if self._valid_poe2_bases and en_canon in self._valid_poe2_bases:
                self._items_by_zh.setdefault(_norm_key(poe1_zh), en_canon)
                self._items_by_zh.setdefault(_norm_key(poe2_zh), en_canon)
        for trap in ZH_MISTRANSLATION_TRAPS:
            if trap.en_term and trap.poe2_zh and "{" not in trap.en_term and "/" not in trap.en_term:
                self._stats_exact.setdefault(_norm_key(trap.poe2_zh), trap.en_term)

    def _load_l10n_dir(self, l10n_dir: Path) -> None:
        # zh-rTW first (fallback), then zh-rCN (priority override).
        # Filter items.json strictly by valid PoE2 0.5.5 itemBases to prevent PoE1 leftover
        # entries (e.g. `Viridian Jewel` overwriting `Emerald`) from causing #2582 item drops.
        valid_bases = self._valid_poe2_bases
        for loc in ("zh-rTW", "zh-rCN"):
            loc_dir = l10n_dir / loc
            if not loc_dir.is_dir():
                continue
            is_cn = loc == "zh-rCN"
            for en, zh in _load_entries(loc_dir / "items.json").items():
                key = _norm_key(zh)
                if (not valid_bases or en in valid_bases) and (is_cn or key not in self._items_by_zh):
                    self._items_by_zh[key] = en
                if en in self._known_en_runes and (is_cn or key not in self._runes_by_zh):
                    self._runes_by_zh[key] = en
            for en, zh in _load_entries(loc_dir / "uniques.json").items():
                key = _norm_key(zh)
                if is_cn or key not in self._uniques_by_zh:
                    self._uniques_by_zh[key] = en
            for en, zh in _load_entries(loc_dir / "stats.json").items():
                if "{" not in zh and "{" not in en:
                    key = _norm_key(zh)
                    if is_cn or key not in self._stats_exact:
                        self._stats_exact[key] = en
            for en, zh in _load_entries(loc_dir / "gems.json").items():
                for is_sup in (False, True):
                    cands = self._gems_by_key.get((en, is_sup))
                    if cands:
                        for c in cands:
                            self._register_gem(zh, c)

    def translate_rune(self, raw_name: str) -> tuple[str, bool]:
        """Reverse-translate a socketed Rune / Soul Core / Idol / Gaze name to PoB2 EN."""
        cleaned = clean_wegame_text(raw_name)
        if not cleaned or cleaned.lower() == "none":
            return "", True
        if not HAS_CJK_RE.search(cleaned):
            return cleaned, True

        key = _norm_key(cleaned)
        if key in self._runes_by_zh:
            return self._runes_by_zh[key], True

        bridged = key
        for cn_tok, tw_tok in RUNE_CN_TO_TW_BRIDGES:
            if cn_tok in bridged:
                cand = bridged.replace(cn_tok, tw_tok)
                if cand in self._runes_by_zh:
                    return self._runes_by_zh[cand], True
                bridged = cand
        if bridged in self._runes_by_zh:
            return self._runes_by_zh[bridged], True
        return cleaned, False

    def translate_item_name(self, raw_name: str, *, is_unique: bool = False) -> tuple[str, bool]:
        """Reverse-translate an item base type or unique name (`zh-rCN` priority + `zh-rTW` fallback)."""
        cleaned = clean_wegame_text(raw_name)
        if not cleaned:
            return "", True
        if not HAS_CJK_RE.search(cleaned):
            return cleaned, True
        key = _norm_key(cleaned)
        if is_unique and key in self._uniques_by_zh:
            return self._uniques_by_zh[key], True
        if key in self._items_by_zh:
            return self._items_by_zh[key], True
        if key in self._uniques_by_zh:
            return self._uniques_by_zh[key], True
        return cleaned, False

    def translate_gem(self, raw_name: str, *, is_support: bool) -> GemResolution:
        """Resolve gem by `(zh_name, is_support)`, preferring regular gems over item-granted variants."""
        cleaned = clean_wegame_text(raw_name)
        if not cleaned:
            return GemResolution(None, warning="Empty gem name in WeGame skills payload.")
        cands = list(
            self._gems_by_key.get((cleaned, is_support))
            or self._gems_by_key.get((_norm_key(cleaned), is_support))
            or []
        )
        # Also consult PoE1->PoE2 CN alias table for renamed skills/supports.
        if not cands:
            alias = POE1_ZH_TO_POE2_ALIAS.get(_norm_key(cleaned))
            if alias is not None:
                cands = list(self._gems_by_key.get((alias[0], is_support)) or [])
        if not cands:
            return GemResolution(None, warning=f"Unknown gem ({'support' if is_support else 'active'}): {cleaned}")

        # Deduplicate by skill_id and prefer ordinary skill gems over item-granted / unique-only variants.
        by_skill: dict[str, GemCandidate] = {}
        for c in cands:
            prev = by_skill.get(c.skill_id)
            if prev is None or (prev.is_item_granted and not c.is_item_granted):
                by_skill[c.skill_id] = c
        unique_cands = list(by_skill.values())
        if len(unique_cands) > 1:
            regular = [c for c in unique_cands if not c.is_item_granted]
            if regular:
                unique_cands = regular
        if len(unique_cands) == 1:
            return GemResolution(unique_cands[0])
        return GemResolution(
            None,
            ambiguous=True,
            warning=f"Ambiguous gem ({'support' if is_support else 'active'}): {cleaned} -> {[c.skill_id for c in unique_cands]}",
        )

    def translate_stat_line(self, raw_line: str) -> tuple[str, bool]:
        """Reverse-translate a single modifier or quest_stat line; never silently drops unmatched text."""
        cleaned = clean_wegame_text(raw_line)
        if not cleaned:
            return "", True
        if not HAS_CJK_RE.search(cleaned):
            return cleaned, True
        key = _norm_key(cleaned)
        if key in self._stats_exact:
            return self._stats_exact[key], True
        matched = match_builtin_stat_pattern(key)
        if matched is not None:
            return matched, True
        return cleaned, False

    def format_quality_line(self, raw_name: str, raw_val: str) -> str | None:
        """Format normal quality (`[Quality|品质]`) or catalyst quality (`品质（火焰词缀）` / `品质(火焰词缀)`)."""
        name = clean_wegame_text(raw_name).strip().rstrip(":：").strip()
        val = (raw_val or "").strip()
        if not val:
            return None
        if name in ("", "Quality", "品质", "品質"):
            num = parse_leading_int(val)
            return f"Quality: {num}" if num is not None else None
        if name.startswith("Quality (") and name.endswith(" Modifiers)"):
            return f"{name}: {val}"
        folded = _norm_key(name)
        m = CATALYST_PAREN_RE.match(folded)
        if m is not None:
            inner = m.group(1).strip()
            for suffix in ("词缀", "效果"):
                if inner.endswith(suffix):
                    inner = inner[: -len(suffix)].strip()
            en_cat = CATALYST_ZH_TO_EN.get(inner)
            if en_cat:
                return f"Quality ({en_cat} Modifiers): {val}"
        return f"Note: {name}: {val}"


__all__ = [
    "GemCandidate",
    "GemResolution",
    "WeGameTranslator",
    "clean_wegame_text",
    "has_shaman_bonded_marker",
    "is_progress_counter_stat",
    "parse_leading_int",
    "strip_shaman_bonded_prefix",
]
