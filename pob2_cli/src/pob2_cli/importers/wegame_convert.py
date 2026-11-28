# SPDX-License-Identifier: GPL-3.0-only
"""Convert sanitized CN WeGame PoE2 share bundles into validated PoB2 0.5.5 XML (L1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ..errors import DaemonRequestError
from ..guardrails import validate_build_xml
from .wegame_item_render import render_wegame_item_text
from .wegame_tables import (
    DEFAULT_QUEST_BOOLEAN_KEYS,
    DEFAULT_QUEST_OPTION_KEYS,
    EQUIPMENT_SLOT_MAP,
    PASSIVE_JEWEL_SLOTS,
    ClassSpec,
    resolve_class_spec,
    resolve_jewel_base_from_metadata_id,
)
from .wegame_translate import WeGameTranslator, is_progress_counter_stat, parse_leading_int

_PASSIVE_JEWEL_RE = re.compile(r"^PassiveJewel_(\d+)$")


def _pob2_xml_attr(value: str) -> str:
    """Format a PoB2 XML attribute matching PoE2/lua/xml.lua `encodeContent`.

    PoB2's `xml.lua` `decodeContent` ONLY decodes `&amp;`, `&lt;`, `&gt;`, `&apos;`, `&quot;`
    and strips `&#10;` to empty string. Keep literal `\\n` inside double-quoted attributes.
    """
    escaped = (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    return f'"{escaped}"'


@dataclass(frozen=True)
class WeGameImportResult:
    """Output of converting a WeGame PoE2 share bundle into PoB2 XML."""

    xml: str
    class_name: str
    ascendancy_name: str
    level: int
    warnings: tuple[str, ...]
    stats: dict[str, int] = field(default_factory=dict)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "className": self.class_name,
            "ascendancyName": self.ascendancy_name,
            "level": self.level,
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
        }


def _map_equipment_slot(item: dict[str, Any]) -> str | None:
    inv = str(item.get("inventoryId") or "")
    if inv == "Flask":
        x = item.get("x")
        if isinstance(x, int) and 0 <= x <= 4:
            return f"Flask {x + 1}" if x < 2 else f"Charm {x - 1}"
        return None
    return EQUIPMENT_SLOT_MAP.get(inv)


def _parse_jewel_data(raw_jewel_data: str) -> list[dict[str, Any]]:
    """Decode GetJewels.jewel_data JSON string, handling WeGame's optional extra `\\"` escape layer."""
    text = (raw_jewel_data or "").strip()
    if not text:
        return []
    parsed: Any = None
    for candidate in (text, text.replace('\\"', '"')):
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(parsed, list):
        raise DaemonRequestError("Invalid WeGame GetJewels.jewel_data JSON payload.")
    return [entry for entry in parsed if isinstance(entry, dict)]


def _resolve_jewel_node_id(socket_id: str) -> int | None:
    """Resolve `PassiveJewel_<n>` via exact 31-row table or direct numeric tree nodeId."""
    s = (socket_id or "").strip()
    m = _PASSIVE_JEWEL_RE.match(s)
    if m is not None:
        return PASSIVE_JEWEL_SLOTS.get(int(m.group(1)))
    if s.isdigit():
        return int(s)
    return None


def _parse_gem_property(gem_obj: dict[str, Any], prop_type: int) -> int | None:
    props = gem_obj.get("properties")
    if not isinstance(props, list):
        return None
    for prop in props:
        if isinstance(prop, dict) and prop.get("type") == prop_type:
            vals = prop.get("values")
            if isinstance(vals, list) and vals and isinstance(vals[0], list) and vals[0]:
                return parse_leading_int(str(vals[0][0]))
    return None


