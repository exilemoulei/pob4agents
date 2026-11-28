# SPDX-License-Identifier: GPL-3.0-only
"""Dataclass contracts and PoB2 stat snapshot builders for net-gap analysis (L2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..rune.socket_rules import SocketParityResult


@dataclass(frozen=True, slots=True)
class StatSnapshot:
    """Character attribute, resistance, and Spirit state across WS1 and WS2."""

    level: int
    ascendancy: str
    str_val: float
    req_str: float
    dex_val: float
    req_dex: float
    int_val: float
    req_int: float
    fire_total: float
    fire_max: float
    cold_total: float
    cold_max: float
    lightning_total: float
    lightning_max: float
    chaos_total: float
    chaos_max: float
    spirit_total: float
    spirit_reserved: float
    spirit_unreserved: float
    combined_dps: float = 0.0
    total_ehp: float = 0.0
    giants_blood: bool = False
    ws2_req_str: float = 0.0
    ws2_req_dex: float = 0.0
    ws2_req_int: float = 0.0
    ws2_str_val: float | None = None
    ws2_dex_val: float | None = None
    ws2_int_val: float | None = None

    def effective_attr_deficits(self) -> dict[str, int]:
        """Return worst-case Str/Dex/Int hard deficit across BOTH WS1 and WS2."""
        d_str = max(0, round(self.req_str - self.str_val))
        d_dex = max(0, round(self.req_dex - self.dex_val))
        d_int = max(0, round(self.req_int - self.int_val))
        if self.ws2_str_val is not None:
            d_str = max(d_str, round(self.ws2_req_str - self.ws2_str_val))
        if self.ws2_dex_val is not None:
            d_dex = max(d_dex, round(self.ws2_req_dex - self.ws2_dex_val))
        if self.ws2_int_val is not None:
            d_int = max(d_int, round(self.ws2_req_int - self.ws2_int_val))
        return {"str": d_str, "dex": d_dex, "int": d_int}

    def effective_attr_surpluses(self) -> dict[str, int]:
        """Return safe Str/Dex/Int surplus above requirements across BOTH WS1 and WS2."""
        s_str = round(self.str_val - self.req_str)
        s_dex = round(self.dex_val - self.req_dex)
        s_int = round(self.int_val - self.req_int)
        if self.ws2_str_val is not None:
            s_str = min(s_str, round(self.ws2_str_val - self.ws2_req_str))
        if self.ws2_dex_val is not None:
            s_dex = min(s_dex, round(self.ws2_dex_val - self.ws2_req_dex))
        if self.ws2_int_val is not None:
            s_int = min(s_int, round(self.ws2_int_val - self.ws2_req_int))
        return {"str": max(0, s_str), "dex": max(0, s_dex), "int": max(0, s_int)}

    def resistance_hard_deficits(self, *, chaos_target: float = 0.0) -> dict[str, int]:
        """Hard Floor deficits: each element MUST meet its own MaxRes (`Res >= MaxRes`)."""
        return {
            "fire": max(0, round(self.fire_max - self.fire_total)),
            "cold": max(0, round(self.cold_max - self.cold_total)),
            "lightning": max(0, round(self.lightning_max - self.lightning_total)),
            "chaos": max(0, round(chaos_target - self.chaos_total)),
        }

    def resistance_overcap_needs(self, *, overcap_target: float = 20.0) -> dict[str, int]:
        """Soft Overcap Buffer gaps (never penalizes elements already exceeding `MaxRes + overcap`)."""
        if overcap_target <= 0:
            return {"fire": 0, "cold": 0, "lightning": 0}
        return {
            "fire": max(0, round((self.fire_max + overcap_target) - self.fire_total)),
            "cold": max(0, round((self.cold_max + overcap_target) - self.cold_total)),
            "lightning": max(0, round((self.lightning_max + overcap_target) - self.lightning_total)),
        }

    def spirit_deficit(self) -> int:
        """True Spirit deficit (`>0` when reserved exceeds total Spirit)."""
        unres = min(self.spirit_unreserved, self.spirit_total - self.spirit_reserved)
        return max(0, round(-unres))

    def to_dict(self, *, chaos_target: float = 0.0, overcap_target: float = 20.0) -> dict[str, Any]:
        return {
            "level": self.level,
            "ascendancy": self.ascendancy,
            "attributes": {
                "str": round(self.str_val, 1), "reqStr": round(self.req_str, 1),
                "dex": round(self.dex_val, 1), "reqDex": round(self.req_dex, 1),
                "int": round(self.int_val, 1), "reqInt": round(self.req_int, 1),
                "hardDeficits": self.effective_attr_deficits(),
                "giantsBlood": self.giants_blood,
            },
            "resistances": {
                "fire": round(self.fire_total, 1), "fireMax": round(self.fire_max, 1),
                "fireOvercap": round(self.fire_total - self.fire_max, 1),
                "cold": round(self.cold_total, 1), "coldMax": round(self.cold_max, 1),
                "coldOvercap": round(self.cold_total - self.cold_max, 1),
                "lightning": round(self.lightning_total, 1), "lightningMax": round(self.lightning_max, 1),
                "lightningOvercap": round(self.lightning_total - self.lightning_max, 1),
                "chaos": round(self.chaos_total, 1), "chaosMax": round(self.chaos_max, 1),
                "hardDeficits": self.resistance_hard_deficits(chaos_target=chaos_target),
                "overcapNeeds": self.resistance_overcap_needs(overcap_target=overcap_target),
            },
            "spirit": {
                "total": round(self.spirit_total, 1),
                "reserved": round(self.spirit_reserved, 1),
                "unreserved": round(self.spirit_unreserved, 1),
                "deficit": self.spirit_deficit(),
            },
            "combinedDps": round(self.combined_dps, 2),
            "totalEhp": round(self.total_ehp, 2),
        }


@dataclass(frozen=True)
class EquippedSlotAudit:
    """Single equipped slot's socket editability, Crafted slot status, and catalyst eligibility."""

    slot_name: str
    item_id: int
    name: str
    base_name: str
    rarity: str
    item_type: str
    raw_text: str
    stripped_raw_text: str
    parity: SocketParityResult
    editable_socket_count: int
    stripped_runes: tuple[str, ...]
    preserved_bound_runes: tuple[str, ...]
    has_open_crafted_slot: bool
    open_prefix_count: int
    open_suffix_count: int
    catalyst_eligible: bool
    is_weapon_slot: bool


