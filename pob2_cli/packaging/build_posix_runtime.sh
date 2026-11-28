#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Build minimal native POSIX headless runtime for pob24agents (Path of Building PoE2):
#   - LuaJIT 2.1 (exact 2460b3f commit supporting PoB2 0.5.5+ compound += / ..= syntax)
#   - lua-utf8.so (starwing/luautf8 0.1.6 single-file C module)
#
# Output:
#   <OUT_DIR>/bin/luajit
#   <OUT_DIR>/bin/lua-utf8.so
#   <OUT_DIR>/bin/runtime.stamp  (matches runtime_pin() in pob2_cli/src/pob2_cli/updater/posix_runtime.py)
#
# Usage:
#   ./build_posix_runtime.sh <OUT_DIR>

set -euo pipefail

OUT_DIR="${1:-./posix-runtime}"
mkdir -p "$OUT_DIR/bin"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

# Keep these four equal to POSIX_* in pob2_cli/src/pob2_cli/updater/constants.py.
LUAJIT_COMMIT="2460b3ff93a1c955de3d62cfc825de7d68dc272e"
LUAJIT_SHA256="5c52d06876d86aa199f9b8cdc65f6f9ac086fdfa8a4489ebf8dc4516e194550b"
LUAUTF8_TAG="0.1.6"
LUAUTF8_SHA256="b76873b4afb09abd00508a66887ae1658492078869f9414c334dea35b5769656"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

check_sha256() {
  local actual
  actual="$(sha256_of "$1")"
  if [ "$actual" != "$2" ]; then
    echo "[pob2-runtime] ERROR: $1 has SHA-256 $actual, expected $2" >&2
    exit 1
  fi
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/pob2-posix-rt.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
echo "[pob2-runtime] Building for ${OS_NAME} (${ARCH_NAME}) into ${OUT_DIR}/bin"

# 1. Fetch & build pinned LuaJIT 2.1 (supporting PoB2 += / ..= syntax)
LUAJIT_TAR="$WORKDIR/luajit.tar.gz"
LUAJIT_SRC="$WORKDIR/luajit-src"
mkdir -p "$LUAJIT_SRC"
curl -fsSL "https://github.com/LuaJIT/LuaJIT/archive/${LUAJIT_COMMIT}.tar.gz" -o "$LUAJIT_TAR"
check_sha256 "$LUAJIT_TAR" "$LUAJIT_SHA256"
tar -xzf "$LUAJIT_TAR" --strip-components=1 -C "$LUAJIT_SRC"

MAKE_ARGS=(
  BUILDMODE=static
)

if [ "$OS_NAME" = "Darwin" ]; then
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
  JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  make -C "$LUAJIT_SRC/src" -j"$JOBS" "${MAKE_ARGS[@]}" amalg
else
  JOBS="$(nproc 2>/dev/null || echo 4)"
  make -C "$LUAJIT_SRC/src" -j"$JOBS" "${MAKE_ARGS[@]}" amalg
fi

cp "$LUAJIT_SRC/src/luajit" "$OUT_DIR/bin/luajit"
chmod +x "$OUT_DIR/bin/luajit"

# 2. Fetch & build starwing/luautf8 (single-file lutf8lib.c -> lua-utf8.so)
LUAUTF8_TAR="$WORKDIR/luautf8.tar.gz"
LUAUTF8_SRC="$WORKDIR/luautf8-src"
mkdir -p "$LUAUTF8_SRC"
curl -fsSL "https://github.com/starwing/luautf8/archive/refs/tags/${LUAUTF8_TAG}.tar.gz" -o "$LUAUTF8_TAR"
check_sha256 "$LUAUTF8_TAR" "$LUAUTF8_SHA256"
tar -xzf "$LUAUTF8_TAR" --strip-components=1 -C "$LUAUTF8_SRC"

CC_BIN="${CC:-cc}"
if [ "$OS_NAME" = "Darwin" ]; then
  "$CC_BIN" -O2 -fPIC -bundle -undefined dynamic_lookup \
    -I"$LUAJIT_SRC/src" \
    "$LUAUTF8_SRC/lutf8lib.c" \
    -o "$OUT_DIR/bin/lua-utf8.so"
else
  "$CC_BIN" -O2 -fPIC -shared \
    -I"$LUAJIT_SRC/src" \
    "$LUAUTF8_SRC/lutf8lib.c" \
    -o "$OUT_DIR/bin/lua-utf8.so"
fi
chmod +x "$OUT_DIR/bin/lua-utf8.so"

# 3. Smoke-test compiled runtime: compound assignment (+= / ..=), lua-utf8, and FFI libz
echo "[pob2-runtime] Running PoB2 runtime self-test..."
LUA_CPATH="$OUT_DIR/bin/?.so;;" "$OUT_DIR/bin/luajit" -e '
  local count = 1
  count += 41
  assert(count == 42, "compound += failed")
  local s = "poe"
  s ..= "2"
  assert(s == "poe2", "compound ..= failed")
  local utf8 = require("lua-utf8")
  assert(utf8.len("精魂") == 2, "lua-utf8 failed")
  local ffi = require("ffi")
  local z
  for _, name in ipairs({ "z", "libz.so.1", "libz.so", "/usr/lib/libz.dylib", "libz.dylib" }) do
    local ok, lib = pcall(ffi.load, name)
    if ok then z = lib; break end
  end
  assert(z, "no system zlib")
  ffi.cdef("const char *zlibVersion(void);")
  print("OK: LuaJIT=" .. jit.version .. " utf8=ok zlib=" .. ffi.string(z.zlibVersion()))
'

# Same format as runtime_pin() in pob2_cli/src/pob2_cli/updater/posix_runtime.py.
printf 'luajit=%s lua-utf8=%s\n' "$LUAJIT_COMMIT" "$LUAUTF8_TAG" > "$OUT_DIR/bin/runtime.stamp"

ls -lh "$OUT_DIR/bin/luajit" "$OUT_DIR/bin/lua-utf8.so" "$OUT_DIR/bin/runtime.stamp"
