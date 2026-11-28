# SPDX-License-Identifier: GPL-3.0-only
"""Cookie-free CN WeGame PoE2 share-link validator, concurrent fetcher, and PII sanitizer (L1).

Protocol reference: ackness/pobr (MIT) `web/public/_worker.js`.
Security & privacy contract:
  - Strictly validates `https://www.wegame.com.cn/helper/poe2/#/share/<16..256>`.
  - Zero cookies, zero redirects (`follow_redirects=False`), 4 MiB per-response cap.
  - All-or-nothing across all 5 `wegame.pallas.poe2.Profile/*` POST endpoints.
  - Immediately strips PII (`openid`, `account_name`, `role_id`, `name`, `nick_name`, `share_code`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..errors import DaemonRequestError
from ..guardrails import redact_secrets, validate_url
from .url_guard import check_public_https_url

WEGAME_API_BASE = "https://www.wegame.com.cn/api/v1/wegame.pallas.poe2.Profile/"
WEGAME_METHODS: tuple[str, ...] = (
    "GetRoleInfo",
    "GetEquipments",
    "GetTalentTree",
    "GetJewels",
    "GetSkills",
)
WEGAME_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SHARE_FRAGMENT_RE = re.compile(r"^/share/([A-Za-z0-9_-]{16,256})/?$")

# Strict calculation-only field whitelist for equipment & skill gem objects.
_ITEM_ALLOWED_KEYS: tuple[str, ...] = (
    "baseType",
    "typeLine",
    "name",
    "frameType",
    "ilvl",
    "inventoryId",
    "x",
    "properties",
    "requirements",
    "implicitMods",
    "explicitMods",
    "runeMods",
    "enchantMods",
    "craftedMods",
    "fracturedMods",
    "desecratedMods",
    "mutatedMods",
    "corrupted",
    "support",
    "socket",
    "sockets",
)

# Explicit PII fields that must never survive sanitization.
PII_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "openid",
        "account_name",
        "accountName",
        "role_id",
        "roleId",
        "nick_name",
        "nickName",
        "share_code",
        "shareCode",
        "uin",
        "qq",
        "avatar",
        "guild",
    }
)


def is_wegame_share_url(text: str) -> bool:
    """Return True if *text* targets WeGame PoE2 helper share links."""
    s = (text or "").strip().lower()
    return "wegame.com.cn/helper/poe2" in s


def parse_wegame_share_url(url: str) -> str:
    """Validate a CN WeGame PoE2 share URL and extract its 16..256-char share code.

    Calls the PoE1/PoE2 URL gate first so PoE1 links raise ``CrossGameHallucinationError`` (exit 1).
    """
    raw = (url or "").strip()
    validate_url(raw, expected_game="poe2", cli_subcommand="import")

    parsed = urlsplit(raw)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "www.wegame.com.cn"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/helper/poe2/"
    ):
        raise DaemonRequestError(
            "Invalid WeGame PoE2 share URL: expected "
            "`https://www.wegame.com.cn/helper/poe2/#/share/<share_code>` "
            "(no custom port or credentials)."
        )
    match = _SHARE_FRAGMENT_RE.match(parsed.fragment)
    if not match:
        raise DaemonRequestError(
            "Invalid WeGame PoE2 share code: fragment must match `#/share/<16..256 chars [A-Za-z0-9_-]>`."
        )
    return match.group(1)


def sanitize_item_fields(item: Any, *, depth: int = 0) -> dict[str, Any]:
    """Keep only calculation fields on equipment/gem items, recursively stripping PII."""
    if not isinstance(item, dict) or depth > 8:
        raise DaemonRequestError("Invalid WeGame item payload structure.")
    out: dict[str, Any] = {k: item[k] for k in _ITEM_ALLOWED_KEYS if k in item}
    socketed = item.get("socketedItems")
    if isinstance(socketed, list):
        out["socketedItems"] = [sanitize_item_fields(child, depth=depth + 1) for child in socketed]
    return out


def sanitize_talent_tree(tree: Any) -> dict[str, Any]:
    """Keep calculation-only passive tree fields, separating radius metadata from GetJewels."""
    if not isinstance(tree, dict):
        raise DaemonRequestError("Invalid WeGame talent_tree payload.")
    hashes = tree.get("hashes")
    specialisations = tree.get("specialisations")
    skill_overrides = tree.get("skill_overrides")
    quest_stats = tree.get("quest_stats")
    sanitized: dict[str, Any] = {
        "hashes": list(hashes) if isinstance(hashes, list) else [],
        "specialisations": dict(specialisations) if isinstance(specialisations, dict) else {},
        "skill_overrides": dict(skill_overrides) if isinstance(skill_overrides, dict) else {},
        "quest_stats": list(quest_stats) if isinstance(quest_stats, list) else [],
    }
    # Preserve tree jewel radius metadata under a distinct key so it is never confused
    # with top-level GetJewels.jewel_data (JSON string of socketed jewels).
    radius_meta = tree.get("jewel_data")
    if isinstance(radius_meta, dict):
        sanitized["jewel_radius_meta"] = radius_meta
    return sanitized


def sanitize_wegame_responses(responses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validate all 5 raw WeGame endpoint payloads and return a PII-free v1 share bundle."""
    for method in WEGAME_METHODS:
        data = responses.get(method)
        if not isinstance(data, dict):
            raise DaemonRequestError(f"WeGame {method} returned non-object payload.")
        res = data.get("result")
        err_code = res.get("error_code") if isinstance(res, dict) else None
        if err_code != 0:
            err_msg = res.get("error_message", "") if isinstance(res, dict) else ""
            detail = f" ({err_msg})" if err_msg else ""
            raise DaemonRequestError(
                f"WeGame {method} failed with error_code={err_code!r}{detail}; "
                "share link may have expired or been made private (aborting without partial build)."
            )

    role_obj = responses["GetRoleInfo"].get("role")
    equip_list = responses["GetEquipments"].get("equipments")
    tree_obj = responses["GetTalentTree"].get("talent_tree")
    jewel_str = responses["GetJewels"].get("jewel_data")
    skill_list = responses["GetSkills"].get("skills")

    if (
        not isinstance(role_obj, dict)
        or not isinstance(equip_list, list)
        or not isinstance(tree_obj, dict)
        or not isinstance(jewel_str, str)
        or not isinstance(skill_list, list)
    ):
        raise DaemonRequestError(
            "Incomplete WeGame share response across the 5 Profile endpoints (aborting without partial build)."
        )

    level = role_obj.get("level")
    class_name = role_obj.get("class_name")
    if not isinstance(level, int) or isinstance(level, bool) or not (1 <= level <= 100):
        raise DaemonRequestError(f"Invalid WeGame character level: {level!r}.")
    if not isinstance(class_name, str) or not class_name.strip():
        raise DaemonRequestError("WeGame GetRoleInfo is missing role.class_name.")

    # Deliberately drop role.{openid, account_name, role_id, name}, top-level nick_name, share_code.
    return {
        "format": "wegame",
        "version": 1,
        "role": {"level": level, "class_name": class_name.strip()},
        "equipments": [sanitize_item_fields(item) for item in equip_list],
        "talent_tree": sanitize_talent_tree(tree_obj),
        "jewel_data": jewel_str,
        "skills": [sanitize_item_fields(skill) for skill in skill_list],
    }


