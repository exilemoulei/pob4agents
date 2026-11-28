#!/usr/bin/env bash
# Install or upgrade pob24agents (`pob2`, Path of Exile 2) on macOS or Linux.
#   curl -fsSL https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install-poe2.sh | bash
# Discovers latest version via bucket/pob2.json (never /releases/latest or api.github.com).
# Verifies Ed25519 signature + SHA256SUMS.txt; falls back to wheel/sdist if standalone does not run.
# Env: POB2_VERSION, POB2_INSTALL_DIR, POB2_BIN_DIR, POB2_PROXY, POB2_GITHUB_MIRROR,
#      POB2_SKIP_SIGNATURE=1, POB2_FROM_SOURCE=1, POB2_BRANCH, POB2_VERIFY_WITH.
# Self-test: ./install-poe2.sh --self-test

set -euo pipefail

REPO="exilemoulei/pob4agents"
CHANNEL_MANIFEST_URL="https://raw.githubusercontent.com/$REPO/main/bucket/pob2.json"
# Release signing keys: base64 raw Ed25519 public keys, strictly matching RELEASE_PUBLIC_KEYS in
# pob2_cli/src/pob2_cli/updater/signature.py.
RELEASE_PUBLIC_KEYS=(fvwea+k3NZ54EzRFCoMYgvZLy3XTkKGXxo2UPmsNZIM=)
TAG_PATTERN='^poe2-v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'

say() { printf '[pob2-install] %s\n' "$*"; }
die() { printf '[pob2-install] ERROR: %s\n' "$*" >&2; exit 1; }

# --- Downloads ---------------------------------------------------------------------------------

github_url() {
  if [ -n "${POB2_GITHUB_MIRROR:-}" ]; then printf '%s/%s' "${POB2_GITHUB_MIRROR%/}" "$1"; else printf '%s' "$1"; fi
}

curl_args() {
  printf '%s\n' -fsSL --retry 2 --connect-timeout 15 -A pob2-installer/1.0
  if [ -n "${POB2_PROXY:-}" ]; then printf '%s\n' -x "$POB2_PROXY"; fi
}

fetch() { # url out-file
  local args=()
  while IFS= read -r a; do args+=("$a"); done < <(curl_args)
  curl "${args[@]}" -o "$2" "$1"
}

latest_tag() {
  local args=() url body ver
  while IFS= read -r a; do args+=("$a"); done < <(curl_args)
  url="$(github_url "$CHANNEL_MANIFEST_URL")"
  body="$(curl "${args[@]}" "$url")" || die "Could not fetch $url. Set POB2_VERSION or POB2_PROXY."
  ver="$(printf '%s\n' "$body" | sed -nE 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -n 1)"
  if [ -z "$ver" ]; then
    die "Could not parse version from bucket/pob2.json ($url). Set POB2_VERSION or POB2_PROXY."
  fi
  printf 'poe2-v%s' "$ver"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# --- Ed25519 (RFC 8032) ------------------------------------------------------------------------

PY_ED25519='
import base64, hashlib, sys
P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)
def add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % P
    b = (p[1] + p[0]) * (q[1] + q[0]) % P
    c = 2 * p[3] * q[3] * D % P
    d = 2 * p[2] * q[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)
def mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = add(q, p)
        p = add(p, p)
        s >>= 1
    return q
def recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * I % P
    if (x * x - x2) % P:
        return None
    return P - x if (x & 1) != sign else x
def point(raw):
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)
def verify(pk, msg, sig):
    if len(pk) != 32 or len(sig) != 64:
        return False
    a, r, s = point(pk), point(sig[:32]), int.from_bytes(sig[32:], "little")
    if a is None or r is None or s >= L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pk + msg).digest(), "little") % L
    gy = 4 * pow(5, P - 2, P) % P
    gx = recover_x(gy, 0)
    u, v = mul(s, (gx, gy, 1, gx * gy % P)), add(r, mul(h, a))
    return (u[0] * v[2] - v[0] * u[2]) % P == 0 and (u[1] * v[2] - v[1] * u[2]) % P == 0
try:
    pk, sig = base64.b64decode(sys.argv[1]), base64.b64decode(sys.argv[3])
    with open(sys.argv[2], "rb") as fh:
        ok = verify(pk, fh.read(), sig)
except Exception:
    ok = False
sys.exit(0 if ok else 1)
'

