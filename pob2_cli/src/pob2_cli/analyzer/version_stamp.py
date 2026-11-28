# SPDX-License-Identifier: GPL-3.0-only
"""PoB2 version & game-patch stamp with automatic stale-patch `[待复核]` flagging (L2)."""

from __future__ import annotations

import re
from typing import Any

TARGET_GAME_PATCH = "0.5.5"
TARGET_MAJOR_MINOR = "0.5"
_TREE_VER_RE = re.compile(r"^(\d+)_(\d+)")


def infer_patch_from_tree_version(tree_version: str | None) -> str:
    tv = str(tree_version or "").strip()
    m = _TREE_VER_RE.match(tv)
    if not m:
        return TARGET_GAME_PATCH
    major, minor = m.group(1), m.group(2)
    if f"{major}.{minor}" == TARGET_MAJOR_MINOR:
        return TARGET_GAME_PATCH
    return f"{major}.{minor}"


def build_version_stamp(
    *,
    pob2_version: str | None = None,
    tree_version: str | None = None,
    game_patch: str | None = None,
) -> dict[str, Any]:
    """Build canonical version metadata; flag `[待复核]` when major.minor patch drifts from 0.5.x."""
    pob_ver = str(pob2_version or "unknown")
    tv = str(tree_version or "0_5")
    detected_patch = str(game_patch or infer_patch_from_tree_version(tv))
    det_parts = detected_patch.split(".")
    detected_mm = ".".join(det_parts[:2]) if len(det_parts) >= 2 else detected_patch
    mismatch = detected_mm != TARGET_MAJOR_MINOR
    review_flag = "[待复核]" if mismatch else None
    out: dict[str, Any] = {
        "pob2Version": pob_ver,
        "gamePatch": detected_patch,
        "targetGamePatch": TARGET_GAME_PATCH,
        "treeVersion": tv,
        "patchMismatch": mismatch,
        "reviewStatus": "[待复核]" if mismatch else "ok",
    }
    if review_flag:
        out["reviewFlag"] = review_flag
        out["reviewReason"] = (
            f"Build tree/patch ({detected_patch}) differs from target PoE2 {TARGET_GAME_PATCH}; "
            "mechanics & numbers require manual re-verification."
        )
    return out


def attach_version_stamp(
    payload: dict[str, Any],
    *,
    pob2_version: str | None = None,
    tree_version: str | None = None,
    game_patch: str | None = None,
) -> dict[str, Any]:
    """Stamp ``pob2Version``, ``gamePatch``, ``reviewStatus`` onto an analysis result dict."""
    stamp = build_version_stamp(
        pob2_version=pob2_version,
        tree_version=tree_version,
        game_patch=game_patch,
    )
    out = dict(payload)
    out["pob2Version"] = stamp["pob2Version"]
    out["gamePatch"] = stamp["gamePatch"]
    out["versionStamp"] = stamp
    if stamp["patchMismatch"]:
        out["reviewFlag"] = "[待复核]"
    return out


__all__ = [
    "TARGET_GAME_PATCH",
    "TARGET_MAJOR_MINOR",
    "attach_version_stamp",
    "build_version_stamp",
    "infer_patch_from_tree_version",
]
