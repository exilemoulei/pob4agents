# SPDX-License-Identifier: GPL-3.0-only
"""Clean-room compressed-state Bellman MDP Policy Iteration solver & concrete-item replay."""

from __future__ import annotations

import math
import random
from typing import Any

from .mdp_transitions import (
    MdpAction,
    MdpState,
    PoolStatsCache,
    actions_for_state,
    bone_family_for_slug,
    state_to_key,
)
from .models import (
    ActionEval,
    BaseItemEntry,
    CraftingState,
    CraftPlan,
    ItemRarity,
    PolicyStep,
    TargetSpec,
    WeightConfidence,
    lowest_confidence,
)
from .pool_engine import ModPoolEngine
from .price_book import PriceBook

_INF_COST = 1e12


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


class CraftMdpSolver:
    """Bellman Policy Iteration over compressed crafting states + concrete-item replay."""

    def __init__(self, engine: ModPoolEngine | None = None) -> None:
        self.engine = engine or ModPoolEngine()

    def _cost(self, pb: PriceBook, lever_name: str, omens: tuple[str, ...] = ()) -> float | None:
        lv = self.engine.resolve_lever(lever_name)
        if pb.is_excluded(lv):
            return None
        om_entries = tuple(self.engine.resolve_lever(o) for o in omens)
        if any(pb.is_excluded(o) for o in om_entries):
            return None
        try:
            return pb.action_cost(lv, om_entries)
        except ValueError:
            return None

    def _solve_bellman(
        self,
        start_key: MdpState,
        base: BaseItemEntry,
        ilvl: int,
        targets: tuple[TargetSpec, ...],
        pb: PriceBook,
        base_cost_ex: float,
    ) -> tuple[dict[MdpState, MdpAction], dict[MdpState, float], int]:
        full_mask = (1 << len(targets)) - 1
        cache = PoolStatsCache(self.engine, base, ilvl, targets, pb)
        state_actions: dict[MdpState, list[MdpAction]] = {}
        queue: list[MdpState] = [start_key]
        visited: set[MdpState] = {start_key}
        while queue and len(visited) < 4000:
            cur = queue.pop(0)
            if cur.hit_mask == full_mask:
                continue
            acts = actions_for_state(cur, start_key, base, ilvl, targets, cache, pb, base_cost_ex)
            state_actions[cur] = acts
            for act in acts:
                for nxt, _ in act.transitions:
                    if nxt not in visited:
                        visited.add(nxt)
                        queue.append(nxt)

        values: dict[MdpState, float] = {s: (0.0 if s.hit_mask == full_mask else 1e6) for s in visited}
        policy: dict[MdpState, MdpAction] = {s: acts[0] for s, acts in state_actions.items() if acts}

        iters = 0
        for _ in range(44):
            iters += 1
            for _ in range(250):
                max_diff = 0.0
                for s, act in policy.items():
                    nv = min(_INF_COST, act.cost_ex + sum(p * values.get(ns, _INF_COST) for ns, p in act.transitions))
                    diff = abs(nv - values[s])
                    if diff > max_diff:
                        max_diff = diff
                    values[s] = nv
                if max_diff < 1e-3:
                    break
            changed = False
            for s, acts in state_actions.items():
                if not acts:
                    continue
                best = min(
                    acts,
                    key=lambda a: (
                        a.cost_ex + sum(p * values.get(ns, _INF_COST) for ns, p in a.transitions),
                        len(a.omens),
                        a.cost_ex,
                    ),
                )
                if policy.get(s) != best:
                    policy[s] = best
                    changed = True
            if not changed:
                break
        return policy, values, iters

    def _replay_concrete(
        self,
        start_state: CraftingState,
        targets: tuple[TargetSpec, ...],
        policy: dict[MdpState, MdpAction],
        base_cost_ex: float,
        n_sims: int,
        seed: int,
    ) -> tuple[list[float], float]:
        rng = random.Random(seed)
        desired = {mid for t in targets for mid in t.matching_mod_ids}
        full_mask = (1 << len(targets)) - 1
        costs: list[float] = []
        restarts = 0
        for _ in range(max(10, n_sims)):
            st = start_state.clone()
            total_c = base_cost_ex if start_state.rarity == ItemRarity.NORMAL else 0.0
            did_restart = False
            for _step in range(800):
                key = state_to_key(st, targets)
                if key.hit_mask == full_mask:
                    break
                act = policy.get(key)
                if act is None or act.kind == "restart":
                    st = CraftingState(base=start_state.base, ilvl=start_state.ilvl, rarity=ItemRarity.NORMAL)
                    total_c += base_cost_ex
                    did_restart = True
                    continue
                total_c += act.cost_ex
                st = self.engine.sample_transition(st, act.lever_name, act.omens, desired, rng)
            costs.append(total_c)
            if did_restart:
                restarts += 1
        costs.sort()
        return costs, restarts / max(1, len(costs))

    def _build_playbook(
        self,
        start_key: MdpState,
        targets: tuple[TargetSpec, ...],
        policy: dict[MdpState, MdpAction],
    ) -> tuple[PolicyStep, ...]:
        steps: list[PolicyStep] = []
        cur = start_key
        full_mask = (1 << len(targets)) - 1
        seen: set[MdpState] = set()
        for step_idx in range(1, 12):
            if cur.hit_mask == full_mask or cur in seen:
                break
            seen.add(cur)
            act = policy.get(cur)
            if act is None:
                break
            prog = [t for t in act.transitions if t[0].hit_mask > cur.hit_mask]
            hit_p = sum(p for _, p in prog)
            next_st = (
                max(prog, key=lambda x: (bin(x[0].hit_mask).count("1"), x[1]))[0]
                if prog
                else act.transitions[0][0]
            )
            miss_states = [t[0] for t in act.transitions if t[0].hit_mask <= cur.hit_mask]
            on_miss = "止损重开（换白底重新起手）"
            if miss_states:
                m_act = policy.get(miss_states[0])
                if m_act and m_act.kind == "lever":
                    om_txt = f" + {' + '.join(m_act.omens)}" if m_act.omens else ""
                    on_miss = f"抢救: {m_act.lever_name}{om_txt}"
            new_bits = next_st.hit_mask & ~cur.hit_mask
            goal_names = [targets[i].label for i in range(len(targets)) if new_bits & (1 << i)]
            steps.append(
                PolicyStep(
                    step_index=step_idx,
                    stage=f"{cur.rarity.value} ({cur.n_pref}P/{cur.n_suff}S)",
                    action=act.lever_name,
                    omens=act.omens,
                    goal=", ".join(goal_names) if goal_names else "推进稀有度/槽位检查点",
                    step_cost_ex=act.cost_ex,
                    hit_probability=hit_p,
                    on_miss=on_miss,
                    weight_confidence=act.confidence,
                    notes=act.note,
                )
            )
            cur = next_st
        return tuple(steps)

    def evaluate_next_actions(
        self,
        state: CraftingState,
        targets: tuple[TargetSpec, ...],
        price_book: PriceBook,
        *,
        protected_mod_ids: set[str] | None = None,
    ) -> list[ActionEval]:
        unhit_ids = {
            mid for t in targets if not t.is_satisfied_by(state) for mid in t.matching_mod_ids
        }
        protected = set(protected_mod_ids or set())
        cands: list[tuple[str, list[str]]] = []
        if state.rarity == ItemRarity.NORMAL:
            cands = [
                ("Orb of Transmutation", []), ("Greater Orb of Transmutation", []),
                ("Perfect Orb of Transmutation", []), ("Orb of Alchemy", []),
            ]
        elif state.rarity == ItemRarity.MAGIC:
            if state.has_open_prefix(ItemRarity.MAGIC) or state.has_open_suffix(ItemRarity.MAGIC):
                cands += [
                    ("Orb of Augmentation", []), ("Greater Orb of Augmentation", []),
                    ("Perfect Orb of Augmentation", []),
                ]
            cands += [
                ("Regal Orb", []), ("Greater Regal Orb", []), ("Perfect Regal Orb", []),
                ("Regal Orb", ["Omen of Sinistral Coronation"]),
                ("Regal Orb", ["Omen of Dextral Coronation"]),
            ]
            for ess in self.engine.list_levers("essence"):
                if ess.forced_mods.get(state.base.poe2db_slug) in unhit_ids:
                    cands.append((ess.name, []))
        elif state.rarity == ItemRarity.RARE:
            for ex in ("Exalted Orb", "Greater Exalted Orb", "Perfect Exalted Orb"):
                cands += [
                    (ex, []), (ex, ["Omen of Sinistral Exaltation"]), (ex, ["Omen of Dextral Exaltation"])
                ]
            for ch in ("Chaos Orb", "Greater Chaos Orb", "Perfect Chaos Orb"):
                cands += [(ch, []), (ch, ["Omen of Whittling"])]
            cands += [
                ("Orb of Annulment", []),
                ("Orb of Annulment", ["Omen of Sinistral Annulment"]),
                ("Orb of Annulment", ["Omen of Dextral Annulment"]),
            ]
            if state.has_desecrated:
                cands.append(("Orb of Annulment", ["Omen of Light"]))
            if len(state.mods) >= 4 and not state.has_fractured:
                cands.append(("Fracturing Orb", []))
            for lv in self.engine.list_levers("perfect_or_corrupted_essence") + self.engine.list_levers("alloy"):
                if lv.forced_mods.get(state.base.poe2db_slug) in unhit_ids:
                    cands += [
                        (lv.name, []),
                        (lv.name, ["Omen of Sinistral Crystallisation"]),
                        (lv.name, ["Omen of Dextral Crystallisation"]),
                    ]
            bfam = bone_family_for_slug(state.base.poe2db_slug)
            if bfam and not state.has_desecrated:
                bname = f"Ancient {bfam}" if state.ilvl >= 40 else f"Preserved {bfam}"
                cands += [(bname, []), (bname, ["Omen of Abyssal Echoes"])]

        results: list[ActionEval] = []
        for lname, omens in cands:
            c = self._cost(price_book, lname, tuple(omens))
            if c is None:
                continue
            ev = self.engine.evaluate_action(state, unhit_ids, lname, omens, protected_mod_ids=protected)
            if ev.hit_probability <= 0.0 and ev.whittling is None:
                continue
            exp_c = (c / ev.hit_probability) if ev.hit_probability > 0 else None
            results.append(
                ActionEval(
                    lever=ev.lever.name, omens=tuple(o.name for o in ev.omens), step_cost_ex=c,
                    hit_probability=ev.hit_probability, expected_cost_per_hit_ex=exp_c,
                    collateral_risk=ev.collateral_remove_probability,
                    weight_confidence=ev.weight_confidence, notes=ev.notes, whittling=ev.whittling,
                )
            )
        results.sort(key=lambda a: (a.expected_cost_per_hit_ex is None, a.expected_cost_per_hit_ex or _INF_COST))
        return results

    def plan_craft(
        self,
        base: str | BaseItemEntry,
        ilvl: int,
        target_mods: list[str | dict[str, Any] | TargetSpec],
        *,
        starting_state: CraftingState | None = None,
        protect_existing: bool = False,
        price_book: PriceBook | None = None,
        currency_prices_ex: dict[str, float] | None = None,
        excluded: list[str] | tuple[str, ...] | None = None,
        base_cost_ex: float = 2.0,
        buy_price_ex: float | None = None,
        n_sims: int = 600,
        seed: int = 42,
    ) -> CraftPlan:
        """Solve optimal PoE2 0.5.5 crafting policy via compressed-state MDP + concrete-item replay."""
        b_entry = base if isinstance(base, BaseItemEntry) else self.engine.resolve_base(base)
        pb = price_book or PriceBook.build(currency_prices_ex, excluded=excluded)
        targets = tuple(self.engine.resolve_target(b_entry.poe2db_slug, t, ilvl=ilvl) for t in target_mods)
        if not targets:
            raise ValueError("At least one target modifier is required.")

        st = (
            starting_state.clone()
            if starting_state is not None
            else CraftingState(base=b_entry, ilvl=ilvl, rarity=ItemRarity.NORMAL)
        )
        start_key = state_to_key(st, targets)
        fresh_key = MdpState(ItemRarity.NORMAL, 0, 0, 0, False, False, False, False)

        policy, values, iters = self._solve_bellman(fresh_key, b_entry, ilvl, targets, pb, base_cost_ex)
        if start_key not in values:
            p2, v2, it2 = self._solve_bellman(start_key, b_entry, ilvl, targets, pb, base_cost_ex)
            policy.update(p2)
            values.update(v2)
            iters = max(iters, it2)

        mdp_expected = values.get(start_key, _INF_COST) + (base_cost_ex if st.rarity == ItemRarity.NORMAL else 0.0)
        sim_costs, restart_rate = self._replay_concrete(st, targets, policy, base_cost_ex, n_sims, seed)
        p50, p80, p95 = _percentile(sim_costs, 0.50), _percentile(sim_costs, 0.80), _percentile(sim_costs, 0.95)
        mean_sim = sum(sim_costs) / max(1, len(sim_costs))
        exp_cost = min(mdp_expected, mean_sim) if mdp_expected < _INF_COST / 2 else mean_sim

        protected = (
            {m.mod_id for m in st.mods}
            if protect_existing
            else {m.mod_id for t in targets for m in st.mods if m.mod_id in t.matching_mod_ids}
        )
        cands = self.evaluate_next_actions(st, targets, pb, protected_mod_ids=protected)
        playbook = self._build_playbook(start_key, targets, policy)

        warnings: list[str] = list(pb.warnings)
        if self.engine.bundle.patch_mismatch_warning:
            warnings.append(self.engine.bundle.patch_mismatch_warning)
        if st.has_fractured_crafted_mod:
            warnings.append("当前破裂词缀占用唯一 Crafted 名额，0.5.0+ 已禁止全部精华/合金动作。")
        if not b_entry.has_measured_weights:
            warnings.append(f"底材 `{b_entry.poe2db_slug}` 常规权重未实测（confidence=unmeasured）。")

        fresh_total = values.get(fresh_key, _INF_COST) + base_cost_ex
        rec = "craft"
        if buy_price_ex is not None and buy_price_ex > 0 and buy_price_ex < exp_cost * 0.95:
            rec = "buy"
        elif st.rarity != ItemRarity.NORMAL and (
            (policy.get(start_key) and policy[start_key].kind == "restart")
            or exp_cost > fresh_total * 1.05
        ):
            rec = "stop_loss_restart"

        confs = [s.weight_confidence for s in playbook] or [a.weight_confidence for a in cands[:5]]
        if not b_entry.has_measured_weights:
            confs.append(WeightConfidence.UNMEASURED)
        overall_conf = lowest_confidence(confs) if confs else WeightConfidence.UNMEASURED

        return CraftPlan(
            patch=self.engine.patch,
            base_type=b_entry.name,
            base_slug=b_entry.poe2db_slug,
            ilvl=ilvl,
            targets=tuple(t.label for t in targets),
            expected_cost_ex=exp_cost,
            expected_cost_div=(exp_cost / pb.divine_rate_ex) if pb.divine_rate_ex > 0 else None,
            p50_cost_ex=p50,
            p80_cost_ex=p80,
            p95_cost_ex=p95,
            restart_rate=restart_rate,
            mdp_states_solved=len(values),
            mdp_iterations=iters,
            recommendation=rec,
            weight_confidence=overall_conf,
            policy_steps=playbook,
            candidate_actions=tuple(cands[:12]),
            warnings=tuple(warnings),
            used_fallback_prices=pb.used_fallback,
            buy_price_ex=buy_price_ex,
        )
