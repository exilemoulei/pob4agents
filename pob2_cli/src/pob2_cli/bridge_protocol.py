"""Headless PoB2 bridge protocol constants, PoE2 blocklist, transport helpers, and #2529/#2582 shield parsers."""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import CrossGameHallucinationError, DaemonRequestError
from .json_io import read_json_dict as read_slot_state  # noqa: F401
from .share_codec import (
    MAX_SHARE_XML_BYTES,
    ShareCodeLimitError,
    share_code_to_xml,
    xml_to_share_code,
)

MAX_UNDO_STEPS = 30

# PoE2 hard RPC blocklist (no PoE1 timeless tj_*, no tattoos, no tree links, no direct disk writes)
FORBIDDEN_METHODS: dict[str, str] = {
    "tj_open": "PoE2 has no PoE1 timeless-jewel seed LUT; tj_open fails under PoB2.",
    "tj_close": "PoE2 has no PoE1 timeless-jewel seed LUT; tj_close fails under PoB2.",
    "tattoo_open": "PoE2 has no PoE1 tattoos; use attribute overrides instead.",
    "import_tree_url": "PoB2 bridge does not support PoE1 tree URL imports.",
    "export_tree_url": "PoB2 bridge does not support PoE1 tree URL exports.",
    "config_mod_search": "configModBrowser is unavailable headless; use list_config/set_config/set_custom_mods.",
    "config_mod_add": "configModBrowser is unavailable headless; use list_config/set_config/set_custom_mods.",
    "set_pob_options": "Writing Settings.xml at runtime is forbidden across headless workers.",
    "oauth_logout": "Writing Settings.xml at runtime is forbidden across headless workers.",
    "import_from_uri": "import_from_uri is blocked; fetch externally and use import_code.",
    "save_build_as": "Direct disk writes inside the Lua engine are forbidden; use save_xml.",
}


class BridgeError(RuntimeError):
    """Raised when the headless PoB2 bridge returns an error or violates a contract."""


class BridgeTimeoutError(BridgeError):
    """Raised when an RPC call exceeds its deadline and the worker is recycled."""


class BridgeRejectedError(BridgeError):
    """Engine answered the RPC with an error while staying healthy."""

    def __init__(self, message: str, *, code: str | None = None, kind: str = "daemon") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


ENGINE_ERROR_KINDS: tuple[tuple[str, str, str], ...] = (
    ("bad_request", "", "request"),
    ("lua_error", "empty code", "request"),
    ("lua_error", "code too large", "request"),
    ("lua_error", "not a Path of Building code", "request"),
    ("lua_error", "code did not decode to a build", "request"),
    ("lua_error", "code is not a build file for this game", "request"),
)


def decode_share_code_xml(code: str) -> str | None:
    """Decode a PoB2 URL-safe Base64 zlib/gzip share code with a 64 MiB cap (or pass through raw XML)."""
    text = (code or "").strip()
    if not text:
        return None
    if text.startswith("<"):
        if len(text.encode("utf-8", errors="replace")) > MAX_SHARE_XML_BYTES:
            return None
        return text
    try:
        return share_code_to_xml(text)
    except ValueError:
        return None


def encode_xml_share_code(xml_text: str) -> str:
    """Encode PoB2 XML into a URL-safe Base64 zlib share code."""
    return xml_to_share_code(xml_text)


def validate_poe2_xml_root(code_or_xml: str) -> None:
    """Reject PoE1 ``<PathOfBuilding>`` XML or >64 MiB share-code bombs before touching the PoB2 engine."""
    raw = (code_or_xml or "").strip()
    if not raw:
        return
    if raw.startswith("<"):
        if len(raw.encode("utf-8", errors="replace")) > MAX_SHARE_XML_BYTES:
            raise DaemonRequestError(
                f"Build XML exceeds {MAX_SHARE_XML_BYTES // (1024 * 1024)} MiB decompression limit."
            )
        xml_text: str | None = raw
    else:
        try:
            xml_text = share_code_to_xml(raw)
        except ShareCodeLimitError as exc:
            raise DaemonRequestError(str(exc)) from exc
        except ValueError:
            xml_text = None
    if not xml_text:
        return
    head = xml_text.lstrip()[:256]
    if "<PathOfBuilding>" in head or ("<PathOfBuilding " in head and "<PathOfBuilding2" not in head):
        raise CrossGameHallucinationError(
            game="poe2",
            detected_game="poe1",
            trigger="<PathOfBuilding>",
            error="Received a Path of Exile 1 build XML in `pob2`.",
            remediation="Use `pob` (PoE1) for `<PathOfBuilding>` builds; `pob2` requires `<PathOfBuilding2>`.",
            equivalent_command="pob load <source>",
            rule_id="POE2-XML-ROOT",
        )


