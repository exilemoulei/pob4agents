# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 0.5.5 catalog for 287 runes/soul cores/idols, 13 Liquid Emotions, and 875 ordered instillations (L2)."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..han_fold import fold_han

_CATALOG_GZ_PATH = Path(__file__).resolve().parent.parent / "data" / "rune_instill_catalog.json.gz"


@dataclass(frozen=True, slots=True)
class RuneSlotEffect:
    """Modifier block granted by a rune/soul core/idol in a specific ModRunes slot key."""

    slot_key: str
    kind: str  # "Rune" | "SoulCore" | "Idol"
    req_level: int
    is_socket_bound: bool
    stats: tuple[str, ...]
    bonded: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuneEntry:
    """Single PoE2 0.5.5 socketable augment from ModRunes.lua (287 total)."""

    name: str
    zh_cn: str
    zh_tw: str
    kind: str
    is_socket_bound: bool
    req_level: int
    slots: dict[str, RuneSlotEffect]


@dataclass(frozen=True, slots=True)
class EmotionEntry:
    """Single amulet-valid Liquid Emotion from LiquidEmotions.lua (13 total; radiusJewel=false)."""

    short: str
    legacy_name: str
    live_name: str
    metadata_id: str
    tier_level: int
    zh_cn: str
    zh_tw: str


@dataclass(frozen=True, slots=True)
class InstillRecipe:
    """Single ordered 3-emotion amulet instillation recipe from TreeData/0_5/tree.lua (875 total)."""

    node_id: int
    name: str
    zh_cn: str
    recipe: tuple[str, str, str]  # Strictly ordered: (Slot 1, Slot 2, Slot 3)
    stats: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpiritSupportEntry:
    """Fixed flat-Spirit support gem from PoE2/Data/Skills/*.lua."""

    name: str
    skill_id: str
    spirit_cost: int
    zh_cn: str
    downgrade_to: str | None


@dataclass(frozen=True)
class RuneInstillCatalog:
    """In-memory indexed PoE2 0.5.5 catalog for runes, Liquid Emotions, instillations, and Spirit supports."""

    version: str
    runes: tuple[RuneEntry, ...]
    runes_by_key: dict[str, RuneEntry]
    socket_bound_names: frozenset[str]
    emotions: tuple[EmotionEntry, ...]
    emotions_by_key: dict[str, EmotionEntry]
    excluded_jewel_emotions: frozenset[str]
    instills: tuple[InstillRecipe, ...]
    instills_by_key: dict[str, InstillRecipe]
    instills_by_node_id: dict[int, InstillRecipe]
    instills_by_recipe: dict[tuple[str, str, str], InstillRecipe]
    gold_respec_prices: tuple[int, ...]
    spirit_supports: tuple[SpiritSupportEntry, ...]
    spirit_supports_by_key: dict[str, SpiritSupportEntry]

    def gold_respec_cost(self, level: int, *, nodes: int = 1) -> int:
        """Return exact PoE2 gold respec cost for *nodes* passives at character *level*."""
        if not self.gold_respec_prices or nodes <= 0:
            return 0
        idx = max(0, min(len(self.gold_respec_prices) - 1, int(level) - 1))
        return self.gold_respec_prices[idx] * int(nodes)

    def lookup_rune(self, query: str) -> RuneEntry | None:
        """Find a rune/soul core/idol by EN, CN, or TW name."""
        key = _norm(query)
        return self.runes_by_key.get(key)

    def lookup_instill(self, query: str) -> InstillRecipe | None:
        """Find an amulet instillation notable by EN name, CN name, or node ID."""
        q = (query or "").strip()
        if q.isdigit():
            return self.instills_by_node_id.get(int(q))
        return self.instills_by_key.get(_norm(q))

    def search_instills(self, query: str, *, limit: int = 20) -> list[InstillRecipe]:
        """Search instillations by notable name (EN/CN) or stat text substring."""
        q = _norm(query)
        if not q:
            return list(self.instills[:limit])
        exact = self.lookup_instill(query)
        hits: list[InstillRecipe] = [exact] if exact is not None else []
        seen = {exact.node_id} if exact is not None else set()
        for inst in self.instills:
            if inst.node_id in seen:
                continue
            blob = f"{inst.name.lower()} {fold_han(inst.zh_cn).lower()} {' '.join(inst.stats).lower()}"
            if q in blob:
                hits.append(inst)
                seen.add(inst.node_id)
                if len(hits) >= limit:
                    break
        return hits

    def lookup_instill_by_recipe(self, s1: str, s2: str, s3: str) -> InstillRecipe | None:
        """Find the notable matching an exact ORDERED `[Slot 1 -> Slot 2 -> Slot 3]` emotion triplet."""
        e1 = self.emotions_by_key.get(_norm(s1))
        e2 = self.emotions_by_key.get(_norm(s2))
        e3 = self.emotions_by_key.get(_norm(s3))
        if not (e1 and e2 and e3):
            return None
        return self.instills_by_recipe.get((e1.short, e2.short, e3.short))


def _norm(text: str) -> str:
    return fold_han((text or "").strip()).lower()