@dataclass(frozen=True)
class NetGapAudit:
    """Complete Step 0 bare-build net gap analysis before Stages 1-5 run."""

    current: StatSnapshot
    bare_net: StatSnapshot
    slots: tuple[EquippedSlotAudit, ...]
    protected_radius_jewel_nodes: tuple[int, ...] = ()
    charm_resists_excluded: bool = True
    warnings: tuple[str, ...] = ()
    crafted_slot_hints: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    current_in_game: StatSnapshot | None = None


def _resolve_res_max(stats: dict[str, Any], elem: str, capped_res: float, overcap: float) -> float:
    """Compute real PoE2 MaxRes from PoB2 stats (which omit `*ResistMax` and expose `Missing*Resist` / `*ResistOver75`)."""
    explicit = stats.get(f"{elem}ResistMax")
    if isinstance(explicit, (int, float)):
        return float(explicit)
    missing = stats.get(f"Missing{elem}Resist")
    if isinstance(missing, (int, float)) and isinstance(stats.get(f"{elem}Resist"), (int, float)):
        return float(capped_res) + float(missing)
    if isinstance(missing, (int, float)) and float(missing) > 0.0:
        return float(capped_res) + float(missing)
    if overcap > 0.0 and isinstance(stats.get(f"{elem}Resist"), (int, float)):
        return float(capped_res)
    over75 = stats.get(f"{elem}ResistOver75")
    over75_f = float(over75) if isinstance(over75, (int, float)) else 0.0
    return 75.0 + over75_f


