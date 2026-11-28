# SPDX-License-Identifier: GPL-3.0-only
"""Outbound anti-SSRF guard for PoE2 remote build URLs: public HTTPS only on every redirect hop."""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse

import httpx

from ..errors import DaemonRequestError
from ..share_codec import MAX_SHARE_XML_BYTES

MAX_REDIRECTS: int = 5
# Clash / sing-box fake-ip DNS answers domain names from these benchmark/ULA ranges while the proxy
# resolves the real upstream; allowed only for DNS-resolved names, never literal IP URLs.
_FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("fc00::/18"))
_BLOCKED_HOSTS = frozenset({"api.github.com", "localhost", "localhost.localdomain"})
_NUMERIC_IPV4_OBFUSCATION_RE = re.compile(r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}$", re.I)


def _resolve_host(host: str) -> list[str]:
    """Return all TCP socket addresses ``host`` resolves to (empty when offline/unresolvable)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _is_public_ip(addr: str, *, resolved: bool = True) -> bool:
    """Return True iff ``addr`` is a public global unicast IP (or fake-ip when ``resolved=True``)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if resolved and any(ip in net for net in _FAKE_IP_NETS):
        return True
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def check_public_https_url(url: str) -> None:
    """Refuse anything except a public ``https://`` URL (rejecting loopback/RFC1918/link-local/reserved)."""
    try:
        parsed = httpx.URL(url.strip())
        scheme = parsed.scheme.lower()
        host = parsed.raw_host.decode("ascii").rstrip(".").lower()
        has_userinfo = bool(parsed.userinfo)
    except (httpx.InvalidURL, UnicodeError):
        scheme, host, has_userinfo = "", "", False

    if scheme != "https" or not host:
        raise DaemonRequestError(
            f"Refusing to fetch {url!r}: only public https:// share links are supported."
        )
    if has_userinfo:
        raise DaemonRequestError(
            f"Refusing to fetch {url!r}: embedded URL credentials are not allowed."
        )
    if host in _BLOCKED_HOSTS or host.endswith((".localhost", ".local")):
        raise DaemonRequestError(f"Refusing to fetch {url!r}: host {host!r} is not allowed.")

    bare_host = host.strip("[]")
    try:
        ipaddress.ip_address(bare_host.split("%", 1)[0])
        literal = True
    except ValueError:
        literal = False
        if _NUMERIC_IPV4_OBFUSCATION_RE.match(bare_host):
            raise DaemonRequestError(
                f"Refusing to fetch {url!r}: obfuscated numeric IP host {host!r} is not allowed."
            ) from None

    targets = [bare_host] if literal else _resolve_host(bare_host)
    for addr in targets:
        if not _is_public_ip(addr, resolved=not literal):
            raise DaemonRequestError(
                f"Refusing to fetch {url!r}: host {host!r} resolves to a private, loopback, "
                "link-local, multicast, or reserved address."
            )


def get_public_https(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int | None = None,
) -> httpx.Response:
    """GET ``url``, validating ``check_public_https_url`` on the initial URL and every redirect hop."""
    limit = MAX_SHARE_XML_BYTES if max_bytes is None else int(max_bytes)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_public_https_url(current)
        with client.stream("GET", current, follow_redirects=False) as resp:
            if resp.is_redirect:
                location = (resp.headers.get("location") or "").strip()
                if location:
                    current = urllib.parse.urljoin(str(resp.url), location)
                    continue
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > limit:
                    raise DaemonRequestError(
                        f"Remote build response from `{current}` exceeded {limit // (1024 * 1024)} MiB limit."
                    )
            return httpx.Response(
                status_code=resp.status_code,
                headers=resp.headers,
                content=bytes(buf),
                request=resp.request,
            )
    raise DaemonRequestError(f"Refusing to fetch {url!r}: exceeded {MAX_REDIRECTS} redirects.")


__all__ = [
    "MAX_REDIRECTS",
    "check_public_https_url",
    "get_public_https",
]
