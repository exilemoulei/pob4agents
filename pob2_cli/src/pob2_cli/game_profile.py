"""PoE2 game profile, environment isolation contract, and L0 path resolution.

Strict isolation invariants:
  - Reads ONLY ``POB2_*`` environment variables (never ``POB_*``).
  - Uses dedicated app data dirs (``%LOCALAPPDATA%/pob24agents`` on Windows,
    ``$XDG_DATA_HOME/pob24agents`` or ``~/.local/share/pob24agents`` on POSIX,
    and ``pob2_cli/.runtime`` in source checkouts) so ``pob`` and ``pob2`` never
    share state, ports, locks, or caches.
  - Stamps every daemon/CLI JSON envelope with ``"game": "poe2"`` and uses
    ``X-PoB2-Token`` for daemon authentication.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

GAME_ID: str = "poe2"
GAME_NAME: str = "Path of Exile 2"
CLI_NAME: str = "pob2"
PACKAGE_NAME: str = "pob24agents"
LICENSE_SPDX: str = "GPL-3.0-only"
XML_ROOT_TAG: str = "PathOfBuilding2"
ENGINE_SUBDIR: str = "PoE2"
DATA_SUBDIR: str = "poe2"
APP_DIR_NAME: str = "pob24agents"

# Daemon HTTP & lock isolation (distinct from PoE1 X-PoB-Token / daemon.json)
TOKEN_HEADER: str = "X-PoB2-Token"
DAEMON_STATE_FILENAME: str = "daemon_poe2.json"
DAEMON_LOCK_FILENAME: str = "daemon_poe2.lock"
DAEMON_LOG_FILENAME: str = "daemon_poe2.log"
DAEMON_BOOT_ERROR_FILENAME: str = "daemon_poe2_boot_error.json"
DEFAULT_DAEMON_HOST: str = "127.0.0.1"
DEFAULT_DAEMON_IDLE_SEC: int = 1800

# Environment variable registry (POB2_* ONLY — never POB_*)
ENV_RUNTIME_DIR: str = "POB2_RUNTIME_DIR"
ENV_HOME: str = "POB2_HOME"
ENV_CACHE_DIR: str = "POB2_CACHE_DIR"
ENV_ENGINE_DIR: str = "POB2_ENGINE_DIR"
ENV_ROOT: str = "POB2_ROOT"
ENV_DATA_DIR: str = "POB2_DATA_DIR"
ENV_LEGACY_DIR: str = "POB2_LEGACY_DIR"
ENV_LANG: str = "POB2_LANG"
ENV_SLOT: str = "POB2_SLOT"
ENV_OFFLINE: str = "POB2_OFFLINE"
ENV_PROXY: str = "POB2_PROXY"
ENV_GITHUB_MIRROR: str = "POB2_GITHUB_MIRROR"
ENV_LUAJIT: str = "POB2_LUAJIT"
ENV_ENGINE_CMD: str = "POB2_ENGINE_CMD"
ENV_DAEMON_HOST: str = "POB2_DAEMON_HOST"
ENV_DAEMON_PORT: str = "POB2_DAEMON_PORT"
ENV_DAEMON_IDLE_SEC: str = "POB2_DAEMON_IDLE_SEC"
ENV_OAUTH_CLIENT_ID: str = "POB2_OAUTH_CLIENT_ID"
ENV_TEST_PERF_SLACK: str = "POB2_TEST_PERF_SLACK"

# PoE2 0.5.5 non-contiguous class integer IDs (TreeData/0_5/tree.lua -> PassiveTree.lua)
POE2_CLASS_IDS: tuple[int, ...] = (1, 2, 6, 7, 8, 9, 10, 11)
POE2_CLASS_NAMES: dict[int, str] = {
    1: "Witch",
    2: "Ranger",
    6: "Warrior",
    7: "Sorceress",
    8: "Huntress",
    9: "Mercenary",
    10: "Monk",
    11: "Druid",
}
POE2_CLASS_NAME_TO_ID: dict[str, int] = {name.lower(): cid for cid, name in POE2_CLASS_NAMES.items()}

# Attribution notice mandated by GPLv3 §7(b)
POE2BRAIN_ATTRIBUTION: str = (
    "Based on poe2brain by lifan-builds (https://github.com/lifan-builds/poe2brain)"
)

__all__ = [
    "APP_DIR_NAME",
    "CLI_NAME",
    "DAEMON_BOOT_ERROR_FILENAME",
    "DAEMON_LOCK_FILENAME",
    "DAEMON_LOG_FILENAME",
    "DAEMON_STATE_FILENAME",
    "DATA_SUBDIR",
    "DEFAULT_DAEMON_HOST",
    "DEFAULT_DAEMON_IDLE_SEC",
    "ENGINE_SUBDIR",
    "ENV_CACHE_DIR",
    "ENV_DAEMON_HOST",
    "ENV_DAEMON_IDLE_SEC",
    "ENV_DAEMON_PORT",
    "ENV_DATA_DIR",
    "ENV_ENGINE_CMD",
    "ENV_ENGINE_DIR",
    "ENV_GITHUB_MIRROR",
    "ENV_HOME",
    "ENV_LANG",
    "ENV_LEGACY_DIR",
    "ENV_LUAJIT",
    "ENV_OAUTH_CLIENT_ID",
    "ENV_OFFLINE",
    "ENV_PROXY",
    "ENV_ROOT",
    "ENV_RUNTIME_DIR",
    "ENV_SLOT",
    "ENV_TEST_PERF_SLACK",
    "GAME_ID",
    "GAME_NAME",
    "LICENSE_SPDX",
    "PACKAGE_NAME",
    "POE2BRAIN_ATTRIBUTION",
    "POE2_CLASS_IDS",
    "POE2_CLASS_NAMES",
    "POE2_CLASS_NAME_TO_ID",
    "TOKEN_HEADER",
    "XML_ROOT_TAG",
    "default_slot",
    "detect_checkout_pkg_root",
    "env_str",
    "has_runtime_data",
    "is_offline",
    "resolve_cache_dir",
    "resolve_data_dir_override",
    "resolve_engine_dir_override",
    "resolve_runtime_dir",
    "stamp_game",
]


def env_str(name: str, default: str = "") -> str:
    """Read a trimmed string value from ``os.environ``."""
    return os.environ.get(name, default).strip()


def is_offline() -> bool:
    """Return True when ``POB2_OFFLINE`` enables offline-only execution."""
    return env_str(ENV_OFFLINE).lower() in ("1", "true", "yes", "on")


def default_slot() -> str:
    """Return normalized default build slot slug from ``POB2_SLOT`` (fallback ``'main'``)."""
    val = env_str(ENV_SLOT)
    return (val or "main").lower()


def stamp_game(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``payload`` stamped with ``"game": "poe2"``."""
    out = dict(payload)
    out["game"] = GAME_ID
    return out