def stat_snapshot_from_pob_stats(
    stats: dict[str, Any],
    *,
    level: int = 90,
    ascendancy: str = "",
    giants_blood: bool = False,
    ws2_stats: dict[str, Any] | None = None,
) -> StatSnapshot:
    """Build a `StatSnapshot` from PoB2 `get_stats().stats` dictionary."""
    def _f(d: dict[str, Any], key: str, default: float = 0.0) -> float:
        v = d.get(key)
        return float(v) if isinstance(v, (int, float)) else default

    fire_res = _f(stats, "FireResist")
    fire_over = _f(stats, "FireResistOverCap")
    fire_max = _resolve_res_max(stats, "Fire", fire_res, fire_over)
    fire_tot = _f(stats, "FireResistTotal", fire_res + fire_over)
    cold_res = _f(stats, "ColdResist")
    cold_over = _f(stats, "ColdResistOverCap")
    cold_max = _resolve_res_max(stats, "Cold", cold_res, cold_over)
    cold_tot = _f(stats, "ColdResistTotal", cold_res + cold_over)
    light_res = _f(stats, "LightningResist")
    light_over = _f(stats, "LightningResistOverCap")
    light_max = _resolve_res_max(stats, "Lightning", light_res, light_over)
    light_tot = _f(stats, "LightningResistTotal", light_res + light_over)
    chaos_res = _f(stats, "ChaosResist")
    chaos_over = _f(stats, "ChaosResistOverCap")
    chaos_max = _resolve_res_max(stats, "Chaos", chaos_res, chaos_over)
    chaos_tot = _f(stats, "ChaosResistTotal", chaos_res + chaos_over)

    spirit_total = _f(stats, "Spirit")
    spirit_unres = _f(stats, "SpiritUnreserved", spirit_total)
    spirit_res = _f(stats, "SpiritReserved", max(0.0, spirit_total - spirit_unres))
    true_unres = min(spirit_unres, spirit_total - spirit_res)

    return StatSnapshot(
        level=level,
        ascendancy=ascendancy,
        str_val=_f(stats, "Str"),
        req_str=_f(stats, "ReqStr"),
        dex_val=_f(stats, "Dex"),
        req_dex=_f(stats, "ReqDex"),
        int_val=_f(stats, "Int"),
        req_int=_f(stats, "ReqInt"),
        fire_total=fire_tot,
        fire_max=fire_max,
        cold_total=cold_tot,
        cold_max=cold_max,
        lightning_total=light_tot,
        lightning_max=light_max,
        chaos_total=chaos_tot,
        chaos_max=chaos_max,
        spirit_total=spirit_total,
        spirit_reserved=spirit_res,
        spirit_unreserved=true_unres,
        combined_dps=_f(stats, "CombinedDPS") or _f(stats, "TotalDPS"),
        total_ehp=_f(stats, "TotalEHP"),
        giants_blood=giants_blood,
        ws2_req_str=_f(ws2_stats, "ReqStr") if ws2_stats else 0.0,
        ws2_req_dex=_f(ws2_stats, "ReqDex") if ws2_stats else 0.0,
        ws2_req_int=_f(ws2_stats, "ReqInt") if ws2_stats else 0.0,
        ws2_str_val=_f(ws2_stats, "Str") if ws2_stats else None,
        ws2_dex_val=_f(ws2_stats, "Dex") if ws2_stats else None,
        ws2_int_val=_f(ws2_stats, "Int") if ws2_stats else None,
    )


__all__ = [
    "EquippedSlotAudit",
    "NetGapAudit",
    "StatSnapshot",
    "stat_snapshot_from_pob_stats",
]
