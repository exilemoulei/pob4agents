# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 transactional build mutators and diff helpers (L2)."""

from __future__ import annotations

from .what_if import diff_against_source, run_what_if

__all__ = ["diff_against_source", "run_what_if"]
