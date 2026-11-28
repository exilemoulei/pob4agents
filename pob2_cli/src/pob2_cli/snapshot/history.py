"""A slot's committed PoE2 build, undo/redo stacks, revision, and source label."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..bridge_protocol import MAX_UNDO_STEPS, read_slot_state, write_slot_state
from ..updater import get_runtime_dir


def compact_source_label(label: Any) -> str | None:
    """Keep persisted source labels bounded and never store raw build XML or share blobs."""
    if not isinstance(label, str):
        return None
    s = label.strip()
    if not s:
        return None
    if s.startswith("<") or len(s) > 220:
        return "inline_build"
    return s


class SlotHistory:
    """Committed PoE2 build of one slot; single owner of ``slot_<slug>.json``."""

    def __init__(
        self,
        slot_name: str = "main",
        runtime_dir: Path | None = None,
        *,
        persist: bool | None = None,
    ) -> None:
        self.slot_name = slot_name
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else None
        self.persist = slot_name != "test" if persist is None else persist
        self.persist_warning: str | None = None
        self.stale: bool = False
        self.committed: str | None = None
        self.undo: list[str] = []
        self.redo: list[str] = []
        self.rev = 0
        self.source_label: str | None = None
        self.active: Any = None
        self._worker: Any = None
        self._disk_mtime_ns: int | None = None

    @property
    def path(self) -> Path:
        return get_runtime_dir(self.runtime_dir) / f"slot_{self.slot_name}.json"

    @classmethod
    def load(cls, slot_name: str, runtime_dir: Path | None = None, *, persist: bool | None = None) -> SlotHistory:
        hist = cls(slot_name, runtime_dir, persist=persist)
        data = read_slot_state(hist.path) if hist.persist else None
        if data:
            hist.committed = data.get("last_good_code") or None
            hist.undo = [c for c in data.get("undo_stack") or [] if isinstance(c, str)]
            hist.redo = [c for c in data.get("redo_stack") or [] if isinstance(c, str)]
            hist.source_label = compact_source_label(data.get("source_label"))
            raw_rev = data.get("rev")
            if isinstance(raw_rev, int) and not isinstance(raw_rev, bool) and raw_rev >= 0:
                hist.rev = raw_rev
            try:
                hist._disk_mtime_ns = hist.path.stat().st_mtime_ns
            except OSError:
                hist._disk_mtime_ns = None
        return hist

    def bind(self, worker: Any) -> None:
        self._worker = worker
        worker.slot_history = self
        worker.last_good_code = self.committed
        worker.has_build = bool(self.committed)

    def _changed(self) -> None:
        self.rev += 1
        self.stale = False
        if self._worker is not None:
            self._worker.last_good_code = self.committed
            self._worker.has_build = bool(self._worker.last_good_code)
            if getattr(self._worker, "restore_failed", None) is not None:
                self._worker.restore_failed = None
        self.save()

    def save(self) -> None:
        if not self.persist:
            return
        self.source_label = compact_source_label(self.source_label)
        ok = write_slot_state(
            self.path,
            slot_name=self.slot_name,
            last_good_code=self.committed,
            undo_stack=self.undo,
            redo_stack=self.redo,
            source_label=self.source_label,
            rev=self.rev,
        )
        if ok is False:
            self.persist_warning = (
                f"This slot's build is live in the daemon but could not be saved to {self.path}; "
                "the build and its undo history will be lost when the daemon restarts."
            )
        else:
            self.persist_warning = None
            try:
                self._disk_mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                self._disk_mtime_ns = None

    def load_build(self, code: str | None, source_label: str | None) -> None:
        self.committed = code or None
        self.source_label = compact_source_label(source_label)
        self.undo.clear()
        self.redo.clear()
        self._changed()

    def commit(self, before: str | None, after: str | None, *, undoable: bool = True) -> None:
        if undoable and before:
            self.undo.append(before)
            del self.undo[:-MAX_UNDO_STEPS]
        self.redo.clear()
        self.committed = after or None
        self._changed()

    def step(self, action: str, current: str) -> str:
        src, dst = (self.undo, self.redo) if action == "undo" else (self.redo, self.undo)
        target = src.pop()
        dst.append(current)
        del dst[:-MAX_UNDO_STEPS]
        self.committed = target
        self._changed()
        return target

    def depths(self) -> dict[str, Any]:
        out: dict[str, Any] = {"undoDepth": len(self.undo), "redoDepth": len(self.redo)}
        if self.persist_warning:
            out["persistWarning"] = self.persist_warning
        return out


def history_of(worker: Any) -> SlotHistory:
    hist = getattr(worker, "slot_history", None)
    if isinstance(hist, SlotHistory):
        return hist
    hist = SlotHistory(
        str(getattr(worker, "slot_name", "") or "test"),
        getattr(worker, "runtime_dir", None),
        persist=False,
    )
    hist.committed = getattr(worker, "last_good_code", None) or None
    hist._worker = worker
    try:
        worker.slot_history = hist
    except AttributeError:
        pass
    return hist


def rebuild_if_stale(worker: Any, *, required: bool = True) -> None:
    """Rebuild calc output left stale by an interrupted transaction before a subsequent read."""
    hist = history_of(worker)
    if not hist.stale or hist.active is not None:
        return
    try:
        worker.call("pump", {"seconds": 0})
    except Exception:
        if required:
            raise
        return
    hist.stale = False
