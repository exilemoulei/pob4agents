# SPDX-License-Identifier: GPL-3.0-only
"""PyInstaller frozen entry point for standalone ``pob2`` with UTF-8 stdio configuration."""

from __future__ import annotations

import os
import sys

from pob2_cli.compat import ensure_utf8


def run_frozen_cli() -> None:
    """Configure UTF-8 stdio/child environment and launch the ``pob2`` CLI (or internal host mode)."""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    ensure_utf8(sys.stdout)
    ensure_utf8(sys.stderr)

    from pob2_cli.cli import main

    main()


if __name__ == "__main__":
    run_frozen_cli()
