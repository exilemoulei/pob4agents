# SPDX-License-Identifier: GPL-3.0-only
"""Markdown rendering for craft outcomes and similarity-ranked target suggestions (L2)."""

from __future__ import annotations

import difflib
import re
from typing import Any

from .models import ModEntry

_FILLER_TOKENS: frozenset[str] = frozenset({"to", "of", "the", "on", "in", "with", "and", "per", "all", "for", "your"})
_SECONDARY_QUALIFIERS: frozenset[str] = frozenset(
    {"regeneration", "leech", "leeched", "flask", "minion", "minions", "kill", "recoup", "recouped", "totem", "trap"}
)
_CLEAN_PAREN_RE = re.compile(r"[+-]?\([^)]*\)\s*%?\s*(?:to\s+)?")
_CLEAN_LEADING_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%?\s*(?:to\s+)?")
_MULTI_SPACE_RE = re.compile(r"\s+")
_TIER_PREFIX_RE = re.compile(r"^\s*t(\d+)\b\s*(.*)$", re.IGNORECASE)


def clean_mod_phrase(stat_text: str, fallback: str) -> str:
    first_ln = (stat_text.splitlines()[0] if stat_text else fallback).strip()
    clean = _CLEAN_PAREN_RE.sub("", first_ln).strip()
    clean = _CLEAN_LEADING_NUM_RE.sub("", clean).strip()
    clean = _MULTI_SPACE_RE.sub(" ", clean)
    return clean or fallback


def _fmt_top_tier_roll(outcome: dict[str, Any]) -> str:
    tiers = outcome.get("tiers") or ()
    if not tiers:
        return "-"
    top = tiers[0]
    vr = top.get("valueRange")
    if vr is None:
        return str(top.get("tier") or "-")
    if vr and isinstance(vr[0], list):
        parts = [f"{p[0]}..{p[1]}" if p[0] != p[1] else f"{p[0]}" for p in vr]
        return f"{top['tier']}: {' / '.join(parts)}"
    lo, hi = vr[0], vr[1]
    span = f"{lo}..{hi}" if lo != hi else f"{lo}"
    return f"{top['tier']}: {span}"


def render_outcomes_md(data: dict[str, Any], *, max_rows: int = 40) -> str:
    """Render Markdown summary and probability table for `craft odds --outcomes`."""
    from tabulate import tabulate

    omen_tag = f" + {' + '.join(data['omens'])}" if data.get("omens") else ""
    lines = [
        f"### 下一步词缀族概率分布 [PoE2 {data['patch']}] — {data['base']} (`{data['baseSlug']}`, ilvl {data['ilvl']})",
        f"- 当前稀有度: `{data['rarity']}` | 通货: `{data['lever']}{omen_tag}` | 最低词缀等级门槛: `floor={data['minModLevelFloor']}`",
        f"- 总词缀池权重: `{data['totalPoolWeight']}` | 可命中词缀族数: `{data['familyCount']}` | "
        f"概率总和: `{data['probabilitySum']:.4f}` | 置信度: `{data['weightConfidence']}`",
    ]
    outcomes = data.get("outcomes") or []
    if not outcomes:
        lines.append("- 当前状态与通货组合下无可用词缀池（权重为 0）。")
        return "\n".join(lines)

    rows = [
        [
            o["slot"],
            o["familyId"],
            o["name"],
            o["tierRange"] + (" [豁免]" if o.get("exemptedBelowFloor") else ""),
            _fmt_top_tier_roll(o),
            f"{o['reqLevelRange'][0]}-{o['reqLevelRange'][1]}",
            o["weight"],
            f"{o['probability'] * 100:.3f}%",
        ]
        for o in outcomes[:max_rows]
    ]
    lines.append("")
    lines.append(
        tabulate(
            rows,
            headers=["槽位", "FamilyId", "词缀描述", "可用阶位", "最佳阶数值", "req_lvl", "权重", "单抽命中率"],
            tablefmt="github",
        )
    )
    if len(outcomes) > max_rows:
        lines.append(f"\n- 其余 {len(outcomes) - max_rows} 个低权重词缀族已折叠（传入 `--json` 查看完整列表）。")
    return "\n".join(lines)