def validate_rpc_method(method: str, _caps: dict[str, Any] | None = None) -> None:
    if method in FORBIDDEN_METHODS:
        raise BridgeError(FORBIDDEN_METHODS[method])
    if method.startswith("compare_") or method.endswith("_tree_url"):
        raise BridgeError(f"Blocked PoB2 RPC method: {method}")


def format_rpc_error(err_obj: Any) -> str:
    if isinstance(err_obj, dict):
        return err_obj.get("message") or json.dumps(err_obj, ensure_ascii=False)
    return str(err_obj or "unknown bridge error")


def rpc_rejection(method: str, err_obj: Any) -> BridgeRejectedError:
    code = err_obj.get("code") if isinstance(err_obj, dict) else None
    code = code if isinstance(code, str) else None
    text = format_rpc_error(err_obj)
    first = text.split("\n", 1)[0].strip()
    kind = next((k for c, start, k in ENGINE_ERROR_KINDS if c == code and first.startswith(start)), "daemon")
    return BridgeRejectedError(f"RPC '{method}' failed: {first if kind == 'request' else text}", code=code, kind=kind)


def extract_xml_item_bases(xml_text: str) -> list[dict[str, Any]]:
    """Parse ``<Item>`` blocks and equipped slots from PoB2 XML for #2582 missing-base detection."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items_node = root.find("Items")
    if items_node is None:
        return []
    slot_refs: dict[int, list[str]] = {}
    for slot_el in items_node.iter("Slot"):
        sname = (slot_el.attrib.get("name") or "").strip()
        try:
            iid = int(slot_el.attrib.get("itemId") or 0)
        except ValueError:
            iid = 0
        if sname and iid > 0:
            slot_refs.setdefault(iid, [])
            if sname not in slot_refs[iid]:
                slot_refs[iid].append(sname)

    parsed: list[dict[str, Any]] = []
    for item_el in items_node.findall("Item"):
        try:
            iid = int(item_el.attrib.get("id") or 0)
        except ValueError:
            continue
        raw_lines = [ln.strip() for ln in (item_el.text or "").splitlines() if ln.strip()]
        rarity = "Normal"
        header_lines: list[str] = []
        for ln in raw_lines:
            if ln.startswith("Rarity:"):
                rarity = ln.split(":", 1)[1].strip()
                continue
            if ln.startswith(("Item Class:", "Unique ID:", "Item Level:", "Quality:", "Sockets:", "--------")):
                if ln.startswith("--------"):
                    break
                continue
            header_lines.append(ln)
            if len(header_lines) >= 2:
                break
        if rarity.upper() in ("RARE", "UNIQUE") and len(header_lines) >= 2:
            title, base_name = header_lines[0], header_lines[1]
        elif header_lines:
            title, base_name = "", header_lines[0]
        else:
            title, base_name = "", ""
        slots = slot_refs.get(iid, [])
        parsed.append(
            {
                "itemId": iid,
                "rarity": rarity,
                "title": title,
                "baseName": base_name,
                "slots": slots,
                "isWeaponSlot": any(s.startswith("Weapon") for s in slots),
            }
        )
    return parsed


def detect_dropped_items_against_loaded(
    xml_text: str,
    loaded_item_ids: set[int],
) -> list[dict[str, Any]]:
    """Return structured #2582 alerts for XML items silently dropped by PoB2 because of missing bases."""
    alerts: list[dict[str, Any]] = []
    for rec in extract_xml_item_bases(xml_text):
        if rec["itemId"] not in loaded_item_ids:
            is_wpn = bool(rec["isWeaponSlot"])
            alerts.append(
                {
                    "issue": "#2582",
                    "code": "missing_item_base_dropped",
                    "severity": "critical" if is_wpn else "warning",
                    "itemId": rec["itemId"],
                    "rarity": rec["rarity"],
                    "title": rec["title"],
                    "missingBase": rec["baseName"],
                    "affectedSlots": rec["slots"],
                    "isWeaponSlot": is_wpn,
                    "message": (
                        f"Item #{rec['itemId']} (base={rec['baseName']!r}, slots={rec['slots']}) "
                        "was silently dropped by PoB2 because its base type is missing from the 0.5.5 item table."
                    ),
                }
            )
    return alerts


