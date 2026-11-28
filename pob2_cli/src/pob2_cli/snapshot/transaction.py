"""``BuildTransaction`` & ``BuildScope``: transactional CoW/code checkpoints for every PoE2 build edit."""

from __future__ import annotations

import enum
from typing import Any

from .checkpoint import (
    BuildSnapshot,
    drop_snapshot,
    fall_back_to_code,
    restore_snapshot,
    supports_cow_snapshot,
    take_snapshot,
)
from .history import SlotHistory, history_of
from .verify import verify_build


class BuildScope(enum.Enum):
    """How far an edit reaches, deciding whether a Lua CoW or full code checkpoint is taken."""

    ACTIVE_SETS = 1
    SETS = 2
    REPLACES_BUILD = 3

    @classmethod
    def widest(cls, scopes: Any) -> BuildScope:
        return max(scopes, key=lambda s: s.value, default=cls.ACTIVE_SETS)


class BuildTransaction:
    """Holds the worker lock and a base checkpoint; rolls back on exit unless committed."""

    def __init__(
        self,
        worker: Any,
        *,
        scope: BuildScope = BuildScope.ACTIVE_SETS,
        will_commit: bool = False,
    ) -> None:
        self.worker = worker
        self.scope = scope
        self.will_commit = will_commit
        self.history: SlotHistory = history_of(worker)
        self.parent: BuildTransaction | None = None
        self.base: BuildSnapshot | None = None
        self.before_code: str | None = None
        self.verify_digest: dict[str, Any] | None = None
        self.stale = False
        self.notes: list[str] = []
        self._nested: list[BuildSnapshot] = []
        self._entry_code: str | None = None
        self._sent_at: int | None = None
        self._closed = False
        self._committed = False
        self._lock: Any = None

    @property
    def use_cow(self) -> bool:
        return self.scope is BuildScope.ACTIVE_SETS and not self.will_commit and supports_cow_snapshot(self.worker)

    def _known_code(self) -> str | None:
        code = self.history.committed
        if self.parent is None and code and getattr(self.worker, "last_good_code", None) == code:
            return code
        return None

    def _nothing_to_keep(self) -> bool:
        return (
            self.parent is None
            and self.will_commit
            and self.scope is BuildScope.REPLACES_BUILD
            and not self.history.committed
            and not getattr(self.worker, "last_good_code", None)
        )

    def __enter__(self) -> BuildTransaction:
        self._lock = self.worker.lock
        self._lock.__enter__()
        try:
            self.parent = self.history.active
            if self.parent is not None:
                self.parent.fresh()
            self._entry_code = getattr(self.worker, "last_good_code", None)
            known = self._known_code()
            if self._nothing_to_keep():
                self.base = None
            else:
                self.base = take_snapshot(self.worker, prefer_cow=self.use_cow, fresh_fallback=True, code=known)
                self.before_code = known or (self.base.token if self.base.kind == "code" else self.base.fallback_code)
            self._sent_at = self._requests_sent()
            self.history.active = self
        except BaseException as exc:
            self._lock.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        try:
            if not self._closed:
                try:
                    self._rollback_on_exit(exc)
                except Exception as restore_exc:
                    if exc is None:
                        raise
                    exc.add_note(f"Restoring the build afterwards also failed: {restore_exc}")
        finally:
            self.history.active = self.parent
            if self.parent is not None:
                self.parent.stale = self.parent.stale or self.stale
            elif self.stale:
                if getattr(self.worker, "is_running", True):
                    try:
                        self.fresh()
                    except Exception:
                        self.history.stale = True
                else:
                    self.history.stale = True
            if not self._committed and getattr(self.worker, "last_good_code", None) is not None:
                self.worker.last_good_code = self._entry_code
            self._lock.__exit__(exc_type, exc, tb)
        return False

    def _rollback_on_exit(self, exc: BaseException | None) -> None:
        """Roll back an unclosed transaction; verify CoW restore when exiting via exception."""
        untouched = self._sent_at is not None and self._requests_sent() == self._sent_at and not self._nested
        if (
            exc is None
            or untouched
            or self.verify_digest is None
            or self.base is None
            or self.base.kind != "cow"
        ):
            self.rollback()
            return
        check = self.rollback_verified(self.verify_digest)
        if not check.get("verified"):
            exc.add_note(f"Restoring the build afterwards could not be verified: {check.get('detail')}")
        elif check.get("fallback"):
            exc.add_note(f"The build was restored by re-importing its code: {check['fallback']}")

    def checkpoint(self) -> BuildSnapshot:
        cp = take_snapshot(self.worker, prefer_cow=self.use_cow, fresh_fallback=True)
        self._nested.append(cp)
        return cp

    def drop(self, cp: BuildSnapshot | None) -> None:
        if cp is None:
            return
        if cp in self._nested:
            self._nested.remove(cp)
        drop_snapshot(self.worker, cp)

    def restore(self, cp: BuildSnapshot | None = None, *, rebuild: bool = True, drop: bool = False) -> str | None:
        target = cp or self.base
        assert target is not None
        note = restore_snapshot(self.worker, target, drop=drop, rebuild=rebuild)
        if drop and target in self._nested:
            self._nested.remove(target)
        self.stale = target.kind == "cow" and (self.stale or not rebuild)
        if note:
            self.notes.append(note)
        return note

    def fresh(self) -> None:
        if self.stale or self.history.stale:
            self.worker.call("pump", {"seconds": 0})
            self.stale = False
            self.history.stale = False

    def verify(self, before_digest: dict[str, Any], cp: BuildSnapshot | None = None) -> dict[str, Any]:
        target = cp or self.base
        assert target is not None
        self.fresh()
        if target.kind != "cow":
            return verify_build(self.worker, before_digest, code=target.token)
        check = verify_build(self.worker, before_digest)
        if check.get("verified") or not target.fallback_code:
            return check
        note = fall_back_to_code(self.worker, target, f"CoW restore drifted ({check.get('detail')})")
        self.notes.append(note)
        check = verify_build(self.worker, before_digest, code=target.token)
        check["fallback"] = note
        return check

    def _drop_nested(self) -> None:
        while self._nested:
            drop_snapshot(self.worker, self._nested.pop())

    def _requests_sent(self) -> int | None:
        count = getattr(self.worker, "_req_counter", None)
        return count if isinstance(count, int) else None

    def rollback(self) -> str | None:
        if self._sent_at is not None and self._requests_sent() == self._sent_at and not self._nested:
            self.release()
            return None
        self._closed = True
        self._drop_nested()
        return self.restore(drop=True) if self.base is not None else None

    def rollback_verified(self, before_digest: dict[str, Any]) -> dict[str, Any]:
        note = self.rollback()
        check = self.verify(before_digest)
        if note and not check.get("fallback"):
            check["fallback"] = note
        return check

    def release(self) -> None:
        self._closed = True
        self._drop_nested()
        drop_snapshot(self.worker, self.base)

    def _end_committed(self) -> None:
        if self.parent is not None:
            raise RuntimeError("a nested build transaction cannot commit")
        self.release()
        self._committed = True

    def _export(self) -> str:
        return str(self.worker.call("export_code", {}).get("code") or "")

    def commit(self, *, undoable: bool = True, after_code: str | None = None) -> None:
        after = after_code or self._export()
        self._end_committed()
        self.history.commit(self.before_code, after, undoable=undoable)

    def commit_load(self, source_label: str | None) -> None:
        code = self._export()
        self._end_committed()
        self.history.load_build(code, source_label)

    def commit_step(self, action: str) -> None:
        current = self.before_code or ""
        self._end_committed()
        self.history.step(action, current)
