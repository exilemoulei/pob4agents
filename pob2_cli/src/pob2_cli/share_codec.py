# SPDX-License-Identifier: GPL-3.0-only
"""PoB2 share-code codec (L0): URL-safe Base64 over zlib/gzip/deflate with a strict 64 MiB inflate cap."""

from __future__ import annotations

import base64
import binascii
import re
import zlib

# A legitimate PoB2 build XML is at most a few MiB; anything past 64 MiB is a decompression bomb.
MAX_SHARE_XML_BYTES: int = 64 * 1024 * 1024

_NOT_BASE64_RE = re.compile(r"[^A-Za-z0-9+/]")


class ShareCodeLimitError(ValueError):
    """Raised when inflating a compressed PoB2 share code exceeds ``MAX_SHARE_XML_BYTES``."""


def inflate_capped(
    raw_bytes: bytes,
    wbits: int = zlib.MAX_WBITS | 32,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Incremental ``zlib.decompressobj`` inflate bounded strictly to ``max_bytes`` (default 64 MiB)."""
    limit = MAX_SHARE_XML_BYTES if max_bytes is None else int(max_bytes)
    inflater = zlib.decompressobj(wbits)
    out = inflater.decompress(raw_bytes, limit + 1)
    if len(out) > limit or inflater.unconsumed_tail:
        mib = limit / (1024 * 1024)
        label = f"{int(mib)} MiB" if mib.is_integer() else f"{limit} bytes"
        raise ShareCodeLimitError(
            f"Decompressed PoB2 build XML exceeds {label} ({limit} bytes) decompression cap."
        )
    if not inflater.eof:
        raise zlib.error("incomplete or truncated compressed stream")
    return out


def xml_to_share_code(xml_text: str) -> str:
    """Compress raw PoB2 XML text into a URL-safe Base64 zlib share code."""
    clean_xml = xml_text.lstrip("\ufeff")
    compressed = zlib.compress(clean_xml.encode("utf-8"), level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii")


def share_code_to_xml(code: str, *, max_bytes: int | None = None) -> str:
    """Decode a URL-safe Base64 PoB2 share code (zlib, #2502 gzip, or raw deflate) with a 64 MiB cap."""
    cleaned = _NOT_BASE64_RE.sub("", (code or "").replace("-", "+").replace("_", "/"))
    if not cleaned:
        raise ValueError("Invalid PoB2 share code: empty share code.")
    cleaned += "=" * ((-len(cleaned)) % 4)
    try:
        raw_bytes = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid PoB2 share code: {exc}") from exc
    try:
        try:
            decompressed = inflate_capped(raw_bytes, zlib.MAX_WBITS | 32, max_bytes=max_bytes)
        except zlib.error:
            decompressed = inflate_capped(raw_bytes, -zlib.MAX_WBITS, max_bytes=max_bytes)
    except ShareCodeLimitError as exc:
        raise ShareCodeLimitError(f"Invalid PoB2 share code: {exc}") from exc
    except (zlib.error, ValueError) as exc:
        raise ValueError(f"Invalid PoB2 share code: {exc}") from exc
    return decompressed.decode("utf-8-sig", errors="replace").lstrip("\ufeff")


__all__ = [
    "MAX_SHARE_XML_BYTES",
    "ShareCodeLimitError",
    "inflate_capped",
    "share_code_to_xml",
    "xml_to_share_code",
]
