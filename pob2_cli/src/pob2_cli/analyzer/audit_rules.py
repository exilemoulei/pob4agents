# SPDX-License-Identifier: GPL-3.0-only
"""Resistance cap/overcap policy and weapon-set swap audit classifiers for PoE2 Deep Audit (L2)."""

from __future__ import annotations

from typing import Any

DEFAULT_OVERCAP_TARGET = 15
IDEAL_OVERCAP_UPPER = 30


def evaluate_resistances_cap_overcap(
    resists: dict[str, dict[str, Any]],
    *,
    overcap_target: int = DEFAULT_OVERCAP_TARGET,
    chaos_floor: int = 0,
) -> dict[str, Any]:
    """Audit PoE2 resistances strictly per item-101 rule:
      1. Hard floor: each Elemental Resist >= its own MaxResist cap (Chaos >= chaos_floor).
      2. Overcap buffer: report +15%..+30% headroom for Waystone curses / Exposure.
      3. NEVER penalize unequal healthy overcaps (e.g. 105%/78%/82% is healthy, NOT an issue!).
    """
    issues: list[dict[str, Any]] = []
    entries: dict[str, dict[str, Any]] = {}

    for elem in ("Fire", "Cold", "Lightning", "Chaos"):
        rec = resists.get(elem) or {}
        final_v = float(rec.get("final", 0))
        total_v = float(rec.get("total", final_v))
        cap_v = float(rec.get("cap", 75))
        overcap = total_v - cap_v
        is_ele = elem != "Chaos"

        if is_ele:
            capped = final_v >= cap_v
            if not capped:
                status = "uncapped"
                issues.append(
                    {
                        "code": "resistance_uncapped",
                        "severity": "critical",
                        "element": elem,
                        "final": final_v,
                        "cap": cap_v,
                        "deficit": cap_v - final_v,
                        "message": f"{elem} Resistance is uncapped ({final_v:.0f}% / {cap_v:.0f}% cap; missing {cap_v - final_v:.0f}%).",
                    }
                )
            elif overcap < overcap_target:
                status = "capped_low_overcap"
                issues.append(
                    {
                        "code": "resistance_low_overcap_buffer",
                        "severity": "advisory",
                        "element": elem,
                        "final": final_v,
                        "total": total_v,
                        "cap": cap_v,
                        "overcapBuffer": overcap,
                        "recommendedBuffer": overcap_target,
                        "message": (
                            f"{elem} Resistance is capped ({final_v:.0f}%) with +{overcap:.0f}% overcap buffer "
                            f"(recommend +{overcap_target}%..+{IDEAL_OVERCAP_UPPER}% buffer for Waystone curses/Exposure)."
                        ),
                    }
                )
            else:
                status = "capped_with_buffer"
        else:
            capped = final_v >= chaos_floor
            if final_v < chaos_floor:
                status = "below_floor"
                issues.append(
                    {
                        "code": "chaos_resistance_low",
                        "severity": "warning",
                        "element": elem,
                        "final": final_v,
                        "floor": chaos_floor,
                        "message": f"Chaos Resistance is {final_v:.0f}% (below target floor {chaos_floor}%).",
                    }
                )
            else:
                status = "healthy"

        entries[elem] = {
            "final": final_v,
            "total": total_v,
            "cap": cap_v,
            "overcapBuffer": overcap,
            "capped": capped,
            "status": status,
        }

    return {
        "policy": "cap_plus_overcap_buffer_no_equalization",
        "overcapTargetPct": overcap_target,
        "elements": entries,
        "allElementalCapped": all(entries[e]["capped"] for e in ("Fire", "Cold", "Lightning")),
        "issues": issues,
    }


