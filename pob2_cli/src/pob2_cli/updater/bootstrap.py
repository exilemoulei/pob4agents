"""Atomic staging/rollback gate and PoB2 slim engine bootstrapper."""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .constants import (
    _CHECK_TIMEOUT_SPEC,
    POBTOOLS_APP_ONLY_PREFIXES,
    POBTOOLS_DATA_ONLY_PREFIXES,
    STATE_BOOTSTRAP_MISSING,
    STATE_DEGRADED_ROLLBACK,
    STATE_UP_TO_DATE,
    STATE_UPDATED,
    EngineGateError,
    PobManifest,
    UpdateError,
    _is_version_gte,
    _to_httpx_timeout,
)
from .engine_lock import (
    BACKUP_DIRNAME,
    STAGING_DIRNAME,
    EngineLockError,
    backup_file,
    backup_in_the_way,
    discard_backup,
    engine_update_lock,
    restore_interrupted_apply,
)
from .manifest import _render_local_manifest_xml, diff_pob_manifest
from .paths import get_runtime_dir, is_valid_engine_dir, seed_from_legacy_dir
from .remote import (
    _download_and_stage_release_zip,
    _download_pob_files_to_staging,
    _pick_data_asset,
    _pick_pobtools_asset,
)
from .state import (
    _save_state_snapshot,
    _write_pobtools_meta,
    detect_local_versions,
    probe_engine_gate,
    save_update_state,
)


def _pkg() -> Any:
    return sys.modules["pob2_cli.updater"]


def _apply_staged_files_with_gate(
    engine_dir: Path,
    staged_files: dict[Path, Path],
    deleted_rel_paths: list[Path] | None = None,
    *,
    probe_fn: Callable[[Path], dict[str, Any]] = probe_engine_gate,
) -> dict[str, Any]:
    eng = Path(engine_dir)
    bak_dir = eng / BACKUP_DIRNAME
    staging_dir = eng / STAGING_DIRNAME
    restore_interrupted_apply(eng)
    blocked = backup_in_the_way(eng)
    if blocked:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise EngineLockError(blocked)
    bak_dir.mkdir(parents=True, exist_ok=True)

    backed_up: list[tuple[Path, Path]] = []
    created: list[Path] = []
    kept = False
    rolled_back = False

    def _rollback() -> bool:
        whole = True
        for dst, bak in reversed(backed_up):
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if bak.exists():
                    os.replace(bak, dst)
            except Exception:
                try:
                    if bak.exists():
                        shutil.copy2(bak, dst)
                except Exception:
                    whole = False
        for created_file in reversed(created):
            try:
                if created_file.is_file():
                    created_file.unlink()
            except Exception:
                whole = False
        return whole

    try:
        for rel_path, staged_src in staged_files.items():
            dst = eng / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                bak = bak_dir / rel_path
                backup_file(dst, bak)
                backed_up.append((dst, bak))
            else:
                created.append(dst)
            os.replace(staged_src, dst)

        for rel_del in deleted_rel_paths or []:
            dst = eng / rel_del
            if dst.is_file():
                bak = bak_dir / rel_del
                backup_file(dst, bak)
                backed_up.append((dst, bak))
                dst.unlink()

        probe = probe_fn(eng)
        if not probe.get("ok"):
            rolled_back = _rollback()
            return {"ok": False, "rolledBack": rolled_back, "probe": probe, "error": probe.get("error")}
        kept = True
        return {"ok": True, "rolledBack": False, "probe": probe}
    except Exception as exc:
        rolled_back = _rollback()
        return {"ok": False, "rolledBack": rolled_back, "probe": None, "error": f"{type(exc).__name__}: {exc}"}
    except BaseException:
        rolled_back = _rollback()
        raise
    finally:
        if kept:
            discard_backup(eng)
        elif rolled_back:
            shutil.rmtree(bak_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)


def _stage_bootstrap_assets(
    eng_dir: Path,
    staging_dir: Path,
    tools_info: dict[str, Any],
    pob_manifest: PobManifest,
    *,
    include_zh: bool,
    proxy: str | None,
    mirror: str | None,
) -> tuple[dict[Path, Path], str | None]:
    pkg = _pkg()
    staged: dict[Path, Path] = {}
    tools_manifest = tools_info.get("manifest")
    if not isinstance(tools_manifest, dict):
        raise UpdateError("PobTools-zh release manifest JSON missing.")
    app_asset = _pick_pobtools_asset(tools_manifest, prefer_update=True)
    if not app_asset:
        raise UpdateError("No PobTools zip asset found in release manifest.")
    staged.update(
        _download_and_stage_release_zip(
            tools_info["tag"],
            app_asset,
            staging_dir,
            proxy=proxy,
            mirror=mirror,
            skip_prefixes=("PoE1", "PoE2"),
            only_prefixes=POBTOOLS_APP_ONLY_PREFIXES,
        )
    )
    if not pkg.is_windows():
        staged.update(pkg.ensure_posix_runtime(eng_dir, staging_dir, proxy=proxy, mirror=mirror))

    installed_data_ver: str | None = None
    data_manifest = tools_info.get("dataManifest")
    if include_zh and isinstance(data_manifest, dict) and _is_version_gte(tools_info["version"], data_manifest.get("minApp")):
        d_asset = _pick_data_asset(data_manifest)
        if d_asset:
            data_tag = str(data_manifest.get("tag") or tools_manifest.get("dataTag") or "")
            staged.update(
                _download_and_stage_release_zip(
                    data_tag,
                    d_asset,
                    staging_dir,
                    proxy=proxy,
                    mirror=mirror,
                    skip_prefixes=("PoE1", "PoE2"),
                    only_prefixes=POBTOOLS_DATA_ONLY_PREFIXES,
                )
            )
            installed_data_ver = data_tag or None

    to_download, _ = diff_pob_manifest(eng_dir / "PoE2", pob_manifest, verify_local_sha1=True, slim=True)
    staged.update(_download_pob_files_to_staging(pob_manifest, to_download, staging_dir, proxy=proxy, mirror=mirror))
    manifest_staged = staging_dir / "PoE2" / "manifest.xml"
    manifest_staged.parent.mkdir(parents=True, exist_ok=True)
    manifest_staged.write_text(_render_local_manifest_xml(pob_manifest), encoding="utf-8")
    staged[Path("PoE2") / "manifest.xml"] = manifest_staged
    return staged, installed_data_ver


