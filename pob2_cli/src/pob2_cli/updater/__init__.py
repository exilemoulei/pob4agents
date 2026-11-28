"""Managed headless PoB2 slim engine resolver, bootstrapper, and atomic incremental updater."""

from __future__ import annotations

from typing import Any

from .paths import (
    build_engine_command,
    detect_source_checkout_root,
    get_bundled_headless_runner_path,
    get_posix_platform_tag,
    get_runtime_dir,
    get_shared_cache_dir,
    is_valid_engine_dir,
    is_windows,
    resolve_bridge_lua,
    resolve_data_root,
    resolve_engine_root,
    resolve_game_dir,
    resolve_luajit_binary,
    seed_from_legacy_dir,
)

_BOOTSTRAP_EXPORTS: frozenset[str] = frozenset({"_apply_staged_files_with_gate", "bootstrap_engine"})
_CONSTANTS_EXPORTS: frozenset[str] = frozenset(
    {
        "_ASSET_TIMEOUT_SPEC",
        "_AUTO_CHECK_TIMEOUT_SPEC",
        "_CHECK_TIMEOUT_SPEC",
        "ASSET_MAX_RETRIES",
        "GITHUB_MIRROR_PREFIXES",
        "OK_STATES",
        "POB2_CHANNEL_JSON_URL",
        "POB2_DEFAULT_BRANCH",
        "POB2_DOWNLOAD_BASE",
        "POB2_MANIFEST_URL",
        "POBTOOLS_APP_ONLY_PREFIXES",
        "POBTOOLS_DATA_ONLY_PREFIXES",
        "POBTOOLS_DOWNLOAD_BASE",
        "POBTOOLS_LATEST_URL",
        "POBTOOLS_META_FILENAME",
        "POSIX_LUAJIT_COMMIT",
        "POSIX_LUAJIT_TARBALL_SHA256",
        "POSIX_LUAJIT_TARBALL_URL",
        "POSIX_LUAUTF8_TAG",
        "POSIX_LUAUTF8_TARBALL_SHA256",
        "POSIX_LUAUTF8_TARBALL_URL",
        "STATE_BOOTSTRAP_MISSING",
        "STATE_DEGRADED_NETWORK",
        "STATE_DEGRADED_ROLLBACK",
        "STATE_UP_TO_DATE",
        "STATE_UPDATED",
        "UPDATE_STATE_FILENAME",
        "USER_AGENT",
        "EngineGateError",
        "ManifestFileEntry",
        "PobManifest",
        "UpdateError",
        "_is_version_gte",
        "_is_version_newer",
        "_make_http_client",
        "_parse_semver",
        "_to_httpx_timeout",
        "resolve_mirror",
        "resolve_proxy",
        "rewrite_github_url",
    }
)
_ENGINE_LOCK_EXPORTS: frozenset[str] = frozenset(
    {"EngineBusyError", "EngineLockError", "engine_update_lock", "recover_idle_engine", "unrestored_swap"}
)
_MANIFEST_EXPORTS: frozenset[str] = frozenset(
    {
        "_render_local_manifest_xml",
        "build_manifest_file_url",
        "diff_pob_manifest",
        "is_slim_manifest_entry",
        "parse_pob_manifest",
        "verify_pob_sha1",
    }
)
_POSIX_RUNTIME_EXPORTS: frozenset[str] = frozenset(
    {
        "_build_posix_runtime_from_source",
        "_extract_tar_gz_safely",
        "_try_download_prebuilt_posix_runtime",
        "ensure_posix_runtime",
        "posix_runtime_is_current",
        "runtime_asset_name",
        "runtime_pin",
    }
)
_RELEASES_EXPORTS: frozenset[str] = frozenset(
    {"asset_url", "is_newer", "latest_release_tag", "parse_version", "release_platform_tag", "signed_sums"}
)
_REMOTE_EXPORTS: frozenset[str] = frozenset(
    {
        "ALLOWED_RELEASE_ASSET_EXTS",
        "_download_and_stage_release_zip",
        "_download_bytes_with_retry",
        "_download_pob_files_to_staging",
        "_extract_tag_from_location",
        "_extract_zip_safely",
        "_network_errors",
        "_safe_asset_name",
        "fetch_pob_manifest_remote",
        "fetch_pobtools_latest_info",
        "redact_proxy_credentials",
        "validate_release_asset_name",
    }
)
_SIGNATURE_EXPORTS: frozenset[str] = frozenset({"RELEASE_PUBLIC_KEYS", "ed25519_verify", "verify_sums"})
_STATE_EXPORTS: frozenset[str] = frozenset(
    {
        "detect_local_versions",
        "format_stderr_update_alert",
        "get_update_state_path",
        "get_update_warning",
        "load_update_state",
        "probe_engine_gate",
        "save_update_state",
    }
)


