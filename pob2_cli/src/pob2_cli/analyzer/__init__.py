# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 build analysis, S4/T3.4 deep audit, dehydration, and version stamping (L2)."""

from __future__ import annotations

from .deep_audit import DEFAULT_OVERCAP_TARGET, evaluate_resistances_cap_overcap, run_deep_audit
from .summary import (
    audit_build,
    diagnose_build,
    extract_notes,
    extract_summary,
    inspect_config,
    inspect_items,
    inspect_skills,
    inspect_tree,
)
from .version_stamp import (
    TARGET_GAME_PATCH,
    TARGET_MAJOR_MINOR,
    attach_version_stamp,
    build_version_stamp,
    infer_patch_from_tree_version,
)

__all__ = [
    "DEFAULT_OVERCAP_TARGET",
    "TARGET_GAME_PATCH",
    "TARGET_MAJOR_MINOR",
    "attach_version_stamp",
    "audit_build",
    "build_version_stamp",
    "diagnose_build",
    "evaluate_resistances_cap_overcap",
    "extract_notes",
    "extract_summary",
    "infer_patch_from_tree_version",
    "inspect_config",
    "inspect_items",
    "inspect_skills",
    "inspect_tree",
    "run_deep_audit",
]