def _build_skill_groups(
    skills: list[dict[str, Any]],
    translator: WeGameTranslator,
    warnings: set[str],
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        active_res = translator.translate_gem(
            str(skill.get("baseType") or ""), is_support=bool(skill.get("support"))
        )
        if active_res.warning:
            warnings.add(active_res.warning)
        if active_res.candidate is None:
            continue
        lvl = _parse_gem_property(skill, 5)
        if lvl is None:
            lvl = 1
            if not active_res.candidate.is_support:
                warnings.add(f"Missing gem level; defaulted to 1: {active_res.candidate.name}")
        group = [
            {
                "cand": active_res.candidate,
                "level": lvl,
                "quality": _parse_gem_property(skill, 6) or 0,
                "corrupted": bool(skill.get("corrupted") is True),
            }
        ]
        for sup in skill.get("socketedItems") or []:
            if not isinstance(sup, dict):
                continue
            sup_res = translator.translate_gem(
                str(sup.get("baseType") or ""), is_support=bool(sup.get("support", True))
            )
            if sup_res.warning:
                warnings.add(sup_res.warning)
            if sup_res.candidate is None:
                continue
            group.append(
                {
                    "cand": sup_res.candidate,
                    "level": _parse_gem_property(sup, 5) or 1,
                    "quality": _parse_gem_property(sup, 6) or 0,
                    "corrupted": bool(sup.get("corrupted") is True),
                }
            )
        groups.append(group)
    return groups


def _parse_tree_section(
    tree: dict[str, Any],
    translator: WeGameTranslator,
    warnings: set[str],
) -> tuple[list[int], list[int], list[int], dict[str, list[int]], list[str]]:
    base_nodes: set[int] = {int(n) for n in (tree.get("hashes") or []) if isinstance(n, int)}
    ws1: list[int] = []
    ws2: list[int] = []
    specs = tree.get("specialisations")
    if isinstance(specs, dict):
        for set_name, ids in specs.items():
            id_list = [int(n) for n in ids if isinstance(n, int)] if isinstance(ids, list) else []
            if set_name == "set1":
                ws1 = sorted(set(id_list))
                base_nodes.update(ws1)
            elif set_name == "set2":
                ws2 = sorted(set(id_list))
                base_nodes.update(ws2)
            elif id_list:
                warnings.add(f"Inactive passive specialisation omitted: {set_name}")

    attr_nodes: dict[str, list[int]] = {"str": [], "dex": [], "int": []}
    overrides = tree.get("skill_overrides")
    if isinstance(overrides, dict):
        for node_key, node_val in overrides.items():
            if not isinstance(node_val, dict):
                continue
            try:
                nid = int(node_key)
            except (TypeError, ValueError):
                continue
            if int(node_val.get("grantedStrength") or 0) > 0:
                attr_nodes["str"].append(nid)
            elif int(node_val.get("grantedDexterity") or 0) > 0:
                attr_nodes["dex"].append(nid)
            elif int(node_val.get("grantedIntelligence") or 0) > 0:
                attr_nodes["int"].append(nid)
    for k in attr_nodes:
        attr_nodes[k] = sorted(set(attr_nodes[k]))

    quest_mods: list[str] = []
    for raw_q in tree.get("quest_stats") or []:
        if not isinstance(raw_q, str) or not raw_q.strip():
            continue
        if is_progress_counter_stat(raw_q):
            continue
        en_q, ok = translator.translate_stat_line(raw_q)
        if not ok:
            warnings.add(f"Untranslated quest_stat preserved: {en_q}")
        if en_q:
            quest_mods.append(en_q)

    return sorted(base_nodes), ws1, ws2, attr_nodes, quest_mods


def _format_pob2_xml(
    *,
    spec: ClassSpec,
    level: int,
    nodes: list[int],
    ws1: list[int],
    ws2: list[int],
    attr_nodes: dict[str, list[int]],
    jewel_sockets: list[tuple[int, int]],
    items: list[tuple[int, str]],
    slot_bindings: list[tuple[str, int]],
    use_second_ws: bool,
    skill_groups: list[list[dict[str, Any]]],
    quest_mods: list[str],
    warnings: list[str],
) -> str:
    def _csv(vals: list[int]) -> str:
        return ",".join(str(v) for v in vals)

    lines: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>', "<PathOfBuilding2>"]
    lines.append(
        f'\t<Build level="{level}" targetVersion="0_1" mainSocketGroup="1" '
        f'className={_pob2_xml_attr(spec.class_name)} ascendClassName={_pob2_xml_attr(spec.ascendancy_name)} '
        f'characterLevelAutoMode="false"/>'
    )

    asc_internal = spec.ascendancy_internal_id or "nil"
    lines.append('\t<Tree activeSpec="1">')
    lines.append(
        f'\t\t<Spec treeVersion="0_5" classId="{spec.class_id}" classInternalId="{spec.class_id}" '
        f'ascendClassId="{spec.ascend_id}" secondaryAscendClassId="nil" '
        f'ascendancyInternalId={_pob2_xml_attr(asc_internal)} nodes="{_csv(nodes)}" masteryEffects="">'
    )
    lines.append(f'\t\t\t<WeaponSet1 nodes="{_csv(ws1)}"/>')
    lines.append(f'\t\t\t<WeaponSet2 nodes="{_csv(ws2)}"/>')
    lines.append("\t\t\t<Sockets>")
    for node_id, item_id in jewel_sockets:
        lines.append(f'\t\t\t\t<Socket nodeId="{node_id}" itemId="{item_id}"/>')
    lines.append("\t\t\t</Sockets>")
    lines.append("\t\t\t<Overrides>")
    lines.append(
        f'\t\t\t\t<AttributeOverride strNodes="{_csv(attr_nodes["str"])}" '
        f'dexNodes="{_csv(attr_nodes["dex"])}" intNodes="{_csv(attr_nodes["int"])}"/>'
    )
    lines.append("\t\t\t</Overrides>")
    lines.append("\t\t</Spec>")
    lines.append("\t</Tree>")

    lines.append('\t<Skills activeSkillSet="1" defaultGemLevel="normalMaximum" defaultGemQuality="0">')
    lines.append('\t\t<SkillSet id="1">')
    for idx, grp in enumerate(skill_groups):
        main_attr = ' mainActiveSkill="1" mainActiveSkillCalcs="1"' if idx == 0 else ' mainActiveSkill="nil"'
        lines.append(f'\t\t\t<Skill enabled="true"{main_attr} includeInFullDPS="nil" label="">')
        for g in grp:
            c = g["cand"]
            corr = "true" if g["corrupted"] else "nil"
            lines.append(
                f'\t\t\t\t<Gem count="1" skillId={_pob2_xml_attr(c.skill_id)} gemId={_pob2_xml_attr(c.gem_id)} '
                f'variantId={_pob2_xml_attr(c.variant_id)} nameSpec={_pob2_xml_attr(c.name)} '
                f'level="{g["level"]}" quality="{g["quality"]}" enabled="true" '
                f'enableGlobal1="true" enableGlobal2="true" corrupted="{corr}"/>'
            )
        lines.append("\t\t\t</Skill>")
    lines.append("\t\t</SkillSet>")
    lines.append("\t</Skills>")

    lines.append('\t<Config activeConfigSet="1">')
    lines.append('\t\t<ConfigSet id="1" title="Default">')
    for q_key in DEFAULT_QUEST_BOOLEAN_KEYS:
        lines.append(f'\t\t\t<Input name={_pob2_xml_attr(q_key)} boolean="false"/>')
    for q_key in DEFAULT_QUEST_OPTION_KEYS:
        lines.append(f'\t\t\t<Input name={_pob2_xml_attr(q_key)} string=""/>')
    if quest_mods:
        # Keep literal newlines in string="..." for PoE2/lua/xml.lua decodeContent compatibility.
        lines.append(f'\t\t\t<Input name="customMods" string={_pob2_xml_attr(chr(10).join(quest_mods))}/>')
    lines.append("\t\t</ConfigSet>")
    lines.append("\t</Config>")

    # In PoB2, useSecondWeaponSet="true" activates WS2 as the active panel; keep WS1 active ("false")
    # by default while still populating Weapon 1 Swap / Weapon 2 Swap slots and <WeaponSet2>.
    _ = use_second_ws
    ws_flag = "false"
    lines.append(f'\t<Items activeItemSet="1" useSecondWeaponSet="{ws_flag}" showStatDifferences="true">')
    for item_id, body in items:
        lines.append(f'\t\t<Item id="{item_id}">')
        lines.append(escape(body))
        lines.append("\t\t</Item>")
    lines.append(f'\t\t<ItemSet id="1" useSecondWeaponSet="{ws_flag}" title="Default">')
    for slot_name, item_id in slot_bindings:
        lines.append(f'\t\t\t<Slot name={_pob2_xml_attr(slot_name)} itemId="{item_id}"/>')
    for node_id, _item_id in jewel_sockets:
        lines.append(f'\t\t\t<SocketIdURL nodeId="{node_id}" name="Jewel {node_id}" itemPbURL=""/>')
    lines.append("\t\t</ItemSet>")
    lines.append("\t</Items>")

    note_lines = ["WeGame PoE2 CN Import", *(f"[import] {w}" for w in warnings)]
    lines.append(f"\t<Notes>{escape(chr(10).join(note_lines))}</Notes>")
    lines.append("</PathOfBuilding2>")
    return "\n".join(lines) + "\n"


def convert_wegame_bundle_to_pob2_xml(
    bundle: dict[str, Any],
    *,
    translator: WeGameTranslator | None = None,
    data_dir: Path | None = None,
) -> WeGameImportResult:
    """Convert a sanitized v1 WeGame share bundle into a validated PoB2 0.5.5 XML build."""
    if not isinstance(bundle, dict) or bundle.get("format") != "wegame" or bundle.get("version") != 1:
        raise DaemonRequestError("Unsupported WeGame bundle: expected format='wegame', version=1.")

    role = bundle.get("role")
    if not isinstance(role, dict):
        raise DaemonRequestError("WeGame bundle is missing role metadata.")
    level = role.get("level")
    if not isinstance(level, int) or isinstance(level, bool) or not (1 <= level <= 100):
        raise DaemonRequestError(f"Unsupported WeGame character level: {level!r}.")
    class_spec = resolve_class_spec(str(role.get("class_name") or ""))
    if class_spec is None:
        raise DaemonRequestError(f"Unknown PoE2 class/ascendancy in WeGame bundle: {role.get('class_name')!r}.")

    tr = translator or WeGameTranslator(data_dir=data_dir)
    is_shaman = class_spec.ascendancy_name == "Shaman"
    warnings: set[str] = set()

    tree_obj = bundle.get("talent_tree") if isinstance(bundle.get("talent_tree"), dict) else {}
    nodes, ws1, ws2, attr_nodes, quest_mods = _parse_tree_section(tree_obj, tr, warnings)

    items: list[tuple[int, str]] = []
    slot_bindings: list[tuple[str, int]] = []
    next_item_id = 1
    has_swap_weapon = False
    for raw_item in bundle.get("equipments") or []:
        if not isinstance(raw_item, dict):
            continue
        slot_name = _map_equipment_slot(raw_item)
        if slot_name is None:
            warnings.add(f"Equipment slot omitted: {raw_item.get('inventoryId')!r}")
            continue
        if slot_name in ("Weapon 1 Swap", "Weapon 2 Swap"):
            has_swap_weapon = True
        body = render_wegame_item_text(raw_item, tr, warnings, is_shaman=is_shaman)
        items.append((next_item_id, body))
        slot_bindings.append((slot_name, next_item_id))
        next_item_id += 1

    # Read top-level GetJewels.jewel_data (distinct from talent_tree.jewel_data radius dict).
    raw_jewel_str = bundle.get("jewel_data")
    jewels_parsed = _parse_jewel_data(raw_jewel_str) if isinstance(raw_jewel_str, str) else []
    jewel_sockets: list[tuple[int, int]] = []
    for entry in jewels_parsed:
        jewel = entry.get("jewel")
        if not isinstance(jewel, dict):
            continue
        sock_id = str(entry.get("socket_id") or "")
        node_id = _resolve_jewel_node_id(sock_id)
        if node_id is None:
            warnings.add(f"Unknown jewel socket: {sock_id}")
            continue
        mod_lines: list[Any] = []
        for grp in jewel.get("mod_descriptions") or []:
            if isinstance(grp, dict) and isinstance(grp.get("values"), list):
                mod_lines.extend(grp["values"])
        # Prefer jewel.id metadata path (e.g. Metadata/Items/Jewels/JewelDex -> Emerald) over
        # localized name so PoE1 legacy names never corrupt PoE2 jewel base types.
        jewel_base = (
            resolve_jewel_base_from_metadata_id(jewel.get("id"))
            or jewel.get("name")
            or jewel.get("id")
            or "Ruby"
        )
        synth_item = {
            "baseType": jewel_base,
            "name": jewel.get("display_name") or "",
            "frameType": jewel.get("rarity", 2),
            "explicitMods": mod_lines,
        }
        body = render_wegame_item_text(synth_item, tr, warnings, is_shaman=is_shaman)
        items.append((next_item_id, body))
        jewel_sockets.append((node_id, next_item_id))
        next_item_id += 1

    skill_groups = _build_skill_groups(bundle.get("skills") or [], tr, warnings)

    warnings.add(
        "Check main skill, combat configuration, and resistance cap/overcap buffer penalty before comparing upgrades."
    )
    use_second_ws = has_swap_weapon or bool(ws2)
    if use_second_ws:
        warnings.add("WeGame does not export skill weapon-set bindings; assign WS1/WS2 bindings on the Skills tab.")

    sorted_warnings = sorted(warnings)
    xml = _format_pob2_xml(
        spec=class_spec,
        level=level,
        nodes=nodes,
        ws1=ws1,
        ws2=ws2,
        attr_nodes=attr_nodes,
        jewel_sockets=jewel_sockets,
        items=items,
        slot_bindings=slot_bindings,
        use_second_ws=use_second_ws,
        skill_groups=skill_groups,
        quest_mods=quest_mods,
        warnings=sorted_warnings,
    )
    validate_build_xml(xml, expected_game="poe2")

    return WeGameImportResult(
        xml=xml,
        class_name=class_spec.class_name,
        ascendancy_name=class_spec.ascendancy_name,
        level=level,
        warnings=tuple(sorted_warnings),
        stats={
            "equipments": len(slot_bindings),
            "jewels": len(jewel_sockets),
            "skillGroups": len(skill_groups),
            "gems": sum(len(g) for g in skill_groups),
            "passiveNodes": len(nodes),
            "weaponSet1Nodes": len(ws1),
            "weaponSet2Nodes": len(ws2),
            "attributeOverrides": sum(len(v) for v in attr_nodes.values()),
            "questStats": len(quest_mods),
        },
    )


__all__ = [
    "WeGameImportResult",
    "convert_wegame_bundle_to_pob2_xml",
]
