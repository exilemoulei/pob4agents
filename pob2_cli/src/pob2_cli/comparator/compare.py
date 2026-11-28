# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 5-Dimension Compare Power Report: Items / Passive Tree / Main Skill / Support Gems / Config (L2)."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from typing import Any

from ..analyzer.version_stamp import attach_version_stamp
from ..bridge_protocol import decode_share_code_xml, encode_xml_share_code
from ..errors import DaemonRequestError
from ..game_profile import GAME_ID
from ..importers.share_urls import resolve_poe2_build_source
from ..snapshot import BuildScope, BuildTransaction, capture_digest
from ..snapshot.helpers import finite_float

_DIMENSION_LABELS: dict[str, tuple[str, str]] = {
    "items": ("Items", "装备"),
    "tree": ("Passive Tree", "天赋树"),
    "main_skill": ("Main Active Skill", "主技能"),
    "supports": ("Support Gems", "辅助宝石"),
    "config": ("Config & Custom Mods", "配置"),
}


def _export_xml(worker: Any) -> str:
    res = worker.call("save_xml", {}) or {}
    xml_txt = str(res.get("xml") or "").strip()
    if xml_txt.startswith("<"):
        return xml_txt
    code = str((worker.call("export_code", {}) or {}).get("code") or "")
    decoded = decode_share_code_xml(code)
    if not decoded:
        raise DaemonRequestError("Failed exporting PoB2 XML from worker for comparison.")
    return decoded


def _find_main_skill_elem(root: ET.Element) -> ET.Element | None:
    build_el = root.find("Build")
    try:
        main_idx = int((build_el.attrib.get("mainSocketGroup") if build_el is not None else "1") or "1")
    except ValueError:
        main_idx = 1
    skills_el = root.find("Skills")
    if skills_el is None:
        return None
    active_set_id = skills_el.attrib.get("activeSkillSet", "1")
    target_set = None
    for sset in skills_el.findall("SkillSet"):
        if sset.attrib.get("id") == active_set_id:
            target_set = sset
            break
    if target_set is None:
        target_set = skills_el.find("SkillSet")
    container = target_set if target_set is not None else skills_el
    groups = container.findall("Skill")
    if not groups:
        return None
    idx = max(0, min(main_idx - 1, len(groups) - 1))
    return groups[idx]


def _is_support_gem(gem_el: ET.Element, index: int) -> bool:
    name_spec = (gem_el.attrib.get("nameSpec") or "").lower()
    skill_id = (gem_el.attrib.get("skillId") or "").lower()
    gem_id = (gem_el.attrib.get("gemId") or "").lower()
    if "support" in name_spec or "support" in skill_id or "support" in gem_id:
        return True
    return index > 0


def _splice_dimension_xml(main_xml: str, ref_xml: str, dimension: str) -> str:
    m_root = ET.fromstring(main_xml)
    r_root = ET.fromstring(ref_xml)

    def _replace_section(tag: str) -> None:
        r_el = r_root.find(tag)
        if r_el is None:
            return
        m_el = m_root.find(tag)
        if m_el is not None:
            m_root.remove(m_el)
        m_root.append(copy.deepcopy(r_el))

    if dimension == "items":
        _replace_section("Items")
    elif dimension == "tree":
        _replace_section("Tree")
        m_b, r_b = m_root.find("Build"), r_root.find("Build")
        if m_b is not None and r_b is not None:
            for attr in ("className", "ascendClassName", "level"):
                if r_b.attrib.get(attr):
                    m_b.attrib[attr] = r_b.attrib[attr]
    elif dimension == "config":
        _replace_section("Config")
    elif dimension in ("main_skill", "supports"):
        m_grp = _find_main_skill_elem(m_root)
        r_grp = _find_main_skill_elem(r_root)
        if m_grp is not None and r_grp is not None:
            m_gems = list(m_grp.findall("Gem"))
            r_gems = list(r_grp.findall("Gem"))
            m_active = [g for i, g in enumerate(m_gems) if not _is_support_gem(g, i)]
            m_supps = [g for i, g in enumerate(m_gems) if _is_support_gem(g, i)]
            r_active = [g for i, g in enumerate(r_gems) if not _is_support_gem(g, i)]
            r_supps = [g for i, g in enumerate(r_gems) if _is_support_gem(g, i)]
            for g in m_gems:
                m_grp.remove(g)
            new_gems = (
                [copy.deepcopy(g) for g in (r_active or m_active)] + [copy.deepcopy(g) for g in m_supps]
                if dimension == "main_skill"
                else [copy.deepcopy(g) for g in m_active] + [copy.deepcopy(g) for g in r_supps]
            )
            for g in new_gems:
                m_grp.append(g)
            if dimension == "main_skill" and r_grp.attrib.get("mainActiveSkill"):
                m_grp.attrib["mainActiveSkill"] = r_grp.attrib["mainActiveSkill"]

    return ET.tostring(m_root, encoding="unicode")


def _pct(delta: float, base: float) -> float:
    if abs(base) <= 1e-6:
        return 0.0 if abs(delta) <= 1e-6 else 100.0
    return round((delta / abs(base)) * 100.0, 1)


