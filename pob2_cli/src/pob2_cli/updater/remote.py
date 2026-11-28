"""Remote metadata fetching and safe asset staging for PathOfBuilding-PoE2 (dev) and PobTools-zh."""

from __future__ import annotations

import concurrent.futures
import contextlib
import functools
import hashlib
import re
import shutil
import sys
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

from ..guardrails import redact_secrets, redact_url_credentials
from .constants import (
    _ASSET_TIMEOUT_SPEC,
    _CHECK_TIMEOUT_SPEC,
    ASSET_MAX_RETRIES,
    POB2_MANIFEST_URL,
    POBTOOLS_DOWNLOAD_BASE,
    POBTOOLS_LATEST_URL,
    ManifestFileEntry,
    PobManifest,
    UpdateError,
    _to_httpx_timeout,
    resolve_mirror,
    rewrite_github_url,
)
from .manifest import build_manifest_file_url, parse_pob_manifest

_SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SAFE_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+()\[\]-]{0,199}")
_WINDOWS_DEVICE_RE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", re.IGNORECASE)
ALLOWED_RELEASE_ASSET_EXTS: tuple[str, ...] = (".zip", ".tar.gz", ".whl")


def redact_proxy_credentials(text: str) -> str:
    """Scrub proxy/URL credentials and auth secrets before logging or surfacing updater errors."""
    return redact_secrets(redact_url_credentials(text))


def _safe_asset_name(
    raw: Any,
    *,
    allowed_extensions: tuple[str, ...] = ALLOWED_RELEASE_ASSET_EXTS,
    expected_prefix: str | None = None,
) -> str:
    """Validate a release asset filename (single safe component, allowlisted extension, no Windows devices)."""
    name = str(raw or "") if isinstance(raw, str) else ""
    if (
        not name
        or "/" in name
        or "\\" in name
        or ".." in name
        or Path(name).name != name
        or not _SAFE_ASSET_NAME_RE.fullmatch(name)
        or _WINDOWS_DEVICE_RE.fullmatch(name)
    ):
        raise UpdateError(f"Refusing release asset with an unsafe name: {str(raw)[:80]!r}")
    low = name.lower()
    if allowed_extensions and not any(low.endswith(ext.lower()) for ext in allowed_extensions):
        raise UpdateError(
            f"Refusing release asset {name!r}: unexpected extension (allowed: {', '.join(allowed_extensions)})."
        )
    if expected_prefix and not low.startswith(expected_prefix.lower()):
        raise UpdateError(
            f"Refusing release asset {name!r}: mismatched asset prefix (expected {expected_prefix!r})."
        )
    return name


validate_release_asset_name = _safe_asset_name


@contextlib.contextmanager
def _network_errors(what: str) -> Iterator[None]:
    """Translate HTTP/proxy/OS errors into ``UpdateError`` with proxy credentials redacted."""
    import httpx

    try:
        yield
    except UpdateError as exc:
        clean = redact_proxy_credentials(str(exc))
        if clean != str(exc):
            raise UpdateError(clean) from exc
        raise
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
        detail = redact_proxy_credentials(str(exc).strip()) or type(exc).__name__
        raise UpdateError(f"{what} failed: {detail} (offline? try --proxy or --mirror).") from exc


def _pkg() -> Any:
    return sys.modules["pob2_cli.updater"]


def _extract_tag_from_location(location: str) -> str:
    if "/tag/" in location:
        tag = urllib.parse.unquote(location.rsplit("/tag/", 1)[-1].split("?", 1)[0].strip("/"))
        if tag and ".." not in tag and _SAFE_TAG_RE.fullmatch(tag):
            return tag
    raise UpdateError(
        f"Could not extract safe release tag from Location header: {redact_proxy_credentials(location)!r}"
    )


def _github_first(fetch: Any, fallback: Any) -> Any:
    from xml.etree.ElementTree import ParseError

    import httpx

    try:
        return fetch(lambda url: url)
    except (httpx.HTTPError, UpdateError, ValueError, ParseError):
        return fallback()


def fetch_pobtools_latest_info(
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    timeout: Any = None,
    fetch_manifest: bool = True,
) -> dict[str, Any]:
    """Resolve latest PobTools-zh tag via 302 redirect (never api.github.com) and optional manifest JSON."""
    eff_timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC if timeout is None else timeout)
    via_mirror = functools.partial(rewrite_github_url, mirror=mirror)

    def _fetch(via: Any) -> dict[str, Any]:
        return _fetch_pobtools_latest_info(via, proxy, eff_timeout, fetch_manifest)

    if not fetch_manifest or not resolve_mirror(mirror):
        return _fetch(via_mirror)
    return _github_first(_fetch, lambda: _fetch(via_mirror))


