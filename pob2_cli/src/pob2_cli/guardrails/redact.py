# SPDX-License-Identifier: GPL-3.0-only
"""L0 secret, proxy-credential, and auth-header redaction helpers for ``pob2_cli``."""

from __future__ import annotations

import os
import re
from urllib.parse import quote

_COOKIE_SECRET_RE = re.compile(
    r"\b(?:POESESSID|POE_CN_SESSID|cf_clearance)=(?:\"[^\r\n\"]*\"|'[^\r\n']*'|[^;\s\"'<>]+)",
    re.IGNORECASE,
)
_HEX_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_URL_USERINFO_SECRET_RE = re.compile(r"(://)[^/?#@\s\"'<>]+(?::[^/?#@\s\"'<>]*)?@")
_AUTH_HEADER_RE = re.compile(
    r"(\b(?:Authorization|Proxy-Authorization|X-PoB2?-Token)[\"']?\s*[:=]\s*[\"']?)"
    r"(?:(?:Bearer|Basic|Token)\s+)?[^\s,;\"'<>}]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[^\s,;\"'<>]+", re.IGNORECASE)
_COOKIE_HEADER_RE = re.compile(
    r"(\bCookie[\"']?\s*[:=]\s*[\"']?)"
    r"((?:(?:[^\s;\r\n\"'<>]|\"[^\r\n\"]*\"|'[^\r\n']*')+;\s*)*"
    r"(?:[^\s;\r\n\"'<>]|\"[^\r\n\"]*\"|'[^\r\n']*')+)",
    re.IGNORECASE,
)

_SECRET_ENV_VARS: tuple[str, ...] = (
    "POESESSID",
    "POE_COOKIE",
    "POB2_OAUTH_TOKEN",
)


def _collect_env_secrets() -> list[str]:
    candidates: set[str] = set()
    for var in _SECRET_ENV_VARS:
        raw = (os.environ.get(var) or "").strip()
        if not raw:
            continue
        raw_values = [raw]
        for part in raw.split(";"):
            if "=" in part:
                val = part.split("=", 1)[1].strip()
                if len(val) >= 8:
                    raw_values.append(val)
        for val in raw_values:
            if val:
                candidates.add(val)
                encoded = quote(val, safe="")
                if encoded:
                    candidates.add(encoded)
    return sorted(candidates, key=len, reverse=True)


def _redact_cookie_header(match: re.Match[str]) -> str:
    prefix, body = match.group(1), match.group(2)
    parts = re.split(r"(;\s*)", body)
    scrubbed = [part if idx % 2 == 1 else "[redacted]" for idx, part in enumerate(parts)]
    return prefix + "".join(scrubbed)


def redact_url_credentials(text: str) -> str:
    """Redact ``scheme://user:secret@host`` and ``scheme://token@host`` credentials in URLs/messages."""
    if not text:
        return ""
    return _URL_USERINFO_SECRET_RE.sub(r"\g<1>[redacted]@", str(text))


def redact_secrets(text: str) -> str:
    """Scrub env secret values, proxy/URL credentials, auth headers, cookies, and >=16-char hex tokens."""
    if not text:
        return ""
    cleaned = str(text)

    env_secrets = _collect_env_secrets()
    if env_secrets:
        env_re = re.compile("|".join(re.escape(s) for s in env_secrets))
        cleaned = env_re.sub("[redacted]", cleaned)

    cleaned = _URL_USERINFO_SECRET_RE.sub(r"\g<1>[redacted]@", cleaned)
    cleaned = _AUTH_HEADER_RE.sub(r"\g<1>[redacted]", cleaned)
    cleaned = _BEARER_RE.sub("Bearer [redacted]", cleaned)
    cleaned = _COOKIE_HEADER_RE.sub(_redact_cookie_header, cleaned)
    cleaned = _COOKIE_SECRET_RE.sub("[redacted]", cleaned)
    return _HEX_TOKEN_RE.sub("[redacted]", cleaned)


redact = redact_secrets


def get_auth_env_status() -> dict[str, bool]:
    """Return pure-boolean presence flags for PoE2 auth environment variables (never secret values)."""
    return {
        "poesessid": bool((os.environ.get("POESESSID") or "").strip()),
        "poeCookie": bool((os.environ.get("POE_COOKIE") or "").strip()),
    }


__all__ = [
    "get_auth_env_status",
    "redact",
    "redact_secrets",
    "redact_url_credentials",
]