def detect_checkout_pkg_root() -> Path | None:
    """Return ``<repo>/pob2_cli`` when running from a source checkout, else None."""
    if getattr(sys, "frozen", False):
        return None
    try:
        parents = Path(__file__).resolve().parents
        if len(parents) > 2:
            pkg_root = parents[2]
            if (pkg_root / "pyproject.toml").is_file() and (pkg_root / "src" / "pob2_cli").is_dir():
                return pkg_root
    except OSError:
        return None
    return None


def has_runtime_data(path: Path) -> bool:
    """Return True when ``path`` contains initialized PoE2 runtime state."""
    try:
        if not path.is_dir():
            return False
        for marker in ("engine", DAEMON_STATE_FILENAME, "update_state.json", "cache", "config.json"):
            if (path / marker).exists():
                return True
        return any(path.glob("slot_*.json"))
    except OSError:
        return False


def resolve_runtime_dir(runtime_dir: Path | None = None) -> Path:
    """Resolve the persistent PoE2 runtime directory with zero overlap with PoE1.

    Priority:
      1. Explicit ``runtime_dir`` argument.
      2. ``POB2_RUNTIME_DIR`` environment variable.
      3. ``POB2_HOME`` environment variable (``<POB2_HOME>/.runtime``).
      4. Source checkout ``<repo>/pob2_cli/.runtime`` (gitignored by ``.runtime/``;
         physically separate from PoE1 ``<repo>/.runtime``).
      5. OS user app data dir:
         - Windows: ``%LOCALAPPDATA%/pob24agents`` (fallback ``~/.pob24agents``)
         - POSIX: ``$XDG_DATA_HOME/pob24agents`` (fallback ``~/.local/share/pob24agents``)
    """
    if runtime_dir is not None:
        return Path(runtime_dir)

    env_rt = env_str(ENV_RUNTIME_DIR)
    if env_rt:
        return Path(env_rt)

    env_home = env_str(ENV_HOME)
    if env_home:
        return Path(env_home) / ".runtime"

    pkg_root = detect_checkout_pkg_root()
    if pkg_root is not None:
        return pkg_root / ".runtime"

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / APP_DIR_NAME
        return Path.home() / f".{APP_DIR_NAME}"

    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return Path(xdg_data) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def resolve_cache_dir(runtime_dir: Path | None = None) -> Path:
    """Resolve the PoE2 cache directory (``POB2_CACHE_DIR`` or ``<runtime_dir>/cache``)."""
    env_cache = env_str(ENV_CACHE_DIR)
    if env_cache:
        return Path(env_cache)
    return resolve_runtime_dir(runtime_dir) / "cache"


def resolve_engine_dir_override() -> Path | None:
    """Return explicit external engine dir from ``POB2_ENGINE_DIR`` or ``POB2_ROOT``."""
    raw = env_str(ENV_ENGINE_DIR) or env_str(ENV_ROOT)
    return Path(raw) if raw else None


def resolve_data_dir_override() -> Path | None:
    """Return explicit external translation/data dir from ``POB2_DATA_DIR``."""
    raw = env_str(ENV_DATA_DIR)
    return Path(raw) if raw else None
