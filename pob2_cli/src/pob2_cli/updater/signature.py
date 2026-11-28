"""Ed25519 signature verification for pob24agents release checksums (RFC 8032, section 5.1.7)."""

from __future__ import annotations

import base64
import binascii
import hashlib

RELEASE_PUBLIC_KEYS: tuple[str, ...] = (
    "fvwea+k3NZ54EzRFCoMYgvZLy3XTkKGXxo2UPmsNZIM=",
)

SUMS_NAME = "SHA256SUMS.txt"
SIG_NAME = "SHA256SUMS.txt.sig"

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

_Point = tuple[int, int, int, int]


def _add(p: _Point, q: _Point) -> _Point:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, p: _Point) -> _Point:
    q: _Point = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p: _Point, q: _Point) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decompress(raw: bytes) -> _Point | None:
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _base_point() -> _Point:
    y = 4 * pow(5, _P - 2, _P) % _P
    x = _recover_x(y, 0)
    if x is None:
        raise RuntimeError("Ed25519 base point")
    return (x, y, 1, x * y % _P)


_G = _base_point()


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Return True when ``signature`` is a valid Ed25519 signature of ``message`` under ``public_key``."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _decompress(public_key)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


def _b64(text: str, size: int) -> bytes | None:
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == size else None


def verify_sums(sums: bytes, sig_text: str, keys: tuple[str, ...] | None = None) -> bool:
    """Return True when base64 ``sig_text`` signs ``sums`` under one of ``keys``."""
    signature = _b64(sig_text, 64)
    if signature is None:
        return False
    for key in RELEASE_PUBLIC_KEYS if keys is None else keys:
        public_key = _b64(key, 32)
        if public_key is not None and ed25519_verify(public_key, sums, signature):
            return True
    return False
