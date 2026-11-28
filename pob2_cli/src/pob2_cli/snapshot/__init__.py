"""PoE2 build digests, checkpoints, slot history, and CoW transactions."""

from __future__ import annotations

from .checkpoint import (
    BuildSnapshot,
    drop_snapshot,
    fall_back_to_code,
    restore_snapshot,
    supports_cow_snapshot,
    supports_direct_calc,
    take_snapshot,
    worker_caps,
)
from .commands import load_build, redo_build, run_mutation, undo_build
from .digest import (
    capture_digest,
    capture_direct_digest,
    compact_digest_summary,
    diff_digest,
    digest_from_stats,
)
from .helpers import (
    CORE_STAT_KEYS,
    call_batch,
    extract_points_used,
    finite_float,
    fmt_delta,
    fmt_num,
    json_safe_stat,
    parse_numeric_or_none,
    safe_min_ele_max_hit,
    stat_float,
    strip_pob_escapes,
)
from .history import SlotHistory, compact_source_label, history_of, rebuild_if_stale
from .transaction import BuildScope, BuildTransaction
from .verify import stat_drift, verify_build, verify_cow_restored

__all__ = [
    "CORE_STAT_KEYS",
    "BuildScope",
    "BuildSnapshot",
    "BuildTransaction",
    "SlotHistory",
    "call_batch",
    "capture_digest",
    "capture_direct_digest",
    "compact_digest_summary",
    "compact_source_label",
    "diff_digest",
    "digest_from_stats",
    "drop_snapshot",
    "extract_points_used",
    "fall_back_to_code",
    "finite_float",
    "fmt_delta",
    "fmt_num",
    "history_of",
    "json_safe_stat",
    "load_build",
    "parse_numeric_or_none",
    "rebuild_if_stale",
    "redo_build",
    "restore_snapshot",
    "run_mutation",
    "safe_min_ele_max_hit",
    "stat_drift",
    "stat_float",
    "strip_pob_escapes",
    "supports_cow_snapshot",
    "supports_direct_calc",
    "take_snapshot",
    "undo_build",
    "verify_build",
    "verify_cow_restored",
    "worker_caps",
]
