"""PoE2 runtime directory, slim engine root, data dir, and OS command resolution.

Windows ALWAYS launches via native ctypes ``win_lua_host.py`` loading ``PoE2/lua51.dll``
(zero ``pob-zh.exe``). POSIX ALWAYS requires the pinned LuaJIT ``2460b3f`` binary under
``<engine_dir>/bin/luajit`` or explicit ``POB2_LUAJIT`` (never stock ``luajit`` on PATH).
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..game_profile import (
    ENGINE_SUBDIR,
    ENV_ENGINE_CMD,
    ENV_LEGACY_DIR,
    ENV_LUAJIT,
    detect_checkout_pkg_root,
    env_str,
    resolve_cache_dir,
    resolve_data_dir_override,
    resolve_engine_dir_override,
    resolve_runtime_dir,
)
from .constants import SEED_DATA_SUBDIRS, SEED_POE2_SUBDIRS, UpdateError


def _pkg() -> Any:
    return sys.modules["pob2_cli.updater"]


def is_windows() -> bool:
    return sys.platform == "win32"


def get_runtime_dir(runtime_dir: Path | None = None) -> Path:
    return resolve_runtime_dir(runtime_dir)


def get_posix_platform_tag() -> str:
    os_part = "darwin" if sys.platform == "darwin" else "linux"
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch_part = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch_part = "x64"
    else:
        arch_part = machine or "x64"
    return f"{os_part}-{arch_part}"


def get_bundled_headless_runner_path() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "pob2_cli" / "lua" / "headless_runner.lua"
        if cand.is_file():
            return cand
    return Path(__file__).resolve().parents[1] / "lua" / "headless_runner.lua"


def resolve_game_dir(engine_dir: Path) -> Path:
    """Resolve the PoE2 Lua tree dir from either a bundle root (``<root>/PoE2``) or direct ``PoE2/``."""
    eng = Path(engine_dir)
    sub = eng / ENGINE_SUBDIR
    if sub.is_dir():
        return sub
    if (eng / "Launch.lua").is_file() and (eng / "GameVersions.lua").is_file():
        return eng
    return sub


def resolve_data_root(engine_dir: Path, data_dir: Path | None = None) -> Path:
    """Resolve the ``Data`` root holding ``bridge/bridge.lua`` and ``poe2/{zh-rCN,zh-rTW}``."""
    explicit = Path(data_dir) if data_dir is not None else resolve_data_dir_override()
    if explicit is not None:
        if explicit.name.lower() == "poe2" and (explicit.parent / "bridge").is_dir():
            return explicit.parent
        return explicit
    eng = Path(engine_dir)
    if (eng / "Data").is_dir():
        return eng / "Data"
    if (eng.parent / "Data").is_dir():
        return eng.parent / "Data"
    return eng / "Data"


def resolve_bridge_lua(engine_dir: Path, data_dir: Path | None = None) -> Path:
    """Locate ``bridge.lua`` under ``Data/bridge/`` (supports both root and ``POB2_DATA_DIR``)."""
    data_root = resolve_data_root(engine_dir, data_dir=data_dir)
    for cand in (
        data_root / "bridge" / "bridge.lua",
        Path(engine_dir) / "Data" / "bridge" / "bridge.lua",
        Path(engine_dir).parent / "Data" / "bridge" / "bridge.lua",
    ):
        if cand.is_file():
            return cand
    return data_root / "bridge" / "bridge.lua"


def resolve_luajit_binary(engine_dir: Path | None = None) -> Path | None:
    """Locate pinned PoB2 LuaJIT binary on POSIX (ONLY ``POB2_LUAJIT`` or ``<engine>/bin/luajit``).

    Never falls back to stock ``luajit`` on ``PATH`` because PoB2 requires compound assignment
    (``+=`` / ``..=``) from pinned commit ``2460b3f``.
    """
    env_lj = env_str(ENV_LUAJIT)
    if env_lj:
        p = Path(env_lj)
        if p.is_file():
            return p
        found = shutil.which(env_lj)
        return Path(found) if found else None
    if engine_dir is not None:
        eng = Path(engine_dir)
        for cand in (eng / "bin" / "luajit", eng.parent / "bin" / "luajit"):
            if cand.is_file():
                return cand
    return None


def is_valid_engine_dir(path: Path | None, *, data_dir: Path | None = None) -> bool:
    """Return True when ``path`` provides a runnable PoE2 slim engine for the current OS."""
    if path is None:
        return False
    eng = Path(path)
    game_dir = resolve_game_dir(eng)
    if not game_dir.is_dir() or not (game_dir / "Launch.lua").is_file():
        return False
    bridge_lua = resolve_bridge_lua(eng, data_dir=data_dir)
    if not bridge_lua.is_file():
        return False
    if env_str(ENV_ENGINE_CMD):
        return True
    if _pkg().is_windows():
        return (game_dir / "lua51.dll").is_file() and (game_dir / "zlib1.dll").is_file()
    return resolve_luajit_binary(eng) is not None


def build_engine_command(
    engine_dir: Path,
    *,
    data_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build native headless PoB2 launch command and isolated child env (zero ``pob-zh.exe``)."""
    eng = Path(engine_dir)
    game_dir = resolve_game_dir(eng)
    data_root = resolve_data_root(eng, data_dir=data_dir)
    bridge_lua = resolve_bridge_lua(eng, data_dir=data_dir)
    runner_lua = get_bundled_headless_runner_path()
    on_windows = bool(_pkg().is_windows())

    if not game_dir.is_dir() or not (game_dir / "Launch.lua").is_file():
        raise UpdateError(f"PoE2 engine directory not found or missing Launch.lua at {game_dir}")
    if not bridge_lua.is_file():
        raise UpdateError(f"PoB2 bridge.lua not found at {bridge_lua}")
    if not runner_lua.is_file():
        raise UpdateError(f"Bundled PoB2 headless_runner.lua not found at {runner_lua}")

    rt_dir = get_runtime_dir(runtime_dir)
    user_dir = rt_dir / "pob2_user"
    extra_env = {
        "POB_ZH_HEADLESS": "1",
        "POB_GAME": "poe2",
        "POB2_ENGINE_ROOT": str(eng),
        "POB_ENGINE_ROOT": str(data_root.parent if data_root.name.lower() == "data" else eng),
        "POB2_DATA_ROOT": str(data_root),
        "POB2_USER_PATH": str(user_dir),
        "POB2_READONLY_GAME_DIR": "1",
    }
    if not getattr(sys, "frozen", False):
        src_root = str(Path(__file__).resolve().parents[2])
        prev_pp = os.environ.get("PYTHONPATH", "").strip()
        extra_env["PYTHONPATH"] = f"{src_root}{os.pathsep}{prev_pp}" if prev_pp else src_root

    override = env_str(ENV_ENGINE_CMD)
    if override:
        rendered = override.format(
            engine_dir=str(eng),
            game_dir=str(game_dir),
            data_dir=str(data_root),
            bridge_lua=str(bridge_lua),
            runner_lua=str(runner_lua),
        )
        cmd = shlex.split(rendered, posix=not on_windows)
        if not cmd:
            raise UpdateError(f"{ENV_ENGINE_CMD} resolved to an empty command")
        return cmd, extra_env

    if on_windows:
        if not ((game_dir / "lua51.dll").is_file() and (game_dir / "zlib1.dll").is_file()):
            raise UpdateError(
                f"Windows native Slim host requires PoE2/lua51.dll + PoE2/zlib1.dll under {game_dir} "
                "(pob-zh.exe is intentionally unused in pob2)."
            )
        host_prefix = (
            [sys.executable, "_lua-host"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "pob2_cli", "_lua-host"]
        )
        return [*host_prefix, str(runner_lua), str(game_dir), str(bridge_lua)], extra_env

    luajit_bin = resolve_luajit_binary(eng)
    if luajit_bin is None:
        raise UpdateError(
            f"No pinned PoB2 LuaJIT runtime at {eng / 'bin' / 'luajit'} (stock luajit lacks += syntax). "
            f"Run `pob2 bootstrap` or set {ENV_LUAJIT}."
        )
    return [str(luajit_bin), str(runner_lua), str(game_dir), str(bridge_lua)], extra_env