def _stem_token(tok: str) -> str:
    if len(tok) >= 6 and tok.endswith("ing"):
        return tok[:-3]
    if len(tok) >= 5 and tok.endswith("ed"):
        return tok[:-2]
    if len(tok) >= 5 and tok.endswith("es"):
        return tok[:-2]
    if len(tok) >= 4 and tok.endswith("s"):
        return tok[:-1]
    return tok


def rank_target_suggestions(
    cands: list[ModEntry],
    translated_query: str,
    *,
    limit: int = 6,
) -> list[str]:
    """Rank valid `--want` candidate strings by token/stem overlap + edit-distance similarity."""
    raw_q = (translated_query or "").strip()
    m_tier = _TIER_PREFIX_RE.match(raw_q)
    req_tier = int(m_tier.group(1)) if m_tier else None
    core_q = (m_tier.group(2) if m_tier else raw_q).strip().lower()
    if not core_q:
        core_q = raw_q.lower()

    all_q_toks = [t for t in re.split(r"[^a-z0-9]+", core_q) if len(t) >= 2 and not t.isdigit()]
    content_q_toks = [t for t in all_q_toks if t not in _FILLER_TOKENS] or all_q_toks
    q_stems = [_stem_token(t) for t in content_q_toks]

    reps: dict[str, tuple[int, int, str]] = {}
    for m in cands:
        clean = clean_mod_phrase(m.stat_text, m.mod_type)
        key_low = clean.lower()
        pool_prio = 2 if m.source_pool == "normal" else (1 if m.source_pool == "desecrated" else 0)
        score_w = pool_prio * 100000 + m.weight
        prev = reps.get(key_low)
        if prev is None:
            reps[key_low] = (score_w, m.display_tier, clean)
        else:
            reps[key_low] = (
                max(prev[0], score_w),
                max(prev[1], m.display_tier),
                prev[2] if prev[0] >= score_w else clean,
            )

    ranked: list[tuple[float, int, str]] = []
    for clean_low, (w_prio, max_t, clean_txt) in reps.items():
        eff_t = min(req_tier, max(1, max_t)) if req_tier is not None else 1
        label = f"T{eff_t} {clean_txt}"

        c_toks = [t for t in re.split(r"[^a-z0-9]+", clean_low) if len(t) >= 2]
        c_content = [t for t in c_toks if t not in _FILLER_TOKENS] or c_toks
        c_stems = {_stem_token(t) for t in c_content}
        c_word_set = set(c_content)

        if content_q_toks:
            exact_hits = sum(1 for t in content_q_toks if t in c_word_set)
            stem_hits = sum(
                1
                for t, st in zip(content_q_toks, q_stems, strict=False)
                if st in c_stems or any(ct.startswith(st) or st.startswith(ct) for ct in c_stems if len(ct) >= 3)
            )
            exact_frac = exact_hits / len(content_q_toks)
            stem_frac = stem_hits / len(content_q_toks)
            best_tok_sim = sum(
                max((difflib.SequenceMatcher(None, qt, ct).ratio() for ct in c_content), default=0.0)
                for qt in content_q_toks
            ) / len(content_q_toks)
        else:
            exact_frac = stem_frac = best_tok_sim = 0.0

        sub_bonus = 3.0 if (core_q and core_q in clean_low) else 0.0
        seq_sim = difflib.SequenceMatcher(None, core_q, clean_low).ratio() if core_q else 0.0

        score = exact_frac * 6.0 + stem_frac * 3.5 + sub_bonus + seq_sim * 4.0 + best_tok_sim * 2.5
        if exact_frac > 0 or stem_frac > 0 or best_tok_sim >= 0.72:
            extra_words = max(0, len(c_content) - len(content_q_toks))
            score -= 0.45 * extra_words
            unrequested_sec = sum(1 for w in c_content if w in _SECONDARY_QUALIFIERS and w not in content_q_toks)
            score -= 0.8 * unrequested_sec
        ranked.append((score, w_prio, label))

    ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [lbl for _, _, lbl in ranked[:limit]]


__all__ = [
    "clean_mod_phrase",
    "rank_target_suggestions",
    "render_outcomes_md",
]