verify_openssl() { # key-b64 msg-file sig-b64
  local d rc=0
  d="$(mktemp -d)"
  {
    printf '%s\n' '-----BEGIN PUBLIC KEY-----'
    { printf '\060\052\060\005\006\003\053\145\160\003\041\000'; printf '%s' "$1" | openssl base64 -d -A; } | openssl base64 -A
    printf '\n%s\n' '-----END PUBLIC KEY-----'
  } >"$d/pub.pem" 2>/dev/null || rc=1
  printf '%s' "$3" | openssl base64 -d -A >"$d/sig" 2>/dev/null || rc=1
  if [ "$rc" -eq 0 ]; then
    openssl pkeyutl -verify -pubin -inkey "$d/pub.pem" -rawin -in "$2" -sigfile "$d/sig" >/dev/null 2>&1 || rc=1
  fi
  rm -rf "$d"
  return "$rc"
}

verify_python3() { python3 -I -c "$PY_ED25519" "$1" "$2" "$3" >/dev/null 2>&1; }

RFC_KEYS=(11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo= PUAXw+hDiVqStwqnTRt+vJyYLM8uxJaMwM1V8Sr0Zgw= /FHNjmIYoaONpH7QAjDwWAgW7RO6MwOsXeuRFUiQgCU=)
RFC_MSGS=('' '\162' '\257\202')
RFC_SIGS=(
  5VZDAMNgrHKQhuLMgG6CioSHfx645dl02HPgZSJJAVVfuIIVkKM7rMYeOXAc+bRr0lv18FlbviRlUUFDjnoQCw==
  kqAJqfDUyrhyDoILX2QlQKKye1QWUD+Ps3YiI+vbadoIWsHkPhWZbkWPNhPQ8R2MOHsurrQwKu6wDSkWErsMAA==
  YpHWV97sJAJIJ+acOr4BowzlSKKEdDpEXjaA19taw6wY/5tTjRbykK5n92CYTcZZSnwV6XFu0o3AJ77O6h7ECg==
)

backend_passes_rfc() { # backend
  local d i ok=0
  d="$(mktemp -d)"
  for i in 0 1 2; do
    # shellcheck disable=SC2059
    printf "${RFC_MSGS[i]}" >"$d/m"
    # shellcheck disable=SC2059
    printf "${RFC_MSGS[i]}x" >"$d/bad"
    "verify_$1" "${RFC_KEYS[i]}" "$d/m" "${RFC_SIGS[i]}" || ok=1
    if "verify_$1" "${RFC_KEYS[i]}" "$d/bad" "${RFC_SIGS[i]}"; then ok=1; fi
  done
  rm -rf "$d"
  return "$ok"
}

python3_usable() {
  command -v python3 >/dev/null 2>&1 || return 1
  if [ "$(uname -s)" = Darwin ] && [ "$(command -v python3)" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
    return 1
  fi
  python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 6))' >/dev/null 2>&1
}

pick_backend() {
  local candidates=() b
  if [ -n "${POB2_VERIFY_WITH:-}" ]; then candidates=("$POB2_VERIFY_WITH"); else candidates=(openssl python3); fi
  for b in "${candidates[@]}"; do
    case "$b" in
      openssl) command -v openssl >/dev/null 2>&1 || continue ;;
      python3) python3_usable || continue ;;
      *) continue ;;
    esac
    if backend_passes_rfc "$b"; then printf '%s' "$b"; return 0; fi
  done
  return 0
}

self_test() {
  local b found=0
  for b in openssl python3; do
    if [ -n "${POB2_VERIFY_WITH:-}" ] && [ "$b" != "$POB2_VERIFY_WITH" ]; then continue; fi
    if [ "$b" = openssl ] && ! command -v openssl >/dev/null 2>&1; then say "$b: not installed"; continue; fi
    if [ "$b" = python3 ] && ! python3_usable; then say "$b: not usable"; continue; fi
    if backend_passes_rfc "$b"; then say "$b: RFC 8032 vectors pass"; found=1; else say "$b: cannot verify Ed25519"; fi
  done
  [ "$found" -eq 1 ] || die "No Ed25519 verifier on this machine (install OpenSSL 3 or python3)."
  say "Ed25519 self-test passed."
}

verify_sums_signature() { # sums-file sig-file backend
  local sig key
  sig="$(tr -d ' \r\n' <"$2")"
  for key in ${RELEASE_PUBLIC_KEYS[@]+"${RELEASE_PUBLIC_KEYS[@]}"}; do
    if "verify_$3" "$key" "$1" "$sig"; then return 0; fi
  done
  return 1
}

# --- Python package (fallback, and POB2_BRANCH / POB2_FROM_SOURCE) -----------------------------

