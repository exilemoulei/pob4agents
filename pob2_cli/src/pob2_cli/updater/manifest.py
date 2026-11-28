"""PoB2 manifest.xml parsing, slim-engine filtering, SHA1 verification, and diffing."""

from __future__ import annotations

import hashlib
import re
import stat
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from .constants import (
    POB2_DEFAULT_BRANCH,
    ManifestFileEntry,
    PobManifest,
    UpdateError,
    rewrite_github_url,
)

SLIM_IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ico", ".gif", ".dds", ".zst", ".tga"}
)
SLIM_WIN32_RUNTIME_DLLS: frozenset[str] = frozenset(
    {"lua51.dll", "zlib1.dll", "lua-utf8.dll", "vcruntime140.dll", "msvcp140.dll"}
)
_TREE_LUA_VER_RE = re.compile(r"^TreeData/(\d+)_(\d+)/tree\.lua$", re.IGNORECASE)


def _pkg() -> Any:
    return sys.modules["pob2_cli.updater"]


def _detect_latest_tree_versions(raw_names: list[str], *, count: int = 2) -> tuple[str, ...]:
    found: dict[tuple[int, int], str] = {}
    for name in raw_names:
        m = _TREE_LUA_VER_RE.match(name.replace("\\", "/"))
        if m:
            found[(int(m.group(1)), int(m.group(2)))] = f"{m.group(1)}_{m.group(2)}"
    ordered = sorted(found.items(), key=lambda item: item[0], reverse=True)
    return tuple(ver for _, ver in ordered[:count])


def is_slim_manifest_entry(
    local_rel: str,
    *,
    keep_win32: bool,
    latest_trees: tuple[str, ...] | str | None,
) -> bool:
    """Return True if a PoB2 manifest entry belongs in the headless slim runtime."""
    posix = local_rel.replace("\\", "/")
    low = posix.lower()
    if any(low.endswith(ext) for ext in SLIM_IMAGE_EXTS):
        return False
    parts = posix.split("/")
    if len(parts) == 1:
        if low.endswith(".lua"):
            return True
        return keep_win32 and low in SLIM_WIN32_RUNTIME_DLLS
    top = parts[0].lower()
    if top in ("simplegraphic", "assets"):
        return False
    if top in ("modules", "classes"):
        return True
    if top == "lua":
        return low.endswith(".lua")
    if top == "data":
        return not (low.startswith("data/timelessjeweldata/") and ".zip" in parts[-1].lower())
    if top == "treedata":
        if low.endswith(".lua") and len(parts) == 3 and parts[1].lower() == "legion":
            return True
        vers = (latest_trees,) if isinstance(latest_trees, str) else (latest_trees or ())
        allowed: set[str] = set()
        for ver in vers:
            lt = ver.lower()
            allowed.add(f"treedata/{lt}/tree.lua")
            allowed.add(f"treedata/{lt}/sprites.lua")
            allowed.add(f"treedata/{lt}/assets.lua")
        return low in allowed
    return False


def verify_pob_sha1(data: bytes, expected_sha1: str) -> bool:
    """Verify bytes against PoB2 manifest SHA1 (with official LF/CRLF tolerance)."""
    exp = expected_sha1.strip().lower()
    if hashlib.sha1(data).hexdigest() == exp:
        return True
    if b"\n" in data and b"\r\n" not in data:
        if hashlib.sha1(data.replace(b"\n", b"\r\n")).hexdigest() == exp:
            return True
    if b"\r\n" in data:
        if hashlib.sha1(data.replace(b"\r\n", b"\n")).hexdigest() == exp:
            return True
    return False


