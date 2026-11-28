"""Per-engine exclusive file lock and interrupted-swap recovery for PoB2 updates."""

from __future__ import annotations

import os
import shutil
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .constants import UpdateError

BACKUP_DIRNAME = ".bak"
_KEPT_MARKER = ".bak-kept"
_DISCARDED_DIRNAME = ".bak-done"
STAGING_DIRNAME = ".staging"
LOCK_FILENAME = ".update_poe2.lock"
_PARTIAL_SUFFIX = ".pob2-partial"

_held = threading.local()


class EngineLockError(UpdateError):
    """The PoB2 engine folder could not be taken for an install or update."""


class EngineBusyError(EngineLockError):
    """Another process or thread is installing or updating the same PoB2 engine folder."""


def _lock_file(fh: Any, *, release: bool = False) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        mode = msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK
        msvcrt.locking(fh.fileno(), mode, 1)
        return
    import fcntl

    op = fcntl.LOCK_UN if release else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(fh.fileno(), op)


@contextmanager
def engine_update_lock(engine_dir: Path) -> Iterator[None]:
    """Hold ``engine_dir`` exclusively for staging, atomic swap, probe, and rollback."""
    eng = Path(engine_dir)
    key = os.path.normcase(os.path.abspath(eng))
    held: set[str] = _held.__dict__.setdefault("keys", set())
    if key in held:
        yield
        return
    try:
        eng.mkdir(parents=True, exist_ok=True)
        fh = open(eng / LOCK_FILENAME, "a+b")
    except OSError as exc:
        raise EngineLockError(f"Cannot write to PoB2 engine folder {eng}: {exc}") from exc
    with fh:
        try:
            _lock_file(fh)
        except OSError as exc:
            raise EngineBusyError(
                f"Another pob2 process is installing or updating {eng}; let it finish first."
            ) from exc
        held.add(key)
        try:
            restore_interrupted_apply(eng)
            yield
        finally:
            held.discard(key)
            with suppress(OSError):
                _lock_file(fh, release=True)


def backup_file(src: Path, bak: Path) -> None:
    """Copy ``src`` to ``bak`` atomically via partial temp file."""
    bak.parent.mkdir(parents=True, exist_ok=True)
    partial = bak.with_name(bak.name + _PARTIAL_SUFFIX)
    shutil.copy2(src, partial)
    os.replace(partial, bak)


def discard_backup(engine_dir: Path) -> None:
    """Drop backups of a kept update swap cleanly."""
    eng = Path(engine_dir)
    bak_dir = eng / BACKUP_DIRNAME
    if not bak_dir.is_dir():
        return
    try:
        (eng / _KEPT_MARKER).touch()
    except OSError:
        with suppress(OSError):
            os.rename(bak_dir, eng / _DISCARDED_DIRNAME)
    _finish_discard(eng)


def _finish_discard(eng: Path) -> None:
    shutil.rmtree(eng / _DISCARDED_DIRNAME, ignore_errors=True)
    marker = eng / _KEPT_MARKER
    if not marker.exists():
        return
    bak_dir = eng / BACKUP_DIRNAME
    shutil.rmtree(bak_dir, ignore_errors=True)
    if not bak_dir.exists():
        with suppress(OSError):
            marker.unlink(missing_ok=True)


def unrestored_swap(engine_dir: Path) -> str | None:
    eng = Path(engine_dir)
    bak_dir = eng / BACKUP_DIRNAME
    if (eng / _KEPT_MARKER).exists() or not bak_dir.is_dir():
        return None
    return (
        f"An earlier PoB2 update was cut short and {bak_dir} could not be restored. "
        "Run `pob2 stop` and retry."
    )


def backup_in_the_way(engine_dir: Path) -> str | None:
    eng = Path(engine_dir)
    marker = eng / _KEPT_MARKER
    if not marker.exists():
        return unrestored_swap(eng)
    bak_dir = eng / BACKUP_DIRNAME
    left = bak_dir if bak_dir.exists() else marker
    return f"Previous PoB2 backup artifact {left} is still held open; close readers and retry."


def restore_interrupted_apply(engine_dir: Path) -> int:
    """Put back files replaced by a killed update run; return restored file count."""
    eng = Path(engine_dir)
    _finish_discard(eng)
    if (eng / _KEPT_MARKER).exists():
        return 0
    bak_dir = eng / BACKUP_DIRNAME
    if not bak_dir.is_dir():
        return 0
    restored = 0
    complete = True
    for bak in sorted(p for p in bak_dir.rglob("*") if p.is_file()):
        if bak.name.endswith(_PARTIAL_SUFFIX):
            continue
        dst = eng / bak.relative_to(bak_dir)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(bak, dst)
            restored += 1
        except OSError:
            complete = False
    if complete:
        shutil.rmtree(bak_dir, ignore_errors=True)
    return restored


def recover_idle_engine(engine_dir: Path) -> int:
    """Restore an interrupted apply in ``engine_dir`` if no other writer holds the lock."""
    if not (Path(engine_dir) / BACKUP_DIRNAME).is_dir():
        return 0
    eng = Path(engine_dir)
    key = os.path.normcase(os.path.abspath(eng))
    if key in _held.__dict__.get("keys", ()):
        return 0
    try:
        fh = open(eng / LOCK_FILENAME, "a+b")
    except OSError:
        return 0
    with fh:
        try:
            _lock_file(fh)
        except OSError:
            return 0
        try:
            return restore_interrupted_apply(eng)
        finally:
            with suppress(OSError):
                _lock_file(fh, release=True)