def _fetch_pobtools_latest_info(
    via: Any, proxy: str | None, eff_timeout: Any, fetch_manifest: bool
) -> dict[str, Any]:
    make_client = _pkg()._make_http_client
    with _network_errors("Fetching PobTools-zh release metadata"):
        with make_client(timeout=eff_timeout, proxy=proxy, follow_redirects=False) as client:
            resp = client.get(via(POBTOOLS_LATEST_URL))
            if resp.status_code in (301, 302, 303, 307, 308):
                tag = _extract_tag_from_location(resp.headers.get("location", ""))
            elif resp.status_code == 200 and "/tag/" in str(resp.url):
                tag = _extract_tag_from_location(str(resp.url))
            else:
                raise UpdateError(f"Unexpected status {resp.status_code} from PobTools-zh releases/latest")

        info: dict[str, Any] = {
            "tag": tag,
            "version": tag.lstrip("vV"),
            "manifest": None,
            "dataManifest": None,
        }
        if not fetch_manifest:
            return info

        manifest_url = via(f"{POBTOOLS_DOWNLOAD_BASE}/{tag}/PobTools-manifest-{tag}.json")
        with make_client(timeout=eff_timeout, proxy=proxy, follow_redirects=True) as client:
            mresp = client.get(manifest_url)
            mresp.raise_for_status()
            manifest_data = mresp.json()
            info["manifest"] = manifest_data
            if isinstance(manifest_data, dict):
                if manifest_data.get("version"):
                    info["version"] = str(manifest_data["version"]).strip()
                data_tag = manifest_data.get("dataTag")
                if data_tag:
                    safe_dtag = str(data_tag).strip()
                    if ".." in safe_dtag or not _SAFE_TAG_RE.fullmatch(safe_dtag):
                        raise UpdateError(f"Unsafe dataTag in PobTools manifest: {safe_dtag[:80]!r}")
                    dresp = client.get(
                        via(f"{POBTOOLS_DOWNLOAD_BASE}/{safe_dtag}/PobTools-manifest-{safe_dtag}.json")
                    )
                    if dresp.status_code == 200:
                        info["dataManifest"] = dresp.json()
        return info


def fetch_pob_manifest_remote(
    *,
    etag: str | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    timeout: Any = None,
    slim: bool = True,
) -> tuple[str, PobManifest | None, str | None]:
    """Fetch official PathOfBuilding-PoE2 ``dev`` manifest.xml with optional If-None-Match ETag."""
    eff_timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC if timeout is None else timeout)
    headers = {"If-None-Match": etag} if etag else {}

    def _fetch(via: Any) -> tuple[str, PobManifest | None, str | None]:
        with _network_errors("Fetching PoB2 manifest"):
            with _pkg()._make_http_client(timeout=eff_timeout, proxy=proxy, follow_redirects=True) as client:
                resp = client.get(via(POB2_MANIFEST_URL), headers=headers)
                if resp.status_code == 304:
                    return ("not_modified", None, resp.headers.get("etag") or etag)
                resp.raise_for_status()
                return ("ok", parse_pob_manifest(resp.text, slim=slim), resp.headers.get("etag") or etag)

    result = _fetch(functools.partial(rewrite_github_url, mirror=mirror))
    if result[0] != "ok" or not resolve_mirror(mirror):
        return result
    return _github_first(_fetch, lambda: result)


def _download_bytes_with_retry(
    client: httpx.Client,
    url: str,
    retries: int = ASSET_MAX_RETRIES,
    *,
    deadline: float | None = None,
) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req_kw: dict[str, Any] = {}
            if deadline is not None:
                rem = deadline - time.monotonic()
                if rem <= 0.05:
                    raise UpdateError(f"Timed out fetching '{url}' within budget")
                req_kw["timeout"] = _to_httpx_timeout(min(5.0, max(0.5, rem)))
            resp = client.get(url, **req_kw)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            last_exc = exc
            if attempt >= retries or (deadline is not None and (time.monotonic() + 0.35) >= deadline):
                raise
            time.sleep(0.25)
    assert last_exc is not None
    raise last_exc


def _download_pob_files_to_staging(
    manifest: PobManifest,
    entries: list[ManifestFileEntry],
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    max_workers: int = 12,
) -> dict[Path, Path]:
    staged: dict[Path, Path] = {}
    if not entries:
        return staged
    pkg = _pkg()
    verify_fn = pkg.verify_pob_sha1

    with pkg._make_http_client(
        timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC),
        proxy=proxy,
        follow_redirects=True,
        max_connections=max(max_workers, 8),
    ) as client:

        def _fetch_one(entry: ManifestFileEntry) -> tuple[Path, Path]:
            url = build_manifest_file_url(manifest, entry, mirror=mirror)
            data = _download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES)
            if not verify_fn(data, entry.sha1):
                raise UpdateError(
                    f"SHA1 mismatch for '{entry.local_rel}': expected {entry.sha1}, "
                    f"got {hashlib.sha1(data).hexdigest()}"
                )
            rel_path = Path("PoE2") / entry.local_rel
            out_path = staging_dir / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            return rel_path, out_path

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(entries))) as pool:
            for fut in concurrent.futures.as_completed([pool.submit(_fetch_one, e) for e in entries]):
                rel_path, out_path = fut.result()
                staged[rel_path] = out_path
    return staged