def _parse_catalog_payload(data: dict[str, Any]) -> RuneInstillCatalog:
    runes: list[RuneEntry] = []
    runes_by_key: dict[str, RuneEntry] = {}
    bound_names: set[str] = set()
    for r in data.get("runes") or ():
        slots: dict[str, RuneSlotEffect] = {}
        for skey, sval in (r.get("slots") or {}).items():
            slots[str(skey)] = RuneSlotEffect(
                slot_key=str(skey),
                kind=str(sval.get("kind") or "Rune"),
                req_level=int(sval.get("req_level") or 0),
                is_socket_bound=bool(sval.get("is_socket_bound")),
                stats=tuple(str(s) for s in (sval.get("stats") or ())),
                bonded=tuple(str(s) for s in (sval.get("bonded") or ())),
            )
        entry = RuneEntry(
            name=str(r["name"]),
            zh_cn=str(r.get("zh_cn") or ""),
            zh_tw=str(r.get("zh_tw") or ""),
            kind=str(r.get("kind") or "Rune"),
            is_socket_bound=bool(r.get("is_socket_bound")),
            req_level=int(r.get("req_level") or 0),
            slots=slots,
        )
        runes.append(entry)
        if entry.is_socket_bound:
            bound_names.add(entry.name)
        for alias in (entry.name, entry.zh_cn, entry.zh_tw):
            if alias:
                runes_by_key[_norm(alias)] = entry

    emotions: list[EmotionEntry] = []
    emotions_by_key: dict[str, EmotionEntry] = {}
    for e in data.get("emotions") or ():
        em = EmotionEntry(
            short=str(e["short"]),
            legacy_name=str(e["legacy_name"]),
            live_name=str(e["live_name"]),
            metadata_id=str(e.get("metadata_id") or ""),
            tier_level=int(e.get("tier_level") or 1),
            zh_cn=str(e.get("zh_cn") or ""),
            zh_tw=str(e.get("zh_tw") or ""),
        )
        emotions.append(em)
        for alias in (em.short, em.legacy_name, em.live_name, em.zh_cn, em.zh_tw):
            if alias:
                emotions_by_key[_norm(alias)] = em

    excluded_jewel = frozenset(str(x) for x in (data.get("excluded_jewel_emotions") or ()))

    instills: list[InstillRecipe] = []
    instills_by_key: dict[str, InstillRecipe] = {}
    instills_by_node_id: dict[int, InstillRecipe] = {}
    instills_by_recipe: dict[tuple[str, str, str], InstillRecipe] = {}
    for ins in data.get("instills") or ():
        rec = tuple(str(x) for x in (ins.get("recipe") or ()))
        if len(rec) != 3:
            continue
        item = InstillRecipe(
            node_id=int(ins["node_id"]),
            name=str(ins["name"]),
            zh_cn=str(ins.get("zh_cn") or ""),
            recipe=(rec[0], rec[1], rec[2]),
            stats=tuple(str(s) for s in (ins.get("stats") or ())),
        )
        instills.append(item)
        instills_by_node_id[item.node_id] = item
        instills_by_recipe[item.recipe] = item
        for alias in (item.name, item.zh_cn):
            if alias:
                instills_by_key[_norm(alias)] = item

    gold_prices = tuple(int(x) for x in (data.get("gold_respec_prices") or ()))

    spirit_supports: list[SpiritSupportEntry] = []
    spirit_by_key: dict[str, SpiritSupportEntry] = {}
    for sp in data.get("spirit_supports") or ():
        s_ent = SpiritSupportEntry(
            name=str(sp["name"]),
            skill_id=str(sp["skill_id"]),
            spirit_cost=int(sp["spirit_cost"]),
            zh_cn=str(sp.get("zh_cn") or ""),
            downgrade_to=str(sp["downgrade_to"]) if sp.get("downgrade_to") else None,
        )
        spirit_supports.append(s_ent)
        for alias in (s_ent.name, s_ent.skill_id, s_ent.zh_cn):
            if alias:
                spirit_by_key[_norm(alias)] = s_ent

    return RuneInstillCatalog(
        version=str(data.get("version") or "0.5.5"),
        runes=tuple(runes),
        runes_by_key=runes_by_key,
        socket_bound_names=frozenset(bound_names),
        emotions=tuple(emotions),
        emotions_by_key=emotions_by_key,
        excluded_jewel_emotions=excluded_jewel,
        instills=tuple(instills),
        instills_by_key=instills_by_key,
        instills_by_node_id=instills_by_node_id,
        instills_by_recipe=instills_by_recipe,
        gold_respec_prices=gold_prices,
        spirit_supports=tuple(spirit_supports),
        spirit_supports_by_key=spirit_by_key,
    )


@lru_cache(maxsize=1)
def load_rune_instill_catalog(path: Path | None = None) -> RuneInstillCatalog:
    """Load and index `data/rune_instill_catalog.json.gz`."""
    target = path or _CATALOG_GZ_PATH
    if not target.is_file():
        return _parse_catalog_payload({})
    raw = gzip.decompress(target.read_bytes()).decode("utf-8")
    return _parse_catalog_payload(json.loads(raw))


__all__ = [
    "EmotionEntry",
    "InstillRecipe",
    "RuneEntry",
    "RuneInstillCatalog",
    "RuneSlotEffect",
    "SpiritSupportEntry",
    "load_rune_instill_catalog",
]