def __getattr__(name: str) -> Any:
    if name in _BOOTSTRAP_EXPORTS:
        from . import bootstrap as _mod
    elif name in _CONSTANTS_EXPORTS:
        from . import constants as _mod
    elif name in _ENGINE_LOCK_EXPORTS:
        from . import engine_lock as _mod
    elif name in _MANIFEST_EXPORTS:
        from . import manifest as _mod
    elif name in _POSIX_RUNTIME_EXPORTS:
        from . import posix_runtime as _mod
    elif name in _RELEASES_EXPORTS:
        from . import releases as _mod
    elif name in _REMOTE_EXPORTS:
        from . import remote as _mod
    elif name in _SIGNATURE_EXPORTS:
        from . import signature as _mod
    elif name in _STATE_EXPORTS:
        from . import state as _mod
    elif name == "check_and_apply_updates":
        from . import sync as _mod
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    val = getattr(_mod, name)
    globals()[name] = val
    return val


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "ALLOWED_RELEASE_ASSET_EXTS",
    "ASSET_MAX_RETRIES",
    "GITHUB_MIRROR_PREFIXES",
    "OK_STATES",
    "POB2_CHANNEL_JSON_URL",
    "POB2_DEFAULT_BRANCH",
    "POB2_DOWNLOAD_BASE",
    "POB2_MANIFEST_URL",
    "POBTOOLS_APP_ONLY_PREFIXES",
    "POBTOOLS_DATA_ONLY_PREFIXES",
    "POBTOOLS_DOWNLOAD_BASE",
    "POBTOOLS_LATEST_URL",
    "POBTOOLS_META_FILENAME",
    "POSIX_LUAJIT_COMMIT",
    "POSIX_LUAJIT_TARBALL_SHA256",
    "POSIX_LUAJIT_TARBALL_URL",
    "POSIX_LUAUTF8_TAG",
    "POSIX_LUAUTF8_TARBALL_SHA256",
    "POSIX_LUAUTF8_TARBALL_URL",
    "RELEASE_PUBLIC_KEYS",
    "STATE_BOOTSTRAP_MISSING",
    "STATE_DEGRADED_NETWORK",
    "STATE_DEGRADED_ROLLBACK",
    "STATE_UPDATED",
    "STATE_UP_TO_DATE",
    "UPDATE_STATE_FILENAME",
    "USER_AGENT",
    "_ASSET_TIMEOUT_SPEC",
    "_AUTO_CHECK_TIMEOUT_SPEC",
    "_CHECK_TIMEOUT_SPEC",
    "EngineBusyError",
    "EngineGateError",
    "EngineLockError",
    "ManifestFileEntry",
    "PobManifest",
    "UpdateError",
    "_apply_staged_files_with_gate",
    "_build_posix_runtime_from_source",
    "_download_and_stage_release_zip",
    "_download_bytes_with_retry",
    "_download_pob_files_to_staging",
    "_extract_tag_from_location",
    "_extract_tar_gz_safely",
    "_extract_zip_safely",
    "_is_version_gte",
    "_is_version_newer",
    "_make_http_client",
    "_network_errors",
    "_parse_semver",
    "_render_local_manifest_xml",
    "_safe_asset_name",
    "_to_httpx_timeout",
    "_try_download_prebuilt_posix_runtime",
    "asset_url",
    "bootstrap_engine",
    "build_engine_command",
    "build_manifest_file_url",
    "check_and_apply_updates",
    "detect_local_versions",
    "detect_source_checkout_root",
    "diff_pob_manifest",
    "ed25519_verify",
    "engine_update_lock",
    "ensure_posix_runtime",
    "fetch_pob_manifest_remote",
    "fetch_pobtools_latest_info",
    "format_stderr_update_alert",
    "get_bundled_headless_runner_path",
    "get_posix_platform_tag",
    "get_runtime_dir",
    "get_shared_cache_dir",
    "get_update_state_path",
    "get_update_warning",
    "is_newer",
    "is_slim_manifest_entry",
    "is_valid_engine_dir",
    "is_windows",
    "latest_release_tag",
    "load_update_state",
    "parse_pob_manifest",
    "parse_version",
    "posix_runtime_is_current",
    "probe_engine_gate",
    "recover_idle_engine",
    "redact_proxy_credentials",
    "release_platform_tag",
    "resolve_bridge_lua",
    "resolve_data_root",
    "resolve_engine_root",
    "resolve_game_dir",
    "resolve_luajit_binary",
    "resolve_mirror",
    "resolve_proxy",
    "rewrite_github_url",
    "runtime_asset_name",
    "runtime_pin",
    "save_update_state",
    "seed_from_legacy_dir",
    "signed_sums",
    "unrestored_swap",
    "validate_release_asset_name",
    "verify_pob_sha1",
    "verify_sums",
]
