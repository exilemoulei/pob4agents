"""Windows native ctypes LuaJIT host embedding Path of Building (PoE2) lua51.dll.

Invoked via ``pob2 _lua-host <runner_lua> <game_dir> [bridge_lua ...]`` (or
``python -m pob2_cli _lua-host ...``) before importing Click, giving Windows a
zero-``pob-zh.exe`` Slim LuaJIT headless runtime identical to POSIX.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Sequence
from ctypes import POINTER, byref, c_char, c_char_p, c_int, c_size_t, c_void_p
from pathlib import Path
from typing import Any

LUA_GLOBALSINDEX = -10002
LUA_TSTRING = 4
LUA_TTABLE = 5


class LuaHostError(RuntimeError):
    """Raised when loading or running PoE2/lua51.dll fails."""


def _configure_lua_dll(dll: ctypes.CDLL) -> ctypes.CDLL:
    """Bind the minimal Lua 5.1 / LuaJIT C API prototypes needed to host headless_runner.lua."""
    dll.luaL_newstate.restype = c_void_p
    dll.luaL_openlibs.argtypes = [c_void_p]
    dll.lua_close.argtypes = [c_void_p]

    dll.luaL_loadbuffer.argtypes = [c_void_p, c_char_p, c_size_t, c_char_p]
    dll.luaL_loadbuffer.restype = c_int
    dll.lua_pcall.argtypes = [c_void_p, c_int, c_int, c_int]
    dll.lua_pcall.restype = c_int

    dll.lua_getfield.argtypes = [c_void_p, c_int, c_char_p]
    dll.lua_setfield.argtypes = [c_void_p, c_int, c_char_p]
    dll.lua_createtable.argtypes = [c_void_p, c_int, c_int]
    dll.lua_rawseti.argtypes = [c_void_p, c_int, c_int]

    dll.lua_pushlstring.argtypes = [c_void_p, c_char_p, c_size_t]
    dll.lua_pushboolean.argtypes = [c_void_p, c_int]

    dll.lua_type.argtypes = [c_void_p, c_int]
    dll.lua_type.restype = c_int
    dll.lua_tolstring.argtypes = [c_void_p, c_int, POINTER(c_size_t)]
    dll.lua_tolstring.restype = POINTER(c_char)
    dll.lua_gettop.argtypes = [c_void_p]
    dll.lua_gettop.restype = c_int
    dll.lua_settop.argtypes = [c_void_p, c_int]
    return dll


def _read_lua_string(dll: ctypes.CDLL, state: c_void_p, idx: int) -> str:
    length = c_size_t(0)
    ptr = dll.lua_tolstring(state, idx, byref(length))
    if not ptr:
        return ""
    return ctypes.string_at(ptr, length.value).decode("utf-8", errors="replace")


def _push_utf8(dll: ctypes.CDLL, state: c_void_p, text: str) -> None:
    raw = text.encode("utf-8")
    dll.lua_pushlstring(state, raw, len(raw))


def _load_lua51_dll(game_dir: Path, dll_path: Path | None = None) -> tuple[ctypes.CDLL, Any]:
    """Load ``PoE2/lua51.dll`` (preloading sibling CRT DLLs when present)."""
    resolved_dir = game_dir.resolve()
    target_dll = (dll_path or (resolved_dir / "lua51.dll")).resolve()
    if not target_dll.is_file():
        raise LuaHostError(f"lua51.dll not found at {target_dll}")

    dll_dir_handle: Any = None
    add_dll_dir = getattr(os, "add_dll_directory", None)
    if callable(add_dll_dir):
        dll_dir_handle = add_dll_dir(str(target_dll.parent))

    for crt_name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
        crt_path = target_dll.parent / crt_name
        if crt_path.is_file():
            try:
                ctypes.CDLL(str(crt_path))
            except OSError:
                pass

    try:
        dll = ctypes.CDLL(str(target_dll))
        configured = _configure_lua_dll(dll)
    except Exception as exc:
        if dll_dir_handle is not None:
            dll_dir_handle.close()
        raise LuaHostError(f"Failed to load or bind {target_dll}: {exc}") from exc

    return configured, dll_dir_handle


def _populate_lua_globals(
    dll: ctypes.CDLL,
    state: c_void_p,
    runner_lua: Path,
    script_args: Sequence[str],
) -> None:
    dll.lua_createtable(state, len(script_args), 1)
    _push_utf8(dll, state, str(runner_lua))
    dll.lua_rawseti(state, -2, 0)
    for idx, val in enumerate(script_args, start=1):
        _push_utf8(dll, state, val)
        dll.lua_rawseti(state, -2, idx)
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"arg")

    dll.lua_pushboolean(state, 1)
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"__POB2_WIN_LUA_HOST")
    dll.lua_pushboolean(state, 1)
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"__POB_WIN_LUA_HOST")

    _push_utf8(dll, state, runner_lua.resolve().parent.as_posix())
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"__POB_LUA_DIR")


def _push_traceback_handler(dll: ctypes.CDLL, state: c_void_p) -> int:
    dll.lua_getfield(state, LUA_GLOBALSINDEX, b"debug")
    if dll.lua_type(state, -1) != LUA_TTABLE:
        dll.lua_settop(state, -2)
        return 0
    dll.lua_getfield(state, -1, b"traceback")
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"__POB_HOST_TB_TMP")
    dll.lua_settop(state, -2)
    dll.lua_getfield(state, LUA_GLOBALSINDEX, b"__POB_HOST_TB_TMP")
    return dll.lua_gettop(state)


def run_lua_script(
    game_dir: Path,
    runner_lua: Path,
    script_args: Sequence[str] = (),
    *,
    dll_path: Path | None = None,
) -> int:
    """Execute ``runner_lua`` inside ``game_dir/lua51.dll`` with ``arg[1..N] = script_args``."""
    runner_resolved = Path(runner_lua).resolve()
    if not runner_resolved.is_file():
        raise LuaHostError(f"Lua runner script not found at {runner_resolved}")

    game_dir_resolved = Path(game_dir).resolve()
    norm_args = list(script_args)
    if norm_args and Path(norm_args[0]) == Path(game_dir):
        norm_args[0] = str(game_dir_resolved)

    dll, dll_dir_handle = _load_lua51_dll(game_dir_resolved, dll_path=dll_path)
    state = dll.luaL_newstate()
    if not state:
        if dll_dir_handle is not None:
            dll_dir_handle.close()
        raise LuaHostError("luaL_newstate() returned NULL")

    try:
        dll.luaL_openlibs(state)
        _populate_lua_globals(dll, state, runner_resolved, norm_args)
        sys.stdout.flush()
        sys.stderr.flush()

        raw_script = runner_resolved.read_bytes()
        chunk_name = f"@{runner_resolved.as_posix()}".encode()
        err_idx = _push_traceback_handler(dll, state)

        rc = dll.luaL_loadbuffer(state, raw_script, len(raw_script), chunk_name)
        if rc != 0:
            err_msg = _read_lua_string(dll, state, -1)
            sys.stderr.write(f"win_lua_host load error: {err_msg}\n")
            sys.stderr.flush()
            return 1

        rc = dll.lua_pcall(state, 0, 0, err_idx)
        if rc != 0:
            err_msg = _read_lua_string(dll, state, -1)
            sys.stderr.write(f"win_lua_host runtime error: {err_msg}\n")
            sys.stderr.flush()
            return 1
        return 0
    finally:
        dll.lua_close(state)
        if dll_dir_handle is not None:
            dll_dir_handle.close()


def _parse_host_args(args: Sequence[str]) -> tuple[Path | None, Path, Path, list[str]]:
    idx = 0
    dll_override: Path | None = None
    items = list(args)
    if len(items) >= 2 and items[0] == "--dll":
        dll_override = Path(items[1])
        idx = 2
    positional = items[idx:]
    if len(positional) < 2:
        raise ValueError("Usage: pob2 _lua-host [--dll <lua51.dll>] <runner_lua> <game_dir> [bridge_lua ...]")
    runner_lua = Path(positional[0])
    game_dir = Path(positional[1])
    script_args = positional[1:]
    if dll_override is None:
        env_dll = os.environ.get("POB2_LUA51_DLL", "").strip()
        if env_dll:
            dll_override = Path(env_dll)
    return dll_override, runner_lua, game_dir, script_args


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``pob2 _lua-host <runner_lua> <game_dir> [bridge_lua ...]``."""
    if argv is None:
        raw_args = list(sys.argv[2:]) if sys.argv[1:2] == ["_lua-host"] else list(sys.argv[1:])
    else:
        raw_args = list(argv)

    try:
        dll_override, runner_lua, game_dir, script_args = _parse_host_args(raw_args)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.stderr.flush()
        return 2

    try:
        return run_lua_script(game_dir, runner_lua, script_args, dll_path=dll_override)
    except LuaHostError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
