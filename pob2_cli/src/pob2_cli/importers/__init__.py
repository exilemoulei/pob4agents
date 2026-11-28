# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 build importers package (L1): multi-source share URLs, post-load fixups, and CN WeGame importer."""

from __future__ import annotations

from typing import Any

from ..share_codec import (
    MAX_SHARE_XML_BYTES,
    ShareCodeLimitError,
    inflate_capped,
    share_code_to_xml,
    xml_to_share_code,
)
from .post_load_fixups import (
    apply_post_load_fixups,
    detect_worker_res_override_lines,
    fetch_clean_notes,
    scan_res_override_lines_in_items,
    strip_pob_color_codes,
)

_SHARE_URL_EXPORTS: frozenset[str] = frozenset(
    {
        "ResolvedBuildSource",
        "TextFetcher",
        "load_build_into_worker",
        "load_source_label",
        "parse_share_url",
        "resolve_poe2_build_source",
    }
)
_URL_GUARD_EXPORTS: frozenset[str] = frozenset({"MAX_REDIRECTS", "check_public_https_url", "get_public_https"})
_WEGAME_CONVERT_EXPORTS: frozenset[str] = frozenset({"WeGameImportResult", "convert_wegame_bundle_to_pob2_xml"})
_WEGAME_FETCH_EXPORTS: frozenset[str] = frozenset(
    {
        "PII_FIELD_NAMES",
        "WEGAME_API_BASE",
        "WEGAME_METHODS",
        "EndpointPoster",
        "fetch_wegame_share",
        "is_wegame_share_url",
        "parse_wegame_share_url",
        "sanitize_item_fields",
        "sanitize_talent_tree",
        "sanitize_wegame_responses",
    }
)
_WEGAME_TABLES_EXPORTS: frozenset[str] = frozenset(
    {
        "CLASS_SPECS",
        "DEFAULT_QUEST_BOOLEAN_KEYS",
        "DEFAULT_QUEST_OPTION_KEYS",
        "EQUIPMENT_SLOT_MAP",
        "JEWEL_METADATA_ID_TO_BASE",
        "PASSIVE_JEWEL_SLOTS",
        "ClassSpec",
        "resolve_class_spec",
        "resolve_jewel_base_from_metadata_id",
    }
)
_WEGAME_TRANSLATE_EXPORTS: frozenset[str] = frozenset(
    {
        "GemCandidate",
        "GemResolution",
        "WeGameTranslator",
        "clean_wegame_text",
        "has_shaman_bonded_marker",
        "is_progress_counter_stat",
        "parse_leading_int",
    }
)


def __getattr__(name: str) -> Any:
    if name in _SHARE_URL_EXPORTS:
        from . import share_urls as _mod
    elif name in _URL_GUARD_EXPORTS:
        from . import url_guard as _mod
    elif name in _WEGAME_CONVERT_EXPORTS:
        from . import wegame_convert as _mod
    elif name in _WEGAME_FETCH_EXPORTS:
        from . import wegame_fetch as _mod
    elif name in _WEGAME_TABLES_EXPORTS:
        from . import wegame_tables as _mod
    elif name in _WEGAME_TRANSLATE_EXPORTS:
        from . import wegame_translate as _mod
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    val = getattr(_mod, name)
    globals()[name] = val
    return val


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "CLASS_SPECS",
    "DEFAULT_QUEST_BOOLEAN_KEYS",
    "DEFAULT_QUEST_OPTION_KEYS",
    "EQUIPMENT_SLOT_MAP",
    "JEWEL_METADATA_ID_TO_BASE",
    "MAX_REDIRECTS",
    "MAX_SHARE_XML_BYTES",
    "PASSIVE_JEWEL_SLOTS",
    "PII_FIELD_NAMES",
    "WEGAME_API_BASE",
    "WEGAME_METHODS",
    "ClassSpec",
    "EndpointPoster",
    "GemCandidate",
    "GemResolution",
    "ResolvedBuildSource",
    "ShareCodeLimitError",
    "TextFetcher",
    "WeGameImportResult",
    "WeGameTranslator",
    "apply_post_load_fixups",
    "check_public_https_url",
    "clean_wegame_text",
    "convert_wegame_bundle_to_pob2_xml",
    "detect_worker_res_override_lines",
    "fetch_clean_notes",
    "fetch_wegame_share",
    "get_public_https",
    "has_shaman_bonded_marker",
    "inflate_capped",
    "is_progress_counter_stat",
    "is_wegame_share_url",
    "load_build_into_worker",
    "load_source_label",
    "parse_leading_int",
    "parse_share_url",
    "parse_wegame_share_url",
    "resolve_class_spec",
    "resolve_jewel_base_from_metadata_id",
    "resolve_poe2_build_source",
    "sanitize_item_fields",
    "sanitize_talent_tree",
    "sanitize_wegame_responses",
    "scan_res_override_lines_in_items",
    "share_code_to_xml",
    "strip_pob_color_codes",
    "xml_to_share_code",
]
