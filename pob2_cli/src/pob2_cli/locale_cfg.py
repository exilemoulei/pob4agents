"""L0 locale configuration, Han folding re-exports, and runtime config.json persistence.

Supports ``zh-CN`` (default), ``zh-TW``, and ``en``. Strictly reads ``POB2_LANG``
(never ``POB_LANG``) and stores config under the isolated PoE2 runtime directory.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from .game_profile import ENV_LANG, has_runtime_data, resolve_runtime_dir
from .han_fold import HAN_FOLD, fold_han, fold_search_text

SUPPORTED_LANGS: tuple[str, ...] = ("zh-CN", "zh-TW", "en")
DEFAULT_LANG: str = "zh-CN"

_LANG_ALIASES: dict[str, str] = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh-rcn": "zh-CN",
    "zh_rcn": "zh-CN",
    "cn": "zh-CN",
    "chs": "zh-CN",
    "simplified": "zh-CN",
    "zh-tw": "zh-TW",
    "zh_tw": "zh-TW",
    "zh-rtw": "zh-TW",
    "zh_rtw": "zh-TW",
    "zh-hk": "zh-TW",
    "zh_hk": "zh-TW",
    "tw": "zh-TW",
    "cht": "zh-TW",
    "traditional": "zh-TW",
    "en": "en",
    "en-us": "en",
    "en_us": "en",
    "en-gb": "en",
    "en_gb": "en",
    "english": "en",
}

_DATA_SUBDIRS: dict[str, str | None] = {
    "zh-CN": "zh-rCN",
    "zh-TW": "zh-rTW",
    "en": None,
}

_WARNED_ENV_VALUES: set[str] = set()

__all__ = [
    "DEFAULT_LANG",
    "HAN_FOLD",
    "SUPPORTED_LANGS",
    "_has_runtime_data_l0",
    "current_lang",
    "fold_han",
    "fold_search_text",
    "get_active_lang",
    "invalid_env_lang",
    "lang_to_data_subdir",
    "normalize_lang",
    "resolve_runtime_dir_l0",
    "set_active_lang",
]


def normalize_lang(val: str | None) -> str:
    """Normalize a locale identifier to one of ``('zh-CN', 'zh-TW', 'en')``."""
    if val is None:
        raise ValueError(f"Unsupported language {val!r}; valid choices: {', '.join(SUPPORTED_LANGS)}")
    cleaned = str(val).strip().lower()
    mapped = _LANG_ALIASES.get(cleaned)
    if mapped is None:
        raise ValueError(f"Unsupported language {val!r}; valid choices: {', '.join(SUPPORTED_LANGS)}")
    return mapped


def lang_to_data_subdir(lang: str) -> str | None:
    """Map normalized locale to ``Data/poe2/<subdir>`` (``'zh-rCN'``, ``'zh-rTW'``, or ``None``)."""
    return _DATA_SUBDIRS[normalize_lang(lang)]


def _has_runtime_data_l0(path: Path) -> bool:
    return has_runtime_data(path)


def resolve_runtime_dir_l0(runtime_dir: Path | None = None) -> Path:
    """L0 stdlib-only PoE2 runtime directory resolver."""
    return resolve_runtime_dir(runtime_dir)


def invalid_env_lang() -> str | None:
    """Return the raw ``POB2_LANG`` value when set to an unsupported locale, else None."""
    env_val = os.environ.get(ENV_LANG, "").strip()
    if not env_val:
        return None
    try:
        normalize_lang(env_val)
    except ValueError:
        return env_val
    return None


def get_active_lang(runtime_dir: Path | None = None) -> tuple[str, str]:
    """Return ``(lang, source)`` where ``source`` is ``'env'``, ``'config'``, or ``'default'``."""
    env_val = os.environ.get(ENV_LANG, "").strip()
    if env_val:
        try:
            return normalize_lang(env_val), "env"
        except ValueError:
            if env_val not in _WARNED_ENV_VALUES:
                _WARNED_ENV_VALUES.add(env_val)
                print(
                    f"Warning: ignoring {ENV_LANG}={env_val!r}; valid choices: {', '.join(SUPPORTED_LANGS)}.",
                    file=sys.stderr,
                )

    cfg_path = resolve_runtime_dir_l0(runtime_dir) / "config.json"
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("lang") is not None:
                return normalize_lang(str(raw["lang"])), "config"
        except (OSError, ValueError, TypeError):
            pass

    return DEFAULT_LANG, "default"


def current_lang(runtime_dir: Path | None = None) -> str:
    """Return the active normalized locale (``'zh-CN'``, ``'zh-TW'``, or ``'en'``)."""
    return get_active_lang(runtime_dir)[0]


def set_active_lang(lang: str, runtime_dir: Path | None = None) -> dict[str, Any]:
    """Normalize ``lang``, atomically persist ``<runtime_dir>/config.json``, and return status dict."""
    norm = normalize_lang(lang)
    rt_dir = resolve_runtime_dir_l0(runtime_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = rt_dir / "config.json"

    payload: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = dict(existing)
        except (OSError, ValueError, TypeError):
            payload = {}

    payload["lang"] = norm
    tmp_path = cfg_path.with_name(f".config.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, cfg_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return {"lang": norm, "configPath": str(cfg_path)}
