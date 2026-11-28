# SPDX-License-Identifier: GPL-3.0-only
"""CLI helpers for `pob2 what-if`: `--unequip` command wrapper and nested JSON spec normalization (L4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from .cli_common import Pob2Command
from .errors import DaemonRequestError
from .guardrails import normalize_poe2_equipment_slot

UNEQUIP_USE_SLOT = "__USE_SLOT__"


class WhatIfEquipCommand(Pob2Command):
    """Allow bare `--unequip` (using `--slot`) without swallowing trailing flags like `--json`."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        rewritten: list[str] = []
        i = 0
        while i < len(args):
            tok = args[i]
            if tok == "--":
                rewritten.extend(args[i:])
                break
            if tok == "--unequip":
                if i + 1 >= len(args) or args[i + 1].startswith("-"):
                    rewritten.extend(["--unequip", UNEQUIP_USE_SLOT])
                    i += 1
                    continue
            rewritten.append(tok)
            i += 1
        return super().make_context(info_name, rewritten, parent=parent, **extra)


_SPEC_KEY_MAP: dict[str, str] = {
    "apply": "apply",
    "level": "level",
    "slot": "slot",
    "item_text": "item_text",
    "itemText": "item_text",
    "item_file": "item_file",
    "itemFile": "item_file",
    "unequip": "unequip_slot",
    "unequip_slot": "unequip_slot",
    "unequipSlot": "unequip_slot",
    "weapon_swap": "weapon_swap",
    "weaponSwap": "weapon_swap",
    "alloc_mode": "alloc_mode",
    "allocMode": "alloc_mode",
    "tree_node": "tree_node",
    "treeNode": "tree_node",
    "anoint_preview": "anoint_preview",
    "anointPreview": "anoint_preview",
    "isolated_node": "anoint_preview",
    "isolatedNode": "anoint_preview",
    "attribute_node": "attribute_node",
    "attributeNode": "attribute_node",
    "attribute": "attribute",
    "gem_group": "gem_group",
    "gemGroup": "gem_group",
    "gem_index": "gem_index",
    "gemIndex": "gem_index",
    "gem_level": "gem_level",
    "gemLevel": "gem_level",
    "gem_quality": "gem_quality",
    "gemQuality": "gem_quality",
    "gem_enabled": "gem_enabled",
    "gemEnabled": "gem_enabled",
    "set1": "set1",
    "set2": "set2",
    "config_key": "config_key",
    "configKey": "config_key",
    "config_value": "config_value",
    "configValue": "config_value",
    "custom_mods": "custom_mods",
    "customMods": "custom_mods",
}

_EQUIP_NESTED_MAP: dict[str, str] = {
    "slot": "slot",
    "item_text": "item_text",
    "itemText": "item_text",
    "item_file": "item_file",
    "itemFile": "item_file",
    "unequip": "unequip_slot",
    "unequip_slot": "unequip_slot",
    "unequipSlot": "unequip_slot",
    "weapon_swap": "weapon_swap",
    "weaponSwap": "weapon_swap",
}

_TREE_NESTED_MAP: dict[str, str] = {
    "tree_node": "tree_node",
    "treeNode": "tree_node",
    "node": "tree_node",
    "alloc_mode": "alloc_mode",
    "allocMode": "alloc_mode",
    "anoint_preview": "anoint_preview",
    "anointPreview": "anoint_preview",
    "isolated_node": "anoint_preview",
    "isolatedNode": "anoint_preview",
    "attribute_node": "attribute_node",
    "attributeNode": "attribute_node",
    "attribute": "attribute",
}

_SKILL_NESTED_MAP: dict[str, str] = {
    "gem_group": "gem_group",
    "gemGroup": "gem_group",
    "group": "gem_group",
    "gem_index": "gem_index",
    "gemIndex": "gem_index",
    "index": "gem_index",
    "gem_level": "gem_level",
    "gemLevel": "gem_level",
    "level": "gem_level",
    "gem_quality": "gem_quality",
    "gemQuality": "gem_quality",
    "quality": "gem_quality",
    "gem_enabled": "gem_enabled",
    "gemEnabled": "gem_enabled",
    "enabled": "gem_enabled",
    "set1": "set1",
    "set2": "set2",
}


def parse_config_value(raw: str | None) -> Any:
    if raw is None:
        return None
    low = raw.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _reject_unknown_section_keys(domain: str, bad_keys: list[str], allowed: str) -> None:
    raise DaemonRequestError(
        f"[POE2-INVALID-SPEC] Unknown `{domain}` spec key(s): {', '.join(sorted(bad_keys))}. Allowed: {allowed}."
    )


