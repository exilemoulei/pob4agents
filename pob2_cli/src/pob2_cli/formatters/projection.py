# SPDX-License-Identifier: GPL-3.0-only
"""Deterministic `--section` and `--fields` projection helpers for PoE2 CLI read commands (L2)."""

from __future__ import annotations

import json
from typing import Any

from ..errors import DaemonRequestError

_ENVELOPE_KEYS: tuple[str, ...] = ("ok", "game")


def parse_csv_tokens(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated option string into deduplicated non-empty tokens."""
    if not raw:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for part in str(raw).split(","):
        tok = part.strip()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return tuple(out)


def _available_top_keys(payload: dict[str, Any], envelope_keys: tuple[str, ...]) -> list[str]:
    env = set(envelope_keys)
    return [k for k in payload if k not in env]


def _resolve_key_ci(mapping: dict[str, Any], target: str) -> str | None:
    if target in mapping:
        return target
    low = target.lower()
    for k in mapping:
        if k.lower() == low:
            return k
    return None


def _extract_path(
    data: Any,
    parts: list[str],
    *,
    full_path: str,
    prefix: str = "",
) -> Any:
    if not parts:
        return data
    head, tail = parts[0], parts[1:]
    if isinstance(data, dict):
        matched = _resolve_key_ci(data, head)
        if matched is None:
            valid = sorted(str(k) for k in data)
            loc = f" under `{prefix}`" if prefix else ""
            raise DaemonRequestError(
                f"[POE2-INVALID-FIELD] Unknown field `{full_path}` (segment `{head}` not found{loc}). "
                f"Valid keys: {', '.join(valid) if valid else '(none)'}."
            )
        nxt_prefix = f"{prefix}.{matched}" if prefix else matched
        return _extract_path(data[matched], tail, full_path=full_path, prefix=nxt_prefix)
    if isinstance(data, list):
        projected_items: list[Any] = []
        for item in data:
            if isinstance(item, dict):
                matched = _resolve_key_ci(item, head)
                if matched is not None:
                    val = _extract_path(item[matched], tail, full_path=full_path, prefix=f"{prefix}[].{matched}")
                    projected_items.append(val)
        if not projected_items and data:
            sample_keys = sorted({str(k) for it in data if isinstance(it, dict) for k in it})
            raise DaemonRequestError(
                f"[POE2-INVALID-FIELD] Unknown field `{full_path}` on list items under `{prefix}`. "
                f"Valid item keys: {', '.join(sample_keys) if sample_keys else '(none)'}."
            )
        return projected_items
    raise DaemonRequestError(
        f"[POE2-INVALID-FIELD] Cannot traverse `{full_path}` through non-object value at `{prefix}`."
    )


def _merge_path(dest: dict[str, Any], parts: list[str], value: Any) -> None:
    cur = dest
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _canonical_path_parts(data: dict[str, Any], parts: list[str]) -> list[str]:
    out: list[str] = []
    cur: Any = data
    for p in parts:
        if isinstance(cur, dict):
            matched = _resolve_key_ci(cur, p)
            if matched is None:
                out.append(p)
                cur = None
            else:
                out.append(matched)
                cur = cur[matched]
        else:
            out.append(p)
            cur = None
    return out


def project_payload(
    payload: dict[str, Any],
    *,
    sections_raw: str | None = None,
    fields_raw: str | None = None,
    allowed_sections: tuple[str, ...] | None = None,
    envelope_keys: tuple[str, ...] = _ENVELOPE_KEYS,
) -> dict[str, Any]:
    """Apply validated `--section` and `--fields` projection to a CLI response dict."""
    sections = parse_csv_tokens(sections_raw)
    fields = parse_csv_tokens(fields_raw)
    if not sections and not fields:
        return payload

    valid_sections = list(allowed_sections) if allowed_sections is not None else _available_top_keys(payload, envelope_keys)
    working: dict[str, Any] = payload

    if sections:
        valid_lookup = {s.lower(): s for s in valid_sections}
        bad_secs = [s for s in sections if s.lower() not in valid_lookup]
        if bad_secs:
            raise DaemonRequestError(
                f"[POE2-INVALID-SECTION] Unknown section(s): {', '.join(bad_secs)}. "
                f"Valid sections: {', '.join(valid_sections) if valid_sections else '(none)'}."
            )
        selected: dict[str, Any] = {}
        for ek in envelope_keys:
            if ek in payload:
                selected[ek] = payload[ek]
        for s in sections:
            canon = valid_lookup[s.lower()]
            if canon in payload:
                selected[canon] = payload[canon]
            else:
                matched = _resolve_key_ci(payload, canon)
                if matched is not None:
                    selected[matched] = payload[matched]
        working = selected

    if fields:
        env_set = set(envelope_keys)
        searchable = {k: v for k, v in working.items() if k not in env_set}
        projected: dict[str, Any] = {}
        for ek in envelope_keys:
            if ek in working:
                projected[ek] = working[ek]
        for raw_path in fields:
            parts = [p.strip() for p in raw_path.split(".") if p.strip()]
            if not parts:
                continue
            val = _extract_path(searchable, parts, full_path=raw_path)
            canon_parts = _canonical_path_parts(searchable, parts)
            _merge_path(projected, canon_parts, val)
        return projected

    return working


def render_projected_markdown(
    projected: dict[str, Any],
    *,
    sections_raw: str | None = None,
    fields_raw: str | None = None,
    envelope_keys: tuple[str, ...] = _ENVELOPE_KEYS,
) -> str:
    """Render concise human-readable Markdown for a `--section`/`--fields`-projected payload."""
    env_set = set(envelope_keys)
    body = {k: v for k, v in projected.items() if k not in env_set}
    hdr_parts: list[str] = []
    if sections_raw:
        hdr_parts.append(f"section={sections_raw}")
    if fields_raw:
        hdr_parts.append(f"fields={fields_raw}")
    lines = [f"### Projected View ({', '.join(hdr_parts)})" if hdr_parts else "### Projected View"]
    for key, val in body.items():
        if isinstance(val, dict):
            lines.append(f"\n#### {key}")
            for sub_k, sub_v in val.items():
                if isinstance(sub_v, (dict, list)):
                    lines.append(f"- **{sub_k}**: `{json.dumps(sub_v, ensure_ascii=False)}`")
                else:
                    lines.append(f"- **{sub_k}**: {sub_v}")
        elif isinstance(val, list):
            lines.append(f"\n#### {key} ({len(val)})")
            for item in val[:25]:
                if isinstance(item, (dict, list)):
                    lines.append(f"- `{json.dumps(item, ensure_ascii=False)}`")
                else:
                    lines.append(f"- {item}")
            if len(val) > 25:
                lines.append(f"- ... ({len(val) - 25} more; use `--json` for full list)")
        else:
            lines.append(f"- **{key}**: {val}")
    return "\n".join(lines)


__all__ = [
    "parse_csv_tokens",
    "project_payload",
    "render_projected_markdown",
]
