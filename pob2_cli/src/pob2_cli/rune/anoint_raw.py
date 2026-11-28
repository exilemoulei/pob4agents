# SPDX-License-Identifier: GPL-3.0-only
"""PoB2 amulet `Allocates <Notable>` enchant extraction, stripping, and dual-write rendering (L2)."""

from __future__ import annotations

import re

_IMPLICITS_RE = re.compile(r"^Implicits:\s*(\d+)\s*$", re.IGNORECASE)
_ALLOCATES_RE = re.compile(r"^(?:\{[^}]+\})*allocates\s+(.+)$", re.IGNORECASE)


def extract_existing_anoint(raw_text: str) -> str | None:
    """Extract the currently allocated notable name from an amulet's `Allocates <Name>` line, if any."""
    for ln in (raw_text or "").splitlines():
        m = _ALLOCATES_RE.match(ln.strip())
        if m:
            return m.group(1).strip()
    return None


def strip_anoint_from_amulet_raw(raw_text: str) -> tuple[str, str | None]:
    """Remove any existing `Allocates <Name>` enchant from an amulet and keep `Implicits: N` accurate."""
    lines = (raw_text or "").splitlines()
    existing = extract_existing_anoint(raw_text)
    if existing is None:
        return raw_text, None

    imp_idx = next((i for i, ln in enumerate(lines) if _IMPLICITS_RE.match(ln.strip())), None)
    if imp_idx is None:
        cleaned = [ln for ln in lines if not _ALLOCATES_RE.match(ln.strip())]
        return "\n".join(cleaned), existing

    im = _IMPLICITS_RE.match(lines[imp_idx].strip())
    orig_count = int(im.group(1)) if im else 0
    imp_start = imp_idx + 1
    imp_end = min(len(lines), imp_start + orig_count)
    kept_imps = [ln for ln in lines[imp_start:imp_end] if not _ALLOCATES_RE.match(ln.strip())]
    kept_tail = [ln for ln in lines[imp_end:] if not _ALLOCATES_RE.match(ln.strip())]
    lines[imp_idx] = f"Implicits: {len(kept_imps)}"
    lines[imp_start:] = [*kept_imps, *kept_tail]
    return "\n".join(lines), existing


def apply_anoint_to_amulet_raw(raw_text: str, notable_name: str) -> str:
    """Replace or insert `{enchant}Allocates <Notable>` on an uncorrupted/unmirrored amulet with exact `Implicits: N`."""
    lines = (raw_text or "").splitlines()
    for ln in lines:
        low = ln.strip().lower()
        if low in ("corrupted", "sanctified", "mirrored"):
            raise ValueError(f"Cannot instill amulet: item is {low.title()}.")
    alloc_line = f"{{enchant}}Allocates {notable_name.strip()}"

    imp_idx = next((i for i, ln in enumerate(lines) if _IMPLICITS_RE.match(ln.strip())), None)
    if imp_idx is None:
        cleaned = [ln for ln in lines if not _ALLOCATES_RE.match(ln.strip())]
        tail_idx = next(
            (i for i, ln in enumerate(cleaned) if ln.strip().lower() in ("corrupted", "sanctified", "mirrored")),
            len(cleaned),
        )
        cleaned[tail_idx:tail_idx] = ["Implicits: 1", alloc_line]
        return "\n".join(cleaned)

    im = _IMPLICITS_RE.match(lines[imp_idx].strip())
    orig_count = int(im.group(1)) if im else 0
    imp_start = imp_idx + 1
    imp_end = min(len(lines), imp_start + orig_count)
    kept = [ln for ln in lines[imp_start:imp_end] if not _ALLOCATES_RE.match(ln.strip())]
    kept_tail = [ln for ln in lines[imp_end:] if not _ALLOCATES_RE.match(ln.strip())]
    new_imps = [alloc_line, *kept]
    lines[imp_idx] = f"Implicits: {len(new_imps)}"
    lines[imp_start:] = [*new_imps, *kept_tail]
    return "\n".join(lines)


__all__ = [
    "apply_anoint_to_amulet_raw",
    "extract_existing_anoint",
    "strip_anoint_from_amulet_raw",
]
