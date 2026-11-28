"""pob24agents GitHub release discovery via static bucket/pob2.json + Ed25519 SHA256SUMS."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .constants import POB2_CHANNEL_JSON_URL, POB2_DOWNLOAD_BASE, rewrite_github_url
from .paths import get_posix_platform_tag, is_windows
from .signature import SIG_NAME, SUMS_NAME, verify_sums

_LOG = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^(?:poe2-)?v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def release_platform_tag() -> str:
    if is_windows():
        return "windows-x64"
    return get_posix_platform_tag().replace("darwin-", "macos-", 1)


def parse_version(text: str) -> tuple[int, int, int, int] | None:
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1)


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def latest_release_tag(client: Any, mirror: str | None) -> str | None:
    """Resolve latest stable ``poe2-vX.Y.Z`` tag via ``bucket/pob2.json`` (never ``/releases/latest``)."""
    try:
        resp = client.get(rewrite_github_url(POB2_CHANNEL_JSON_URL, mirror=mirror))
        if resp.status_code != 200:
            return None
        ver = str(resp.json().get("version", "")).strip()
        if parse_version(ver) is None:
            return None
        return f"poe2-v{ver}"
    except (httpx.HTTPError, OSError, ValueError, AttributeError) as exc:
        _LOG.debug("Release channel lookup failed: %s", exc)
        return None


def asset_url(tag: str, name: str, mirror: str | None) -> str:
    return rewrite_github_url(f"{POB2_DOWNLOAD_BASE}/{tag}/{name}", mirror=mirror)


def signed_sums(client: Any, tag: str, mirror: str | None) -> dict[str, str] | None:
    """Return ``{filename: sha256}`` from a PoE2 release's Ed25519-signed SHA256SUMS.txt."""
    sums_resp = client.get(asset_url(tag, SUMS_NAME, mirror))
    sig_resp = client.get(asset_url(tag, SIG_NAME, mirror))
    if sums_resp.status_code != 200 or sig_resp.status_code != 200:
        return None
    if not verify_sums(sums_resp.content, sig_resp.text):
        return None
    out: dict[str, str] = {}
    for line in sums_resp.text.splitlines():
        digest, _, name = line.partition("  ")
        if len(digest) == 64 and name:
            out[name.lstrip("*").strip()] = digest.lower()
    return out