def compare_builds(
    main_worker: Any,
    *,
    ref_worker: Any | None = None,
    ref_source: str | None = None,
    ref_xml_code: str | None = None,
) -> dict[str, Any]:
    """Compare ``main_worker`` against a reference build across 5 dimensions (Items, Tree, Main Skill, Supports, Config)."""
    if ref_source or ref_xml_code:
        resolved = resolve_poe2_build_source(
            source=ref_source,
            xml_code=ref_xml_code,
            data_dir=getattr(main_worker, "data_dir", None),
        )
        ref_xml = resolved.xml
        ref_label = resolved.source_label
    elif ref_worker is not None:
        with ref_worker.lock:
            ref_xml = _export_xml(ref_worker)
        ref_label = f"slot:{getattr(ref_worker, 'slot_name', 'ref')}"
    else:
        raise DaemonRequestError("`compare` requires a reference build source (`ref_source`/`ref_xml_code`) or loaded `ref` slot.")

    with BuildTransaction(main_worker, scope=BuildScope.REPLACES_BUILD, will_commit=False) as tx:
        main_xml = _export_xml(main_worker)
        main_digest = capture_digest(main_worker)
        base_dps = finite_float(main_digest.get("primaryDps"))
        base_ehp = finite_float(main_digest.get("totalEhp"))
        base_life = finite_float(main_digest.get("life"))

        # Measure full reference build on the same engine for exact apples-to-apples headline
        main_worker.call("import_code", {"code": encode_xml_share_code(ref_xml), "mode": "replace"})
        ref_digest = capture_digest(main_worker)
        ref_dps = finite_float(ref_digest.get("primaryDps"))
        ref_ehp = finite_float(ref_digest.get("totalEhp"))
        ref_life = finite_float(ref_digest.get("life"))

        dimensions: list[dict[str, Any]] = []
        for dim_key, (label_en, label_zh) in _DIMENSION_LABELS.items():
            try:
                hybrid_xml = _splice_dimension_xml(main_xml, ref_xml, dim_key)
                main_worker.call("import_code", {"code": encode_xml_share_code(hybrid_xml), "mode": "replace"})
                d_dig = capture_digest(main_worker)
                d_dps = finite_float(d_dig.get("primaryDps"))
                d_ehp = finite_float(d_dig.get("totalEhp"))
                d_life = finite_float(d_dig.get("life"))
                delta_dps = d_dps - base_dps
                delta_ehp = d_ehp - base_ehp
                dps_pct = _pct(delta_dps, base_dps)
                ehp_pct = _pct(delta_ehp, base_ehp)
                dimensions.append(
                    {
                        "dimension": dim_key,
                        "label": label_en,
                        "labelZh": label_zh,
                        "dps": d_dps,
                        "deltaDps": delta_dps,
                        "deltaDpsPct": dps_pct,
                        "totalEhp": d_ehp,
                        "deltaEhp": delta_ehp,
                        "deltaEhpPct": ehp_pct,
                        "life": d_life,
                        "deltaLife": d_life - base_life,
                        "impactScore": round(abs(dps_pct) + 0.5 * abs(ehp_pct), 1),
                    }
                )
            except Exception as exc:
                dimensions.append(
                    {
                        "dimension": dim_key,
                        "label": label_en,
                        "labelZh": label_zh,
                        "error": str(exc),
                        "deltaDps": 0.0,
                        "deltaDpsPct": 0.0,
                        "deltaEhp": 0.0,
                        "deltaEhpPct": 0.0,
                        "impactScore": 0.0,
                    }
                )
        tx.rollback()

    dimensions.sort(key=lambda r: (r.get("deltaDpsPct", 0.0), r.get("deltaEhpPct", 0.0), r.get("impactScore", 0.0)), reverse=True)
    for rank_i, row in enumerate(dimensions, 1):
        row["rank"] = rank_i

    info = main_digest.get("buildInfo") or {}
    return attach_version_stamp(
        {
            "game": GAME_ID,
            "reference": ref_label,
            "main": {
                "className": info.get("className"),
                "level": info.get("level"),
                "primaryDpsKey": main_digest["primaryDpsKey"],
                "primaryDps": base_dps,
                "totalEhp": base_ehp,
                "life": base_life,
            },
            "ref": {
                "className": (ref_digest.get("buildInfo") or {}).get("className"),
                "level": (ref_digest.get("buildInfo") or {}).get("level"),
                "primaryDpsKey": ref_digest["primaryDpsKey"],
                "primaryDps": ref_dps,
                "totalEhp": ref_ehp,
                "life": ref_life,
            },
            "gap": {
                "deltaDps": ref_dps - base_dps,
                "deltaDpsPct": _pct(ref_dps - base_dps, base_dps),
                "deltaEhp": ref_ehp - base_ehp,
                "deltaEhpPct": _pct(ref_ehp - base_ehp, base_ehp),
            },
            "topBottleneck": dimensions[0]["dimension"] if dimensions else None,
            "dimensions": dimensions,
        },
        pob2_version=getattr(main_worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


__all__ = ["compare_builds"]
