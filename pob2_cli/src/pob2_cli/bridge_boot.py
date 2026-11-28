"""Boot-time child environment setup and bounded build restore for PoB2 ``BridgeWorker``."""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bridge_protocol import (
    BridgeError,
    BridgeRejectedError,
    decode_share_code_xml,
    rpc_rejection,
)
from .errors import DaemonRequestError
from .game_profile import ENV_LANG, ENV_PROXY, POE2_CLASS_IDS
from .updater import get_runtime_dir, get_shared_cache_dir

_LOG = logging.getLogger(__name__)
RawCall = Callable[[str, dict[str, Any], float], dict[str, Any]]

_VALID_POE2_CLASS_ID_SET: frozenset[int] = frozenset(POE2_CLASS_IDS)
_POE2_CLASS_LABELS = "1=Witch, 2=Ranger, 6=Warrior, 7=Sorceress, 8=Huntress, 9=Mercenary, 10=Monk, 11=Druid"
_SPEC_CLASS_ID_RE = re.compile(r"<\s*Spec\b[^>]*\bclassId\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_LUA_LOC_PREFIX_RE = re.compile(r"(?:[A-Za-z]:)?[^\s:]+?\.lua:\d+:\s*")
_REQUEST_ERROR_MARKERS: tuple[str, ...] = (
    "no node ",
    "not an attribute node",
    "attribute must be",
    "mode must be",
    "no slot ",
    "no flask slot",
    "item does not fit slot",
    "item base not recognised",
    "item text too large",
    "params.",
    "no socket group",
    "no item ",
    "no item set",
    "no skill",
    "no config",
    "unknown power stat",
    "unknown method",
    "path:",
    "not a mastery",
    "no mastery effect",
    "cannot delete",
    "empty code",
    "code too large",
    "not a path of building code",
    "code did not decode",
    "code is not a build file",
    "no build is loaded",
)
_REMEDIATION_HINTS: tuple[tuple[str, str], ...] = (
    ("no node ", "Run `pob2 tree` to inspect valid passive node IDs for this tree version."),
    ("not an attribute node", "Only +5 Attribute travel nodes support `--attribute-node` / `--attribute`."),
    ("no slot ", 'Use a valid PoE2 slot name such as "Weapon 1", "Body Armour", "Helmet", "Ring 1", "Amulet", "Flask 1", or "Charm 1".'),
    ("item does not fit slot", "Ensure the item base type matches the target equipment slot."),
    ("item base not recognised", "Verify the item text uses a valid PoE2 0.5.5 base type."),
    ("no socket group", "Run `pob2 skills` to inspect valid 1-based socket group indices."),
    ("unknown power stat", "Omit `--stat` or choose a valid PoB2 power stat such as `CombinedDPS`, `FullDPS`, or `TotalEHP`."),
    ("unknown method", "Check supported PoB2 bridge RPC methods via `pob2 status`."),
)


def is_debug_enabled() -> bool:
    return os.environ.get("POB2_DEBUG", "").strip().lower() in ("1", "true", "yes")


def sanitize_bridge_error(message: str, *, debug: bool | None = None) -> str:
    """Collapse multi-line Lua stack tracebacks into a clean single-line message unless POB2_DEBUG=1."""
    raw = str(message or "").strip()
    if not raw:
        return "PoB2 engine request failed."
    if is_debug_enabled() if debug is None else debug:
        return raw
    tb_idx = raw.lower().find("stack traceback:")
    head = raw[:tb_idx].strip() if tb_idx >= 0 else raw
    first_line = " ".join(part.strip() for part in head.splitlines() if part.strip()) or "PoB2 engine request failed."
    cleaned = _LUA_LOC_PREFIX_RE.sub("", first_line).strip()
    low = cleaned.lower()
    for marker, hint in _REMEDIATION_HINTS:
        if marker in low and hint not in cleaned:
            cleaned = f"{cleaned} ({hint})"
            break
    return cleaned


def wrap_rpc_rejection(method: str, err_obj: Any) -> BridgeRejectedError:
    exc = rpc_rejection(method, err_obj)
    raw_msg = str(exc)
    low = raw_msg.lower()
    is_req = exc.kind == "request" or any(m in low for m in _REQUEST_ERROR_MARKERS)
    clean_msg = sanitize_bridge_error(raw_msg)
    return BridgeRejectedError(clean_msg, code=exc.code, kind="request" if is_req else exc.kind)


def validate_poe2_spec_class_ids(code_or_xml: str) -> None:
    """Validate `<Spec classId="...">` against PoE2's 8 valid class IDs `{1,2,6,7,8,9,10,11}` before engine import."""
    xml_text = decode_share_code_xml(code_or_xml)
    if not xml_text or "<Spec" not in xml_text:
        return
    found_ids: list[str] = []
    try:
        root = ET.fromstring(xml_text)
        for spec_el in root.iter("Spec"):
            cid_raw = spec_el.attrib.get("classId")
            if cid_raw is not None and str(cid_raw).strip() != "":
                found_ids.append(str(cid_raw).strip())
    except ET.ParseError:
        found_ids.extend(m.group(1).strip() for m in _SPEC_CLASS_ID_RE.finditer(xml_text) if m.group(1).strip())

    for cid_str in found_ids:
        try:
            cid_val = int(cid_str)
        except ValueError:
            raise DaemonRequestError(
                f"[POE2-INVALID-CLASS-ID] Invalid PoE2 passive tree classId={cid_str!r}; "
                f"valid PoE2 classIds are {_POE2_CLASS_LABELS}."
            ) from None
        if cid_val not in _VALID_POE2_CLASS_ID_SET:
            raise DaemonRequestError(
                f"[POE2-INVALID-CLASS-ID] Invalid PoE2 passive tree classId={cid_val}; "
                f"valid PoE2 classIds are {_POE2_CLASS_LABELS}."
            )


def engine_env(worker: Any, extra_env: dict[str, str], lang: str) -> dict[str, str]:
    """Build isolated environment for the headless PoB2 LuaJIT child process."""
    env = os.environ.copy()
    env.update(extra_env)
    env[ENV_LANG] = lang
    env["POB_LANG"] = lang
    try:
        env.setdefault("POB2_CACHE_DIR", str(get_shared_cache_dir(worker.runtime_dir)))
    except OSError as exc:
        _LOG.debug("Could not resolve shared cache dir for PoB2 engine env: %s", exc)
    pob2_proxy = env.get(ENV_PROXY, "").strip()
    if pob2_proxy:
        env.setdefault("HTTPS_PROXY", pob2_proxy)
        env.setdefault("HTTP_PROXY", pob2_proxy)
    return env


def aside_path(worker: Any) -> Path:
    return get_runtime_dir(worker.runtime_dir) / f"slot_{worker.slot_name}.restore_failed.txt"


def put_code_aside(worker: Any, code: str, reason: str, *, skipped: bool = False) -> None:
    code_file: str | None = None
    if worker.slot_name != "test":
        path = aside_path(worker)
        try:
            if not skipped:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(code, encoding="utf-8")
                os.replace(tmp, path)
            code_file = str(path)
        except OSError:
            code_file = None
    worker.last_good_code = None
    worker.restore_failed = {
        "error": (reason or "restore failed").splitlines()[0][:300],
        "codeFile": code_file,
        "skipped": skipped,
        "ts": time.time(),
    }


def _try_new_build(raw_call: RawCall) -> None:
    try:
        raw_call("new_build", {}, 10.0)
    except (BridgeError, OSError, RuntimeError) as exc:
        _LOG.debug("Fallback new_build after restore error failed: %s", exc)


def restore_build_on_start(raw_call: RawCall, target_code: str, build_loaded: Any) -> str | None:
    def _fresh_import() -> str | None:
        try:
            raw_call("new_build", {}, 10.0)
            raw_call("import_code", {"code": target_code, "mode": "replace"}, 20.0)
            return None
        except Exception as exc:
            _try_new_build(raw_call)
            return str(exc) or type(exc).__name__

    if build_loaded is False:
        return _fresh_import()
    try:
        raw_call("import_code", {"code": target_code, "mode": "new"}, 20.0)
        return None
    except Exception as exc:
        if "no build is loaded" in str(exc).lower():
            return _fresh_import()
        _try_new_build(raw_call)
        return str(exc) or type(exc).__name__


def restore_on_boot(worker: Any, target_code: str | None) -> None:
    def raw(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        return worker._raw_call_unlocked(method, params, timeout=timeout, recover=False)

    if not target_code:
        raw("new_build", {}, 10.0)
        return
    error = restore_build_on_start(raw, target_code, worker.boot_build_loaded)
    if error is None:
        worker.last_good_code = target_code
        worker.restore_failed = None
        return
    put_code_aside(worker, target_code, error)
    if not worker.is_running:
        worker._launch_unlocked()
        raw("new_build", {}, 10.0)
