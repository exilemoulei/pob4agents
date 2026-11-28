# SPDX-License-Identifier: GPL-3.0-only
"""Report models, Stage 5 fallback builder, and deterministic offline solver for `pob2 fix-gaps` (L2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..craft.price_book import PriceBook
from ..importers.post_load_fixups import scan_res_override_lines_in_items
from ..rune.catalog import RuneInstillCatalog, load_rune_instill_catalog
from .net_gap import EquippedSlotAudit, NetGapAudit, StatSnapshot
from .spirit_knapsack import SpiritKnapsackPlan
from .step_attributes import AttributeNodeInfo, Stage1AttributePlan, solve_attribute_respec
from .step_catalysts import Stage3CatalystPlan, solve_catalyst_upgrades
from .step_runes import Stage2RunePlan, solve_rune_allocation


@dataclass(frozen=True)
class Stage5PurchaseFallback:
    """Fallback weakest single-slot Rare purchase spec emitted ONLY if Stages 1-4 leave hard deficits."""

    needed: bool
    target_slot: str | None
    current_item: str | None
    required_stats: dict[str, int]
    overcap_bonus_stats: dict[str, int]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": 5,
            "name": "fallback_weakest_rare_slot",
            "needed": self.needed,
            "targetSlot": self.target_slot,
            "currentItem": self.current_item,
            "requiredStats": {k: v for k, v in self.required_stats.items() if v > 0},
            "overcapBonusStats": {k: v for k, v in self.overcap_bonus_stats.items() if v > 0},
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FixGapsReport:
    """Complete 5-stage rebalance report with before/bare/after CoW-verified snapshots."""

    before: StatSnapshot
    bare_net: StatSnapshot
    after_verified: StatSnapshot
    stage1: Stage1AttributePlan
    stage2: Stage2RunePlan
    stage3: Stage3CatalystPlan
    stage4: SpiritKnapsackPlan
    stage5: Stage5PurchaseFallback
    overcap_target: int
    chaos_target: int
    total_gold_cost: int
    total_ex_cost: float
    all_hard_floors_met: bool
    cow_rollback_verified: bool | None
    warnings: tuple[str, ...]
    current_in_game: StatSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        in_game = self.current_in_game or self.before
        without_charm = self.before.to_dict(chaos_target=self.chaos_target, overcap_target=self.overcap_target)
        snap_notes: dict[str, str] = {
            "currentInGame": "Live in-game build stats as loaded (including active hit-triggered Charms and current socketed runes).",
            "withoutConditionalAndCharm": "Permanent stats with hit-triggered Charms deactivated; keeps all equipped gear, socketed runes, passive tree, and permanent campaign quest rewards.",
            "bareNet": "Equipped gear + passive tree + permanent campaign quest rewards + Mirrored/isSocketBound augments KEPT; only Charms deactivated and replaceable socket runes temporarily stripped to measure free socket capacity (NOT unequipped resistances).",
        }
        res_ov_warns = [w for w in self.warnings if "[res-override-line]" in w]
        if res_ov_warns:
            snap_notes["resOverrideWarning"] = res_ov_warns[0]
        return {
            "game": "poe2",
            "patch": "0.5.5",
            "resistancePolicy": "cap_plus_overcap_buffer_never_equalize",
            "overcapTarget": self.overcap_target,
            "chaosTarget": self.chaos_target,
            "allHardFloorsMet": self.all_hard_floors_met,
            "totalGoldCost": self.total_gold_cost,
            "totalExCost": round(self.total_ex_cost, 2),
            "cowRollbackVerified": self.cow_rollback_verified,
            "currentInGame": in_game.to_dict(chaos_target=self.chaos_target, overcap_target=self.overcap_target),
            "withoutConditionalAndCharm": without_charm,
            "before": without_charm,
            "bareNet": self.bare_net.to_dict(chaos_target=self.chaos_target, overcap_target=self.overcap_target),
            "afterVerified": self.after_verified.to_dict(chaos_target=self.chaos_target, overcap_target=self.overcap_target),
            "snapshotNotes": snap_notes,
            "stages": {
                "stage1Attributes": self.stage1.to_dict(),
                "stage2Runes": self.stage2.to_dict(),
                "stage3Catalysts": self.stage3.to_dict(),
                "stage4Spirit": self.stage4.to_dict(),
                "stage5Fallback": self.stage5.to_dict(),
            },
            "warnings": list(self.warnings),
        }


def _pick_weakest_rare_slot(slots: tuple[EquippedSlotAudit, ...]) -> EquippedSlotAudit | None:
    """Locate the lowest-utility replaceable non-weapon Rare armour/jewellery slot."""
    rares = [
        s
        for s in slots
        if s.rarity == "RARE"
        and not s.is_weapon_slot
        and not s.parity.is_mirrored
    ]
    if not rares:
        return None
    slot_weight = {
        "Gloves": 1,
        "Boots": 1,
        "Ring 2": 2,
        "Ring 1": 2,
        "Belt": 2,
        "Helmet": 3,
        "Amulet": 4,
        "Body Armour": 5,
    }
    return min(rares, key=lambda s: (slot_weight.get(s.slot_name, 3), len(s.raw_text.splitlines())))


def build_stage5_fallback(
    slots: tuple[EquippedSlotAudit, ...],
    remaining_hard: dict[str, int],
    remaining_overcap: dict[str, int],
) -> Stage5PurchaseFallback:
    """Emit weakest single-slot Rare replacement spec ONLY if hard floor deficits remain > 0."""
    unmet = {k: v for k, v in remaining_hard.items() if v > 0}
    if not unmet:
        return Stage5PurchaseFallback(
            needed=False,
            target_slot=None,
            current_item=None,
            required_stats={},
            overcap_bonus_stats={},
            reason="All hard floors satisfied zero-buy in Stages 1-4; no gear replacement required.",
        )
    weakest = _pick_weakest_rare_slot(slots)
    over_clean = {k: v for k, v in remaining_overcap.items() if v > 0}
    return Stage5PurchaseFallback(
        needed=True,
        target_slot=weakest.slot_name if weakest else "Ring 2",
        current_item=weakest.name if weakest else None,
        required_stats=unmet,
        overcap_bonus_stats=over_clean,
        reason=(
            f"Hard deficits {unmet} remain after Stages 1-4; replace lowest-utility Rare slot "
            f"`{weakest.slot_name if weakest else 'Ring 2'}` with merged cap + overcap targets."
        ),
    )


def all_hard_met(snap: StatSnapshot, *, chaos_target: int) -> bool:
    """Return True iff all attribute, resistance, and Spirit hard floors are satisfied."""
    attr_bad = any(v > 0 for v in snap.effective_attr_deficits().values())
    res_bad = any(v > 0 for v in snap.resistance_hard_deficits(chaos_target=chaos_target).values())
    sp_bad = snap.spirit_deficit() > 0
    return not (attr_bad or res_bad or sp_bad)


def solve_fix_gaps_offline(
    audit: NetGapAudit,
    attr_nodes: list[AttributeNodeInfo],
    *,
    overcap_target: int = 20,
    chaos_target: int = 0,
    price_book: PriceBook | None = None,
    catalog: RuneInstillCatalog | None = None,
) -> FixGapsReport:
    """Pure offline 5-stage solver (for deterministic unit testing without a live LuaJIT process)."""
    cat = catalog or load_rune_instill_catalog()
    bare = audit.bare_net
    is_shaman = bare.ascendancy.lower() == "shaman"

    s1 = solve_attribute_respec(bare, attr_nodes, catalog=cat)

    res_hard = bare.resistance_hard_deficits(chaos_target=chaos_target)
    res_over = bare.resistance_overcap_needs(overcap_target=overcap_target)
    hard_s2 = {
        **s1.remaining_deficits,
        **res_hard,
        "spirit": bare.spirit_deficit(),
    }
    s2 = solve_rune_allocation(
        audit.slots,
        hard_s2,
        res_over,
        char_level=bare.level,
        is_shaman=is_shaman,
        price_book=price_book,
        crafted_slot_hints=audit.crafted_slot_hints,
        catalog=cat,
    )

    s3 = solve_catalyst_upgrades(
        audit.slots,
        s2.remaining_hard,
        s2.remaining_overcap,
        price_book=price_book,
    )

    rem_spirit = s2.remaining_hard.get("spirit", 0)
    s4 = SpiritKnapsackPlan(
        mode="rescue",
        target_spirit=rem_spirit,
        achieved_spirit=0,
        remaining_deficit=rem_spirit,
        selected=(),
        all_evaluated=(),
    )

    d = {k: 0 for k in ("str", "dex", "int", "fire", "cold", "lightning", "chaos", "spirit")}
    for sw in s1.swaps:
        d[sw.from_attr] -= sw.delta
        d[sw.to_attr] += sw.delta
    for asgn in s2.assignments:
        for k, v in asgn.gains.items():
            d[k] += v
    for act in s3.actions:
        for k, v in act.gains.items():
            d[k] += v

    after = StatSnapshot(
        level=bare.level,
        ascendancy=bare.ascendancy,
        str_val=bare.str_val + d["str"],
        req_str=bare.req_str,
        dex_val=bare.dex_val + d["dex"],
        req_dex=bare.req_dex,
        int_val=bare.int_val + d["int"],
        req_int=bare.req_int,
        fire_total=bare.fire_total + d["fire"],
        fire_max=bare.fire_max,
        cold_total=bare.cold_total + d["cold"],
        cold_max=bare.cold_max,
        lightning_total=bare.lightning_total + d["lightning"],
        lightning_max=bare.lightning_max,
        chaos_total=bare.chaos_total + d["chaos"],
        chaos_max=bare.chaos_max,
        spirit_total=bare.spirit_total + d["spirit"],
        spirit_reserved=bare.spirit_reserved,
        spirit_unreserved=bare.spirit_unreserved + d["spirit"],
        combined_dps=bare.combined_dps,
        total_ehp=bare.total_ehp,
        giants_blood=bare.giants_blood,
        ws2_req_str=bare.ws2_req_str,
        ws2_req_dex=bare.ws2_req_dex,
        ws2_req_int=bare.ws2_req_int,
        ws2_str_val=(bare.ws2_str_val + d["str"]) if bare.ws2_str_val is not None else None,
        ws2_dex_val=(bare.ws2_dex_val + d["dex"]) if bare.ws2_dex_val is not None else None,
        ws2_int_val=(bare.ws2_int_val + d["int"]) if bare.ws2_int_val is not None else None,
    )

    final_hard = {
        **after.effective_attr_deficits(),
        **after.resistance_hard_deficits(chaos_target=chaos_target),
        "spirit": after.spirit_deficit(),
    }
    s5 = build_stage5_fallback(audit.slots, final_hard, after.resistance_overcap_needs(overcap_target=overcap_target))
    res_ov = scan_res_override_lines_in_items(
        [(s.slot_name, s.name or s.base_name, s.raw_text) for s in audit.slots]
    )
    all_warnings = tuple(
        dict.fromkeys([*audit.warnings, *res_ov["warnings"], *s1.warnings, *s2.warnings, *s3.warnings, *s4.warnings])
    )

    return FixGapsReport(
        before=audit.current,
        bare_net=bare,
        after_verified=after,
        stage1=s1,
        stage2=s2,
        stage3=s3,
        stage4=s4,
        stage5=s5,
        overcap_target=overcap_target,
        chaos_target=chaos_target,
        total_gold_cost=s1.gold_cost,
        total_ex_cost=s2.total_cost_ex + s3.total_cost_ex,
        all_hard_floors_met=all_hard_met(after, chaos_target=chaos_target),
        cow_rollback_verified=None,
        warnings=all_warnings,
        current_in_game=audit.current_in_game or audit.current,
    )


__all__ = [
    "FixGapsReport",
    "Stage5PurchaseFallback",
    "all_hard_met",
    "build_stage5_fallback",
    "solve_fix_gaps_offline",
]