install_python_package() { # pip-style requirement
  command -v python3 >/dev/null 2>&1 || die "python3 (>=3.11) is required for this install. On macOS: brew install python@3.13"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11+ is required (found $(python3 --version))."
  if command -v uv >/dev/null 2>&1; then
    say "Installing the Python package with uv tool ..."
    uv tool install --force "$1"
  elif command -v pipx >/dev/null 2>&1; then
    say "Installing the Python package with pipx ..."
    pipx install --force "$1"
  else
    say "Installing the Python package into user site-packages with pip ..."
    python3 -m pip install --user --upgrade "$1"
  fi
  say "Installed: $(pob2 --version 2>/dev/null || echo "run 'hash -r', or add ~/.local/bin to PATH")"
}

# --- Standalone build --------------------------------------------------------------------------

platform_tag() {
  local os arch
  case "$(uname -s)" in Darwin) os=macos ;; Linux) os=linux ;; *) return 1 ;; esac
  case "$(uname -m)" in arm64 | aarch64) arch=arm64 ;; x86_64 | amd64) arch=x64 ;; *) return 1 ;; esac
  printf '%s-%s' "$os" "$arch"
}

remove_old_python_installs() {
  if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "^pob24agents "; then
    say "Removing the earlier uv tool install (pob24agents) ..."
    uv tool uninstall pob24agents >/dev/null 2>&1 || true
  fi
  if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q "^pob24agents "; then
    say "Removing the earlier pipx install (pob24agents) ..."
    pipx uninstall pob24agents >/dev/null 2>&1 || true
  fi
}

restore_previous_build() {
  rm -rf "$1"
  if [ -e "$1.old" ]; then mv "$1.old" "$1" || say "Could not restore the previous build; it is in $1.old."; fi
}

install_build() { # archive
  local install_dir bin_dir stage link version moved_link=0
  install_dir="${POB2_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/pob24agents/bin}"
  bin_dir="${POB2_BIN_DIR:-$HOME/.local/bin}"
  stage="${install_dir}.new"
  rm -rf "$stage"
  mkdir -p "$stage" "$bin_dir" || return 2
  tar -xzf "$1" -C "$stage" || { rm -rf "$stage"; return 2; }
  if ! "$stage/pob2" --version >/dev/null 2>&1; then
    rm -rf "$stage"
    return 1
  fi
  if [ -x "$install_dir/pob2" ]; then
    "$install_dir/pob2" stop >/dev/null 2>&1 || true
  fi
  rm -rf "${install_dir}.old"
  if [ -e "$install_dir" ] && ! mv "$install_dir" "${install_dir}.old"; then
    rm -rf "$stage"
    return 2
  fi
  if ! mv "$stage" "$install_dir"; then
    rm -rf "$stage"
    restore_previous_build "$install_dir"
    return 2
  fi

  link="$bin_dir/pob2"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    remove_old_python_installs
    if [ -e "$link" ] && [ ! -L "$link" ]; then
      mv "$link" "$link.old" || { restore_previous_build "$install_dir"; return 2; }
      moved_link=1
      say "Moved the previous $link to $link.old."
    fi
  fi
  if ! ln -sfn "$install_dir/pob2" "$link" || ! version="$("$link" --version 2>/dev/null)"; then
    if [ "$moved_link" -eq 1 ]; then
      rm -f "$link"
      mv "$link.old" "$link" || say "Could not restore the previous $link; it is in $link.old."
    fi
    restore_previous_build "$install_dir"
    return 2
  fi
  rm -rf "${install_dir}.old"
  say "Installed $version to $install_dir"
  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) say "Add $bin_dir to PATH, e.g.: echo 'export PATH=\"$bin_dir:\$PATH\"' >> ~/.profile" ;;
  esac
  local found
  found="$(command -v pob2 2>/dev/null || true)"
  if [ -n "$found" ] && [ "$found" != "$link" ]; then
    say "Note: \`pob2\` currently resolves to $found, ahead of $link in PATH."
  fi
}

