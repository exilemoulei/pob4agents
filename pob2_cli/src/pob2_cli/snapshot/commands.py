"""Slot-level PoE2 build commands on ``BuildTransaction``: what-if/apply mutations, undo/redo, and load."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..bridge_client import BridgeWorker, validate_poe2_spec_class_ids
from ..bridge_protocol import decode_share_code_xml, encode_xml_share_code, validate_poe2_xml_root
from ..errors import DaemonRequestError
from ..game_profile import GAME_ID
from .digest import capture_digest, diff_digest
from .helpers import clean_points_dict
from .history import history_of
from .transaction import BuildScope, BuildTransaction


def _headline(digest: dict[str, Any]) -> dict[str, Any]:
    return {
        "primaryDpsKey": digest["primaryDpsKey"],
        "primaryDps": digest["primaryDps"],
        "totalEhp": digest["totalEhp"],
        "life": digest["life"],
        "spirit": digest.get("spirit", 0.0),
        "points": clean_points_dict(digest.get("buildInfo", {}).get("points")),
    }


def run_mutation(
    worker: BridgeWorker,
    mutate_fn: Callable[[BridgeWorker], Any],
    *,
    apply: bool = False,
    scope: BuildScope = BuildScope.ACTIVE_SETS,
) -> dict[str, Any]:
    """Execute ``mutate_fn(worker)`` as a what-if (CoW rolled back & verified) or committed edit."""
    with BuildTransaction(worker, scope=scope, will_commit=apply) as tx:
        digest_before = capture_digest(worker)
        tx.verify_digest = digest_before
        action_detail = mutate_fn(worker)
        digest_after = capture_digest(worker)
        diff = diff_digest(digest_before, digest_after)

        if apply:
            tx.commit()
            mode_str = "APPLIED"
        else:
            tx.rollback()
            if tx.base is not None and tx.base.kind == "cow":
                check = tx.verify(digest_before)
                if not check.get("verified"):
                    tx.notes.append(f"restore check: {check.get('detail')}")
            mode_str = "WHAT-IF (not applied; pass --apply to persist)"

        out: dict[str, Any] = {
            "game": GAME_ID,
            "applied": apply,
            "mode": mode_str,
            "detail": action_detail,
            "before": _headline(digest_before),
            "after": _headline(digest_after),
            "diff": diff,
            **tx.history.depths(),
        }
        if tx.notes:
            out["restoreFallback"] = "; ".join(tx.notes)
        return out


def _step_history(worker: BridgeWorker, *, action: str) -> dict[str, Any]:
    with worker.lock:
        history = history_of(worker)
        if not (history.undo if action == "undo" else history.redo):
            raise DaemonRequestError(f"[POE2-EMPTY-HISTORY] {action.capitalize()} stack is empty.")
        with BuildTransaction(worker, scope=BuildScope.REPLACES_BUILD, will_commit=True) as tx:
            src_stack = tx.history.undo if action == "undo" else tx.history.redo
            digest_before = capture_digest(worker)
            worker.call("import_code", {"code": src_stack[-1], "mode": "replace"})
            tx.commit_step(action)
            return {
                "game": GAME_ID,
                "action": action,
                "diff": diff_digest(digest_before, capture_digest(worker)),
                **tx.history.depths(),
            }


def undo_build(worker: BridgeWorker) -> dict[str, Any]:
    return _step_history(worker, action="undo")


def redo_build(worker: BridgeWorker) -> dict[str, Any]:
    return _step_history(worker, action="redo")


def _try_external_importer() -> Any | None:
    try:
        return importlib.import_module("..importers", __package__)
    except ImportError:
        return None


def _resolve_inline_code(source: str | None, xml_code: str | None) -> tuple[str, str]:
    raw = (xml_code or source or "").strip()
    if not raw:
        raise DaemonRequestError("load requires a PoE2 share code, XML string, or XML file path.")
    label = "inline_code"
    if not raw.startswith("<") and len(raw) < 260 and "\n" not in raw:
        p = Path(raw)
        if p.is_file():
            raw = p.read_text(encoding="utf-8").strip()
            label = p.name
        elif p.is_dir():
            raise DaemonRequestError(f"Build file not found (path is a directory): {raw}")
    validate_poe2_xml_root(raw)
    validate_poe2_spec_class_ids(raw)
    if raw.lstrip().startswith("<"):
        return encode_xml_share_code(raw), label if label != "inline_code" else "inline_xml"
    if decode_share_code_xml(raw) is None and (
        raw.lower().endswith((".xml", ".pob", ".pob2", ".txt", ".json"))
        or "\\" in raw
        or ("/" in raw and "://" not in raw)
    ):
        raise DaemonRequestError(f"Build file not found: {raw}")
    return raw, label


def load_build(
    worker: BridgeWorker,
    *,
    source: str | None = None,
    xml_code: str | None = None,
    char_name: str | None = None,
    league: str | None = None,
    realm: str | None = None,
) -> dict[str, Any]:
    """Load a PoE2 build into the slot and commit it with an empty undo/redo history."""
    ext_imp = _try_external_importer()
    with BuildTransaction(worker, scope=BuildScope.REPLACES_BUILD, will_commit=True) as tx:
        if ext_imp is not None and callable(getattr(ext_imp, "load_build_into_worker", None)):
            out = ext_imp.load_build_into_worker(
                worker, source=source, xml_code=xml_code, char_name=char_name, league=league, realm=realm
            )
            label_fn = getattr(ext_imp, "load_source_label", None)
            label = label_fn(source, out.get("character")) if callable(label_fn) else (source or "imported")
            tx.commit_load(label)
            worker.has_build = True
            return out

        code, label = _resolve_inline_code(source, xml_code)
        imp_res = worker.call("import_code", {"code": code, "mode": "replace"})
        info = dict(worker.call("get_build_info", {}) or {})
        if isinstance(info.get("points"), dict):
            info["points"] = clean_points_dict(info["points"])
        tx.commit_load(label)
        worker.has_build = True
        out_payload: dict[str, Any] = {
            "game": GAME_ID,
            "loaded": True,
            "sourceLabel": label,
            "buildInfo": info,
        }
        if isinstance(imp_res, dict):
            for k in ("droppedItems", "engineWarnings", "mainOutputShield"):
                if imp_res.get(k):
                    out_payload[k] = imp_res[k]
        return out_payload
