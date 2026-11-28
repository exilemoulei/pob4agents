# SPDX-License-Identifier: GPL-3.0-only
"""Context-scoped text normalization, matching, item header sanitizer, and warning context (L0)."""

from __future__ import annotations

import re
import sys
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from functools import lru_cache
from typing import Any

from ..errors import HallucinationWarning
from ..game_profile import GAME_ID


class GuardrailScope(StrEnum):
    SLOT = "slot"
    CATEGORY = "category"
    ITEM_RAW = "item_raw"
    GEM_NAME = "gem_name"
    NOTABLE_OR_OIL = "notable_or_oil"
    CONFIG_VAR = "config_var"
    STAT_QUERY = "stat_query"
    CALC_QUERY = "calc_query"
    DATA_QUERY = "data_query"
    COMMAND = "command"


ALL_QUERY_SCOPES: frozenset[GuardrailScope] = frozenset(GuardrailScope)

_POB_ESC_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")
_POE2_LINK_RE = re.compile(r"\[[^\]|]+\|([^\]]+)\]|\[([^\]]+)\]")
_FOLD_MAP = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u02bc": "'",
    "\u0060": "'",
    "\u2010": "-",
    "\u2011": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u3000": " ",
    "\u00a0": " ",
})
_GEM_LEVEL_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
_STAT_THRESHOLD_SUFFIX_RE = re.compile(r"\s*(?:>=|<=|=|>|<)\s*[-+]?\d+(?:\.\d+)?\s*$")
_WORD_EN_CACHE: dict[str, re.Pattern[str]] = {}

_ITEM_HEADER_META_PREFIXES = (
    "item class:", "rarity:", "unique id:", "item level:", "quality:", "sockets:",
    "levelreq:", "variant:", "selected variant:", "prefix:", "suffix:", "crafted:",
    "catalyst:", "catalystquality:", "implicits:", "armour:", "evasion:",
    "energy shield:", "ward:", "spirit:", "charm slots:", "reload time:",
    "physical damage:", "elemental damage:", "chaos damage:",
    "critical strike chance:", "critical hit chance:", "attacks per second:",
    "weapon range:", "radius:", "limited to:", "league:", "source:",
    "corrupted", "sanctified", "twice corrupted", "unidentified", "mirrored",
    "split", "fractured item",
)

_INFLUENCE_LINES = frozenset({
    "shaper item", "elder item", "crusader item", "redeemer item",
    "hunter item", "warlord item", "synthesised item",
})


def normalize_guardrail_text(text: str | None) -> str:
    """Strip PoB color codes & PoE2 rich tooltip links, fold quotes/dashes/diacritics, lowercase."""
    if not text:
        return ""
    s = _POB_ESC_RE.sub("", str(text))
    s = _POE2_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.translate(_FOLD_MAP).lower()
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=1024)
def _normalize_zh_compact_cached(text: str) -> str:
    s = normalize_guardrail_text(text)
    return re.sub(r"[\s·・.．:：()（）]", "", s)


def normalize_zh_compact(text: str | None) -> str:
    """Normalize text for CJK matching by removing intra-CJK whitespace and punctuation."""
    if not text:
        return ""
    s = str(text)
    if len(s) <= 256:
        return _normalize_zh_compact_cached(s)
    return re.sub(r"[\s·・.．:：()（）]", "", normalize_guardrail_text(s))


def strip_scope_suffix(text: str, scope: GuardrailScope) -> str:
    cleaned = text.strip().strip('"').strip("'").strip()
    if scope == GuardrailScope.GEM_NAME:
        cleaned = _GEM_LEVEL_SUFFIX_RE.sub("", cleaned).strip()
    elif scope == GuardrailScope.STAT_QUERY:
        cleaned = _STAT_THRESHOLD_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned


def match_word_en(
    norm_text: str,
    phrase: str,
    exclude_re: re.Pattern[str] | None = None,
) -> str | None:
    """Match an ASCII phrase on strict token boundaries with optional contextual exclusion."""
    key = phrase.lower()
    if key not in norm_text:
        return None
    pat = _WORD_EN_CACHE.get(key)
    if pat is None:
        pat = re.compile(rf"(?<![a-z0-9_'-]){re.escape(key)}(?![a-z0-9_'-])")
        _WORD_EN_CACHE[key] = pat
    for m in pat.finditer(norm_text):
        if exclude_re is not None:
            win = norm_text[max(0, m.start() - 24) : min(len(norm_text), m.end() + 24)]
            if exclude_re.search(win):
                continue
        return m.group(0)
    return None


def match_phrase_zh(
    raw_text: str,
    zh_phrase: str,
    exclude_compounds: tuple[str, ...] = (),
) -> str | None:
    """Match a contiguous normalized CJK substring while excluding known safe compounds."""
    compact = normalize_zh_compact(raw_text)
    if not compact:
        return None
    target = normalize_zh_compact(zh_phrase)
    if not target or target not in compact:
        return None
    start = 0
    while True:
        idx = compact.find(target, start)
        if idx == -1:
            return None
        win = compact[max(0, idx - 5) : min(len(compact), idx + len(target) + 5)]
        if not any(normalize_zh_compact(comp) in win for comp in exclude_compounds):
            return zh_phrase
        start = idx + 1


