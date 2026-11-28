# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Provenance-enforced PoE2 0.5.5 mechanics fact cards and <15ms `pob2 ground` query engine (L1)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..guardrails import (
    GuardrailScope,
    check_input,
    resolve_poe1_zh_alias,
    warning_context,
)

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanics_facts.json"

# Every fact card MUST cite a verifiable source (PoB2 .lua / RePoE / PoE2DB / Patch Notes / EE2).
_PROVENANCE_PATTERN = re.compile(
    r"(?:\.lua\b|poe2db\.tw|poe2wiki\.net|pathofexile\.com|Patch\s+0\.\d|RePoE\s+[a-z0-9_.\[\]]+|EE2\s+[a-z0-9_.]+|PoB2\s+)",
    re.IGNORECASE,
)

_ZH_GROUND_ALIASES: dict[str, str] = {
    "护甲": "armour",
    "破甲": "armour break",
    "护甲破坏": "armour break",
    "元素抗性": "resistance overcap",
    "抗性": "resistance overcap",
    "超抗": "resistance overcap",
    "三抗": "resistance overcap",
    "闪避": "evasion",
    "偏转": "deflection",
    "偏斜": "deflection",
    "格挡": "block",
    "能量护盾": "energy shield",
    "护盾": "energy shield",
    "异灵之体": "chaos inoculation",
    "异能魔力": "eldritch battery",
    "心灵升华": "mind over matter",
    "精魂": "spirit",
    "辅助宝石": "support gems lineage",
    "血脉辅助": "lineage support",
    "血脉": "lineage support",
    "元宝石": "meta gem energy trigger",
    "技能插槽": "sockets jewellers orb",
    "工匠石": "jewellers orb sockets",
    "武器套装": "weapon set",
    "武器组": "weapon set",
    "双武器": "weapon set",
    "双天赋": "weapon set",
    "伤害转化": "damage conversion",
    "转化": "damage conversion",
    "额外伤害": "gain as extra",
    "品质": "quality",
    "暴击": "critical",
    "命中": "accuracy",
    "准确": "accuracy",
    "属性": "attributes strength dexterity intelligence",
    "点燃": "ignite flammability",
    "感电": "shock",
    "触电": "electrocute",
    "冰冻": "freeze",
    "冻结": "freeze",
    "冰缓": "chill",
    "流血": "bleeding",
    "中毒": "poison",
    "眩晕": "stun",
    "重晕": "heavy stun",
    "护符": "charms",
    "咒符": "charms",
    "引路石": "waystone",
    "碑牌": "tablet",
    "精华": "essence crafted",
    "合金": "verisium alloys",
    "亵渎": "desecrated",
    "渎灵": "desecrated",
    "削切预兆": "whittling",
    "削切": "whittling",
    "预兆": "omen",
    "破裂石": "fracturing orb",
    "神圣化": "sanctification",
    "符文": "runes",
    "灵核": "soul cores",
    "灵魂核心": "soul cores",
    "神像": "idols runes",
    "魔偶": "idols runes",
    "涂灌": "liquid emotions",
    "涂油": "liquid emotions",
    "蒸馏": "distilled emotions",
    "液态情感": "liquid emotions",
    "终结": "culling strike",
    "偷取": "leech",
    "移速": "movement speed",
    "德鲁伊": "druid talisman",
    "萨满": "shaman bonded",
    "升华": "ascendancy classes",
    "职业": "classes ascendancy",
    "金币": "gold respec",
    "冷酷盛宴": "grim feast",
    "战吼": "warcry",
}

_GROUND_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "it", "of", "on", "or", "s", "t", "that", "the", "to", "vs", "with",
    }
)


class MissingProvenanceError(ValueError):
    """Raised when a mechanics fact card lacks a verifiable PoB2/Patch/PoE2DB source reference."""


@dataclass(frozen=True, slots=True)
class FactCard:
    slug: str
    title: str
    category: str
    tags: tuple[str, ...]
    poe1_or_stale_trap: str
    poe2_live_rule: str
    formula_expr: str
    source_ref: str
    patch_verified: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "category": self.category,
            "tags": list(self.tags),
            "poe1OrStaleTrap": self.poe1_or_stale_trap,
            "poe2LiveRule": self.poe2_live_rule,
            "formulaExpr": self.formula_expr,
            "sourceRef": self.source_ref,
            "patchVerified": self.patch_verified,
        }


@dataclass(frozen=True, slots=True)
class GroundResult:
    query: str
    normalized_query: str
    elapsed_ms: float
    cards: tuple[FactCard, ...]
    warnings: tuple[dict[str, Any], ...]
    patch: str = "0.5.5"

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": "poe2",
            "patch": self.patch,
            "query": self.query,
            "normalizedQuery": self.normalized_query,
            "elapsedMs": round(self.elapsed_ms, 3),
            "count": len(self.cards),
            "cards": [c.to_dict() for c in self.cards],
            "warnings": list(self.warnings),
        }


def validate_fact_provenance(card: FactCard) -> None:
    """Enforce mandatory verifiable provenance; unsourced rules belong in docs/unverified-mechanics.md."""
    src = (card.source_ref or "").strip()
    if not src or not _PROVENANCE_PATTERN.search(src):
        raise MissingProvenanceError(
            f"FactCard {card.slug!r} lacks a verifiable PoB2 Lua / PoE2DB / Patch Notes source_ref: {src!r}. "
            "Quarantine unverified rules in pob2_cli/docs/unverified-mechanics.md."
        )
    if not (card.patch_verified or "").strip():
        raise MissingProvenanceError(f"FactCard {card.slug!r} is missing patch_verified.")


