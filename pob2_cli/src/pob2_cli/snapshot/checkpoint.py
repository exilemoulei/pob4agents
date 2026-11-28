"""PoE2 build checkpoints: Lua CoW snapshot (``cow_snapshot``) or lossless ``export_code``."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeError
from .helpers import strip_pob_escapes


class BuildSnapshot:
    """Opaque PoE2 build checkpoint backed by Lua CoW or lossless share code."""

    __slots__ = ("fallback_code", "kind", "proc_tag", "token")

    def __init__(
        self,
        kind: str,
        token: str,
        *,
        fallback_code: str | None = None,
        proc_tag: Any = None,
    ) -> None:
        self.kind = kind
        self.token = token
        self.fallback_code = fallback_code
        self.proc_tag = proc_tag


def worker_caps(worker: Any) -> dict[str, Any]:
    caps = getattr(worker, "caps", None)
    return caps if isinstance(caps, dict) else {}


def supports_cow_snapshot(worker: Any) -> bool:
    return bool(worker_caps(worker).get("cowSnapshot"))


def supports_direct_calc(worker: Any) -> bool:
    return bool(worker_caps(worker).get("directCalc"))


def take_snapshot(
    worker: Any,
    *,
    prefer_cow: bool = True,
    fresh_fallback: bool = False,
    code: str | None = None,
) -> BuildSnapshot:
    """Take a Lua CoW snapshot when advertised, otherwise fall back to ``export_code``."""
    if prefer_cow and supports_cow_snapshot(worker):
        proc_tag = getattr(worker, "_proc", None)
        res = worker.call("cow_snapshot", {})
        snap_id = res.get("snapId") if isinstance(res, dict) else None
        if isinstance(snap_id, str) and snap_id:
            fb_code = code or (None if fresh_fallback else getattr(worker, "last_good_code", None))
            if not fb_code:
                try:
                    fb_code = str((worker.call("export_code", {}) or {}).get("code") or "") or None
                except Exception:
                    fb_code = None
            return BuildSnapshot("cow", snap_id, fallback_code=fb_code, proc_tag=proc_tag)
    return BuildSnapshot("code", code or str(worker.call("export_code", {})["code"]))


def _restart_restored(worker: Any, generation_before: Any, code: str) -> bool:
    generation = getattr(worker, "generation", None)
    return (
        generation is not None
        and generation_before is not None
        and generation != generation_before
        and getattr(worker, "restore_failed", None) is None
        and getattr(worker, "last_good_code", None) == code
    )


def fall_back_to_code(worker: Any, snap: BuildSnapshot, reason: str, *, generation_before: Any = None) -> str:
    """Re-import ``snap.fallback_code`` and convert ``snap`` into a code checkpoint."""
    if not snap.fallback_code:
        raise BridgeError(f"{reason}; no saved build code to restore from")
    drop_snapshot(worker, snap)
    if _restart_restored(worker, generation_before, snap.fallback_code):
        how = "the engine restart already restored the saved build code"
    else:
        worker.call("import_code", {"code": snap.fallback_code, "mode": "replace"})
        how = "restored by re-importing the saved build code"
    snap.kind, snap.token, snap.proc_tag = "code", snap.fallback_code, None
    return f"{reason}; {how}"


def restore_snapshot(
    worker: Any,
    snap: BuildSnapshot,
    *,
    drop: bool = False,
    rebuild: bool = True,
) -> str | None:
    """Restore a ``BuildSnapshot`` (CoW or ``import_code``), falling back cleanly on failure."""
    if snap.kind == "cow":
        cur_proc = getattr(worker, "_proc", None)
        reason = "the engine restarted since the CoW snapshot"
        generation_before = None
        if cur_proc is snap.proc_tag:
            generation_before = getattr(worker, "generation", None)
            params: dict[str, Any] = {"snapId": snap.token}
            if drop:
                params["drop"] = True
            if not rebuild:
                params["rebuild"] = False
            try:
                worker.call("cow_restore", params)
                return None
            except Exception as exc:
                if not snap.fallback_code:
                    raise
                first_line = (strip_pob_escapes(str(exc)).strip().splitlines() or [type(exc).__name__])[0]
                reason = f"CoW restore failed ({first_line[:200]})"
        return fall_back_to_code(worker, snap, reason, generation_before=generation_before)
    worker.call("import_code", {"code": snap.token, "mode": "replace"})
    return None


def drop_snapshot(worker: Any, snap: BuildSnapshot | None) -> None:
    """Best-effort release of a Lua CoW snapshot."""
    if snap is None or snap.kind != "cow" or getattr(worker, "_proc", None) is not snap.proc_tag:
        return
    try:
        worker.call("cow_drop", {"snapId": snap.token})
    except (RuntimeError, OSError):
        pass
