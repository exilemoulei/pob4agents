"""Hot-reload, locale switching, and slot status snapshot helpers for ``DaemonManager``."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..game_profile import ENV_LANG, GAME_ID
from ..locale_cfg import get_active_lang, normalize_lang, set_active_lang
from ..snapshot import history_of
from ..updater import is_valid_engine_dir, resolve_data_root, resolve_engine_root
from .runtime import _daemon_state_file, write_private_json

if TYPE_CHECKING:
    from ..bridge_client import BridgeWorker
    from .slots import DaemonManager


def available_locales(pob_dir: Path | None, data_dir: Path | None = None) -> list[str]:
    avail: list[str] = []
    try:
        if pob_dir is not None:
            droot = resolve_data_root(pob_dir, data_dir=data_dir) / "poe2"
            if droot.is_dir():
                for lang, sub in (("zh-CN", "zh-rCN"), ("zh-TW", "zh-rTW")):
                    if (droot / sub).is_dir():
                        avail.append(lang)
    except OSError:
        pass
    return [*avail, "en"]


def update_daemon_state_pob_dir(manager: DaemonManager) -> None:
    state_file = _daemon_state_file(manager.runtime_dir)
    if not state_file.is_file():
        return
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("pid") == os.getpid():
            data["pob_dir"] = str(manager.pob_dir)
            write_private_json(state_file, data)
    except (OSError, ValueError):
        pass


def hot_reload_update(
    manager: DaemonManager,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    force: bool = False,
    offline: bool = False,
) -> dict[str, Any]:
    import pob2_cli.daemon as _daemon_pkg

    with manager._update_check_lock, manager._exclusive_barrier():
        if manager._stopping:
            return manager.update_status
        saved: dict[str, dict[str, Any]] = {}
        for sname, w in list(manager._workers.items()):
            h = history_of(w)
            manager._histories[sname] = h
            saved[sname] = {"was_alive": w.is_alive(), "code": h.committed}
            try:
                w.stop()
            except (OSError, RuntimeError):
                pass
        manager._read_cache.clear_all()
        manager.update_status = _daemon_pkg.check_and_apply_updates(
            engine_dir=manager._explicit_pob_dir,
            runtime_dir=manager.runtime_dir,
            proxy=proxy if proxy is not None else manager.proxy,
            mirror=mirror if mirror is not None else manager.mirror,
            offline=offline,
            check_only=False,
            force=force,
        )
        if manager._explicit_pob_dir is None:
            manager.pob_dir = resolve_engine_root(runtime_dir=manager.runtime_dir)
        update_daemon_state_pob_dir(manager)
        valid = is_valid_engine_dir(manager.pob_dir, data_dir=manager.data_dir)
        new_workers: dict[str, BridgeWorker] = {}
        for sname, snap in saved.items():
            if sname != "main" and not (snap["was_alive"] or snap["code"]):
                continue
            w = manager._new_worker(sname)
            if valid and (sname == "main" or snap["was_alive"]):
                w.start()
            new_workers[sname] = w
        if "main" not in new_workers:
            mw = manager._new_worker("main")
            if valid:
                mw.start()
            new_workers["main"] = mw
        manager._workers = new_workers
        return manager.update_status


def handle_set_lang_action(manager: DaemonManager, params: dict[str, Any]) -> dict[str, Any]:
    raw_lang = params.get("lang")
    persisted_info: dict[str, Any] | None = None
    updated_slots: dict[str, Any] = {}
    with manager._exclusive_barrier():
        if raw_lang is not None and str(raw_lang).strip():
            norm = normalize_lang(str(raw_lang))
            if params.get("persist", True):
                persisted_info = set_active_lang(norm, runtime_dir=manager.runtime_dir)
            target_lang = norm
            if os.environ.get(ENV_LANG, "").strip():
                os.environ[ENV_LANG] = norm
            with manager._lock:
                wsnap = list(manager._workers.items())
            for sname, w in wsnap:
                with manager._get_slot_lock(sname):
                    try:
                        updated_slots[sname] = w.set_locale(target_lang)
                    except Exception as exc:
                        updated_slots[sname] = {"ok": False, "error": str(exc)}
                    manager._read_cache.invalidate_slot(sname)
            manager._read_cache.clear_all()
        else:
            target_lang, _ = get_active_lang(manager.runtime_dir)
        eff_lang, eff_src = get_active_lang(manager.runtime_dir)
    out: dict[str, Any] = {
        "game": GAME_ID,
        "lang": eff_lang if raw_lang is None else target_lang,
        "effectiveLang": eff_lang,
        "source": eff_src,
        "availableLocales": available_locales(manager.pob_dir, manager.data_dir),
        "slotsUpdated": updated_slots,
    }
    if persisted_info:
        out["persistedLang"] = persisted_info["lang"]
        out["configPath"] = persisted_info["configPath"]
    return out


def slot_status_entry(manager: DaemonManager, slot_name: str, worker: BridgeWorker | None) -> dict[str, Any]:
    alive = worker is not None and worker.is_alive()
    info = None
    busy = False
    if alive and worker is not None:
        slock = manager._get_slot_lock(slot_name)
        if slock.acquire(blocking=False):
            try:
                with manager._lock:
                    still = manager._workers.get(slot_name) is worker and worker.is_alive()
                if still:
                    try:
                        info = worker.call("get_build_info", {})
                    except Exception:
                        info = None
                else:
                    alive = False
            finally:
                slock.release()
        else:
            busy = True
    entry: dict[str, Any] = {
        "alive": alive,
        "buildInfo": info,
        "hasLoadedBuild": bool(worker and (getattr(worker, "has_build", False) or worker.last_good_code)),
    }
    if busy:
        entry["busy"] = True
    if worker is not None:
        h = history_of(worker)
        entry.update(h.depths())
        entry["rev"] = h.rev
        if slot_name == "main":
            entry["hello"] = worker.hello_info
        if h.source_label:
            entry["sourceLabel"] = h.source_label
        if worker.restore_failed:
            entry["restoreFailed"] = worker.restore_failed
    return entry


def build_status_payload(manager: DaemonManager) -> dict[str, Any]:
    boot_running = manager._boot_update_thread is not None and manager._boot_update_thread.is_alive()
    with manager._reader_barrier():
        with manager._lock:
            wsnap = dict(manager._workers)
            pob_dir_val = manager.pob_dir
            upd_copy = dict(manager.update_status)
        if not upd_copy and boot_running:
            upd_copy = {"state": "checking"}
        slots_status = {sn: slot_status_entry(manager, sn, w) for sn, w in wsnap.items()}
        main_w = wsnap.get("main")
        main_caps = dict(getattr(main_w, "caps", None) or {})
        main_entry = slots_status.get("main") or slot_status_entry(manager, "main", None)
        if main_caps:
            main_entry.setdefault("caps", main_caps)
        ref_entry = slots_status.get("ref") or {"alive": False, "buildInfo": None, "hasLoadedBuild": False}
        eff_lang, eff_src = get_active_lang(manager.runtime_dir)
        res: dict[str, Any] = {
            "game": GAME_ID,
            "pid": os.getpid(),
            "uptimeSec": round(time.time() - manager.started_at, 1),
            "idleSec": round(time.time() - manager.last_active_ts, 1),
            "engineDir": str(pob_dir_val),
            "runtimeDir": str(manager.runtime_dir),
            "lang": eff_lang,
            "langSource": eff_src,
            "availableLocales": available_locales(pob_dir_val, manager.data_dir),
            "winLuaHost": bool(main_caps.get("winLuaHost")),
            "caps": main_caps,
            "updateStatus": upd_copy,
            "main": main_entry,
            "ref": ref_entry,
            "slots": slots_status,
        }
        return manager._attach_warning_if_degraded(res)