def classify_weapon_set_swap_finding(ws_swap: dict[str, Any]) -> dict[str, Any] | None:
    """Classify WS1 <-> WS2 swap audit without false-positive criticals on empty WS2 or identical deficits."""
    if not ws_swap or not ws_swap.get("checked"):
        return None
    ws1 = ws_swap.get("ws1") or {}
    ws2 = ws_swap.get("ws2") or {}
    active_ws = int(ws_swap.get("activeWeaponSet") or 1)
    cur = ws1 if active_ws == 1 else ws2
    oth = ws2 if active_ws == 1 else ws1

    if "ws2Populated" in ws_swap:
        ws2_populated = bool(ws_swap["ws2Populated"])
    elif "populated" in ws2:
        ws2_populated = bool(ws2["populated"])
    else:
        ws2_populated = bool((ws2.get("dps") or 0) > 0)

    oth_populated = bool(ws_swap.get("ws1Populated", True)) if active_ws == 2 else ws2_populated
    cur_def = cur.get("attrDeficits") or {}
    oth_def = oth.get("attrDeficits") or {}
    identical_deficits = cur_def == oth_def

    introduces_attr = bool(ws_swap.get("introducesNewAttrDeficit"))
    if "introducesNewAttrDeficit" not in ws_swap and oth_populated:
        for attr, o_info in oth_def.items():
            c_info = cur_def.get(attr)
            if not c_info or abs(float(o_info.get("deficit") or 0)) > abs(float(c_info.get("deficit") or 0)):
                introduces_attr = True
                break

    cur_sp = float(cur.get("spiritUnreserved") or 0)
    oth_sp = float(oth.get("spiritUnreserved") or 0)
    introduces_spirit = bool(ws_swap.get("introducesSpiritDeficit"))
    if "introducesSpiritDeficit" not in ws_swap and oth_populated:
        introduces_spirit = (cur_sp >= 0 and oth_sp < 0) or (cur_sp < 0 and oth_sp < cur_sp)
    disables_aura = bool(ws_swap.get("disablesAuraOnSwap")) and oth_populated

    if not ws2_populated:
        if ws_swap.get("hasSwapAttrRedText") or ws_swap.get("hasSwapSpiritDrop"):
            return {
                "category": "weapon_set_swap_gap",
                "severity": "info",
                "ws2Empty": True,
                "hasSwapSpiritDrop": False,
                "hasSwapAttrRedText": bool(ws_swap.get("hasSwapAttrRedText")),
                "ws1": ws1,
                "ws2": ws2,
                "message": (
                    "Weapon Set 2 is empty/unconfigured; attribute or Spirit deficits shown are baseline WS1 state, "
                    "not a weapon-swap regression."
                ),
            }
        return None

    if introduces_attr or introduces_spirit or disables_aura:
        reasons: list[str] = []
        if introduces_attr:
            reasons.append("new attribute deficit")
        if introduces_spirit:
            reasons.append(f"SpiritUnreserved drops to {oth_sp:.0f}")
        if disables_aura:
            reasons.append("disables active aura/reservation")
        return {
            "category": "weapon_set_swap_gap",
            "severity": "critical",
            "hasSwapSpiritDrop": bool(introduces_spirit or disables_aura),
            "hasSwapAttrRedText": bool(introduces_attr),
            "introducesNewAttrDeficit": introduces_attr,
            "introducesSpiritDeficit": introduces_spirit,
            "disablesAuraOnSwap": disables_aura,
            "ws1": ws1,
            "ws2": ws2,
            "message": f"Swapping between Weapon Set 1 and Weapon Set 2 causes {', '.join(reasons)}.",
        }

    if ws_swap.get("hasSwapSpiritDrop") or ws_swap.get("hasSwapAttrRedText"):
        note = (
            "Both weapon sets share identical baseline deficits (no new gap introduced by swapping)."
            if identical_deficits
            else "Swapping to the alternate weapon set does not introduce a new attribute deficit or Spirit overload."
        )
        return {
            "category": "weapon_set_swap_gap",
            "severity": "advisory",
            "identicalDeficits": identical_deficits,
            "hasSwapSpiritDrop": False,
            "hasSwapAttrRedText": bool(ws_swap.get("hasSwapAttrRedText")),
            "ws1": ws1,
            "ws2": ws2,
            "message": note,
        }
    return None


__all__ = [
    "DEFAULT_OVERCAP_TARGET",
    "IDEAL_OVERCAP_UPPER",
    "classify_weapon_set_swap_finding",
    "evaluate_resistances_cap_overcap",
]