def flatten_nested_spec(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize natural nested domain specs (`config`, `equip`, `tree`, `skill`) into flat keys."""
    flat: dict[str, Any] = {}
    config_vars: dict[str, Any] = {}

    for k, v in parsed.items():
        if k == "config":
            if not isinstance(v, dict):
                raise DaemonRequestError("[POE2-INVALID-SPEC] `config` section must be a JSON object.")
            for ck, cv in v.items():
                if ck == "level":
                    flat["level"] = cv
                elif ck in ("custom_mods", "customMods"):
                    flat["custom_mods"] = cv
                elif ck in ("config_key", "configKey", "key"):
                    flat["config_key"] = cv
                elif ck in ("config_value", "configValue", "value"):
                    flat["config_value"] = cv
                else:
                    config_vars[str(ck)] = parse_config_value(cv) if isinstance(cv, str) else cv
        elif k == "equip":
            if not isinstance(v, dict):
                raise DaemonRequestError("[POE2-INVALID-SPEC] `equip` section must be a JSON object.")
            bad = [ek for ek in v if ek not in _EQUIP_NESTED_MAP]
            if bad:
                _reject_unknown_section_keys("equip", bad, "slot, item_text, item_file, unequip, weapon_swap")
            for ek, ev in v.items():
                if ek in ("unequip", "unequip_slot", "unequipSlot") and ev is True:
                    if v.get("slot"):
                        flat["unequip_slot"] = v["slot"]
                    else:
                        raise DaemonRequestError("[POE2-INVALID-SPEC] `equip.unequip: true` requires `equip.slot`.")
                elif ev is not None:
                    flat[_EQUIP_NESTED_MAP[ek]] = ev
            if (
                "unequip_slot" in flat
                and flat.get("slot") == flat["unequip_slot"]
                and not flat.get("item_text")
                and not flat.get("item_file")
            ):
                flat.pop("slot", None)
        elif k == "tree":
            if not isinstance(v, dict):
                raise DaemonRequestError("[POE2-INVALID-SPEC] `tree` section must be a JSON object.")
            bad = [tk for tk in v if tk not in _TREE_NESTED_MAP]
            if bad:
                _reject_unknown_section_keys(
                    "tree", bad, "tree_node/node, alloc_mode, anoint_preview, attribute_node, attribute"
                )
            for tk, tv in v.items():
                if tv is not None:
                    flat[_TREE_NESTED_MAP[tk]] = tv
        elif k == "skill":
            if not isinstance(v, dict):
                raise DaemonRequestError("[POE2-INVALID-SPEC] `skill` section must be a JSON object.")
            bad = [sk for sk in v if sk not in _SKILL_NESTED_MAP]
            if bad:
                _reject_unknown_section_keys(
                    "skill",
                    bad,
                    "gem_group/group, gem_index/index, gem_level/level, gem_quality/quality, gem_enabled/enabled, set1, set2",
                )
            for sk, sv in v.items():
                if sv is not None:
                    flat[_SKILL_NESTED_MAP[sk]] = sv
        else:
            flat[k] = v

    if config_vars:
        flat["config_vars"] = config_vars
    return flat


def load_apply_spec(spec: str | None, spec_file: Path | None) -> dict[str, Any]:
    if spec is not None and spec_file is not None:
        raise click.UsageError("Use either `--spec` or `--spec-file`, not both.")
    if spec is None and spec_file is None:
        raise click.UsageError("Provide a JSON mutation spec via `--spec` (JSON or @file) or `--spec-file`.")
    if spec_file is not None:
        raw_text = spec_file.read_text(encoding="utf-8")
    else:
        assert spec is not None
        stripped = spec.strip()
        if stripped.startswith("@"):
            p = Path(stripped[1:])
            if not p.is_file():
                raise FileNotFoundError(f"Spec file not found: {p}")
            raw_text = p.read_text(encoding="utf-8")
        else:
            raw_text = stripped
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DaemonRequestError(f"[POE2-INVALID-SPEC] Invalid JSON in `--spec`: {exc.msg}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise DaemonRequestError("[POE2-INVALID-SPEC] `--spec` must be a non-empty JSON object.")

    flattened = flatten_nested_spec(parsed)
    config_vars = flattened.pop("config_vars", None)
    unknown = sorted(k for k in flattened if k not in _SPEC_KEY_MAP)
    if unknown:
        allowed = ", ".join(sorted(set(_SPEC_KEY_MAP.values()) | {"config", "equip", "tree", "skill"}))
        raise DaemonRequestError(
            f"[POE2-INVALID-SPEC] Unknown what-if spec key(s): {', '.join(unknown)}. Allowed keys: {allowed}."
        )
    out: dict[str, Any] = {}
    for k, v in flattened.items():
        canon_k = _SPEC_KEY_MAP[k]
        if v is not None:
            out[canon_k] = v
    if config_vars:
        out["config_vars"] = config_vars
    if "slot" in out:
        out["slot"] = normalize_poe2_equipment_slot(str(out["slot"]), command="what-if apply")
    if "unequip_slot" in out:
        out["unequip_slot"] = normalize_poe2_equipment_slot(str(out["unequip_slot"]), command="what-if apply")
    if "item_file" in out:
        p = Path(str(out.pop("item_file")))
        if not p.is_file():
            raise FileNotFoundError(f"Item file not found: {p}")
        out["item_text"] = p.read_text(encoding="utf-8")
    if "config_value" in out and isinstance(out["config_value"], str):
        out["config_value"] = parse_config_value(out["config_value"])
    return out


__all__ = [
    "UNEQUIP_USE_SLOT",
    "WhatIfEquipCommand",
    "flatten_nested_spec",
    "load_apply_spec",
    "parse_config_value",
]
