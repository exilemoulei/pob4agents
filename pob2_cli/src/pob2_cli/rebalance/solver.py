# SPDX-License-Identifier: GPL-3.0-only
"""5-stage zero-buy rebalance orchestrator (`pob2 fix-gaps`) with live CoW verification (L2).

Pipeline:
  - Step 0: Bare net-gap restore (protect Mirrored + 13 isSocketBound, strip replaceable runes, exclude Charms).
  - Stage 1: Gold-respec common-path (`allocMode == 0`) `+5 Str/Dex/Int` swaps (skip radius-jewel nodes).
  - Stage 2: 287-augment minimum-cost socket allocation (Hard Floor first + Overcap Buffer soft bonus, 4 Item.lua rules).
  - Stage 3: 13 PoE2 Breach Catalysts on uncorrupted Jewellery/Belts (skip `{unscalable}`).
  - Stage 4: Discrete Spirit reservation knapsack (`SpiritUnreserved >= 0`).
  - Stage 5: Fallback weakest single-slot Rare purchase spec (ONLY when hard floor still unmet).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..craft.price_book import PriceBook
from ..rune.catalog import RuneInstillCatalog, load_rune_instill_catalog
from ..snapshot import BuildScope, BuildTransaction, capture_digest
from .net_gap import (
    EquippedSlotAudit,
    NetGapAudit,
    StatSnapshot,
    analyze_live_net_gap,
    stat_snapshot_from_pob_stats,
)
from .solver_models import (
    FixGapsReport,
    Stage5PurchaseFallback,
    all_hard_met,
    build_stage5_fallback,
    solve_fix_gaps_offline,
)
from .spirit_knapsack import apply_spirit_plan_live, solve_live_spirit_deficit
from .step_attributes import (
    apply_attribute_swaps_live,
    collect_live_attribute_nodes,
    solve_attribute_respec,
)
from .step_catalysts import apply_catalyst_plan_live, solve_catalyst_upgrades
from .step_runes import (
    Stage2RunePlan,
    apply_rune_plan_live,
    format_max_res_drop_warning,
    solve_rune_allocation,
)


def _has_stage2_regression(baseline: StatSnapshot, after: StatSnapshot, *, chaos_target: int) -> bool:
    if (
        after.fire_max < baseline.fire_max - 1e-6
        or after.cold_max < baseline.cold_max - 1e-6
        or after.lightning_max < baseline.lightning_max - 1e-6
        or after.chaos_max < baseline.chaos_max - 1e-6
    ):
        return True
    if all_hard_met(baseline, chaos_target=chaos_target) and not all_hard_met(after, chaos_target=chaos_target):
        return True
    base_attr = baseline.effective_attr_deficits()
    after_attr = after.effective_attr_deficits()
    if any(after_attr[k] > base_attr[k] for k in base_attr):
        return True
    base_res = baseline.resistance_hard_deficits(chaos_target=chaos_target)
    after_res = after.resistance_hard_deficits(chaos_target=chaos_target)
    return any(after_res[k] > base_res[k] for k in base_res) or after.spirit_deficit() > baseline.spirit_deficit()


def _reset_stripped_slots_live(worker: Any, slots: tuple[EquippedSlotAudit, ...]) -> None:
    for sl in slots:
        if sl.stripped_runes or sl.editable_socket_count > 0:
            worker.call("add_item", {"raw": sl.stripped_raw_text, "slotName": sl.slot_name})


def _run_stage2_with_live_guard(
    worker: Any,
    audit: NetGapAudit,
    snap_after_s1: StatSnapshot,
    *,
    overcap_target: int,
    chaos_target: int,
    is_shaman: bool,
    price_book: PriceBook | None,
    cat: RuneInstillCatalog,
    read_verified_snap: Callable[[], StatSnapshot],
) -> tuple[Stage2RunePlan, StatSnapshot]:
    hard_s2 = {
        **snap_after_s1.effective_attr_deficits(),
        **snap_after_s1.resistance_hard_deficits(chaos_target=chaos_target),
        "spirit": snap_after_s1.spirit_deficit(),
    }
    over_s2 = snap_after_s1.resistance_overcap_needs(overcap_target=overcap_target)
    excluded_s2: set[str] = set()
    extra_s2_warnings: list[str] = []
    bare_level = audit.bare_net.level

    s2 = solve_rune_allocation(
        audit.slots,
        hard_s2,
        over_s2,
        char_level=bare_level,
        is_shaman=is_shaman,
        price_book=price_book,
        crafted_slot_hints=audit.crafted_slot_hints,
        catalog=cat,
        excluded_runes=excluded_s2,
    )
    apply_rune_plan_live(worker, audit.slots, s2, char_level=bare_level, is_shaman=is_shaman, catalog=cat)
    snap_after_s2 = read_verified_snap()

    for _guard_iter in range(3):
        if not s2.assignments or not _has_stage2_regression(snap_after_s1, snap_after_s2, chaos_target=chaos_target):
            break
        newly_rejected: list[str] = []
        seen_probe: set[tuple[str, str]] = set()
        for asgn in s2.assignments:
            key_sr = (asgn.slot_name, asgn.rune_name)
            if key_sr in seen_probe or asgn.rune_name in excluded_s2:
                continue
            seen_probe.add(key_sr)
            _reset_stripped_slots_live(worker, audit.slots)
            probe_plan = Stage2RunePlan(
                assignments=(asgn,), total_cost_ex=asgn.price_ex, remaining_hard={}, remaining_overcap={}
            )
            apply_rune_plan_live(worker, audit.slots, probe_plan, char_level=bare_level, is_shaman=is_shaman, catalog=cat)
            probe_snap = read_verified_snap()
            mr_warn = format_max_res_drop_warning(asgn.rune_name, snap_after_s1, probe_snap, slot_name=asgn.slot_name)
            if mr_warn:
                extra_s2_warnings.append(mr_warn)
                newly_rejected.append(asgn.rune_name)
            elif _has_stage2_regression(snap_after_s1, probe_snap, chaos_target=chaos_target):
                extra_s2_warnings.append(
                    f"[rune-reduces-max-res] Rejected `{asgn.rune_name}` ({asgn.slot_name}): broke hard floor in afterVerified check."
                )
                newly_rejected.append(asgn.rune_name)
        if not newly_rejected:
            for asgn in s2.assignments:
                mr_warn = format_max_res_drop_warning(asgn.rune_name, snap_after_s1, snap_after_s2, slot_name=asgn.slot_name)
                if mr_warn and mr_warn not in extra_s2_warnings:
                    extra_s2_warnings.append(mr_warn)
                newly_rejected.append(asgn.rune_name)
        excluded_s2.update(newly_rejected)
        _reset_stripped_slots_live(worker, audit.slots)
        s2 = solve_rune_allocation(
            audit.slots,
            hard_s2,
            over_s2,
            char_level=bare_level,
            is_shaman=is_shaman,
            price_book=price_book,
            crafted_slot_hints=audit.crafted_slot_hints,
            catalog=cat,
            excluded_runes=excluded_s2,
        )
        apply_rune_plan_live(worker, audit.slots, s2, char_level=bare_level, is_shaman=is_shaman, catalog=cat)
        snap_after_s2 = read_verified_snap()

    if extra_s2_warnings:
        merged_w = list(dict.fromkeys([*extra_s2_warnings, *s2.warnings]))
        s2 = Stage2RunePlan(
            assignments=s2.assignments,
            total_cost_ex=s2.total_cost_ex,
            remaining_hard=s2.remaining_hard,
            remaining_overcap=s2.remaining_overcap,
            crafted_slot_hints=s2.crafted_slot_hints,
            warnings=tuple(merged_w),
        )
    return s2, snap_after_s2


def solve_fix_gaps_live(
    worker: Any,
    *,
    overcap_target: int = 20,
    chaos_target: int = 0,
    apply: bool = False,
    price_book: PriceBook | None = None,
    catalog: RuneInstillCatalog | None = None,
) -> FixGapsReport:
    """Run the full 5-stage solver on a live `BridgeWorker` with end-to-end CoW verification & rollback."""
    cat = catalog or load_rune_instill_catalog()
    before_digest = capture_digest(worker)

    audit = analyze_live_net_gap(worker, catalog=cat)
    bare = audit.bare_net
    is_shaman = bare.ascendancy.lower() == "shaman"
    attr_nodes = collect_live_attribute_nodes(worker, protected_jewel_nodes=audit.protected_radius_jewel_nodes)
    has_ws2 = any(s.slot_name in ("Weapon 1 Swap", "Weapon 2 Swap") for s in audit.slots)

    def _read_verified_snap() -> StatSnapshot:
        ws1 = worker.call("get_stats", {}).get("stats") or {}
        ws2: dict[str, Any] | None = None
        if has_ws2:
            worker.call("set_weapon_swap", {"on": True})
            ws2 = worker.call("get_stats", {}).get("stats") or {}
            worker.call("set_weapon_swap", {"on": False})
        return stat_snapshot_from_pob_stats(
            ws1, level=bare.level, ascendancy=bare.ascendancy, giants_blood=bare.giants_blood, ws2_stats=ws2
        )

    solver_warnings: list[str] = []
    rollback_ok: bool | None = None
    with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=apply) as tx:
        for cname in ("Charm 1", "Charm 2", "Charm 3"):
            try:
                worker.call("set_slot_active", {"slotName": cname, "active": False})
            except Exception as exc:
                solver_warnings.append(f"[rebalance-degraded] Could not deactivate `{cname}` ({exc}).")

        for sl in audit.slots:
            if sl.stripped_runes:
                worker.call("add_item", {"raw": sl.stripped_raw_text, "slotName": sl.slot_name})

        s1 = solve_attribute_respec(bare, attr_nodes, catalog=cat)
        apply_attribute_swaps_live(worker, s1)
        snap_after_s1 = _read_verified_snap()

        s2, snap_after_s2 = _run_stage2_with_live_guard(
            worker,
            audit,
            snap_after_s1,
            overcap_target=overcap_target,
            chaos_target=chaos_target,
            is_shaman=is_shaman,
            price_book=price_book,
            cat=cat,
            read_verified_snap=_read_verified_snap,
        )

        hard_s3 = {
            **snap_after_s2.effective_attr_deficits(),
            **snap_after_s2.resistance_hard_deficits(chaos_target=chaos_target),
        }
        over_s3 = snap_after_s2.resistance_overcap_needs(overcap_target=overcap_target)
        s3 = solve_catalyst_upgrades(audit.slots, hard_s3, over_s3, price_book=price_book)
        apply_catalyst_plan_live(worker, audit.slots, s3)
        snap_after_s3 = _read_verified_snap()

        s4 = solve_live_spirit_deficit(worker, deficit_override=snap_after_s3.spirit_deficit(), catalog=cat)
        apply_spirit_plan_live(worker, s4)
        after_verified = _read_verified_snap()

        final_hard = {
            **after_verified.effective_attr_deficits(),
            **after_verified.resistance_hard_deficits(chaos_target=chaos_target),
            "spirit": after_verified.spirit_deficit(),
        }
        s5 = build_stage5_fallback(
            audit.slots,
            final_hard,
            after_verified.resistance_overcap_needs(overcap_target=overcap_target),
        )

        if apply:
            tx.commit(undoable=True)
        else:
            chk = tx.rollback_verified(before_digest)
            rollback_ok = bool(chk.get("verified"))

    all_warnings = (*audit.warnings, *solver_warnings, *s1.warnings, *s2.warnings, *s3.warnings, *s4.warnings)
    return FixGapsReport(
        before=audit.current,
        bare_net=bare,
        after_verified=after_verified,
        stage1=s1,
        stage2=s2,
        stage3=s3,
        stage4=s4,
        stage5=s5,
        overcap_target=overcap_target,
        chaos_target=chaos_target,
        total_gold_cost=s1.gold_cost,
        total_ex_cost=s2.total_cost_ex + s3.total_cost_ex,
        all_hard_floors_met=all_hard_met(after_verified, chaos_target=chaos_target),
        cow_rollback_verified=rollback_ok,
        warnings=all_warnings,
        current_in_game=audit.current_in_game or audit.current,
    )


__all__ = [
    "FixGapsReport",
    "Stage5PurchaseFallback",
    "build_stage5_fallback",
    "solve_fix_gaps_live",
    "solve_fix_gaps_offline",
]
