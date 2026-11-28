"""Fail-safe JSON object file reading and atomic writing (L0 pure module)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def read_json_dict(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from ``path``; return None when missing, unreadable, or not a dict."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def atomic_write_json(path: Path, payload: Any, *, ensure_ascii: bool = False) -> None:
    """Atomically write UTF-8 JSON to ``path`` via sibling temp file + ``os.replace`` with retry."""
    try:
        try:
            encoded = json.dumps(payload, ensure_ascii=ensure_ascii).encode("utf-8")
        except UnicodeEncodeError:
            if ensure_ascii:
                return
            encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    except (TypeError, ValueError):
        return

    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        for attempt in range(3):
            try:
                os.replace(tmp_path, path)
                tmp_path = None
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.02 * (attempt + 1))
    except OSError:
        pass
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
