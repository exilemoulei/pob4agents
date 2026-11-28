"""Constants, exceptions, dataclasses, and HTTP/version helpers for the PoB2 updater."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

from .. import __version__
from ..game_profile import ENV_GITHUB_MIRROR, ENV_PROXY, PACKAGE_NAME
from ..guardrails import redact_secrets, redact_url_credentials

USER_AGENT = f"{PACKAGE_NAME}/{__version__} (+https://github.com/exilemoulei/pob4agents)"

POBTOOLS_LATEST_URL = "https://github.com/Hsiung-Shao/PobTools-zh/releases/latest"
POBTOOLS_DOWNLOAD_BASE = "https://github.com/Hsiung-Shao/PobTools-zh/releases/download"
POB2_MANIFEST_URL = (
    "https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding-PoE2/master/manifest.xml"
)
POB2_DEFAULT_BRANCH = "master"
# Static channel pointer avoiding api.github.com and leaving /releases/latest untouched for PoE1.
POB2_CHANNEL_JSON_URL = "https://raw.githubusercontent.com/exilemoulei/pob4agents/main/bucket/pob2.json"
POB2_DOWNLOAD_BASE = "https://github.com/exilemoulei/pob4agents/releases/download"

# Exact upstream LuaJIT & luautf8 revisions required by PoB2 0.5.5+ (`+=` / `..=` syntax)
POSIX_LUAJIT_COMMIT = "2460b3ff93a1c955de3d62cfc825de7d68dc272e"
POSIX_LUAJIT_TARBALL_URL = f"https://github.com/LuaJIT/LuaJIT/archive/{POSIX_LUAJIT_COMMIT}.tar.gz"
POSIX_LUAUTF8_TAG = "0.1.6"
POSIX_LUAUTF8_TARBALL_URL = (
    f"https://github.com/starwing/luautf8/archive/refs/tags/{POSIX_LUAUTF8_TAG}.tar.gz"
)
POSIX_LUAJIT_TARBALL_SHA256 = "5c52d06876d86aa199f9b8cdc65f6f9ac086fdfa8a4489ebf8dc4516e194550b"
POSIX_LUAUTF8_TARBALL_SHA256 = "b76873b4afb09abd00508a66887ae1658492078869f9414c334dea35b5769656"

# Verified against PobTools-zh v1.8.14 + data-26: Data/bridge/ in update zip, Data/poe2/* in DataV2 zip
POBTOOLS_APP_ONLY_PREFIXES: tuple[str, ...] = (
    "Data/bridge",
    "Data/poe2/zh-rCN",
    "Data/poe2/zh-rTW",
)
POBTOOLS_DATA_ONLY_PREFIXES: tuple[str, ...] = (
    "Data/bridge",
    "Data/poe2/zh-rCN",
    "Data/poe2/zh-rTW",
    "Data/translations_version.json",
)

_AUTO_CHECK_TIMEOUT_SPEC = (1.5, 5.0, 5.0, 1.5)
_CHECK_TIMEOUT_SPEC = (3.0, 8.0, 8.0, 3.0)
_ASSET_TIMEOUT_SPEC = (5.0, 20.0, 20.0, 5.0)
ASSET_MAX_RETRIES = 1
MANIFEST_STAT_CACHE_FILENAME = "manifest_stat_cache_poe2.json"
UPDATE_STATE_FILENAME = "update_state.json"
POBTOOLS_META_FILENAME = ".pobtools_poe2_version.json"

STATE_UP_TO_DATE = "up_to_date"
STATE_UPDATED = "updated"
STATE_DEGRADED_NETWORK = "degraded_network_error"
STATE_DEGRADED_ROLLBACK = "degraded_gate_rollback"
STATE_BOOTSTRAP_MISSING = "bootstrap_missing"
OK_STATES = frozenset({STATE_UP_TO_DATE, STATE_UPDATED})

# Clean slim seed subsets copied from a local read-only seed (zero pob-zh.exe / Fonts / PoE1)
SEED_POE2_SUBDIRS = ("Classes", "Data", "Modules", "lua", "TreeData")
SEED_DATA_SUBDIRS = ("bridge", "poe2")

GITHUB_MIRROR_PREFIXES = (
    "https://github.com/",
    "https://raw.githubusercontent.com/",
    "https://objects.githubusercontent.com/",
)


class UpdateError(RuntimeError):
    """Base error for PoB2 engine bootstrap/update failures."""


class EngineGateError(UpdateError):
    """Raised when headless PoB2 engine smoke probe (gate_result + hello) fails."""


@dataclass(frozen=True)
class ManifestFileEntry:
    """Single file entry parsed from PathOfBuilding-PoE2 manifest.xml."""

    raw_name: str
    local_rel: str
    part: str
    sha1: str
    runtime: str | None
    platform: str | None


@dataclass(frozen=True)
class PobManifest:
    """Parsed PathOfBuilding-PoE2 manifest.xml."""

    version: str
    branch: str
    platform: str
    sources: dict[str, dict[str, str]]
    files: dict[str, ManifestFileEntry]
    raw_xml: str


def _to_httpx_timeout(timeout: Any) -> httpx.Timeout:
    import httpx

    if isinstance(timeout, httpx.Timeout):
        return timeout
    if isinstance(timeout, tuple) and len(timeout) == 4:
        return httpx.Timeout(connect=timeout[0], read=timeout[1], write=timeout[2], pool=timeout[3])
    return httpx.Timeout(timeout)


def resolve_proxy(proxy: str | None = None) -> str | None:
    """Resolve HTTP/SOCKS proxy from explicit arg, POB2_PROXY, or standard proxy env vars."""
    if proxy is not None and proxy.strip():
        return proxy.strip()
    for var in (ENV_PROXY, "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


def resolve_mirror(mirror: str | None = None) -> str | None:
    """Resolve optional GitHub mirror prefix from explicit arg or POB2_GITHUB_MIRROR."""
    if mirror is not None and mirror.strip():
        return mirror.strip().rstrip("/")
    env_mirror = os.environ.get(ENV_GITHUB_MIRROR, "").strip()
    return env_mirror.rstrip("/") if env_mirror else None


def rewrite_github_url(url: str, mirror: str | None = None) -> str:
    """Optionally rewrite a GitHub URL through a user-configured mirror prefix."""
    eff_mirror = resolve_mirror(mirror)
    if not eff_mirror or not url.startswith(GITHUB_MIRROR_PREFIXES):
        return url
    if "{url}" in eff_mirror:
        return eff_mirror.format(url=url)
    return f"{eff_mirror}/{url}"


def _make_http_client(
    *,
    timeout: Any,
    proxy: str | None = None,
    follow_redirects: bool = True,
    max_connections: int = 16,
) -> httpx.Client:
    import httpx

    eff_proxy = resolve_proxy(proxy)
    limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
    kwargs: dict[str, Any] = {
        "timeout": _to_httpx_timeout(timeout),
        "follow_redirects": follow_redirects,
        "headers": {"User-Agent": USER_AGENT},
        "trust_env": False,
        "limits": limits,
    }
    if eff_proxy:
        kwargs["proxy"] = eff_proxy
    try:
        return httpx.Client(**kwargs)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        detail = redact_secrets(redact_url_credentials(str(exc).strip())) or type(exc).__name__
        raise UpdateError(f"Invalid HTTP/proxy configuration: {detail}") from exc


def _parse_semver(ver: str | None) -> tuple[int, ...]:
    if not ver:
        return ()
    cleaned = ver.strip().lstrip("vV")
    return tuple(int(x) for x in re.findall(r"\d+", cleaned))


def _is_version_gte(ver_a: str | None, ver_b: str | None) -> bool:
    a, b = _parse_semver(ver_a), _parse_semver(ver_b)
    if not b:
        return True
    if not a:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) >= b + (0,) * (n - len(b))


def _is_version_newer(remote: str | None, local: str | None) -> bool:
    r, loc = _parse_semver(remote), _parse_semver(local)
    if not r:
        return False
    if not loc:
        return True
    n = max(len(r), len(loc))
    return r + (0,) * (n - len(r)) > loc + (0,) * (n - len(loc))