def _rel_matches_prefix(rel_posix_lower: str, prefix: str) -> bool:
    p = prefix.replace("\\", "/").strip("/").lower()
    return bool(p) and (rel_posix_lower == p or rel_posix_lower.startswith(p + "/"))


def _extract_zip_safely(
    zip_path: Path,
    dest_dir: Path,
    *,
    skip_prefixes: tuple[str, ...] = (),
    only_prefixes: tuple[str, ...] = (),
) -> dict[Path, Path]:
    import zipfile

    extracted: dict[Path, Path] = {}
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if not infos:
            return extracted
        top_levels = {Path(info.filename).parts[0] for info in infos if Path(info.filename).parts}
        strip_top = len(top_levels) == 1 and next(iter(top_levels)).lower() not in {
            "data",
            "engine",
            "fonts",
            "poe1",
            "poe2",
            "ui",
            "pobtools",
            "filters",
        }
        for info in infos:
            parts = Path(info.filename).parts
            if strip_top and len(parts) > 1:
                parts = parts[1:]
            if not parts or any(p in ("..", "") for p in parts):
                continue
            rel = Path(*parts)
            if rel.is_absolute():
                raise UpdateError(f"Unsafe zip member path: {info.filename}")
            rel_low = rel.as_posix().lower()
            if only_prefixes and not any(_rel_matches_prefix(rel_low, op) for op in only_prefixes):
                continue
            if any(_rel_matches_prefix(rel_low, sp) for sp in skip_prefixes):
                continue
            out = (dest_dir / rel).resolve()
            try:
                out.relative_to(dest_resolved)
            except ValueError as exc:
                raise UpdateError(f"Zip Slip detected: {info.filename}") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_f, open(out, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            extracted[rel] = out
    return extracted


def _pick_pobtools_asset(manifest_data: dict[str, Any], prefer_update: bool) -> dict[str, Any] | None:
    assets = manifest_data.get("assets") or []
    zips = [a for a in assets if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".zip")]
    if not zips:
        return None
    if prefer_update:
        for a in zips:
            if "update" in str(a.get("name", "")).lower():
                return a
    for a in zips:
        nl = str(a.get("name", "")).lower()
        if "update" not in nl and "datav2" not in nl:
            return a
    return zips[0]


def _pick_data_asset(data_manifest: dict[str, Any]) -> dict[str, Any] | None:
    assets = data_manifest.get("assets") or []
    for a in assets:
        if isinstance(a, dict) and "datav2" in str(a.get("name", "")).lower() and str(a.get("name", "")).lower().endswith(".zip"):
            return a
    for a in assets:
        if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".zip"):
            return a
    return None


def _download_and_stage_release_zip(
    tag: str,
    asset: dict[str, Any],
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_prefixes: tuple[str, ...] = ("PoE1", "PoE2"),
    only_prefixes: tuple[str, ...] = (),
) -> dict[Path, Path]:
    name = _safe_asset_name(asset.get("name"), allowed_extensions=(".zip",))
    if not tag or ".." in tag or not _SAFE_TAG_RE.fullmatch(tag):
        raise UpdateError(f"Refusing release download with unsafe tag: {tag[:80]!r}")
    expected_sha256 = str(asset.get("sha256") or "").strip().lower()
    url = rewrite_github_url(f"{POBTOOLS_DOWNLOAD_BASE}/{tag}/{urllib.parse.quote(name)}", mirror=mirror)
    zip_tmp = staging_dir / "_downloads" / name
    zip_tmp.parent.mkdir(parents=True, exist_ok=True)
    with _network_errors(f"Downloading release asset '{name}'"):
        with _pkg()._make_http_client(
            timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC), proxy=proxy, follow_redirects=True
        ) as client:
            data = _download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES)
    actual = hashlib.sha256(data).hexdigest().lower()
    if actual != expected_sha256:
        raise UpdateError(f"SHA256 mismatch for '{name}': expected {expected_sha256 or '<missing>'}, got {actual}")
    zip_tmp.write_bytes(data)
    extract_root = staging_dir / "_extract" / Path(name).stem
    extract_root.mkdir(parents=True, exist_ok=True)
    return _extract_zip_safely(
        zip_tmp,
        extract_root,
        skip_prefixes=skip_prefixes,
        only_prefixes=only_prefixes,
    )