def bootstrap_engine(
    *,
    target_dir: Path | None = None,
    runtime_dir: Path | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    include_zh: bool = True,
    from_dir: Path | None = None,
    force: bool = False,
    probe_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bootstrap a managed PoB2 slim engine from a local read-only seed or GitHub upstreams."""
    rt_dir = get_runtime_dir(runtime_dir)
    eng_dir = Path(target_dir) if target_dir is not None else (rt_dir / "engine")
    probe_callable = probe_fn or probe_engine_gate

    with engine_update_lock(eng_dir):
        if not force and from_dir is None and is_valid_engine_dir(eng_dir):
            vers = detect_local_versions(eng_dir, rt_dir)
            state = {
                "state": STATE_UP_TO_DATE,
                "localPobVersion": vers["localPobVersion"],
                "remotePobVersion": vers["localPobVersion"],
                "dataVersion": vers["dataVersion"],
                "checkedAt": time.time(),
                "engineDir": str(eng_dir),
                "source": "existing",
            }
            save_update_state(state, rt_dir)
            return state
        if from_dir is not None:
            src = Path(from_dir)
            if not is_valid_engine_dir(src):
                raise UpdateError(f"Offline PoE2 seed directory is invalid: {src}")
            staging_dir = eng_dir / STAGING_DIRNAME
            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True)
            seed_from_legacy_dir(src, staging_dir)
            staged = {p.relative_to(staging_dir): p for p in staging_dir.rglob("*") if p.is_file()}
            apply_res = _apply_staged_files_with_gate(eng_dir, staged, probe_fn=probe_callable)
            if not apply_res["ok"]:
                raise EngineGateError(f"Offline PoB2 bootstrap failed gate probe: {apply_res.get('error')}")
            vers = detect_local_versions(eng_dir, rt_dir)
            state = {
                "state": STATE_UPDATED,
                "localPobVersion": apply_res["probe"].get("pobVersion") or vers["localPobVersion"],
                "remotePobVersion": apply_res["probe"].get("pobVersion") or vers["localPobVersion"],
                "dataVersion": vers["dataVersion"],
                "checkedAt": time.time(),
                "engineDir": str(eng_dir),
                "source": "offline-seed",
                "probe": apply_res["probe"],
            }
            save_update_state(state, rt_dir)
            return state

        import httpx

        pkg = _pkg()
        staging_dir = eng_dir / STAGING_DIRNAME
        check_timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_tools = pool.submit(
                    pkg.fetch_pobtools_latest_info,
                    proxy=proxy,
                    mirror=mirror,
                    timeout=check_timeout,
                    fetch_manifest=True,
                )
                fut_pob = pool.submit(
                    pkg.fetch_pob_manifest_remote,
                    etag=None,
                    proxy=proxy,
                    mirror=mirror,
                    timeout=check_timeout,
                    slim=True,
                )
                tools_info = fut_tools.result()
                _, pob_manifest, pob_etag = fut_pob.result()
            assert pob_manifest is not None
            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged_files, installed_data_ver = _stage_bootstrap_assets(
                eng_dir, staging_dir, tools_info, pob_manifest, include_zh=include_zh, proxy=proxy, mirror=mirror
            )
            apply_res = _apply_staged_files_with_gate(eng_dir, staged_files, probe_fn=probe_callable)
            if not apply_res["ok"]:
                err = str(apply_res.get("error") or "gate_result failed")
                _save_state_snapshot(
                    STATE_DEGRADED_ROLLBACK,
                    engine_dir=eng_dir,
                    rt_dir=rt_dir,
                    remote_pob_ver=pob_manifest.version,
                    err=err,
                    rolled_back=bool(apply_res.get("rolledBack")),
                )
                raise EngineGateError(err)
            _write_pobtools_meta(
                eng_dir,
                pobtools_version=tools_info["version"],
                pobtools_tag=tools_info["tag"],
                data_version=installed_data_ver,
            )
            state = {
                "state": STATE_UPDATED,
                "localPobVersion": pob_manifest.version,
                "remotePobVersion": pob_manifest.version,
                "pobToolsVersion": tools_info["version"],
                "pobToolsTag": tools_info["tag"],
                "dataVersion": installed_data_ver,
                "pobManifestEtag": pob_etag,
                "checkedAt": time.time(),
                "engineDir": str(eng_dir),
                "updatedFilesCount": len(staged_files),
                "probe": apply_res["probe"],
            }
            save_update_state(state, rt_dir)
            return state
        except (httpx.HTTPError, OSError, UpdateError) as exc:
            if isinstance(exc, EngineGateError):
                raise
            shutil.rmtree(staging_dir, ignore_errors=True)
            return _save_state_snapshot(
                STATE_BOOTSTRAP_MISSING,
                engine_dir=eng_dir,
                rt_dir=rt_dir,
                err=f"{type(exc).__name__}: {exc}",
            )
