# SPDX-License-Identifier: GPL-3.0-only
"""Release tooling for ``pob24agents`` (PoE2): ``poe2-v*`` tags, archives, Ed25519 sums, and ``bucket/pob2.json``.

Pure-PoE2 release contracts:
  - Tags use ``poe2-v<version>`` (never bare ``v*``, preserving ``/releases/latest`` isolation).
  - Channel manifest lives at ``bucket/pob2.json`` (read by ``pob2_cli.updater.releases.latest_release_tag``).
  - Standalone archives are ``pob24agents-<version>-<platform>.<ext>`` plus ``pob24agents-<platform>.<ext>``,
    with ``pob2.exe`` (Windows) or ``pob2`` (macOS/Linux) at archive root.
  - POSIX runtime tarballs are ``pob24agents-runtime-<version>-<platform>.tar.gz``.
  - ``SHA256SUMS.txt`` is signed into ``SHA256SUMS.txt.sig`` (Ed25519) and verified via
    ``pob2_cli/src/pob2_cli/updater/signature.py``.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_SLUG = "exilemoulei/pob4agents"
PRODUCT = "pob24agents"
DIST_NAME = "pob24agents"
TAG_PREFIX = "poe2-v"
SCOOP_MANIFEST_REL = "bucket/pob2.json"
SUMS_NAME = "SHA256SUMS.txt"
SIG_NAME = "SHA256SUMS.txt.sig"
_HERE = Path(__file__).resolve().parent
SIGNATURE_MODULE = _HERE.parent / "src" / "pob2_cli" / "updater" / "signature.py"

WINDOWS = "windows-x64"
POSIX_PLATFORMS = ("macos-arm64", "macos-x64", "linux-x64", "linux-arm64")
PLATFORMS = (WINDOWS, *POSIX_PLATFORMS)
PLATFORM_EXT = {WINDOWS: "zip", **dict.fromkeys(POSIX_PLATFORMS, "tar.gz")}
RUNTIME_FILES = ("bin/luajit", "bin/lua-utf8.so")
RUNTIME_STAMP = "bin/runtime.stamp"

_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_PEP440_RE = re.compile(
    r"""^(?P<release>[0-9]+\.[0-9]+\.[0-9]+)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?:-(?P<post_implicit>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n>[0-9]+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?$""",
    re.IGNORECASE | re.VERBOSE,
)
_PRE_SPELLING = {
    "alpha": "a",
    "a": "a",
    "beta": "b",
    "b": "b",
    "c": "rc",
    "pre": "rc",
    "preview": "rc",
    "rc": "rc",
}
_ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_version(version: str) -> str:
    cleaned = version.strip()
    if cleaned.startswith(TAG_PREFIX):
        cleaned = cleaned[len(TAG_PREFIX) :]
    elif cleaned.startswith(("v", "V")):
        raise ValueError(f"PoE2 tags must use `{TAG_PREFIX}<version>`, not bare `v`: {version!r}")
    if not _VERSION_RE.match(cleaned):
        raise ValueError(f"Not a valid release version: {version!r} (expected X.Y.Z or X.Y.Z-pre)")
    return cleaned


def release_tag(version: str) -> str:
    return f"{TAG_PREFIX}{check_version(version)}"


def pep440_version(version: str) -> str:
    ver = check_version(version)
    m = _PEP440_RE.match(ver)
    if not m:
        raise ValueError(f"{version!r} is not a PEP 440 version")
    out = ".".join(str(int(part)) for part in m["release"].split("."))
    if m["pre_l"]:
        out += f"{_PRE_SPELLING[m['pre_l'].lower()]}{int(m['pre_n'] or 0)}"
    if m["post_implicit"] is not None:
        out += f".post{int(m['post_implicit'])}"
    elif m["post_l"]:
        out += f".post{int(m['post_n'] or 0)}"
    if m["dev_l"]:
        out += f".dev{int(m['dev_n'] or 0)}"
    return out


def versioned_name(version: str, platform: str) -> str:
    return f"{PRODUCT}-{check_version(version)}-{platform}.{PLATFORM_EXT[platform]}"


def stable_name(platform: str) -> str:
    return f"{PRODUCT}-{platform}.{PLATFORM_EXT[platform]}"


def platform_asset_names(version: str, platform: str) -> list[str]:
    return [versioned_name(version, platform), stable_name(platform)]


def runtime_name(version: str, platform: str) -> str:
    return f"{PRODUCT}-runtime-{check_version(version)}-{platform}.tar.gz"


def python_asset_names(version: str) -> list[str]:
    v = pep440_version(version)
    return [f"{DIST_NAME}-{v}-py3-none-any.whl", f"{DIST_NAME}-{v}.tar.gz"]


def expected_assets(version: str, platforms: list[str]) -> set[str]:
    ver = check_version(version)
    names = {SUMS_NAME, SIG_NAME, *python_asset_names(ver)}
    for platform in platforms:
        names.update(platform_asset_names(ver, platform))
        if platform in POSIX_PLATFORMS:
            names.add(runtime_name(ver, platform))
    return names


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _escapes(name: str) -> bool:
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return True
    norm = posixpath.normpath(name.replace("\\", "/"))
    return norm == ".." or norm.startswith("../")


def _link_escapes(member: tarfile.TarInfo) -> bool:
    if member.linkname.startswith(("/", "\\")):
        return True
    if member.issym():
        return _escapes(posixpath.join(posixpath.dirname(member.name), member.linkname))
    return _escapes(member.linkname)


def tar_problems(path: Path, required: tuple[str, ...]) -> list[str]:
    try:
        with tarfile.open(path, "r:gz") as tf:
            members = {m.name: m for m in tf.getmembers()}
    except (tarfile.TarError, OSError) as exc:
        return [f"{path.name} is not a readable .tar.gz: {exc}"]
    problems = [
        f"{path.name} has no executable {req}"
        for req in required
        if (m := members.get(req)) is None or not m.isreg() or not (m.mode & 0o111)
    ]
    problems += [f"{path.name} has an unsafe path: {n}" for n in members if _escapes(n)]
    problems += [
        f"{path.name} has a link leaving the archive: {m.name} -> {m.linkname}"
        for m in members.values()
        if (m.issym() or m.islnk()) and _link_escapes(m)
    ]
    return problems


def zip_problems(path: Path, required: str) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        return [f"{path.name} is not a readable .zip: {exc}"]
    problems = [] if required in names else [f"{path.name} has no {required} at its root"]
    problems += [f"{path.name} has an unsafe path: {n}" for n in names if _escapes(n)]
    return problems


def pack_windows(dist_dir: Path, out_dir: Path, version: str) -> tuple[Path, str]:
    ver = check_version(version)
    dist_dir, out_dir = Path(dist_dir), Path(out_dir)
    if not (dist_dir / "pob2.exe").is_file():
        raise FileNotFoundError(f"Expected pob2.exe inside {dist_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    names = platform_asset_names(ver, WINDOWS)
    zip_path = out_dir / names[0]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in dist_dir.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(dist_dir).as_posix(), date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    for alias in names[1:]:
        shutil.copyfile(zip_path, out_dir / alias)
    return zip_path, _sha256(zip_path)


def _reproducible_tar_gz(out: Path, root: Path, arcnames: list[str]) -> None:
    with (
        open(out, "wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf,
    ):
        for arcname in arcnames:
            path = root / arcname
            info = tf.gettarinfo(str(path), arcname=arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if info.isreg():
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                with open(path, "rb") as fh:
                    tf.addfile(info, fh)
            else:
                info.mode = 0o755 if info.isdir() else info.mode
                tf.addfile(info)


def _tree(root: Path) -> list[str]:
    names: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        names += [(rel / n).as_posix() for n in dirnames + filenames]
    return sorted(names)


def pack_posix(dist_dir: Path, runtime_dir: Path, out_dir: Path, version: str, platform: str) -> tuple[Path, str]:
    ver = check_version(version)
    if platform not in POSIX_PLATFORMS:
        raise ValueError(f"Not a macOS/Linux platform: {platform}")
    dist_dir, runtime_dir, out_dir = Path(dist_dir), Path(runtime_dir), Path(out_dir)
    if not (dist_dir / "pob2").is_file():
        raise FileNotFoundError(f"Expected pob2 inside {dist_dir}")
    if not all((runtime_dir / rel).is_file() for rel in RUNTIME_FILES):
        raise FileNotFoundError(f"Expected {', '.join(RUNTIME_FILES)} inside {runtime_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    names = platform_asset_names(ver, platform)
    tar_path = out_dir / names[0]
    _reproducible_tar_gz(tar_path, dist_dir, _tree(dist_dir))
    for alias in names[1:]:
        shutil.copyfile(tar_path, out_dir / alias)
    stamp = [RUNTIME_STAMP] if (runtime_dir / RUNTIME_STAMP).is_file() else []
    _reproducible_tar_gz(out_dir / runtime_name(ver, platform), runtime_dir, ["bin", *RUNTIME_FILES, *stamp])
    return tar_path, _sha256(tar_path)


def write_sums(asset_dir: Path) -> Path:
    asset_dir = Path(asset_dir)
    lines = [
        f"{_sha256(p)}  {p.name}"
        for p in sorted(asset_dir.iterdir())
        if p.is_file() and p.name not in (SUMS_NAME, SIG_NAME)
    ]
    sums = asset_dir / SUMS_NAME
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return sums


def signature_module() -> ModuleType:
    return _load_module("pob2_release_signature", SIGNATURE_MODULE)


def _openssl(*args: str, data: bytes | None = None) -> bytes:
    proc = subprocess.run(["openssl", *args], input=data, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def public_key_of(key_pem: Path) -> str:
    der = _openssl("pkey", "-in", str(key_pem), "-pubout", "-outform", "DER")
    return base64.b64encode(der[-32:]).decode("ascii")


def sign_sums(asset_dir: Path, key_pem: Path) -> Path:
    sums = Path(asset_dir) / SUMS_NAME
    raw = _openssl("pkeyutl", "-sign", "-rawin", "-inkey", str(key_pem), "-in", str(sums))
    if len(raw) != 64:
        raise RuntimeError(f"Expected a 64-byte Ed25519 signature, got {len(raw)} bytes")
    sig = Path(asset_dir) / SIG_NAME
    sig.write_text(base64.b64encode(raw).decode("ascii") + "\n", encoding="ascii", newline="\n")
    return sig


def keygen(out: Path) -> str:
    if out.exists():
        raise FileExistsError(f"{out} exists; refusing to overwrite key")
    _openssl("genpkey", "-algorithm", "ed25519", "-out", str(out))
    out.chmod(0o600)
    return public_key_of(out)


def parse_sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = re.match(r"^([0-9a-f]{64}) [ *](\S.*)$", raw)
        if not m:
            raise ValueError(f"Malformed {SUMS_NAME} line: {raw!r}")
        if m.group(2) in out:
            raise ValueError(f"{m.group(2)} is listed twice in {SUMS_NAME}")
        out[m.group(2)] = m.group(1)
    return out


def verify(
    asset_dir: Path, version: str, platforms: list[str], keys: tuple[str, ...] | None = None
) -> list[str]:
    ver = check_version(version)
    asset_dir = Path(asset_dir)
    problems: list[str] = []
    present = {p.name for p in asset_dir.iterdir() if p.is_file()}
    expected = expected_assets(ver, platforms)
    problems += [f"missing asset: {n}" for n in sorted(expected - present)]
    problems += [f"unexpected asset: {n}" for n in sorted(present - expected)]
    if SUMS_NAME not in present:
        return problems
    try:
        sums = parse_sums((asset_dir / SUMS_NAME).read_text(encoding="utf-8"))
    except ValueError as exc:
        return [*problems, str(exc)]
    sig = asset_dir / SIG_NAME
    if sig.is_file():
        sums_bytes = (asset_dir / SUMS_NAME).read_bytes()
        mod = signature_module()
        if keys is None and not mod.RELEASE_PUBLIC_KEYS:
            problems.append("no release public key in pob2_cli/src/pob2_cli/updater/signature.py")
        elif not mod.verify_sums(sums_bytes, sig.read_text(encoding="ascii"), keys):
            problems.append(f"{SIG_NAME} does not verify {SUMS_NAME} against release public keys")
    for name in sorted(present - {SUMS_NAME, SIG_NAME}):
        if name not in sums:
            problems.append(f"{name} has no {SUMS_NAME} entry")
        elif sums[name] != _sha256(asset_dir / name):
            problems.append(f"{name} does not match its {SUMS_NAME} entry")
    problems += [f"{SUMS_NAME} lists a file the release lacks: {n}" for n in sorted(set(sums) - present)]
    for platform in platforms:
        names = [n for n in platform_asset_names(ver, platform) if n in sums]
        if len({sums[n] for n in names}) > 1:
            problems.append(f"{platform} aliases differ from {names[0]}")
    win = asset_dir / versioned_name(ver, WINDOWS)
    if WINDOWS in platforms and win.is_file():
        problems += zip_problems(win, "pob2.exe")
    for platform in platforms:
        if platform in POSIX_PLATFORMS:
            build = asset_dir / versioned_name(ver, platform)
            runtime = asset_dir / runtime_name(ver, platform)
            if build.is_file():
                problems += tar_problems(build, ("pob2",))
            if runtime.is_file():
                problems += tar_problems(runtime, RUNTIME_FILES)
    return problems


def build_scoop_manifest(version: str, sha256: str, base_data: dict[str, Any] | None = None) -> dict[str, Any]:
    ver = check_version(version)
    tag = release_tag(ver)
    dl_base = f"https://github.com/{REPO_SLUG}/releases/download"
    data: dict[str, Any] = (
        dict(base_data)
        if isinstance(base_data, dict)
        else {
            "version": ver,
            "description": "Path of Building (PoE2) headless daemon, craft solver, and high-SNR CLI for AI agents",
            "homepage": f"https://github.com/{REPO_SLUG}",
            "license": "GPL-3.0-only",
            "architecture": {"64bit": {}},
            "bin": "pob2.exe",
            "pre_install": "if (Get-Command pob2 -ErrorAction SilentlyContinue) { try { & pob2 stop 2>$null | Out-Null } catch {} }",
            "pre_uninstall": "if (Test-Path \"$dir\\pob2.exe\") { try { & \"$dir\\pob2.exe\" stop 2>$null | Out-Null } catch {} }",
            "notes": [
                "Run `pob2 bootstrap` once to install the headless PoB2 slim engine.",
                "Runtime state and managed engine live in %LOCALAPPDATA%\\pob24agents (override with POB2_RUNTIME_DIR).",
            ],
            "checkver": {
                "url": f"https://raw.githubusercontent.com/{REPO_SLUG}/main/{SCOOP_MANIFEST_REL}",
                "jsonpath": "$.version",
            },
            "autoupdate": {
                "architecture": {"64bit": {}},
                "hash": {"url": "$baseurl/SHA256SUMS.txt"},
            },
        }
    )
    data["version"] = ver
    data["license"] = "GPL-3.0-only"
    data.setdefault("architecture", {}).setdefault("64bit", {})["url"] = (
        f"{dl_base}/{tag}/{versioned_name(ver, WINDOWS)}"
    )
    data["architecture"]["64bit"]["hash"] = sha256.strip().lower()
    data.setdefault("autoupdate", {}).setdefault("architecture", {}).setdefault("64bit", {})["url"] = (
        f"{dl_base}/{TAG_PREFIX}$version/{PRODUCT}-$version-{WINDOWS}.zip"
    )
    return data


def update_scoop_manifest(manifest_path: Path, version: str, sha256: str) -> dict[str, Any]:
    """Create or update ``bucket/pob2.json`` pointing at ``poe2-v<version>``."""
    manifest_path = Path(manifest_path)
    existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
    updated = build_scoop_manifest(version, sha256, existing)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(updated, indent=4, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return updated


def release_notes(version: str) -> str:
    ver = check_version(version)
    tag = release_tag(ver)
    wheel = python_asset_names(ver)[0]
    return f"""### Install (`pob2` — Path of Exile 2)

**Scoop (`bucket/pob2.json`):**
```powershell
scoop bucket add pob https://github.com/{REPO_SLUG}
scoop install pob2   # later: scoop update pob2
```

**Python 3.11+** (`uv` / `pipx` / `pip`):
```bash
uv tool install "https://github.com/{REPO_SLUG}/releases/download/{tag}/{wheel}"
```

Then run `pob2 bootstrap` once (add `--proxy http://127.0.0.1:7890` if GitHub requires a proxy) to install the PoB2 slim engine.

Every asset is listed in `{SUMS_NAME}`, signed in `{SIG_NAME}` (Ed25519), and tagged `{tag}`.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pure-PoE2 release asset helpers for pob24agents")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack-windows", help="zip dist/pob2 and write its alias")
    p.add_argument("--dist", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--version", required=True)
    p = sub.add_parser("pack-posix", help="tar dist/pob2 and the pinned LuaJIT runtime")
    p.add_argument("--dist", type=Path, required=True)
    p.add_argument("--runtime", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--platform", required=True, choices=POSIX_PLATFORMS)
    p = sub.add_parser("sums", help=f"write {SUMS_NAME} for a directory")
    p.add_argument("--dir", type=Path, required=True)
    p = sub.add_parser("sign", help=f"write {SIG_NAME} with an Ed25519 private key PEM")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--key", type=Path, required=True)
    p = sub.add_parser("pubkey", help="print base64 public key of an Ed25519 PEM")
    p.add_argument("--key", type=Path, required=True)
    p = sub.add_parser("keygen", help="generate an Ed25519 signing key PEM")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("verify", help="verify release files, checksums, and Ed25519 signature")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--platforms", default=",".join(PLATFORMS))
    p.add_argument("--public-key", action="append")
    p = sub.add_parser("update-scoop", help=f"point {SCOOP_MANIFEST_REL} at a poe2-v* release")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--sha256", required=True)
    p = sub.add_parser("notes", help="print PoE2 release notes install block")
    p.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "pack-windows":
        zp, zsha = pack_windows(args.dist, args.out, args.version)
        print(f"zip_name={zp.name}\nzip_sha256={zsha}")
    elif args.cmd == "pack-posix":
        tp, tsha = pack_posix(args.dist, args.runtime, args.out, args.version, args.platform)
        print(f"tar_name={tp.name}\ntar_sha256={tsha}")
    elif args.cmd == "sums":
        print(write_sums(args.dir).read_text(encoding="utf-8"), end="")
    elif args.cmd == "sign":
        print(sign_sums(args.dir, args.key))
    elif args.cmd == "pubkey":
        print(public_key_of(args.key))
    elif args.cmd == "keygen":
        pub = keygen(args.out)
        print(f"Private key: {args.out}\nPublic key:  {pub}")
    elif args.cmd == "verify":
        plats = [p for p in args.platforms.split(",") if p]
        keys = tuple(args.public_key) if args.public_key else None
        probs = verify(args.dir, args.version, plats, keys)
        for pr in probs:
            print(f"FAIL {pr}", file=sys.stderr)
        if probs:
            return 1
        print(f"OK {len(expected_assets(args.version, plats))} files for {release_tag(args.version)}")
    elif args.cmd == "update-scoop":
        update_scoop_manifest(args.manifest, args.version, args.sha256)
    elif args.cmd == "notes":
        print(release_notes(args.version), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