def parse_pob_manifest(
    xml_text: str,
    *,
    include_win32_runtime: bool | None = None,
    slim: bool = False,
) -> PobManifest:
    """Parse a PathOfBuilding-PoE2 manifest.xml string (defaults branch to ``dev``)."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    if root.tag != "PoBVersion":
        raise UpdateError(f"Invalid PoB2 manifest root element: {root.tag}")
    ver_elem = root.find("Version")
    if ver_elem is None or not ver_elem.attrib.get("number"):
        raise UpdateError("PoB2 manifest is missing <Version number=...>")

    version = ver_elem.attrib["number"].strip()
    branch = (ver_elem.attrib.get("branch") or POB2_DEFAULT_BRANCH).strip()
    platform_str = (ver_elem.attrib.get("platform") or "win32").strip()
    keep_win32 = bool(_pkg().is_windows()) if include_win32_runtime is None else bool(include_win32_runtime)

    sources: dict[str, dict[str, str]] = {}
    for src_elem in root.findall("Source"):
        part = (src_elem.attrib.get("part") or "").strip()
        url = (src_elem.attrib.get("url") or "").strip()
        plat = (src_elem.attrib.get("platform") or "any").strip()
        if part and url:
            sources.setdefault(part, {})[plat] = url

    def _eligible(f_elem: ET.Element) -> bool:
        raw_name = (f_elem.attrib.get("name") or "").strip()
        sha1 = (f_elem.attrib.get("sha1") or "").strip().lower()
        plat = f_elem.attrib.get("platform")
        runtime = f_elem.attrib.get("runtime")
        if not raw_name or not sha1:
            return False
        if keep_win32:
            return not ((plat and plat != "win32") or (runtime and runtime != "win32"))
        if (plat and plat == "win32") or (runtime and runtime == "win32"):
            return False
        return not (plat and plat not in ("any", ""))

    file_elems = [e for e in root.findall("File") if _eligible(e)]
    latest_trees = (
        _detect_latest_tree_versions([(e.attrib.get("name") or "").strip() for e in file_elems], count=2)
        if slim
        else ()
    )

    files: dict[str, ManifestFileEntry] = {}
    for f_elem in file_elems:
        raw_name = (f_elem.attrib.get("name") or "").strip()
        part = (f_elem.attrib.get("part") or "program").strip()
        sha1 = (f_elem.attrib.get("sha1") or "").strip().lower()
        plat = f_elem.attrib.get("platform")
        runtime = f_elem.attrib.get("runtime")
        local_rel = raw_name.replace("{space}", " ")
        if Path(local_rel).is_absolute() or ".." in Path(local_rel).parts:
            raise UpdateError(f"Unsafe manifest path: {raw_name}")
        if slim and not is_slim_manifest_entry(local_rel, keep_win32=keep_win32, latest_trees=latest_trees):
            continue
        files[raw_name] = ManifestFileEntry(
            raw_name=raw_name,
            local_rel=local_rel,
            part=part,
            sha1=sha1,
            runtime=runtime,
            platform=plat,
        )

    if not sources or not files:
        raise UpdateError("PoB2 manifest contains no sources or files.")
    return PobManifest(
        version=version,
        branch=branch,
        platform=platform_str,
        sources=sources,
        files=files,
        raw_xml=xml_text,
    )


def build_manifest_file_url(manifest: PobManifest, entry: ManifestFileEntry, mirror: str | None = None) -> str:
    """Build the raw GitHub download URL for a PoB2 manifest file entry."""
    part_sources = manifest.sources.get(entry.part)
    if not part_sources:
        raise UpdateError(f"No <Source> defined in PoB2 manifest for part='{entry.part}'")
    base = part_sources.get(manifest.platform) or part_sources.get("any")
    if not base:
        raise UpdateError(f"No matching platform source for part='{entry.part}'")
    base = base.replace("{branch}", manifest.branch)
    quoted = urllib.parse.quote(entry.raw_name, safe="/")
    return rewrite_github_url(base + quoted, mirror=mirror)


def diff_pob_manifest(
    poe2_dir: Path,
    remote_manifest: PobManifest,
    *,
    verify_local_sha1: bool = True,
    slim: bool = False,
) -> tuple[list[ManifestFileEntry], list[Path]]:
    """Compute PoE2/ files to download and obsolete files to delete against ``remote_manifest``."""
    poe2 = Path(poe2_dir)
    verify_fn = _pkg().verify_pob_sha1
    local_manifest: PobManifest | None = None
    local_manifest_path = poe2 / "manifest.xml"
    if local_manifest_path.is_file():
        try:
            local_manifest = parse_pob_manifest(
                local_manifest_path.read_text(encoding="utf-8", errors="replace"),
                slim=False,
            )
        except Exception:
            local_manifest = None

    to_download: list[ManifestFileEntry] = []
    for entry in remote_manifest.files.values():
        local_path = poe2 / entry.local_rel
        try:
            st = local_path.stat()
        except OSError:
            to_download.append(entry)
            continue
        if not stat.S_ISREG(st.st_mode):
            to_download.append(entry)
            continue
        if verify_local_sha1:
            try:
                data = local_path.read_bytes()
            except OSError:
                to_download.append(entry)
                continue
            if not verify_fn(data, entry.sha1):
                to_download.append(entry)
        elif local_manifest is not None:
            local_entry = local_manifest.files.get(entry.raw_name)
            if local_entry is None or local_entry.sha1 != entry.sha1:
                to_download.append(entry)
        else:
            to_download.append(entry)

    to_delete: list[Path] = []
    if local_manifest is not None:
        remote_rels = {e.local_rel.lower() for e in remote_manifest.files.values()}
        if slim and remote_manifest.raw_xml:
            try:
                remote_full = parse_pob_manifest(remote_manifest.raw_xml, slim=False)
                remote_rels |= {e.local_rel.lower() for e in remote_full.files.values()}
            except (UpdateError, ValueError, KeyError):
                pass
        for old_entry in local_manifest.files.values():
            if old_entry.local_rel.lower() not in remote_rels and (poe2 / old_entry.local_rel).is_file():
                to_delete.append(Path("PoE2") / old_entry.local_rel)

    return to_download, to_delete


def _render_local_manifest_xml(remote_manifest: PobManifest) -> str:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(remote_manifest.raw_xml)
    ver_elem = root.find("Version")
    if ver_elem is not None:
        ver_elem.set("platform", remote_manifest.platform)
        ver_elem.set("branch", remote_manifest.branch)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