def write_slot_state(
    state_path: Path,
    *,
    slot_name: str,
    last_good_code: str | None,
    undo_stack: list[str],
    redo_stack: list[str],
    source_label: str | None,
    rev: int,
) -> bool:
    """Persist slot build code and undo/redo stacks to disk; False (with stderr warning) on failure."""
    tmp_path = state_path.with_suffix(f".{os.getpid()}.tmp")
    try:
        state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "game": "poe2",
            "slot": slot_name,
            "last_good_code": last_good_code,
            "undo_stack": undo_stack[-MAX_UNDO_STEPS:],
            "redo_stack": redo_stack[-MAX_UNDO_STEPS:],
            "source_label": source_label,
            "rev": int(rev),
            "updated_at": time.time(),
        }
        tmp_path.unlink(missing_ok=True)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload))
        os.replace(tmp_path, state_path)
        return True
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            sys.stderr.write(f"[pob2] WARN: could not save slot '{slot_name}' to {state_path}: {exc}\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        return False


def stdout_reader_loop(
    proc: subprocess.Popen[str],
    out_queue: queue.Queue[dict[str, Any] | Exception | None],
) -> None:
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out_queue.put(obj)
    except Exception as exc:
        out_queue.put(exc)
    finally:
        out_queue.put(None)


def stderr_drain_loop(
    proc: subprocess.Popen[str],
    stderr_tail: collections.deque[str],
) -> None:
    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            s = line.rstrip("\r\n")
            if s:
                stderr_tail.append(s)
    except (OSError, ValueError):
        pass


def await_startup_handshake(
    out_queue: queue.Queue[dict[str, Any] | Exception | None],
    stderr_tail: collections.deque[str],
    on_abort: Callable[[], None],
    *,
    timeout: float = 25.0,
) -> tuple[dict[str, Any], str | None, str | None]:
    gate_seen = False
    hello_seen = False
    hello_info: dict[str, Any] = {}
    pob_version: str | None = None
    bridge_version: str | None = None
    deadline = time.monotonic() + timeout
    while not (gate_seen and hello_seen):
        rem = deadline - time.monotonic()
        if rem <= 0:
            on_abort()
            raise BridgeTimeoutError(f"Timed out waiting for PoB2 handshake (stderr: {list(stderr_tail)[-5:]})")
        try:
            msg = out_queue.get(timeout=rem)
        except queue.Empty as exc:
            on_abort()
            raise BridgeTimeoutError("Timed out waiting for PoB2 startup events") from exc
        if msg is None or isinstance(msg, Exception):
            on_abort()
            raise BridgeError(f"PoB2 engine exited during boot: {msg} (stderr: {list(stderr_tail)[-8:]})")
        event = msg.get("event")
        data = msg.get("data") or {}
        if event == "gate_result":
            if not data.get("ok"):
                on_abort()
                raise BridgeError(f"PoB2 bridge gate_result failed: {data.get('failed')}")
            gate_seen = True
        elif event == "hello":
            hello_info = dict(data)
            pob_version = data.get("pobVersion")
            bridge_version = data.get("bridge")
            hello_seen = True
    return hello_info, pob_version, bridge_version


def sync_worker_state(worker: Any, method: str, params: dict[str, Any] | None, result: dict[str, Any]) -> None:
    if method in ("set_translate", "set_locale"):
        worker._tree_data_cache.clear()
    if method == "set_translate":
        if isinstance(result.get("enabled"), bool):
            worker.translate_enabled = result["enabled"]
        elif isinstance(result.get("translate"), bool):
            worker.translate_enabled = result["translate"]
        elif params is not None and "enabled" in params:
            worker.translate_enabled = bool(params["enabled"])
    elif method == "set_locale":
        if isinstance(result.get("lang"), str):
            worker.locale = result["lang"]
        if isinstance(result.get("translate"), bool):
            worker.translate_enabled = result["translate"]
    elif method == "version" and isinstance(result.get("translate"), bool):
        worker.translate_enabled = result["translate"]


def collect_batch_responses(
    out_queue: queue.Queue[dict[str, Any] | Exception | None],
    id_to_index: dict[int, int],
    expected: int,
    timeout: float,
    on_crash: Callable[[], None],
) -> dict[int, dict[str, Any]]:
    raw_responses: dict[int, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout
    while len(raw_responses) < expected:
        rem = deadline - time.monotonic()
        if rem <= 0:
            on_crash()
            raise BridgeTimeoutError(f"PoB2 batch RPC timed out after {timeout:.1f}s")
        try:
            msg = out_queue.get(timeout=rem)
        except queue.Empty as exc:
            on_crash()
            raise BridgeTimeoutError(f"PoB2 batch RPC timed out after {timeout:.1f}s") from exc
        if msg is None or isinstance(msg, Exception):
            on_crash()
            raise BridgeError(f"PoB2 engine crashed during batch RPC: {msg}")
        msg_id = msg.get("id")
        if msg_id in id_to_index:
            raw_responses[id_to_index[msg_id]] = msg
    return raw_responses