def _read_bounded_json(response: httpx.Response, method: str) -> dict[str, Any]:
    """Read HTTP response up to 4 MiB and decode JSON."""
    if response.status_code != 200:
        raise DaemonRequestError(
            f"WeGame {method} returned HTTP {response.status_code} (redirects and non-200 responses are rejected)."
        )
    buf = bytearray()
    for chunk in response.iter_bytes():
        buf.extend(chunk)
        if len(buf) > WEGAME_MAX_RESPONSE_BYTES:
            raise DaemonRequestError(f"WeGame {method} response exceeded 4 MiB limit.")
    try:
        parsed = json.loads(buf.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DaemonRequestError(f"WeGame {method} returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DaemonRequestError(f"WeGame {method} returned non-object JSON.")
    return parsed


def _default_post_endpoint(
    method: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Issue a single cookie-free, redirect-disabled POST request to WeGame Profile API."""
    url = WEGAME_API_BASE + method
    check_public_https_url(url)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        ) as client:
            # Explicit empty cookie jar guarantees zero Cookie headers.
            client.cookies.clear()
            with client.stream("POST", url, json=payload, headers=headers) as resp:
                return _read_bounded_json(resp, method)
    except DaemonRequestError:
        raise
    except httpx.TimeoutException as exc:
        raise DaemonRequestError(f"WeGame {method} timed out after {timeout:.1f}s.") from exc
    except httpx.HTTPError as exc:
        raise DaemonRequestError(f"WeGame {method} network error: {redact_secrets(str(exc))}") from exc


EndpointPoster = Callable[[str, dict[str, Any]], dict[str, Any]]


def fetch_wegame_share(
    url: str,
    *,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
    poster: EndpointPoster | None = None,
) -> dict[str, Any]:
    """Concurrently fetch all 5 WeGame endpoints without cookies and return a PII-free bundle.

    Raises ``CrossGameHallucinationError`` for PoE1 URLs and ``DaemonRequestError`` on any
    validation, HTTP, redirect, timeout, or partial endpoint failure.
    """
    share_code = parse_wegame_share_url(url)
    body = {"share_code": share_code, "area": 0, "from_src": "poe2_helper"}

    def _call(method: str) -> tuple[str, dict[str, Any]]:
        if poster is not None:
            return method, poster(method, dict(body))
        return method, _default_post_endpoint(method, body, timeout=timeout, transport=transport)

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(WEGAME_METHODS)) as pool:
        futures = [pool.submit(_call, m) for m in WEGAME_METHODS]
        errors: list[Exception] = []
        for fut in futures:
            try:
                method, data = fut.result()
                results[method] = data
            except Exception as exc:
                errors.append(exc)
        if errors:
            first = errors[0]
            if isinstance(first, DaemonRequestError):
                raise first
            raise DaemonRequestError(f"WeGame fetch failed: {first}") from first

    return sanitize_wegame_responses(results)


__all__ = [
    "PII_FIELD_NAMES",
    "WEGAME_API_BASE",
    "WEGAME_MAX_RESPONSE_BYTES",
    "WEGAME_METHODS",
    "EndpointPoster",
    "fetch_wegame_share",
    "is_wegame_share_url",
    "parse_wegame_share_url",
    "sanitize_item_fields",
    "sanitize_talent_tree",
    "sanitize_wegame_responses",
]
