# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 S4/T3.4 Deep Audit: 5 intuition-trap audits, WS1/WS2 swap audit, 4-dim dehydration,
item-101 Cap+Overcap resistance audit, and [PoB2未模拟] honest red-card shield (L2).
"""

from __future__ import annotations

from typing import Any

from ..game_profile import GAME_ID
from ..importers.post_load_fixups import detect_worker_res_override_lines
from .audit_rules import (
    DEFAULT_OVERCAP_TARGET,
    classify_weapon_set_swap_finding,
    evaluate_resistances_cap_overcap,
)
from .version_stamp import attach_version_stamp


def _build_resource_and_swap_findings(
    probe: dict[str, Any],
    dehydration: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    sp = probe.get("spirit") or {}
    if sp.get("overloaded"):
        findings.append(
            {
                "category": "spirit_overloaded",
                "severity": "critical",
                "spiritTotal": sp.get("total"),
                "spiritUnreserved": sp.get("unreserved"),
                "message": f"Spirit is overloaded ({sp.get('unreserved'):.0f} unreserved / {sp.get('total'):.0f} total).",
            }
        )
    ws_finding = classify_weapon_set_swap_finding(probe.get("weaponSetSwapAudit") or {})
    if ws_finding is not None:
        findings.append(ws_finding)

    bonded = probe.get("bondedRunes") or {}
    if bonded.get("affectedItemCount", 0) > 0 and not bonded.get("canUseBonded"):
        asc = bonded.get("ascendClassName") or "None"
        slots = [str(it.get("slot") or "") for it in bonded.get("affectedItems") or [] if isinstance(it, dict)]
        findings.append(
            {
                "category": "inactive_bonded_runes",
                "severity": "warning",
                "ascendClassName": asc,
                "isShaman": bool(bonded.get("isShaman")),
                "canUseBonded": False,
                "affectedSlots": slots,
                "message": (
                    f"Build ({asc}) lacks Shaman's 'Wisdom of the Maji' (CanUseBondedModifiers); "
                    f"Bonded rune modifiers on {bonded['affectedItemCount']} item(s) ({', '.join(slots)}) are inactive."
                ),
            }
        )

    cb = probe.get("charmBloat") or {}
    if cb.get("hasForUtopia") or cb.get("conditionUsingCharm"):
        findings.append(
            {
                "category": "charm_for_utopia_bloat",
                "severity": "warning",
                "hasForUtopia": bool(cb.get("hasForUtopia")),
                "conditionUsingCharm": bool(cb.get("conditionUsingCharm")),
                "message": "Detected `For Utopia` / conditional Charm active config inflating EHP; stripped in PinnacleSustained.",
            }
        )
    if float(dehydration.get("dpsInflationRatio") or 0) >= 1.5 or float(dehydration.get("ehpInflationRatio") or 0) >= 1.5:
        findings.append(
            {
                "category": "panel_inflation",
                "severity": "warning",
                "dpsInflationRatio": dehydration.get("dpsInflationRatio"),
                "ehpInflationRatio": dehydration.get("ehpInflationRatio"),
                "message": (
                    f"Raw panel is inflated vs PinnacleSustained "
                    f"(DPS {dehydration.get('dpsInflationRatio')}x, EHP {dehydration.get('ehpInflationRatio')}x)."
                ),
            }
        )
    return findings


def _build_findings_from_probe(
    probe: dict[str, Any],
    dehydration: dict[str, Any],
    res_audit: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    # 1. Two-step terminal conversion 0% dead mods
    for dm in (probe.get("conversion") or {}).get("deadMods") or []:
        findings.append(
            {
                "category": "conversion_dead_mod",
                "severity": "warning",
                "sourceDamageType": dm.get("sourceDamageType"),
                "wastedIncPct": dm.get("wastedIncPct", 0),
                "wastedMorePct": dm.get("wastedMorePct", 0),
                "message": (
                    f"100%-converted `{dm.get('sourceDamageType')}` has 0% dead source-type modifiers "
                    f"(+{dm.get('wastedIncPct', 0):.0f}% inc / +{dm.get('wastedMorePct', 0):.0f}% more) "
                    "due to PoE2 two-step terminal conversion."
                ),
            }
        )

    # 2. Armour ArmourRatio=10 pre-resistance drop-off on big hits
    arm = probe.get("armour") or {}
    ladder = arm.get("physicalLadder") or []
    if float(arm.get("armour") or 0) > 0 and len(ladder) >= 4:
        m_1k = ladder[0].get("mitigationPct", 0)
        m_20k = ladder[-1].get("mitigationPct", 0)
        if m_1k - m_20k >= 25.0:
            findings.append(
                {
                    "category": "armour_big_hit_dropoff",
                    "severity": "warning",
                    "armour": arm.get("armour"),
                    "mitigation1kPct": m_1k,
                    "mitigation20kPct": m_20k,
                    "hasElementalArmour": bool(arm.get("hasElementalArmour")),
                    "tooltipErrata": arm.get("tooltipErrata"),
                    "message": (
                        f"Armour ({arm.get('armour'):.0f}, ArmourRatio=10) mitigates {m_1k:.1f}% of a 1k hit "
                        f"but drops to {m_20k:.1f}% against a 20k slam."
                    ),
                }
            )

    # 3. Deflection 95% cap illusion vs raw un-deflected MaxHit
    defl = probe.get("deflection") or {}
    if defl.get("isIllusionRisk"):
        findings.append(
            {
                "category": "deflection_ehp_illusion",
                "severity": "warning",
                "deflectChance": defl.get("chance"),
                "deflectCap": defl.get("chanceCap", 95),
                "totalEhp": defl.get("totalEhp"),
                "rawPhysMaxHit": defl.get("rawPhysMaxHit"),
                "rawEleMinMaxHit": defl.get("rawEleMinMaxHit"),
                "message": (
                    f"Deflection ({defl.get('chance'):.0f}% / {defl.get('chanceCap', 95)}% cap) inflates expected TotalEHP "
                    f"({defl.get('totalEhp', 0):.0f}), but raw un-deflected MaxHit is {defl.get('rawPhysMaxHit', 0):.0f} Phys / "
                    f"{defl.get('rawEleMinMaxHit', 0):.0f} Ele (0% deflection mitigation on lethal bypass hits)."
                ),
            }
        )

    # 3b. 50% Block cap overcap waste
    blk = probe.get("block") or {}
    if float(blk.get("overcap") or 0) > 0:
        findings.append(
            {
                "category": "block_cap_overcap",
                "severity": "advisory",
                "blockChance": blk.get("chance"),
                "blockCap": blk.get("maxCap", 50),
                "overcap": blk.get("overcap"),
                "message": f"Block Chance exceeds PoE2 {blk.get('maxCap', 50):.0f}% cap by +{blk.get('overcap'):.1f}%.",
            }
        )

    # 3c. Crossbow EffectiveReloadTime cycle loss
    xbow = probe.get("crossbow")
    if isinstance(xbow, dict) and float(xbow.get("reloadDpsLossPct") or 0) >= 10.0:
        findings.append(
            {
                "category": "crossbow_reload_cycle",
                "severity": "advisory",
                "reloadTime": xbow.get("reloadTime"),
                "effectiveReloadTime": xbow.get("effectiveReloadTime"),
                "reloadDpsLossPct": xbow.get("reloadDpsLossPct"),
                "message": (
                    f"Crossbow reload cycle (EffectiveReloadTime={xbow.get('effectiveReloadTime'):.2f}s) "
                    f"reduces sustained DPS rate by {xbow.get('reloadDpsLossPct'):.1f}% vs burst firing rate."
                ),
            }
        )

    # 4. Chaos 2x ES, Poison bypass, CI 1-Life stun/freeze perma-lock
    es_ci = probe.get("esChaosCi") or {}
    if es_ci.get("ciStunFreezeLockRisk"):
        findings.append(
            {
                "category": "ci_1life_stun_freeze_lock",
                "severity": "critical",
                "message": (
                    "Chaos Inoculation (1 Life) without ES-based Stun/Freeze threshold or immunity "
                    "exposes the character to permanent stun/freeze locks."
                ),
            }
        )
    elif es_ci.get("chaosDoubleEsDamage"):
        findings.append(
            {
                "category": "chaos_2x_es_and_poison_bypass",
                "severity": "warning",
                "life": es_ci.get("life"),
                "energyShield": es_ci.get("energyShield"),
                "message": (
                    f"Non-CI Energy Shield ({es_ci.get('energyShield'):.0f} ES / {es_ci.get('life'):.0f} Life) "
                    "takes 2x Chaos damage to ES and Poison bypasses ES directly to Life."
                ),
            }
        )

    # 4b / 4c. Spirit overload, WS1/WS2 swap audit, inactive Bonded runes, and Charm/panel inflation
    findings.extend(_build_resource_and_swap_findings(probe, dehydration))

    # 5. [PoB2未模拟] honest red cards
    for rc in probe.get("unsimulatedRedCards") or []:
        findings.append(
            {
                "category": "pob2_unsimulated_red_card",
                "severity": "red_card",
                "tag": "[PoB2未模拟]",
                "issue": rc.get("issue"),
                "skillName": rc.get("skillName"),
                "message": f"[PoB2未模拟] {rc.get('skillName')} ({rc.get('issue')}): {rc.get('reason')}",
            }
        )

    # 6. Item-101 Resistance cap + overcap buffer issues (NEVER equalization!)
    for r_iss in res_audit.get("issues") or []:
        findings.append({"category": "resistance_audit", **r_iss})

    return findings


def run_deep_audit(
    worker: Any,
    *,
    overcap_target: int = DEFAULT_OVERCAP_TARGET,
    chaos_floor: int = 0,
) -> dict[str, Any]:
    """Execute full PoE2 S4/T3.4 deep audit + 4-dim honest dehydration on live BridgeWorker."""
    with worker.lock:
        probe = worker.call("deep_audit_probe", {}, timeout=45.0)
        dehydration = worker.call("eval_honest_dehydration", {}, timeout=45.0)
        diag = worker.get_engine_diagnostics()
        res_override = detect_worker_res_override_lines(worker)

    res_audit = evaluate_resistances_cap_overcap(
        probe.get("resists") or {},
        overcap_target=overcap_target,
        chaos_floor=chaos_floor,
    )
    if res_override["matches"]:
        for warn_txt in res_override["warnings"]:
            res_audit["issues"].append(
                {
                    "code": "res_override_line",
                    "tag": "[res-override-line]",
                    "severity": "warning",
                    "affectedElements": res_override["affectedElements"],
                    "affectedSlots": res_override["affectedSlots"],
                    "matches": res_override["matches"],
                    "message": warn_txt,
                }
            )
    findings = _build_findings_from_probe(probe, dehydration, res_audit)
    payload: dict[str, Any] = {
        "game": GAME_ID,
        "dehydration": dehydration,
        "conversion": probe.get("conversion"),
        "tabulateAttribution": probe.get("tabulateAttribution") or [],
        "armour": probe.get("armour"),
        "deflection": probe.get("deflection"),
        "block": probe.get("block"),
        "esChaosCi": probe.get("esChaosCi"),
        "pinnacleBuildup": probe.get("pinnacleBuildup"),
        "crossbow": probe.get("crossbow"),
        "spirit": probe.get("spirit"),
        "charmBloat": probe.get("charmBloat"),
        "bondedRunes": probe.get("bondedRunes"),
        "weaponSetSwapAudit": probe.get("weaponSetSwapAudit"),
        "resistances": res_audit,
        "unsimulatedRedCards": probe.get("unsimulatedRedCards") or [],
        "engineDiagnostics": diag,
        "findings": findings,
        "warnings": list(res_override["warnings"]),
    }
    return attach_version_stamp(
        payload,
        pob2_version=probe.get("pob2Version") or getattr(worker, "pob_version", None),
        tree_version=probe.get("treeVersion"),
    )


__all__ = [
    "DEFAULT_OVERCAP_TARGET",
    "evaluate_resistances_cap_overcap",
    "run_deep_audit",
]