def _find_legacy_dir(legacy_dir: Path | None = None) -> Path | None:
    if legacy_dir is not None:
        p = Path(legacy_dir)
        return p if is_valid_engine_dir(p) else None
    raw = env_str(ENV_LEGACY_DIR)
    if raw:
        p = Path(raw)
        return p if is_valid_engine_dir(p) else None
    return None


def resolve_engine_root(runtime_dir: Path | None = None, legacy_dir: Path | None = None) -> Path:
    """Resolve the active PoE2 engine root (``POB2_ENGINE_DIR``/``POB2_ROOT`` -> managed -> legacy)."""
    env_override = resolve_engine_dir_override()
    if env_override is not None:
        if is_valid_engine_dir(env_override):
            return env_override
        return env_override
    managed = get_runtime_dir(runtime_dir) / "engine"
    if is_valid_engine_dir(managed):
        return managed
    leg = _find_legacy_dir(legacy_dir)
    if leg is not None:
        return leg
    return managed


def get_shared_cache_dir(runtime_dir: Path | None = None) -> Path:
    """Resolve PoE2 isolated cache directory (never writes inside external read-only seed dirs)."""
    cache_dir = resolve_cache_dir(runtime_dir)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir
    except OSError:
        tmp_fb = Path(tempfile.gettempdir()) / "pob24agents-cache"
        tmp_fb.mkdir(parents=True, exist_ok=True)
        return tmp_fb


