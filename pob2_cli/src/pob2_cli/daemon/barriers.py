"""Reentrant reader/exclusive RW barrier mixin for ``DaemonManager`` (L3)."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Iterator
from typing import Any

from ..game_profile import GAME_ID
from .runtime import REF_IDLE_TIMEOUT_SEC, _daemon_state_file

_LOG = logging.getLogger(__name__)


class SlotBarrierMixin:
    """Provides reentrant reader/exclusive barriers and idle/shutdown lifecycle for ``DaemonManager``."""

    _lock: threading.RLock
    _cond: threading.Condition
    _reloading: bool
    _reload_owner: int | None
    _reload_depth: int
    _active_ops: int
    _reader_depths: dict[int, int]
    _workers: dict[str, Any]
    _read_cache: Any
    _shutdown_lock: threading.Lock
    _shutdown_started: bool
    _stopping: bool
    _shutdown_done: threading.Event
    runtime_dir: Any
    server: Any
    last_active_ts: float
    disable_global_idle: bool
    global_idle_timeout_sec: float

    def _get_slot_lock(self, slot: str) -> threading.RLock:  # pragma: no cover - provided by subclass
        raise NotImplementedError

    def _new_worker(self, slot_name: str) -> Any:  # pragma: no cover - provided by subclass
        raise NotImplementedError

    @contextlib.contextmanager
    def _reader_barrier(self) -> Iterator[None]:
        cur_tid = threading.get_ident()
        with self._cond:
            if self._reload_owner != cur_tid:
                depth = self._reader_depths.get(cur_tid, 0)
                if depth == 0:
                    while self._reloading and self._reload_owner != cur_tid:
                        self._cond.wait()
                    self._active_ops += 1
                self._reader_depths[cur_tid] = depth + 1
        try:
            yield
        finally:
            with self._cond:
                if self._reload_owner != cur_tid:
                    new_depth = self._reader_depths.get(cur_tid, 1) - 1
                    if new_depth <= 0:
                        self._reader_depths.pop(cur_tid, None)
                        self._active_ops -= 1
                        if self._active_ops == 0:
                            self._cond.notify_all()
                    else:
                        self._reader_depths[cur_tid] = new_depth

    @contextlib.contextmanager
    def _exclusive_barrier(self) -> Iterator[None]:
        cur_tid = threading.get_ident()
        with self._cond:
            while self._reloading and self._reload_owner != cur_tid:
                self._cond.wait()
            if self._reload_owner == cur_tid:
                self._reload_depth += 1
            else:
                self._reloading = True
                self._reload_owner = cur_tid
                self._reload_depth = 1
                while self._active_ops > 0:
                    self._cond.wait()
        try:
            yield
        finally:
            with self._cond:
                self._reload_depth -= 1
                if self._reload_depth == 0:
                    self._reloading = False
                    self._reload_owner = None
                    self._cond.notify_all()

    def check_idle(self) -> bool:
        now = time.time()
        with self._reader_barrier():
            with self._lock:
                candidates = [(name, w) for name, w in self._workers.items() if name != "main"]
            for sname, worker in candidates:
                if not worker.is_alive() or (now - worker.last_active_ts) <= REF_IDLE_TIMEOUT_SEC:
                    continue
                slock = self._get_slot_lock(sname)
                if not slock.acquire(blocking=False):
                    continue
                try:
                    with self._lock:
                        still_same = self._workers.get(sname) is worker
                    if still_same and worker.is_alive() and (time.time() - worker.last_active_ts) > REF_IDLE_TIMEOUT_SEC:
                        try:
                            worker.stop()
                        except (OSError, RuntimeError) as exc:
                            _LOG.debug("Idle worker stop failed for slot %s: %s", sname, exc)
                        with self._lock:
                            if self._workers.get(sname) is worker:
                                self._workers.pop(sname, None)
                                self._read_cache.invalidate_slot(sname)
                finally:
                    slock.release()
        if self.disable_global_idle:
            return False
        return (now - self.last_active_ts) > self.global_idle_timeout_sec

    def shutdown_all(self) -> None:
        import pob2_cli.daemon as _daemon_pkg

        with self._shutdown_lock:
            already = self._shutdown_started
            if not already:
                self._shutdown_started = True
                self._stopping = True
        if already:
            self._shutdown_done.wait(timeout=15.0)
            return
        try:
            with self._lock:
                to_abort = list(self._workers.values())
            for w in to_abort:
                try:
                    w.abort()
                except (OSError, RuntimeError) as exc:
                    _LOG.debug("Worker abort failed during shutdown: %s", exc)
            with self._exclusive_barrier():
                for sname, w in list(self._workers.items()):
                    try:
                        w.stop()
                    except (OSError, RuntimeError) as exc:
                        _LOG.debug("Worker stop failed for slot %s during shutdown: %s", sname, exc)
                    self._read_cache.invalidate_slot(sname)
                self._read_cache.clear_all()
                self._workers = {"main": self._workers.get("main") or self._new_worker("main")}
            state_file = _daemon_state_file(self.runtime_dir)
            try:
                if state_file.exists():
                    existing = _daemon_pkg.read_daemon_state(self.runtime_dir)
                    if existing is None or (
                        existing.get("game") in (None, GAME_ID) and existing.get("pid") in (None, os.getpid())
                    ):
                        state_file.unlink()
            except OSError as exc:
                _LOG.debug("Could not unlink daemon state file %s: %s", state_file, exc)
            if self.server is not None:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
        finally:
            self._shutdown_done.set()


__all__ = ["SlotBarrierMixin"]
