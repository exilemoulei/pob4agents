# SPDX-License-Identifier: GPL-3.0-only
"""Stage 2: 287-augment minimum-cost socket solver (Hard Floor first + Overcap Buffer soft bonus) (L2).

Strict invariants:
  - Enforces all 4 `Item.lua` boundaries (21-slot parity, unique override, `req_level <= char_level`, non-Shaman no `Bonded:`).
  - Honors socket effect multipliers (`Darkness Enthroned`, `Morior Invictus`).
  - Protects `Mirrored` items & 13 `isSocketBound = true` augments.
  - NEVER equalizes Fire/Cold/Lightning or penalizes healthy overcap surplus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..craft.price_book import PriceBook
from ..rune.catalog import RuneEntry, RuneInstillCatalog, load_rune_instill_catalog
from ..rune.instill_bridge import resolve_rune_price_ex
from ..rune.socket_rules import apply_runes_to_item_raw, eligible_runes_for_item
from .net_gap import EquippedSlotAudit
from .rune_effects import (
    STAT_KEYS as _STAT_KEYS,
)
from .rune_effects import (
    AugmentSideEffects,
    format_max_res_drop_warning,
    inspect_augment_side_effects,
    parse_augment_stat_vector,
)


@dataclass(frozen=True, slots=True)
class SocketAssignment:
    """Planned rune/soul core/idol assignment for one editable socket."""

    slot_name: str
    socket_index: int
    rune_name: str
    zh_cn: str
    price_ex: float
    effect_multiplier: float
    gains: dict[str, int]
    purpose: str  # "hard_floor" | "overcap_buffer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot_name,
            "socketIndex": self.socket_index,
            "rune": self.rune_name,
            "zhCn": self.zh_cn,
            "priceEx": round(self.price_ex, 2),
            "effectMultiplier": round(self.effect_multiplier, 2),
            "gains": {k: v for k, v in self.gains.items() if v != 0},
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class Stage2RunePlan:
    """Result of Stage 2 minimum-cost socket assignment."""

    assignments: tuple[SocketAssignment, ...]
    total_cost_ex: float
    remaining_hard: dict[str, int]
    remaining_overcap: dict[str, int]
    crafted_slot_hints: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": 2,
            "name": "socket_augments_287",
            "assignmentCount": len(self.assignments),
            "assignments": [a.to_dict() for a in self.assignments],
            "totalCostEx": round(self.total_cost_ex, 2),
            "remainingHardDeficits": dict(self.remaining_hard),
            "remainingOvercapNeeds": dict(self.remaining_overcap),
            "craftedSlotHints": list(self.crafted_slot_hints),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class _AugmentOption:
    rune: RuneEntry
    price_ex: float
    penalty_ex: float
    multiplier: float
    gains: dict[str, int]


def _slot_options(
    slot: EquippedSlotAudit,
    *,
    char_level: int,
    is_shaman: bool,
    price_book: PriceBook | None,
    max_single_ex: float,
    catalog: RuneInstillCatalog,
    excluded_runes: frozenset[str] | set[str] = frozenset(),
    rejected_max_res: dict[str, str] | None = None,
) -> list[_AugmentOption]:
    elig = eligible_runes_for_item(
        slot.parity,
        char_level=char_level,
        is_shaman=is_shaman,
        include_socket_bound=False,
        catalog=catalog,
    )
    opts: list[_AugmentOption] = []
    for rune, mods in elig:
        if rune.name in excluded_runes:
            continue
        mult = slot.parity.effect_multiplier(rune.kind)
        vec = parse_augment_stat_vector(mods, multiplier=mult, char_level=char_level)
        fx = inspect_augment_side_effects(mods, multiplier=mult)
        if fx.reduces_max_res:
            if rejected_max_res is not None and any(v > 0 for v in vec.values()) and rune.name not in rejected_max_res:
                rejected_max_res[rune.name] = (
                    f"[rune-reduces-max-res] Rejected `{rune.name}` ({slot.slot_name}): "
                    f"lowers maximum resistance ({fx.format_max_res_deltas()})."
                )
            continue
        if not any(v > 0 for v in vec.values()):
            continue
        px, _ = resolve_rune_price_ex(rune, price_book=price_book)
        if px > max_single_ex:
            continue
        opts.append(_AugmentOption(rune=rune, price_ex=px, penalty_ex=fx.penalty_ex, multiplier=mult, gains=vec))
    # Remove strictly dominated options (same or better on every stat at <= price and <= side-effect penalty)
    opts.sort(key=lambda o: (o.price_ex + o.penalty_ex, o.penalty_ex, -sum(o.gains.values()), o.rune.name))
    pruned: list[_AugmentOption] = []
    for cand in opts:
        dominated = any(
            (prev.price_ex + prev.penalty_ex) <= (cand.price_ex + cand.penalty_ex)
            and prev.penalty_ex <= cand.penalty_ex
            and all(prev.gains[k] >= cand.gains[k] for k in _STAT_KEYS)
            for prev in pruned
        )
        if not dominated:
            pruned.append(cand)
    return pruned


def solve_rune_allocation(
    slots: tuple[EquippedSlotAudit, ...],
    hard_deficits: dict[str, int],
    overcap_needs: dict[str, int],
    *,
    char_level: int = 90,
    is_shaman: bool = False,
    max_single_ex: float = 200.0,
    overcap_max_single_ex: float = 8.0,
    price_book: PriceBook | None = None,
    crafted_slot_hints: tuple[dict[str, Any], ...] = (),
    catalog: RuneInstillCatalog | None = None,
    excluded_runes: frozenset[str] | set[str] = frozenset(),
) -> Stage2RunePlan:
    """Solve minimum-cost rune/soul core allocation across editable sockets.

    Phase A closes all Hard Floor deficits first (preferring armour/belt/shield sockets before weapon DPS sockets)
    while rejecting any augment that lowers `maxRes` (`[rune-reduces-max-res]`) and penalizing negative side effects.
    Phase B optionally fills leftover inexpensive armour/utility sockets for `--overcap` buffer without touching
    weapon DPS sockets, negative-tradeoff runes, or equalizing already-capped resistances.
    """
    cat = catalog or load_rune_instill_catalog()
    rem_hard = {k: max(0, int(hard_deficits.get(k, 0))) for k in _STAT_KEYS}
    rem_over = {k: max(0, int(overcap_needs.get(k, 0))) for k in ("fire", "cold", "lightning")}
    has_active_need = any(rem_hard.values()) or any(rem_over.values())
    rejected_max_res: dict[str, str] = {}

    # Order editable sockets: non-weapon highest-multiplier slots FIRST (Darkness Enthroned / Armour before Weapons!)
    open_sockets: list[tuple[EquippedSlotAudit, int]] = []
    ordered_slots = sorted(
        slots,
        key=lambda s: (s.is_weapon_slot, -s.parity.effect_multiplier("Rune"), s.slot_name),
    )
    for sl in ordered_slots:
        if sl.editable_socket_count <= 0:
            continue
        start_idx = len(sl.preserved_bound_runes)
        for k in range(sl.editable_socket_count):
            open_sockets.append((sl, start_idx + k))

    opts_cache: dict[str, list[_AugmentOption]] = {}
    for sl in ordered_slots:
        if sl.editable_socket_count > 0:
            opts_cache[sl.slot_name] = _slot_options(
                sl,
                char_level=char_level,
                is_shaman=is_shaman,
                price_book=price_book,
                max_single_ex=max_single_ex,
                catalog=cat,
                excluded_runes=excluded_runes,
                rejected_max_res=rejected_max_res if has_active_need else None,
            )
    warnings: list[str] = list(rejected_max_res.values())

    assignments: list[SocketAssignment] = []
    used_indices: set[int] = set()
    total_ex = 0.0

    # Phase A: Hard Floor feasibility-first selection (guarantee full hard closure when sockets are tight,
    # while minimizing Exalted + side-effect cost and preferring non-weapon sockets before weapons).
    while any(rem_hard.values()):
        rem_sockets = len(open_sockets) - len(used_indices)
        if rem_sockets <= 0:
            break
        total_rem_hard = sum(rem_hard.values())
        min_needed_per_socket = (total_rem_hard + rem_sockets - 1) // rem_sockets

        all_candidates: list[tuple[bool, bool, float, int, int, _AugmentOption]] = []
        for idx, (sl, _sock_i) in enumerate(open_sockets):
            if idx in used_indices:
                continue
            for opt in opts_cache.get(sl.slot_name, ()):
                pos_cov = sum(min(rem_hard[k], max(0, opt.gains[k])) for k in _STAT_KEYS)
                neg_loss = sum(abs(opt.gains[k]) for k in _STAT_KEYS if opt.gains[k] < 0)
                covered = pos_cov - neg_loss
                if covered <= 0:
                    continue
                over_bonus = sum(min(rem_over[k], max(0, opt.gains[k] - rem_hard[k])) for k in rem_over) * 0.15
                wpn_pen = 50.0 if sl.is_weapon_slot else 0.0
                score = (opt.price_ex + opt.penalty_ex + wpn_pen) / (covered + over_bonus)
                meets_pace = covered >= min_needed_per_socket
                clean_no_pen = opt.penalty_ex <= 0.0 and neg_loss == 0
                all_candidates.append((meets_pace, clean_no_pen, score, -covered, idx, opt))

        if not all_candidates:
            break
        feasible_pace = [c for c in all_candidates if c[0]]
        pool = feasible_pace if feasible_pace else all_candidates
        clean_pool = [c for c in pool if c[1]]
        eff_pool = clean_pool if clean_pool else pool
        if feasible_pace:
            _ok, _cln, _score, _ncov, chosen_idx, opt = min(eff_pool, key=lambda c: (c[2], c[3], c[4]))
        else:
            _ok, _cln, _score, _ncov, chosen_idx, opt = min(eff_pool, key=lambda c: (c[3], c[2], c[4]))
        sl, sock_i = open_sockets[chosen_idx]
        used_indices.add(chosen_idx)
        total_ex += opt.price_ex
        assignments.append(
            SocketAssignment(
                slot_name=sl.slot_name,
                socket_index=sock_i,
                rune_name=opt.rune.name,
                zh_cn=opt.rune.zh_cn,
                price_ex=opt.price_ex,
                effect_multiplier=opt.multiplier,
                gains=dict(opt.gains),
                purpose="hard_floor",
            )
        )
        for k in _STAT_KEYS:
            rem_hard[k] = max(0, rem_hard[k] - opt.gains[k])
        for k in ("fire", "cold", "lightning"):
            rem_over[k] = max(0, rem_over[k] - opt.gains[k])

    # Phase B: Soft Overcap Buffer on remaining non-weapon sockets using cheap zero-penalty runes only
    while any(rem_over.values()):
        best_over: tuple[float, int, _AugmentOption] | None = None
        for idx, (sl, _sock_i) in enumerate(open_sockets):
            if idx in used_indices or sl.is_weapon_slot:
                continue
            for opt in opts_cache.get(sl.slot_name, ()):
                if opt.price_ex > overcap_max_single_ex or opt.penalty_ex > 0.0 or any(v < 0 for v in opt.gains.values()):
                    continue
                covered = sum(min(rem_over[k], opt.gains[k]) for k in rem_over)
                if covered <= 0:
                    continue
                score = opt.price_ex / covered
                if best_over is None or score < best_over[0]:
                    best_over = (score, idx, opt)
        if best_over is None:
            break
        _score, chosen_idx, opt = best_over
        sl, sock_i = open_sockets[chosen_idx]
        used_indices.add(chosen_idx)
        total_ex += opt.price_ex
        assignments.append(
            SocketAssignment(
                slot_name=sl.slot_name,
                socket_index=sock_i,
                rune_name=opt.rune.name,
                zh_cn=opt.rune.zh_cn,
                price_ex=opt.price_ex,
                effect_multiplier=opt.multiplier,
                gains=dict(opt.gains),
                purpose="overcap_buffer",
            )
        )
        for k in ("fire", "cold", "lightning"):
            rem_over[k] = max(0, rem_over[k] - opt.gains[k])

    return Stage2RunePlan(
        assignments=tuple(assignments),
        total_cost_ex=total_ex,
        remaining_hard=rem_hard,
        remaining_overcap=rem_over,
        crafted_slot_hints=crafted_slot_hints,
        warnings=tuple(warnings),
    )


def apply_rune_plan_live(
    worker: Any,
    slots: tuple[EquippedSlotAudit, ...],
    plan: Stage2RunePlan,
    *,
    char_level: int = 90,
    is_shaman: bool = False,
    catalog: RuneInstillCatalog | None = None,
) -> None:
    """Apply planned Stage 2 runes to live `BridgeWorker` slots inside a `BuildTransaction`."""
    cat = catalog or load_rune_instill_catalog()
    by_slot: dict[str, list[str]] = {}
    for asgn in plan.assignments:
        by_slot.setdefault(asgn.slot_name, []).append(asgn.rune_name)
    for sl in slots:
        new_runes = by_slot.get(sl.slot_name)
        if not new_runes and not sl.stripped_runes:
            continue
        full_runes = [*sl.preserved_bound_runes, *(new_runes or [])]
        rendered = apply_runes_to_item_raw(
            sl.stripped_raw_text,
            full_runes,
            item_type=sl.item_type,
            base_name=sl.base_name,
            char_level=char_level,
            is_shaman=is_shaman,
            allow_overwrite_bound=True,
            catalog=cat,
        )
        worker.call("add_item", {"raw": rendered, "slotName": sl.slot_name})


__all__ = [
    "AugmentSideEffects",
    "SocketAssignment",
    "Stage2RunePlan",
    "apply_rune_plan_live",
    "format_max_res_drop_warning",
    "inspect_augment_side_effects",
    "parse_augment_stat_vector",
    "solve_rune_allocation",
]
