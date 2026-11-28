"""POSIX (macOS/Linux) pinned LuaJIT 2460b3f + luautf8 0.1.6 runtime management.

Stock system ``luajit`` is strictly rejected because Path of Building (PoE2) 0.5.5+
uses compound assignment syntax (``+=`` / ``..=``) supported only by the pinned build.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx

from .. import __version__
from ..game_profile import ENV_ENGINE_CMD, ENV_LUAJIT, env_str
from ..os_util import win_hidden_subprocess_kwargs
from .constants import (
    _ASSET_TIMEOUT_SPEC,
    _CHECK_TIMEOUT_SPEC,
    ASSET_MAX_RETRIES,
    POSIX_LUAJIT_COMMIT,
    POSIX_LUAJIT_TARBALL_SHA256,
    POSIX_LUAJIT_TARBALL_URL,
    POSIX_LUAUTF8_TAG,
    POSIX_LUAUTF8_TARBALL_SHA256,
    POSIX_LUAUTF8_TARBALL_URL,
    UpdateError,
    _to_httpx_timeout,
    rewrite_github_url,
)
from .releases import asset_url, latest_release_tag, release_platform_tag, signed_sums

_LOG = logging.getLogger(__name__)

RUNTIME_FILES = (Path("bin") / "luajit", Path("bin") / "lua-utf8.so")
RUNTIME_STAMP = Path("bin") / "runtime.stamp"
_SELF_TEST_LUA = (
    'local c = 1; c += 41; assert(c == 42); local s = "poe"; s ..= "2"; assert(s == "poe2"); '
    'local u = require("lua-utf8"); assert(u.len("精魂") == 2)'
)


def _pkg() -> Any:
    return sys.modules["pob2_cli.updater"]


def runtime_pin() -> str:
    return f"luajit={POSIX_LUAJIT_COMMIT} lua-utf8={POSIX_LUAUTF8_TAG}"


def _runtime_overridden() -> bool:
    return bool(_pkg().is_windows() or env_str(ENV_ENGINE_CMD) or env_str(ENV_LUAJIT))


def posix_runtime_is_current(engine_dir: Path) -> bool:
    if _runtime_overridden():
        return True
    eng = Path(engine_dir)
    if not all((eng / rel).is_file() for rel in RUNTIME_FILES):
        return False
    try:
        return (eng / RUNTIME_STAMP).read_text(encoding="utf-8").strip() == runtime_pin()
    except OSError:
        return False


def _stamped(files: dict[Path, Path], staging_dir: Path, pin: str) -> dict[Path, Path]:
    stamp = staging_dir / "_posix_stamp" / RUNTIME_STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(pin + "\n", encoding="utf-8")
    return {**files, RUNTIME_STAMP: stamp}


def runtime_asset_name(version: str, platform: str | None = None) -> str:
    return f"pob24agents-runtime-{version}-{platform or release_platform_tag()}.tar.gz"


def _extract_tar_gz_safely(tar_path: Path, dest_dir: Path, *, strip_components: int = 0) -> None:
    import tarfile

    dest_resolved = dest_dir.resolve()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            if strip_components > 0:
                if len(parts) <= strip_components:
                    continue
                parts = parts[strip_components:]
            if not parts or any(p in ("..", "") for p in parts):
                continue
            rel = Path(*parts)
            if rel.is_absolute():
                raise UpdateError(f"Unsafe tar member path: {member.name}")
            out = (dest_dir / rel).resolve()
            try:
                out.relative_to(dest_resolved)
            except ValueError as exc:
                raise UpdateError(f"Tar Slip detected: {member.name}") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            src_f = tf.extractfile(member)
            if src_f is None:
                continue
            with src_f, open(out, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            if member.mode & 0o111:
                os.chmod(out, 0o755)


def _self_test_runtime(bin_dir: Path) -> str | None:
    env = os.environ.copy()
    env["LUA_CPATH"] = f"{bin_dir}/?.so;;"
    try:
        proc = subprocess.run(
            [str(bin_dir / "luajit"), "-e", _SELF_TEST_LUA],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            **win_hidden_subprocess_kwargs(subprocess),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or f"exit {proc.returncode}")[-400:]
    return None


def _bundled_posix_runtime(staging_dir: Path) -> dict[Path, Path] | None:
    meipass = getattr(sys, "_MEIPASS", None)
    if not (getattr(sys, "frozen", False) and meipass):
        return None
    root = Path(meipass) / "runtime"
    if not all((root / rel).is_file() for rel in RUNTIME_FILES):
        return None
    out = staging_dir / "_posix_bundled"
    shutil.rmtree(out, ignore_errors=True)
    for rel in RUNTIME_FILES:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, out / rel)
        os.chmod(out / rel, 0o755)
    if _self_test_runtime(out / "bin") is not None:
        return None
    return _stamped({rel: out / rel for rel in RUNTIME_FILES}, staging_dir, runtime_pin())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release_tags(client: Any, mirror: str | None) -> list[str]:
    tags = [f"poe2-v{__version__}"]
    latest = latest_release_tag(client, mirror)
    if latest and latest not in tags:
        tags.append(latest)
    return tags


def _try_download_prebuilt_posix_runtime(
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    own_release_only: bool = False,
) -> dict[Path, Path] | None:
    make_client = _pkg()._make_http_client
    try:
        with make_client(timeout=_to_httpx_timeout(_CHECK_TIMEOUT_SPEC), proxy=proxy, follow_redirects=True) as c:
            tags = [f"poe2-v{__version__}"] if own_release_only else _release_tags(c, mirror)
        with make_client(timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC), proxy=proxy, follow_redirects=True) as c:
            for tag in tags:
                sums = signed_sums(c, tag, mirror)
                ver = tag.removeprefix("poe2-v").removeprefix("v")
                name = runtime_asset_name(ver)
                if not sums or name not in sums:
                    continue
                resp = c.get(asset_url(tag, name, mirror))
                if resp.status_code != 200 or _sha256(resp.content) != sums[name]:
                    continue
                tar_tmp = staging_dir / "_downloads" / name
                tar_tmp.parent.mkdir(parents=True, exist_ok=True)
                tar_tmp.write_bytes(resp.content)
                extract_root = staging_dir / "_posix_prebuilt"
                shutil.rmtree(extract_root, ignore_errors=True)
                extract_root.mkdir(parents=True)
                _extract_tar_gz_safely(tar_tmp, extract_root)
                if not all((extract_root / rel).is_file() for rel in RUNTIME_FILES):
                    continue
                for rel in RUNTIME_FILES:
                    os.chmod(extract_root / rel, 0o755)
                if _self_test_runtime(extract_root / "bin") is not None:
                    return None
                shipped = extract_root / RUNTIME_STAMP
                pin = shipped.read_text(encoding="utf-8").strip() if shipped.is_file() else runtime_pin()
                return _stamped({rel: extract_root / rel for rel in RUNTIME_FILES}, staging_dir, pin)
    except (OSError, ValueError, RuntimeError, tarfile.TarError, zipfile.BadZipFile, httpx.HTTPError) as exc:
        _LOG.debug("Prebuilt POSIX runtime download/extract failed: %s", exc)
        return None
    return None


def _download_pinned(client: Any, url: str, sha256: str) -> bytes:
    data = _pkg()._download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES)
    if _sha256(data) != sha256:
        raise UpdateError(f"Source tarball {url} failed pinned SHA-256 verification.")
    return data


def _build_posix_runtime_from_source(
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[Path, Path]:
    """Compile pinned LuaJIT 2460b3f and luautf8 0.1.6 locally on macOS/Linux."""
    cc_bin = os.environ.get("CC", "").strip() or shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    make_bin = shutil.which("make")
    if not cc_bin or not make_bin:
        raise UpdateError("Cannot build pinned POSIX LuaJIT runtime: missing C compiler or make.")

    build_timeout = _to_httpx_timeout((10.0, 60.0, 10.0, 10.0))
    lj_url = rewrite_github_url(POSIX_LUAJIT_TARBALL_URL, mirror=mirror)
    u8_url = rewrite_github_url(POSIX_LUAUTF8_TARBALL_URL, mirror=mirror)

    with tempfile.TemporaryDirectory(prefix="pob2-posix-build-") as tmp_str:
        tmp = Path(tmp_str)
        lj_tar, u8_tar = tmp / "luajit.tar.gz", tmp / "luautf8.tar.gz"
        lj_src, u8_src = tmp / "luajit-src", tmp / "luautf8-src"
        lj_src.mkdir()
        u8_src.mkdir()

        with _pkg()._make_http_client(timeout=build_timeout, proxy=proxy, follow_redirects=True) as client:
            lj_tar.write_bytes(_download_pinned(client, lj_url, POSIX_LUAJIT_TARBALL_SHA256))
            u8_tar.write_bytes(_download_pinned(client, u8_url, POSIX_LUAUTF8_TARBALL_SHA256))

        _extract_tar_gz_safely(lj_tar, lj_src, strip_components=1)
        _extract_tar_gz_safely(u8_tar, u8_src, strip_components=1)

        jobs = str(max(1, os.cpu_count() or 4))
        env = os.environ.copy()
        if sys.platform == "darwin":
            env.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0")

        lj_proc = subprocess.run(
            [make_bin, "-C", str(lj_src / "src"), f"-j{jobs}", "BUILDMODE=static", "amalg"],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            **win_hidden_subprocess_kwargs(subprocess),
        )
        if lj_proc.returncode != 0:
            raise UpdateError(f"Failed to compile LuaJIT 2460b3f (exit {lj_proc.returncode})")

        out_bin = staging_dir / "bin"
        out_bin.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lj_src / "src" / "luajit", out_bin / "luajit")
        os.chmod(out_bin / "luajit", 0o755)

        link = ["-bundle", "-undefined", "dynamic_lookup"] if sys.platform == "darwin" else ["-shared"]
        u8_proc = subprocess.run(
            [cc_bin, "-O2", "-fPIC", *link, f"-I{lj_src / 'src'}", str(u8_src / "lutf8lib.c"), "-o", str(out_bin / "lua-utf8.so")],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            **win_hidden_subprocess_kwargs(subprocess),
        )
        if u8_proc.returncode != 0:
            raise UpdateError(f"Failed to compile lua-utf8.so (exit {u8_proc.returncode})")
        os.chmod(out_bin / "lua-utf8.so", 0o755)

        failure = _self_test_runtime(out_bin)
        if failure is not None:
            raise UpdateError(f"Compiled POSIX LuaJIT runtime failed self-test: {failure}")
        return _stamped({rel: staging_dir / rel for rel in RUNTIME_FILES}, staging_dir, runtime_pin())


def ensure_posix_runtime(
    engine_dir: Path,
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[Path, Path]:
    if posix_runtime_is_current(engine_dir):
        return {}
    eng = Path(engine_dir)
    installed = all((eng / rel).is_file() for rel in RUNTIME_FILES)
    try:
        return (
            _bundled_posix_runtime(staging_dir)
            or _try_download_prebuilt_posix_runtime(
                staging_dir, proxy=proxy, mirror=mirror, own_release_only=installed
            )
            or _build_posix_runtime_from_source(staging_dir, proxy=proxy, mirror=mirror)
        )
    except Exception:
        if installed:
            return {}
        raise
