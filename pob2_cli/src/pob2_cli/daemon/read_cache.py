"""Per-(slot, rev, lang) bounded LRU read cache, loop-guard, and idempotent sourceHash tracking."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from ..bridge_protocol import encode_xml_share_code
from ..snapshot import history_of

READ_CACHE_ACTIONS: frozenset[str] = frozenset(
    {
        "summary",
        "audit",
        "diagnose",
        "notes",
        "uniques",
        "stats",
        "build_info",
        "tree_state",
        "tree",
        "items",
        "skills",
        "config_list",
        "weapon_set_state",
        "node_power",
        "probe",
    }
)

LOOP_GUARD_WARNING = (
    "[loop-guard] Query repeated 3+ times at unchanged rev; synthesize from existing output."
)

_COMMON_SKIP_KEYS: frozenset[str] = frozenset({"target", "main_target", "skip_periodic_check"})
LIVE_SOURCE_RELOAD_AFTER_SEC = 120.0


def is_live_source(params: dict[str, Any]) -> bool:
    xml_code = params.get("xml_code")
    if isinstance(xml_code, str) and xml_code.strip():
        return False
    src = params.get("source")
    if isinstance(src, str) and src.strip().startswith(("http://", "https://")):
        return True
    return bool(str(params.get("realm") or "").strip())


def compute_source_hash(params: dict[str, Any]) -> str | None:
    explicit = params.get("source_hash") or params.get("sourceHash")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    xml_code = params.get("xml_code")
    src = params.get("source")
    realm = str(params.get("realm") or "").strip().lower()
    canonical: str | None = None
    if isinstance(xml_code, str) and xml_code.strip():
        canonical = "code:" + xml_code.strip()
    elif isinstance(src, str) and src.strip():
        s = src.strip()
        if s.startswith(("http://", "https://")):
            char_nm = str(params.get("char_name") or "").strip()
            lg = str(params.get("league") or "").strip()
            canonical = f"src:{s}|char={char_nm}|league={lg}|realm={realm}"
        elif s.lstrip().startswith("<"):
            try:
                canonical = "code:" + encode_xml_share_code(s).strip()
            except Exception:
                canonical = "xml:" + s
        else:
            canonical = "code:" + s
    elif realm == "cn":
        char_nm = str(params.get("char_name") or "").strip()
        lg = str(params.get("league") or "").strip()
        canonical = f"cn:|char={char_nm}|league={lg}"

    if not canonical:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _canonicalize_params(params: dict[str, Any]) -> str:
    norm: dict[str, Any] = {}
    for k, v in params.items():
        if k in _COMMON_SKIP_KEYS or v is None:
            continue
        norm[k] = list(v) if isinstance(v, tuple) else v
    return json.dumps(norm, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _is_hist_unsettled(hist: Any) -> bool:
    return bool(getattr(hist, "stale", False) or getattr(hist, "active", None) is not None)


def _key_mentions_slot(key: tuple[Any, ...], slot: str, stale_rev: int | None = None) -> bool:
    if len(key) == 5:
        return key[0] == slot and (stale_rev is None or key[1] != stale_rev)
    if len(key) == 6:
        if stale_rev is None:
            return key[0] == slot or key[2] == slot
        return (key[0] == slot and key[1] != stale_rev) or (key[2] == slot and key[3] != stale_rev)
    return False


class SlotReadCache:
    """Thread-safe bounded LRU per-(slot, rev, lang) read cache with sourceHash tracking."""

    def __init__(self, max_entries: int = 256, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._max_hits = max(64, self.max_entries * 4)
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self._hits: OrderedDict[tuple[Any, ...], int] = OrderedDict()
        self._slot_revs: dict[str, int] = {}
        self._slot_sources: dict[str, dict[str, Any]] = {}

    def _sync_slot_rev_unlocked(self, slot: str, rev: int) -> None:
        prev = self._slot_revs.get(slot)
        if prev is not None and prev != rev:
            for k in [k for k in self._entries if _key_mentions_slot(k, slot, stale_rev=rev)]:
                self._entries.pop(k, None)
            for k in [k for k in self._hits if _key_mentions_slot(k, slot, stale_rev=rev)]:
                self._hits.pop(k, None)
        self._slot_revs[slot] = rev

    def _put_entry_unlocked(
        self,
        entry_key: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        clean_copy = copy.deepcopy(payload)
        clean_copy.pop("cached", None)
        clean_copy.pop("loopGuardWarning", None)
        self._entries[entry_key] = clean_copy
        self._entries.move_to_end(entry_key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        self._hits[entry_key] = max(1, self._hits.get(entry_key, 0) + 1)
        self._hits.move_to_end(entry_key)
        while len(self._hits) > self._max_hits:
            self._hits.popitem(last=False)

    def _get_entry_unlocked(self, entry_key: tuple[Any, ...]) -> tuple[dict[str, Any], int] | None:
        stored = self._entries.get(entry_key)
        if stored is None:
            return None
        self._entries.move_to_end(entry_key)
        count = self._hits.get(entry_key, 0) + 1
        self._hits[entry_key] = count
        self._hits.move_to_end(entry_key)
        while len(self._hits) > self._max_hits:
            self._hits.popitem(last=False)
        return copy.deepcopy(stored), count

    def tracked_slots(self) -> set[str]:
        with self._lock:
            slots = set(self._slot_revs) | set(self._slot_sources)
            for k in self._entries:
                if len(k) == 5:
                    slots.add(str(k[0]))
                elif len(k) == 6:
                    slots.add(str(k[0]))
                    slots.add(str(k[2]))
            return slots

    def invalidate_slot(self, slot: str) -> None:
        with self._lock:
            for k in [k for k in self._entries if _key_mentions_slot(k, slot)]:
                self._entries.pop(k, None)
            for k in [k for k in self._hits if _key_mentions_slot(k, slot)]:
                self._hits.pop(k, None)
            self._slot_revs.pop(slot, None)
            self._slot_sources.pop(slot, None)

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits.clear()
            self._slot_revs.clear()
            self._slot_sources.clear()

    def record_slot_load(
        self,
        slot: str,
        rev: int,
        source_hash: str | None,
        load_meta: dict[str, Any] | None = None,
        *,
        live: bool = False,
    ) -> None:
        with self._lock:
            self._sync_slot_rev_unlocked(slot, rev)
            if not source_hash:
                self._slot_sources.pop(slot, None)
                return
            kept_meta: dict[str, Any] = {}
            if isinstance(load_meta, dict):
                for key in ("importNotes", "translateWarnings", "character", "droppedItems", "fixupWarnings", "warnings"):
                    if load_meta.get(key):
                        kept_meta[key] = copy.deepcopy(load_meta[key])
            self._slot_sources[slot] = {
                "sourceHash": source_hash,
                "rev": rev,
                "loadMeta": kept_meta,
                "liveSince": self._clock() if live else None,
            }

    def matching_slot_load(
        self,
        slot: str,
        rev: int,
        has_loaded_build: bool,
        source_hash: str | None,
    ) -> dict[str, Any] | None:
        if not has_loaded_build or not source_hash:
            return None
        with self._lock:
            rec = self._slot_sources.get(slot)
            if rec is None or rec.get("sourceHash") != source_hash or rec.get("rev") != rev:
                return None
            live_since = rec.get("liveSince")
            if live_since is not None and self._clock() - live_since > LIVE_SOURCE_RELOAD_AFTER_SEC:
                return None
            return copy.deepcopy(rec.get("loadMeta") or {})

    def lookup(
        self,
        slot: str,
        rev: int,
        lang: str,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        if action not in READ_CACHE_ACTIONS:
            return None
        ck = _canonicalize_params(params)
        with self._lock:
            self._sync_slot_rev_unlocked(slot, rev)
            hit = self._get_entry_unlocked((slot, rev, lang, action, ck))
            if hit is None:
                return None
            out, count = hit
        out["cached"] = True
        if count >= 3:
            out["loopGuardWarning"] = LOOP_GUARD_WARNING
        return out

    def store(
        self,
        slot: str,
        rev: int,
        lang: str,
        action: str,
        params: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        if action not in READ_CACHE_ACTIONS or not isinstance(payload, dict):
            return
        ck = _canonicalize_params(params)
        with self._lock:
            self._sync_slot_rev_unlocked(slot, rev)
            self._put_entry_unlocked((slot, rev, lang, action, ck), payload)

    def check_slot_read(
        self,
        target: str,
        worker: Any,
        eff_lang: str,
        action: str,
        params: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        if action not in READ_CACHE_ACTIONS:
            return "", None
        w_loc = getattr(worker, "locale", None)
        cache_lang = f"{eff_lang}:{w_loc}" if w_loc else eff_lang
        hist = history_of(worker)
        if _is_hist_unsettled(hist):
            return cache_lang, None
        return cache_lang, self.lookup(target, hist.rev, cache_lang, action, params)

    def store_slot_read(
        self,
        target: str,
        worker: Any,
        cache_lang: str,
        action: str,
        params: dict[str, Any],
        res_slot: Any,
    ) -> None:
        if action not in READ_CACHE_ACTIONS or not isinstance(res_slot, dict):
            return
        hist = history_of(worker)
        if _is_hist_unsettled(hist):
            return
        self.store(target, hist.rev, cache_lang, action, params, res_slot)
