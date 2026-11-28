# SPDX-License-Identifier: GPL-3.0-only
"""Multi-source PoE2 build URL fetcher & resolver (L1).

Supports PoB2 share codes (zlib & #2502 gzip), local XML/JSON files, CN WeGame links,
`pobb.in/{id}`, `poe2db.tw/pob/{id}`, `poe.ninja/poe2/pob/{id}`, `maxroll.gg/poe2/pob/{id}`,
`pastebin.com`, `rentry.co`, and `pob2://` protocol URIs. Rejects PoE1 URLs/XML via guardrails.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..bridge_client import validate_poe2_spec_class_ids
from ..bridge_protocol import encode_xml_share_code
from ..errors import DaemonRequestError
from ..guardrails import redact_secrets, validate_build_xml, validate_url
from ..share_codec import MAX_SHARE_XML_BYTES, ShareCodeLimitError, share_code_to_xml
from ..updater import resolve_proxy
from .url_guard import check_public_https_url, get_public_https
from .wegame_convert import convert_wegame_bundle_to_pob2_xml
from .wegame_fetch import fetch_wegame_share, is_wegame_share_url, parse_wegame_share_url

TextFetcher = Callable[[str, float], str]

_LOCAL_PATH_SUFFIXES: tuple[str, ...] = (".xml", ".pob", ".pob2", ".txt", ".json")
_WIN_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:[/\\]")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_SPEC_NODES_RE = re.compile(r"<\s*Spec\b[^>]*\bnodes\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)


def looks_like_local_file_path(raw: str) -> bool:
    """Return True when ``raw`` resembles a local file path rather than a share code or URL."""
    s = (raw or "").strip()
    if not s or s.startswith("<") or "\n" in s or "://" in s or len(s) >= 260:
        return False
    low = s.lower()
    if low.endswith(_LOCAL_PATH_SUFFIXES):
        return True
    if _WIN_DRIVE_PREFIX_RE.match(s):
        return True
    if "\\" in s:
        return True
    if "/" in s and not low.startswith(("www.", "pobb.", "poe.", "poe2", "maxroll.", "mobalytics.")):
        return True
    return False


def extract_active_spec_node_ids(xml_text: str) -> set[int]:
    """Extract integer passive node IDs from the active ``<Spec nodes="..."/>`` element in PoB2 XML."""
    if not xml_text or "<Spec" not in xml_text:
        return set()
    nodes_attr = ""
    try:
        root = ET.fromstring(xml_text)
        tree_el = root.find("Tree")
        if tree_el is not None:
            specs = tree_el.findall("Spec")
            if specs:
                try:
                    active_idx = int(tree_el.attrib.get("activeSpec") or 1)
                except ValueError:
                    active_idx = 1
                chosen = specs[active_idx - 1] if 1 <= active_idx <= len(specs) else specs[0]
                nodes_attr = chosen.attrib.get("nodes") or ""
    except ET.ParseError:
        m = _SPEC_NODES_RE.search(xml_text)
        if m:
            nodes_attr = m.group(1)
    out: set[int] = set()
    for tok in re.findall(r"\d+", nodes_attr):
        out.add(int(tok))
    return out

_DEFAULT_HEADERS = {
    "User-Agent": "pob24agents/0.1 (PathOfBuilding-PoE2 compatible)",
    "Accept": "text/plain, application/json, */*;q=0.8",
}

_SITE_SPECS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("maxroll", re.compile(r"^https://(?:www\.)?maxroll\.gg/poe2/(?:api/)?pob/([A-Za-z0-9._-]+?)/?(?:[?#].*)?$", re.I), "https://maxroll.gg/poe2/api/pob/{id}"),
    ("pobbin", re.compile(r"^https://(?:www\.)?pobb\.in/(?:pob/)?([A-Za-z0-9._-]+?)(?:/raw)?/?(?:[?#].*)?$", re.I), "https://pobb.in/pob/{id}"),
    ("poeninja", re.compile(r"^https://(?:www\.)?(?:poe\.ninja/poe2|poe2\.ninja)/pob/(?:raw/)?([A-Za-z0-9._-]+?)/?(?:[?#].*)?$", re.I), "https://poe.ninja/poe2/pob/raw/{id}"),
    ("poe2db", re.compile(r"^https://(?:www\.)?poe2db\.tw/pob/([A-Za-z0-9._-]+?)(?:/raw)?/?(?:[?#].*)?$", re.I), "https://poe2db.tw/pob/{id}/raw"),
    ("pastebin", re.compile(r"^https://(?:www\.)?pastebin\.com/(?:raw/)?([A-Za-z0-9]+)/?(?:[?#].*)?$", re.I), "https://pastebin.com/raw/{id}"),
    ("rentry", re.compile(r"^https://(?:www\.)?rentry\.co/(?:paste/)?([A-Za-z0-9_-]+)(?:/raw)?/?(?:[?#].*)?$", re.I), "https://rentry.co/paste/{id}/raw"),
)

_POB2_URI_RE = re.compile(r"^pob2:[/\\]*([a-z0-9]+)[/\\]+([A-Za-z0-9._-]+)$", re.I)
_URI_DOWNLOAD_TEMPLATES: dict[str, tuple[str, str]] = {
    "pobbin": ("pobbin", "https://pobb.in/pob/{id}"),
    "poeninja": ("poeninja", "https://poe.ninja/poe2/pob/raw/{id}"),
    "poe2db": ("poe2db", "https://poe2db.tw/pob/{id}/raw"),
    "maxroll": ("maxroll", "https://maxroll.gg/poe2/api/pob/{id}"),
    "pastebin": ("pastebin", "https://pastebin.com/raw/{id}"),
    "rentry": ("rentry", "https://rentry.co/paste/{id}/raw"),
}


@dataclass(frozen=True)
class ResolvedBuildSource:
    """Normalized PoE2 build payload ready to load into BridgeWorker or save to disk."""

    xml: str
    code: str
    source_kind: str
    source_label: str
    download_url: str | None = None
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def parse_share_url(url: str) -> tuple[str, str] | None:
    """Return ``(site_kind, raw_download_url)`` for supported PoE2 share links, else None."""
    raw = (url or "").strip()
    if not raw:
        return None
    m_uri = _POB2_URI_RE.match(raw)
    if m_uri:
        site_key = m_uri.group(1).lower()
        bid = m_uri.group(2).strip("/")
        if site_key in _URI_DOWNLOAD_TEMPLATES:
            kind, tmpl = _URI_DOWNLOAD_TEMPLATES[site_key]
            return kind, tmpl.format(id=bid)
        return None
    for kind, pat, tmpl in _SITE_SPECS:
        m = pat.match(raw)
        if m:
            return kind, tmpl.format(id=m.group(1))
    return None


def _default_fetch_text(
    url: str,
    timeout: float,
    *,
    transport: httpx.BaseTransport | None = None,
    proxy: str | None = None,
) -> str:
    check_public_https_url(url)
    kwargs: dict[str, Any] = {
        "headers": _DEFAULT_HEADERS,
        "timeout": httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        "follow_redirects": False,
        "trust_env": False,
    }
    if transport is not None:
        kwargs["transport"] = transport
    else:
        eff_proxy = resolve_proxy(proxy)
        if eff_proxy:
            kwargs["proxy"] = eff_proxy
    try:
        with httpx.Client(**kwargs) as client:
            resp = get_public_https(client, url, max_bytes=MAX_SHARE_XML_BYTES)
    except DaemonRequestError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        raise DaemonRequestError(
            f"Failed fetching PoE2 build URL `{redact_secrets(url)}`: {redact_secrets(str(exc))}"
        ) from exc
    if resp.status_code in (403, 429, 503) or resp.headers.get("cf-mitigated"):
        raise DaemonRequestError(
            f"[POE2-FETCH-BLOCKED] PoE2 build URL `{redact_secrets(url)}` returned HTTP {resp.status_code} "
            "(anti-bot / Cloudflare challenge). Open the link in a browser, export the PoB2 share code, "
            "and pass the code or XML file to `pob2 load`."
        )
    if resp.status_code != 200:
        raise DaemonRequestError(
            f"[POE2-FETCH-HTTP-ERROR] PoE2 build URL `{redact_secrets(url)}` returned HTTP {resp.status_code}."
        )
    return resp.text


def _looks_like_html_page(text: str) -> bool:
    head = text[:512].lstrip().lower()
    return (
        head.startswith(("<!doctype html", "<html", "<!--"))
        or "<title>just a moment" in head
        or "cf-browser-verification" in head
        or ("<head>" in head and "<body>" in head)
    )


def _extract_code_or_xml_from_body(
    body: str,
    site_kind: str,
    *,
    source_hint: str | None = None,
) -> tuple[str, str]:
    text = (body or "").strip()
    if not text:
        raise DaemonRequestError(f"Empty response body from `{site_kind}` build link.")
    if len(text.encode("utf-8", errors="replace")) > MAX_SHARE_XML_BYTES:
        raise DaemonRequestError(
            f"Build XML from `{site_kind}` exceeds {MAX_SHARE_XML_BYTES // (1024 * 1024)} MiB limit."
        )
    if _looks_like_html_page(text):
        raise DaemonRequestError(
            f"[POE2-FETCH-BLOCKED] `{site_kind}` returned an HTML page instead of a PoB2 build code "
            "(anti-bot / Cloudflare challenge or invalid link). Export the PoB2 share code manually and pass it to `pob2 load`."
        )
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            for key in ("pobCode", "code", "buildCode", "data", "xml"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    text = val.strip()
                    break
    eff_hint = source_hint or site_kind
    if text.lstrip().startswith("<"):
        validate_build_xml(text, expected_game="poe2", source_hint=eff_hint)
        validate_poe2_spec_class_ids(text)
        return text, encode_xml_share_code(text)
    try:
        decoded = share_code_to_xml(text)
    except ShareCodeLimitError as exc:
        raise DaemonRequestError(str(exc)) from exc
    except ValueError as exc:
        raise DaemonRequestError(
            f"Response from `{site_kind}` did not contain a valid PoB2 share code or XML ({exc})."
        ) from exc
    if "<PathOfBuilding" not in decoded:
        raise DaemonRequestError(f"Response from `{site_kind}` did not contain a valid PoB2 share code or XML.")
    validate_build_xml(decoded, expected_game="poe2", source_hint=eff_hint)
    validate_poe2_spec_class_ids(decoded)
    return decoded, text


def resolve_poe2_build_source(
    source: str | None = None,
    *,
    xml_code: str | None = None,
    data_dir: Path | None = None,
    timeout: float = 15.0,
    fetcher: TextFetcher | None = None,
    transport: httpx.BaseTransport | None = None,
    proxy: str | None = None,
) -> ResolvedBuildSource:
    """Resolve any PoE2 build source (share URL, WeGame link, local file, XML, or code)."""
    raw = (xml_code or source or "").strip()
    if not raw:
        raise DaemonRequestError("Provide a PoE2 share URL, WeGame link, XML file path, or PoB2 share code.")

    if "://" in raw or raw.lower().startswith(("pob:", "pob2:", "www.", "poe", "maxroll.", "pobb.")):
        validate_url(raw, expected_game="poe2", cli_subcommand="import")

    # 1. CN WeGame cookie-free share link
    if is_wegame_share_url(raw):
        parse_wegame_share_url(raw)
        bundle = fetch_wegame_share(raw, timeout=timeout)
        res = convert_wegame_bundle_to_pob2_xml(bundle, data_dir=data_dir)
        validate_build_xml(res.xml, expected_game="poe2", source_hint=raw)
        return ResolvedBuildSource(
            xml=res.xml,
            code=encode_xml_share_code(res.xml),
            source_kind="wegame_url",
            source_label=raw,
            download_url=raw,
            warnings=list(res.warnings),
            stats=dict(res.stats),
            summary=res.to_summary_dict(),
        )

    # 2. Supported remote PoE2 build share URLs & generic public-HTTPS share links (anti-SSRF guarded)
    parsed_site = parse_share_url(raw)
    if parsed_site is not None:
        site_kind, dl_url = parsed_site
        check_public_https_url(dl_url)
        body = fetcher(dl_url, timeout) if fetcher is not None else _default_fetch_text(
            dl_url, timeout, transport=transport, proxy=proxy
        )
        xml_text, code_str = _extract_code_or_xml_from_body(body, site_kind, source_hint=raw)
        return ResolvedBuildSource(
            xml=xml_text,
            code=code_str,
            source_kind=f"{site_kind}_url",
            source_label=raw,
            download_url=dl_url,
        )

    if (
        not raw.startswith("<")
        and "\n" not in raw
        and not _WIN_DRIVE_PREFIX_RE.match(raw)
        and ("//" in raw or (_URL_SCHEME_RE.match(raw) and not raw.lower().startswith("pob2:")))
    ):
        check_public_https_url(raw)
        body = fetcher(raw, timeout) if fetcher is not None else _default_fetch_text(
            raw, timeout, transport=transport, proxy=proxy
        )
        xml_text, code_str = _extract_code_or_xml_from_body(body, "https_share", source_hint=raw)
        return ResolvedBuildSource(
            xml=xml_text,
            code=code_str,
            source_kind="https_share_url",
            source_label=raw,
            download_url=raw,
        )

    # 3. Local file (XML or sanitized WeGame JSON bundle)
    if not raw.startswith("<") and len(raw) < 260 and "\n" not in raw:
        cand = Path(raw)
        if cand.is_file():
            if cand.stat().st_size > MAX_SHARE_XML_BYTES:
                raise DaemonRequestError(
                    f"Build file `{cand.name}` exceeds {MAX_SHARE_XML_BYTES // (1024 * 1024)} MiB limit."
                )
            content = cand.read_text(encoding="utf-8")
            if cand.suffix.lower() == ".json" or content.lstrip().startswith("{"):
                try:
                    bundle = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise DaemonRequestError(f"Invalid JSON bundle `{cand}`: {exc}") from exc
                res = convert_wegame_bundle_to_pob2_xml(bundle, data_dir=data_dir)
                validate_build_xml(res.xml, expected_game="poe2", source_hint=str(cand))
                validate_poe2_spec_class_ids(res.xml)
                return ResolvedBuildSource(
                    xml=res.xml,
                    code=encode_xml_share_code(res.xml),
                    source_kind="wegame_bundle",
                    source_label=cand.name,
                    warnings=list(res.warnings),
                    stats=dict(res.stats),
                    summary=res.to_summary_dict(),
                )
            xml_text, code_str = _extract_code_or_xml_from_body(content, cand.name)
            return ResolvedBuildSource(
                xml=xml_text,
                code=code_str,
                source_kind="pob2_xml_file",
                source_label=cand.name,
            )
        if cand.is_dir():
            raise DaemonRequestError(f"Build file not found (path is a directory): {raw}")

    # 4. Inline PoB2 XML or base64url zlib/gzip share code
    if raw.lstrip().startswith("<"):
        if len(raw.encode("utf-8", errors="replace")) > MAX_SHARE_XML_BYTES:
            raise DaemonRequestError(
                f"Inline build XML exceeds {MAX_SHARE_XML_BYTES // (1024 * 1024)} MiB limit."
            )
        validate_build_xml(raw, expected_game="poe2")
        validate_poe2_spec_class_ids(raw)
        return ResolvedBuildSource(
            xml=raw,
            code=encode_xml_share_code(raw),
            source_kind="pob2_xml",
            source_label="inline_xml",
        )
    try:
        decoded = share_code_to_xml(raw)
    except ShareCodeLimitError as exc:
        raise DaemonRequestError(str(exc)) from exc
    except ValueError:
        decoded = None
    if decoded is not None and "<PathOfBuilding" in decoded:
        validate_build_xml(decoded, expected_game="poe2")
        validate_poe2_spec_class_ids(decoded)
        return ResolvedBuildSource(
            xml=decoded,
            code=raw,
            source_kind="pob2_code",
            source_label="inline_code",
        )

    if looks_like_local_file_path(raw):
        raise DaemonRequestError(f"Build file not found: {raw}")

    raise DaemonRequestError(
        "Unsupported PoE2 build source. Provide a PoB2 code, local XML/WeGame JSON file, "
        "`pobb.in`, `poe2db.tw/pob`, `poe.ninja/poe2/pob`, `maxroll.gg/poe2/pob`, or WeGame share URL."
    )


def load_source_label(source: str | None, _character: Any = None) -> str:
    s = (source or "").strip()
    if not s:
        return "inline_build"
    if s.startswith("<") or len(s) > 200:
        return "inline_build"
    return s


def load_build_into_worker(
    worker: Any,
    *,
    source: str | None = None,
    xml_code: str | None = None,
    char_name: str | None = None,
    league: str | None = None,
    realm: str | None = None,
    auto_fix: bool = True,
    fetcher: TextFetcher | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Resolve a PoE2 build source, import it into ``worker``, run #2401/#2536 fixups, and return notes."""
    from .post_load_fixups import apply_post_load_fixups

    _ = (char_name, league, realm)
    data_dir = getattr(worker, "data_dir", None)
    resolved = resolve_poe2_build_source(
        source=source,
        xml_code=xml_code,
        data_dir=data_dir,
        fetcher=fetcher,
        transport=transport,
    )
    requested_nodes = extract_active_spec_node_ids(resolved.xml)
    imp_res = worker.call("import_code", {"code": resolved.code, "mode": "replace"})
    worker.has_build = True
    fixups = apply_post_load_fixups(worker, auto_fix=auto_fix)
    info = dict(worker.call("get_build_info", {}) or {})
    if isinstance(info.get("points"), dict) and isinstance(info["points"].get("display"), str):
        pts_clean = dict(info["points"])
        pts_clean["display"] = re.sub(r"\^(?:x[0-9A-Fa-f]{6}|\d)", "", pts_clean["display"]).strip()
        info["points"] = pts_clean

    out: dict[str, Any] = {
        "game": "poe2",
        "loaded": True,
        "sourceKind": resolved.source_kind,
        "sourceLabel": resolved.source_label,
        "buildInfo": info,
        "postLoadFixups": fixups["fixups"],
        "notes": fixups["notes"],
    }
    load_warnings: list[str] = []
    if requested_nodes:
        try:
            ts = worker.call("get_tree_state", {}) or {}
            loaded_nodes = {int(n) for n in (ts.get("allocatedNodes") or []) if isinstance(n, int)}
            dropped_ids = sorted(requested_nodes - loaded_nodes)
            if dropped_ids:
                out["disconnectedNodesDropped"] = len(dropped_ids)
                out["requestedNodeCount"] = len(requested_nodes)
                out["loadedNodeCount"] = len(loaded_nodes)
                out["droppedNodeIds"] = dropped_ids[:20]
                load_warnings.append(
                    f"[tree-disconnected] {len(dropped_ids)} passive node(s) from input XML were dropped on load "
                    f"({len(requested_nodes)} requested -> {len(loaded_nodes)} allocated; disconnected from class start "
                    "or invalid for active treeVersion)."
                )
        except (RuntimeError, OSError, ValueError, KeyError):
            pass
    if resolved.warnings:
        out["importWarnings"] = list(resolved.warnings)
    if fixups["warnings"]:
        out["fixupWarnings"] = list(fixups["warnings"])
    all_warnings = [*load_warnings, *list(resolved.warnings), *list(fixups["warnings"])]
    if all_warnings:
        out["warnings"] = all_warnings
    if isinstance(imp_res, dict):
        for k in ("droppedItems", "engineWarnings", "mainOutputShield"):
            if imp_res.get(k):
                out[k] = imp_res[k]
    return out


__all__ = [
    "ResolvedBuildSource",
    "TextFetcher",
    "extract_active_spec_node_ids",
    "load_build_into_worker",
    "load_source_label",
    "looks_like_local_file_path",
    "parse_share_url",
    "resolve_poe2_build_source",
]