def seed_from_legacy_dir(legacy_dir: Path, target_dir: Path) -> bool:
    """Copy ONLY slim PoE2 + Data/{bridge,poe2} files from read-only ``legacy_dir`` into ``target_dir``."""
    src = Path(legacy_dir)
    dst = Path(target_dir)
    if is_valid_engine_dir(dst) or not is_valid_engine_dir(src):
        return False

    src_game = resolve_game_dir(src)
    dst_game = dst / ENGINE_SUBDIR
    dst_game.mkdir(parents=True, exist_ok=True)
    for item in src_game.iterdir():
        if item.is_file() and (
            item.suffix.lower() in (".lua", ".xml")
            or item.name.lower() in ("lua51.dll", "zlib1.dll", "lua-utf8.dll", "vcruntime140.dll", "msvcp140.dll")
        ):
            shutil.copy2(item, dst_game / item.name)
    for dname in SEED_POE2_SUBDIRS:
        s_sub = src_game / dname
        if s_sub.is_dir():
            shutil.copytree(s_sub, dst_game / dname, dirs_exist_ok=True)

    src_data = resolve_data_root(src)
    dst_data = dst / "Data"
    dst_data.mkdir(parents=True, exist_ok=True)
    for dname in SEED_DATA_SUBDIRS:
        s_sub = src_data / dname
        if s_sub.is_dir():
            shutil.copytree(s_sub, dst_data / dname, dirs_exist_ok=True)
    if (src_data / "translations_version.json").is_file():
        shutil.copy2(src_data / "translations_version.json", dst_data / "translations_version.json")
    if (src / "bin").is_dir():
        shutil.copytree(src / "bin", dst / "bin", dirs_exist_ok=True)
    return is_valid_engine_dir(dst)


def detect_source_checkout_root() -> Path | None:
    pkg = detect_checkout_pkg_root()
    return pkg.parent if pkg is not None else None
