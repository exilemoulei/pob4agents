"""Allow launching the PoE2 daemon via ``python -m pob2_cli.daemon``."""

from __future__ import annotations

from .client import main

if __name__ == "__main__":
    main()
