"""Incremental PoB2 (dev) + PobTools-zh updater with offline degradation and atomic gate rollback."""

from __future__ import annotations

import concurrent.futures
import contextlib
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bootstrap import _apply_staged_files_with_gate
from .constants import (
    _AUTO_CHECK_TIMEOUT_SPEC,
    _CHECK_TIMEOUT_SPEC,
    POBTOOLS_APP_ONLY_PREFIXES,
    POBTOOLS_DATA_ONLY_PREFIXES,
    STATE_BOOTSTRAP_MISSING,
    STATE_DEGRADED_NETWORK,
    STATE_DEGRADED_ROLLBACK,
    STATE_UP_TO_DATE,
    STATE_UPDATED,
    _is_version_gte,
    _is_version_newer,
    _to_httpx_timeout,
)
from .engine_lock import STAGING_DIRNAME, EngineLockError, engine_update_lock, recover_idle_engine
from .manifest import _render_local_manifest_xml, diff_pob_manifest
from .paths import get_runtime_dir, is_valid_engine_dir, resolve_engine_root, resolve_game_dir
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
    load_update_state,
    probe_engine_gate,
    save_update_state,
)


def _pkg() -> Any:
    return sys.modules["pob2_cli.updater"]


def _apply_incremental_locked(
    *,
    active_dir: Path,
    rt_dir: Path,
    local_vers: dict[str, str | None],
    remote_manifest: Any,
    remote_pob_ver: str | None,
    new_etag: str | None,
    pob_needs: bool,
    to_download: list[Any],
    to_delete: list[Path],
    tools_info: dict[str, Any],
    tools_needs: bool,
    data_needs: bool,
    remote_data_tag: str | None,
    proxy: str | None,
    mirror: str | None,
    probe_callable: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    pkg = _pkg()
    staging_dir = active_dir / STAGING_DIRNAME
    shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged: dict[Path, Path] = {}

    if remote_manifest is not None and pob_needs:
        staged.update(_download_pob_files_to_staging(remote_manifest, to_download, staging_dir, proxy=proxy, mirror=mirror))
        ms = staging_dir / "PoE2" / "manifest.xml"
        ms.parent.mkdir(parents=True, exist_ok=True)
        ms.write_text(_render_local_manifest_xml(remote_manifest), encoding="utf-8")
        staged[Path("PoE2") / "manifest.xml"] = ms
    if tools_needs and isinstance(tools_info.get("manifest"), dict):
        asset = _pick_pobtools_asset(tools_info["manifest"], prefer_update=True)
        if asset:
            staged.update(
                _download_and_stage_release_zip(
                    tools_info["tag"], asset, staging_dir, proxy=proxy, mirror=mirror, only_prefixes=POBTOOLS_APP_ONLY_PREFIXES
                )
            )
    if data_needs and isinstance(tools_info.get("dataManifest"), dict) and remote_data_tag:
        d_asset = _pick_data_asset(tools_info["dataManifest"])
        if d_asset:
            staged.update(
                _download_and_stage_release_zip(
                    remote_data_tag, d_asset, staging_dir, proxy=proxy, mirror=mirror, only_prefixes=POBTOOLS_DATA_ONLY_PREFIXES
                )
            )
    if not pkg.is_windows():
        with contextlib.suppress(Exception):
            staged.update(pkg.ensure_posix_runtime(active_dir, staging_dir, proxy=proxy, mirror=mirror))

    apply_res = _apply_staged_files_with_gate(active_dir, staged, deleted_rel_paths=to_delete, probe_fn=probe_callable)
    if not apply_res["ok"]:
        return _save_state_snapshot(
            STATE_DEGRADED_ROLLBACK,
            engine_dir=active_dir,
            rt_dir=rt_dir,
            local_pob_ver=local_vers["localPobVersion"],
            remote_pob_ver=remote_pob_ver,
            err=str(apply_res.get("error") or "gate_result failed"),
            rolled_back=bool(apply_res.get("rolledBack")),
        )
    if tools_needs or data_needs:
        _write_pobtools_meta(
            active_dir,
            pobtools_version=tools_info["version"],
            pobtools_tag=tools_info["tag"],
            data_version=remote_data_tag or local_vers["dataVersion"],
        )
    state = {
        "state": STATE_UPDATED,
        "localPobVersion": remote_pob_ver,
        "remotePobVersion": remote_pob_ver,
        "pobToolsVersion": tools_info["version"],
        "pobToolsTag": tools_info["tag"],
        "dataVersion": remote_data_tag or local_vers["dataVersion"],
        "pobManifestEtag": new_etag,
        "checkedAt": time.time(),
        "engineDir": str(active_dir),
        "updatedFilesCount": len(staged),
        "deletedFilesCount": len(to_delete),
        "probe": apply_res["probe"],
    }
    save_update_state(state, rt_dir)
    return state


def check_and_apply_updates(
    *,
    engine_dir: Path | None = None,
    runtime_dir: Path | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    offline: bool = False,
    check_only: bool = False,
    force: bool = False,
    include_zh: bool = True,
    probe_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check PoB2 ``dev`` + PobTools-zh upstreams and atomically apply slim incremental updates."""
    pkg = _pkg()
    rt_dir = get_runtime_dir(runtime_dir)
    prev = load_update_state(rt_dir)
    probe_callable = probe_fn or probe_engine_gate
    active_dir = Path(engine_dir) if engine_dir is not None else resolve_engine_root(runtime_dir=rt_dir)

    if not is_valid_engine_dir(active_dir):
        if offline:
            return _save_state_snapshot(
                STATE_BOOTSTRAP_MISSING,
                engine_dir=active_dir,
                rt_dir=rt_dir,
                err="offline mode enabled and no valid PoB2 engine found",
            )
        return pkg.bootstrap_engine(
            target_dir=active_dir,
            runtime_dir=rt_dir,
            proxy=proxy,
            mirror=mirror,
            include_zh=include_zh,
            probe_fn=probe_callable,
        )

    recover_idle_engine(active_dir)
    local_vers = detect_local_versions(active_dir, rt_dir)
    if offline:
        return _save_state_snapshot(
            STATE_UP_TO_DATE,
            engine_dir=active_dir,
            rt_dir=rt_dir,
            local_pob_ver=local_vers["localPobVersion"],
            remote_pob_ver=prev.get("remotePobVersion"),
            extra={"offline": True},
        )

    timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC if force else _AUTO_CHECK_TIMEOUT_SPEC)
    prev_etag = None if force else prev.get("pobManifestEtag")
    net_errors: list[str] = []
    remote_manifest = None
    new_etag = prev_etag
    tools_info: dict[str, Any] | None = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_pob = pool.submit(
            pkg.fetch_pob_manifest_remote,
            etag=prev_etag, proxy=proxy, mirror=mirror, timeout=timeout, slim=True,
        )
        fut_tools = pool.submit(
            pkg.fetch_pobtools_latest_info,
            proxy=proxy, mirror=mirror, timeout=timeout, fetch_manifest=False,
        )
        try:
            _, remote_manifest, new_etag = fut_pob.result()
        except Exception as exc:
            net_errors.append(f"PoB2 manifest: {type(exc).__name__}: {exc}")
        try:
            tools_info = fut_tools.result()
        except Exception as exc:
            net_errors.append(f"PobTools-zh: {type(exc).__name__}: {exc}")

    if net_errors or tools_info is None:
        return _save_state_snapshot(
            STATE_DEGRADED_NETWORK,
            engine_dir=active_dir,
            rt_dir=rt_dir,
            local_pob_ver=local_vers["localPobVersion"],
            err="; ".join(net_errors) or "metadata fetch failed",
        )

    poe2_dir = resolve_game_dir(active_dir)
    to_download, to_delete = (
        diff_pob_manifest(poe2_dir, remote_manifest, verify_local_sha1=True, slim=True)
        if remote_manifest is not None
        else ([], [])
    )
    remote_pob_ver = remote_manifest.version if remote_manifest is not None else local_vers["localPobVersion"]
    tools_needs = force or _is_version_newer(tools_info["version"], local_vers["pobToolsVersion"])
    data_needs = False
    remote_data_tag: str | None = None
    if tools_needs or (include_zh and not local_vers["dataVersion"]) or force:
        tools_info = pkg.fetch_pobtools_latest_info(
            proxy=proxy, mirror=mirror, timeout=timeout, fetch_manifest=True
        )
        tman, dman = tools_info.get("manifest"), tools_info.get("dataManifest")
        if isinstance(tman, dict) and tman.get("dataTag"):
            remote_data_tag = str(tman["dataTag"])
        if include_zh and isinstance(dman, dict) and remote_data_tag:
            if (force or remote_data_tag != local_vers["dataVersion"]) and _is_version_gte(
                tools_info["version"], dman.get("minApp")
            ):
                data_needs = True

    pob_needs = bool(to_download or to_delete or (remote_manifest and remote_pob_ver != local_vers["localPobVersion"]))
    if not (pob_needs or tools_needs or data_needs) or check_only:
        return _save_state_snapshot(
            STATE_UP_TO_DATE,
            engine_dir=active_dir,
            rt_dir=rt_dir,
            local_pob_ver=local_vers["localPobVersion"],
            remote_pob_ver=remote_pob_ver,
            extra={
                "pobManifestEtag": new_etag if not check_only else prev_etag,
                "updateAvailable": bool(pob_needs or tools_needs or data_needs),
                "pobFilesChanged": len(to_download),
                "pobFilesDeleted": len(to_delete),
            },
        )

    try:
        with engine_update_lock(active_dir):
            return _apply_incremental_locked(
                active_dir=active_dir,
                rt_dir=rt_dir,
                local_vers=local_vers,
                remote_manifest=remote_manifest,
                remote_pob_ver=remote_pob_ver,
                new_etag=new_etag,
                pob_needs=pob_needs,
                to_download=to_download,
                to_delete=to_delete,
                tools_info=tools_info,
                tools_needs=tools_needs,
                data_needs=data_needs,
                remote_data_tag=remote_data_tag,
                proxy=proxy,
                mirror=mirror,
                probe_callable=probe_callable,
            )
    except EngineLockError as exc:
        return _save_state_snapshot(
            STATE_UP_TO_DATE,
            engine_dir=active_dir,
            rt_dir=rt_dir,
            local_pob_ver=local_vers["localPobVersion"],
            remote_pob_ver=remote_pob_ver,
            extra={"updateAvailable": True, "updateSkipped": str(exc)},
        )