def extract_scannable_item_parts(raw_text: str | None) -> dict[str, Any]:
    """Extract scannable sections from raw item text while discarding random Rare/Magic Line-1 names."""
    if not raw_text:
        return {
            "rarity": "",
            "discarded_rare_title": None,
            "unique_title": None,
            "base_line": None,
            "body_lines": [],
            "scannable_text": "",
        }
    rarity = ""
    header_names: list[str] = []
    body_lines: list[str] = []
    in_header = True
    in_flavour = False
    for raw_line in str(raw_text).splitlines():
        stripped = _POB_ESC_RE.sub("", raw_line).strip()
        low = stripped.lower()
        if not stripped:
            continue
        if stripped.startswith("--------"):
            in_header = False
            in_flavour = False
            continue
        if low.startswith("unique id:"):
            continue
        if low.startswith("flavour text:"):
            in_flavour = True
            continue
        if in_flavour:
            continue
        if in_header:
            if low.startswith("rarity:"):
                rarity = stripped.split(":", 1)[1].strip().upper()
                continue
            if low.startswith("implicits:"):
                in_header = False
                body_lines.append(stripped)
                continue
            if low in _INFLUENCE_LINES or low.startswith(("sockets:", "{")):
                body_lines.append(stripped)
                continue
            if low.startswith(_ITEM_HEADER_META_PREFIXES):
                continue
            header_names.append(stripped)
        else:
            body_lines.append(stripped)

    discarded: str | None = None
    unique_title: str | None = None
    base_line: str | None = None
    if rarity in ("RARE", "MAGIC"):
        if len(header_names) >= 2:
            discarded, base_line = header_names[0], header_names[1]
            body_lines = header_names[2:] + body_lines
        elif header_names:
            base_line = header_names[0]
    elif rarity in ("UNIQUE", "RELIC"):
        if len(header_names) >= 2:
            unique_title, base_line = header_names[0], header_names[1]
            body_lines = header_names[2:] + body_lines
        elif header_names:
            unique_title = header_names[0]
    elif header_names:
        base_line = header_names[0]
        body_lines = header_names[1:] + body_lines

    parts = [p for p in (unique_title, base_line, *body_lines) if p]
    return {
        "rarity": rarity,
        "discarded_rare_title": discarded,
        "unique_title": unique_title,
        "base_line": base_line,
        "body_lines": body_lines,
        "scannable_text": "\n".join(parts),
    }


_TLS = threading.local()


def _tls_stack() -> list[list[HallucinationWarning]]:
    stack = getattr(_TLS, "stack", None)
    if stack is None:
        stack = []
        _TLS.stack = stack
    return stack


def _tls_cli_warnings() -> list[HallucinationWarning]:
    buf = getattr(_TLS, "cli_warnings", None)
    if buf is None:
        buf = []
        _TLS.cli_warnings = buf
    return buf


def _tls_emitted_keys() -> set[tuple[str, str]]:
    keys = getattr(_TLS, "emitted_stderr_keys", None)
    if keys is None:
        keys = set()
        _TLS.emitted_stderr_keys = keys
    return keys


def _write_stderr_line(msg: str) -> None:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        pass
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def record_warning(warning: HallucinationWarning, *, emit_stderr: bool = True) -> None:
    stack = _tls_stack()
    target_list = stack[-1] if stack else _tls_cli_warnings()
    key = (warning.rule_id, warning.trigger)
    if not any((w.rule_id, w.trigger) == key for w in target_list):
        target_list.append(warning)
    if emit_stderr:
        emitted = _tls_emitted_keys()
        if key not in emitted:
            emitted.add(key)
            _write_stderr_line(warning.format_stderr())


@contextmanager
def warning_context() -> Iterator[list[HallucinationWarning]]:
    stack = _tls_stack()
    bucket: list[HallucinationWarning] = []
    stack.append(bucket)
    try:
        yield bucket
    finally:
        if stack and stack[-1] is bucket:
            stack.pop()
        elif bucket in stack:
            stack.remove(bucket)


def reset_cli_warnings() -> None:
    _tls_cli_warnings().clear()
    _tls_emitted_keys().clear()
    stack = _tls_stack()
    if stack:
        stack[-1].clear()


def get_active_warnings() -> list[HallucinationWarning]:
    stack = _tls_stack()
    return list(stack[-1] if stack else _tls_cli_warnings())


def get_active_warning_dicts() -> list[dict[str, Any]]:
    return [w.to_dict() for w in get_active_warnings()]


def emit_stderr_warnings_from_dicts(items: list[dict[str, Any]] | None) -> None:
    if not items:
        return
    for item in items:
        if isinstance(item, dict):
            record_warning(HallucinationWarning.from_dict(item, default_game=GAME_ID), emit_stderr=True)