main() {
  if [ "${1:-}" = "--self-test" ]; then self_test; return; fi
  command -v curl >/dev/null 2>&1 || die "curl is required."

  if [ -n "${POB2_BRANCH:-}" ]; then
    say "Installing $REPO@$POB2_BRANCH (pob2_cli) from source."
    install_python_package "git+https://github.com/$REPO.git@$POB2_BRANCH#subdirectory=pob2_cli"
    return
  fi

  local tag version base plat name backend wheel sdist rc
  if [ -n "${POB2_VERSION:-}" ]; then
    case "$POB2_VERSION" in
      poe2-v*) tag="$POB2_VERSION" ;;
      v*) tag="poe2-$POB2_VERSION" ;;
      *) tag="poe2-v$POB2_VERSION" ;;
    esac
  else
    tag="$(latest_tag)"
  fi
  [[ "$tag" =~ $TAG_PATTERN ]] || die "'$tag' is not a PoE2 release tag (expected poe2-vX.Y.Z); refusing to install."
  version="${tag#poe2-v}"
  say "Release: $tag"

  backend="$(pick_backend)"
  if [ -z "$backend" ]; then
    if [ -n "${POB2_GITHUB_MIRROR:-}" ]; then
      die "No Ed25519 verifier (OpenSSL 3 or python3) to check the release signature, and a mirror is set; refusing to install."
    fi
    if [ "${POB2_SKIP_SIGNATURE:-}" != 1 ]; then
      die "No Ed25519 verifier (OpenSSL 3 or python3) to check the release signature; refusing to install. On macOS run 'xcode-select --install' or 'brew install openssl@3', then run this again. To install on HTTPS from GitHub and SHA-256 alone, set POB2_SKIP_SIGNATURE=1."
    fi
    say "WARNING: POB2_SKIP_SIGNATURE=1: the release signature of $tag is NOT checked; trusting HTTPS from GitHub and SHA-256 only."
  fi

  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  base="https://github.com/$REPO/releases/download/$tag"

  fetch "$(github_url "$base/SHA256SUMS.txt")" "$tmp/SHA256SUMS.txt" || die "Could not download SHA256SUMS.txt for $tag."
  if [ -n "$backend" ]; then
    fetch "$(github_url "$base/SHA256SUMS.txt.sig")" "$tmp/SHA256SUMS.txt.sig" ||
      die "Could not download SHA256SUMS.txt.sig for $tag; refusing to install an unsigned PoE2 release."
    verify_sums_signature "$tmp/SHA256SUMS.txt" "$tmp/SHA256SUMS.txt.sig" "$backend" ||
      die "SHA256SUMS.txt of $tag is not signed by a pob24agents release key; refusing to install."
    say "Release signature verified (Ed25519, $backend)."
  fi

  sum_of() { awk -v n="$1" '$2 == n || $2 == "*" n { print $1; exit }' "$tmp/SHA256SUMS.txt"; }
  download_checked() {
    local want
    want="$(sum_of "$1")"
    [ -n "$want" ] || return 1
    fetch "$(github_url "$base/$1")" "$tmp/$1" || die "Could not download $1."
    [ "$(sha256_of "$tmp/$1")" = "$want" ] || die "$1 does not match SHA256SUMS.txt; refusing to install."
  }

  if [ -z "${POB2_FROM_SOURCE:-}" ] && plat="$(platform_tag)"; then
    name="pob24agents-$version-$plat.tar.gz"
    if download_checked "$name"; then
      say "Downloaded $name (SHA-256 verified)."
      rc=0
      install_build "$tmp/$name" || rc=$?
      if [ "$rc" -eq 0 ]; then
        say ""
        say "Next step, once: pob2 bootstrap   (behind a proxy: pob2 bootstrap --proxy http://127.0.0.1:7890)"
        return
      fi
      [ "$rc" -eq 1 ] || die "Could not put the new build in place; the previous install was left as it was."
      say "The standalone build does not run on this machine (glibc too old?); installing the Python package."
    else
      say "$tag has no standalone build for $plat; installing the Python package."
    fi
  fi

  wheel="$(awk '$2 ~ /^(\*)?pob24agents-.*-py3-none-any\.whl$/ { sub(/^\*/, "", $2); print $2; exit }' "$tmp/SHA256SUMS.txt")"
  sdist=""
  if [ -n "$(sum_of "pob24agents-$version.tar.gz")" ]; then
    sdist="pob24agents-$version.tar.gz"
  fi
  if [ -n "$wheel" ]; then
    download_checked "$wheel"
    install_python_package "$tmp/$wheel"
  elif [ -n "$sdist" ]; then
    download_checked "$sdist"
    install_python_package "$tmp/$sdist"
  else
    die "SHA256SUMS.txt of $tag lists no wheel or sdist; refusing to install from an unverified source. To build from source on purpose, set POB2_BRANCH=$tag."
  fi
  say ""
  say "Next step, once: pob2 bootstrap   (behind a proxy: pob2 bootstrap --proxy http://127.0.0.1:7890)"
}

main "$@"