@dataclass(slots=True)
class _CardIndex:
    cards: tuple[FactCard, ...]
    search_blobs: tuple[tuple[str, frozenset[str], frozenset[str]], ...]


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9\u3400-\u9fff]+", text.lower()) if t]


@lru_cache(maxsize=2)
def _load_index(path: Path = _DATA_PATH) -> _CardIndex:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cards: list[FactCard] = []
    blobs: list[tuple[str, frozenset[str], frozenset[str]]] = []
    for item in raw:
        card = FactCard(
            slug=str(item["slug"]),
            title=str(item["title"]),
            category=str(item.get("category") or "general"),
            tags=tuple(str(t) for t in item.get("tags", ())),
            poe1_or_stale_trap=str(item.get("poe1_or_stale_trap") or ""),
            poe2_live_rule=str(item.get("poe2_live_rule") or ""),
            formula_expr=str(item.get("formula_expr") or ""),
            source_ref=str(item.get("source_ref") or ""),
            patch_verified=str(item.get("patch_verified") or ""),
        )
        validate_fact_provenance(card)
        cards.append(card)
        extra_kw = [str(x) for x in item.get("keywords", ())] + [str(x) for x in item.get("zh", ())]
        all_tags = (*card.tags, *extra_kw)
        tag_set = frozenset(t.lower() for t in all_tags if t)
        full_blob = f"{card.slug} {card.title} {' '.join(all_tags)} {card.poe2_live_rule} {card.formula_expr}".lower()
        tok_set = frozenset(_tokenize(full_blob))
        blobs.append((full_blob, tag_set, tok_set))
    return _CardIndex(tuple(cards), tuple(blobs))


def load_fact_cards(path: Path | None = None) -> tuple[FactCard, ...]:
    """Return all provenance-verified PoE2 0.5.5 mechanics fact cards."""
    return _load_index(path or _DATA_PATH).cards


def _expand_zh_ground_query(query: str) -> tuple[str, list[str]]:
    out = query.strip()
    added: list[str] = []
    alias = resolve_poe1_zh_alias(out, emit_warning=False)
    if alias is not None:
        out = f"{out} {alias[0]}"
        added.append(alias[0].lower())
    for zh, en in sorted(_ZH_GROUND_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if zh in out:
            out = f"{out} {en}"
            added.append(en.lower())
    return out, added


def _meaningful_tokens(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        if tok in _GROUND_STOPWORDS:
            continue
        if tok.isascii() and len(tok) < 2:
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def ground_query(
    query: str,
    *,
    category: str | None = None,
    limit: int = 5,
    emit_stderr: bool = False,
    path: Path | None = None,
) -> GroundResult:
    """Fast (<15ms) guardrail-checked PoE2 0.5.5 mechanic grounding query."""
    t0 = time.perf_counter()
    with warning_context() as collected:
        check_input(query, GuardrailScope.DATA_QUERY, expected_game="poe2", emit_stderr=emit_stderr)
        expanded, zh_phrases = _expand_zh_ground_query(query)
        idx = _load_index(path or _DATA_PATH)
        q_low = expanded.lower().strip()
        raw_low = query.lower().strip()
        # Use expanded English tokens when Chinese aliases fired, otherwise score original query tokens.
        score_tokens = _meaningful_tokens(_tokenize(" ".join(zh_phrases))) if zh_phrases else _meaningful_tokens(_tokenize(q_low))

        scored: list[tuple[int, int, FactCard]] = []
        cat_filter = category.lower().strip() if category else None
        for i, card in enumerate(idx.cards):
            if cat_filter and card.category.lower() != cat_filter:
                continue
            blob, tag_set, tok_set = idx.search_blobs[i]
            score = 0
            matched_toks = 0
            if raw_low and raw_low == card.slug.lower():
                score += 200
            if (raw_low and raw_low in tag_set) or any(p in tag_set for p in zh_phrases):
                score += 120
            if (raw_low and len(raw_low) >= 3 and raw_low in card.title.lower()) or any(
                len(p) >= 3 and p in card.title.lower() for p in zh_phrases
            ):
                score += 80
            for tok in score_tokens:
                if tok in tag_set:
                    score += 45
                    matched_toks += 1
                elif tok in tok_set:
                    score += 20
                    matched_toks += 1
                elif not tok.isascii() and len(tok) >= 2 and tok in blob:
                    score += 25
                    matched_toks += 1
                elif tok.isascii() and len(tok) >= 4 and tok in blob:
                    score += 8
                    matched_toks += 1

            if not score_tokens:
                continue
            if len(score_tokens) >= 2:
                if score < 45 and not (matched_toks >= 2 and score >= 30):
                    continue
            elif score < 20:
                continue
            scored.append((score, -i, card))

        scored.sort(reverse=True)
        matched = tuple(c for _, _, c in scored[: max(1, limit)])
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return GroundResult(
            query=query,
            normalized_query=expanded,
            elapsed_ms=elapsed_ms,
            cards=matched,
            warnings=tuple(w.to_dict() for w in collected),
        )
