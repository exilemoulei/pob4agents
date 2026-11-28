# SPDX-License-Identifier: GPL-3.0-only
"""CLI commands for `pob2 craft <inspect|odds|plan|next>` (L4)."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    ctx_wants_json,
    echo_utf8,
    emit_output,
    output_options,
    pob2_group,
)
from .cli_item import (
    handle_craft_or_inspect_error,
    item_group,
    read_item_source,
    render_inspect_md,
    run_inspect_command,
)
from .craft.item_inspector import inspect_item_text
from .errors import EXIT_REQUEST_ERROR

_WANT_HELP = (
    "目标词缀（可重复指定 `--want`；示例: 'T1 Spell Damage', 'T2 Cast Speed', "
    "'+30% Fire Resistance', '最大生命', 'SpellDamageOnWeapon8_'）。"
)

_CRAFT_TARGET_MIGRATION_ERROR = (
    "[POE2-CLI-MIGRATION] `pob2 craft` uses `--want` for target mods "
    "(`--target` is reserved for daemon build slots). Example: `--want \"T2 maximum Life\"`."
)


def _on_legacy_craft_target(ctx: click.Context, _param: click.Parameter, value: tuple[str, ...]) -> None:
    if not value or ctx.resilient_parsing:
        return
    if ctx_wants_json(ctx):
        emit_output(
            ctx,
            {
                "ok": False,
                "game": "poe2",
                "errorKind": "cli_migration",
                "code": "POE2-CLI-MIGRATION",
                "ruleId": "POE2_CLI_CRAFT_TARGET_TO_WANT",
                "trigger": "--target",
                "error": _CRAFT_TARGET_MIGRATION_ERROR,
            },
            as_json=True,
            ok=False,
            ignore_ctx_out=True,
            skip_projection=True,
        )
    else:
        echo_utf8(f"Error: {_CRAFT_TARGET_MIGRATION_ERROR}", err=True)
    ctx.exit(EXIT_REQUEST_ERROR)


def _reject_legacy_target_option(fn: Any) -> Any:
    return click.option(
        "--target",
        "_legacy_target",
        multiple=True,
        hidden=True,
        expose_value=False,
        is_eager=True,
        callback=_on_legacy_craft_target,
    )(fn)


def _render_plan_md(plan: Any, *, title: str) -> str:
    from tabulate import tabulate

    div_str = f" (~{plan.expected_cost_div:.2f} Div)" if plan.expected_cost_div is not None else ""
    lines = [
        f"### {title} [PoE2 {plan.patch}] — {plan.base_type} (`{plan.base_slug}`, ilvl {plan.ilvl})",
        f"- 目标词缀: {', '.join(plan.targets)}",
        f"- 建议裁决: `{plan.recommendation}` | 权重置信度: `{plan.weight_confidence.value}`",
        f"- 动态最优期望造价: **{plan.expected_cost_ex:.1f} Ex**{div_str} | "
        f"预算分位 `P50={plan.p50_cost_ex:.1f} Ex` / `P80={plan.p80_cost_ex:.1f} Ex` / `P95={plan.p95_cost_ex:.1f} Ex` "
        f"| 重开率 `{plan.restart_rate * 100:.1f}%` (MDP {plan.mdp_states_solved} 状态 / {plan.mdp_iterations} 轮收敛)",
    ]
    if plan.policy_steps:
        rows = [
            [
                s.step_index,
                s.stage,
                s.action + (f" + {' + '.join(s.omens)}" if s.omens else ""),
                f"{s.step_cost_ex:.2f} Ex",
                f"{s.hit_probability * 100:.2f}%",
                s.goal,
                s.on_miss,
                s.weight_confidence.value,
            ]
            for s in plan.policy_steps
        ]
        lines.append("")
        lines.append(
            tabulate(
                rows,
                headers=["步", "阶段", "动作与预兆", "单步成本", "命中率", "目标", "歪词缀抢救/止损", "置信度"],
                tablefmt="github",
            )
        )
    if plan.candidate_actions:
        crows = [
            [
                a.lever + (f" + {' + '.join(a.omens)}" if a.omens else ""),
                f"{a.step_cost_ex:.2f} Ex",
                f"{a.hit_probability * 100:.2f}%",
                f"{a.expected_cost_per_hit_ex:.1f} Ex" if a.expected_cost_per_hit_ex else "-",
                f"{a.collateral_risk * 100:.1f}%",
                a.weight_confidence.value,
                a.notes,
            ]
            for a in plan.candidate_actions[:8]
        ]
        lines.append("\n#### 候选单步动作对比")
        lines.append(
            tabulate(
                crows,
                headers=["通货 + 预兆", "单次成本", "命中率", "期望成本/命中", "误伤率", "置信度", "说明"],
                tablefmt="github",
            )
        )
    for w in plan.warnings:
        lines.append(f"- [提示] {w}")
    return "\n".join(lines)


@pob2_group("craft")
def craft_group() -> None:
    """PoE2 0.5.5 做装求解器（词缀体检、单步概率、MDP 路线规划、半成品接力）。"""


@craft_group.command("inspect")
@click.option("--text", "item_text", default=None, help="直接传入 Ctrl+C 装备文本。")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, allow_dash=True), default=None, help="从文件读取装备文本（`-` 表示 stdin）。")
@click.option("--base", "base_override", default=None, help="手动指定底材名或类别 slug。")
@click.option("--ilvl", "ilvl_override", type=int, default=None, help="手动覆盖物品等级。")
@output_options
@click.pass_context
def cmd_craft_inspect(
    ctx: click.Context,
    item_text: str | None,
    file_path: str | None,
    base_override: str | None,
    ilvl_override: int | None,
    as_json: bool,
) -> None:
    """拆解稀有/魔法装备前/后缀、词缀池来源、空槽与 0.5.5 合法性体检。"""
    run_inspect_command(
        ctx,
        item_text=item_text,
        file_path=file_path,
        base_override=base_override,
        ilvl_override=ilvl_override,
        as_json=as_json,
    )


@craft_group.command("odds")
@_reject_legacy_target_option
@click.option("--base", required=True, help="底材名或类别 slug（如 Attuned Wand / Vile Robe / 项链）。")
@click.option("--ilvl", type=int, default=82, show_default=True, help="物品等级。")
@click.option("--want", "wants", multiple=True, required=False, help=_WANT_HELP)
@click.option("--outcomes", "show_outcomes", is_flag=True, default=False, help="输出下一步通货完整词缀族(ModFamily)权重与命中概率分布（无需 --want）。")
@click.option("--lever", default=None, help="指定单个通货（如 Perfect Orb of Transmutation）。")
@click.option("--omen", "omens", multiple=True, help="附加预兆（可重复指定）。")
@click.option("--text", "item_text", default=None, help="可选当前装备 Ctrl+C 文本。")
@click.option("--offline", is_flag=True, default=True, show_default=True, help="使用离线正数基准物价。")
@click.option("--exclude", "excluded", multiple=True, help="屏蔽特定通货或预兆（如 SSF）。")
@output_options
@click.pass_context
def cmd_craft_odds(
    ctx: click.Context,
    base: str,
    ilvl: int,
    wants: tuple[str, ...],
    show_outcomes: bool,
    lever: str | None,
    omens: tuple[str, ...],
    item_text: str | None,
    offline: bool,
    excluded: tuple[str, ...],
    as_json: bool,
) -> None:
    """计算单步通货 + 预兆精确命中率、误伤率、置信度，或完整词缀族(`--outcomes`)概率分布。"""
    from .craft import CraftMdpSolver, ModPoolEngine, fetch_live_price_book
    from .craft.pool_outcomes import (
        compute_matched_tiers_for_state,
        compute_next_step_outcomes,
        default_rarity_for_lever,
        render_outcomes_md,
    )

    eng = ModPoolEngine()
    try:
        if not wants and not show_outcomes:
            raise ValueError(
                "Specify at least one `--want <mod>` or pass `--outcomes` for the full ModFamily probability distribution."
            )
        if item_text:
            rep = inspect_item_text(item_text, eng, base_override=base, ilvl_override=ilvl)
            state = rep.state
            assert state is not None
        else:
            b = eng.resolve_base(base)
            from .craft.models import CraftingState, ItemRarity

            lv_entry = eng.resolve_lever(lever) if lever else None
            def_rar = default_rarity_for_lever(lv_entry) if lv_entry is not None else ItemRarity.NORMAL
            state = CraftingState(base=b, ilvl=ilvl, rarity=def_rar)

        outcomes_payload = (
            compute_next_step_outcomes(eng, state, lever_name=lever, omens=list(omens))
            if show_outcomes
            else None
        )
        if show_outcomes and not wants:
            assert outcomes_payload is not None
            emit_output(
                ctx,
                outcomes_payload,
                as_json=as_json,
                markdown_text=render_outcomes_md(outcomes_payload),
            )
            return

        t_specs = tuple(eng.resolve_target(state.base.poe2db_slug, t, ilvl=state.ilvl) for t in wants)
        t_ids = {mid for ts in t_specs for mid in ts.matching_mod_ids}
        if lever:
            res = eng.evaluate_action(state, t_ids, lever, list(omens))
            eval_dict = res.to_dict()
            payload: dict[str, Any] = {
                "game": "poe2",
                "patch": eng.patch,
                "base": state.base.name,
                "ilvl": state.ilvl,
                "matchedTiers": eval_dict["matchedTiers"],
                "evaluation": eval_dict,
            }
            if outcomes_payload is not None:
                payload["totalPoolWeight"] = outcomes_payload["totalPoolWeight"]
                payload["familyCount"] = outcomes_payload["familyCount"]
                payload["outcomes"] = outcomes_payload["outcomes"]
            msg = (
                f"[{eng.patch}] {res.lever.name}"
                + (f" + {', '.join(o.name for o in res.omens)}" if res.omens else "")
                + f" -> 命中率 **{res.hit_probability * 100:.4f}%** ({res.target_weight}/{res.eligible_pool_weight}) | "
                + f"误伤率 `{res.collateral_remove_probability * 100:.2f}%` | 置信度 `{res.weight_confidence.value}` | {res.notes}"
            )
            if outcomes_payload is not None:
                msg += "\n\n" + render_outcomes_md(outcomes_payload)
            emit_output(ctx, payload, as_json=as_json, markdown_text=msg)
            return

        solver = CraftMdpSolver(eng)
        pb = fetch_live_price_book(offline=offline, excluded=excluded)
        actions = solver.evaluate_next_actions(state, t_specs, pb)
        matched_tiers = compute_matched_tiers_for_state(eng, state, t_specs, omens=list(omens))
    except Exception as exc:
        handle_craft_or_inspect_error(ctx, exc, as_json=as_json)
        return

    out_json: dict[str, Any] = {
        "game": "poe2",
        "patch": eng.patch,
        "base": state.base.name,
        "ilvl": state.ilvl,
        "matchedTiers": matched_tiers,
        "actions": [a.to_dict() for a in actions],
    }
    if pb.warnings:
        out_json["warnings"] = list(pb.warnings)
    if outcomes_payload is not None:
        out_json["totalPoolWeight"] = outcomes_payload["totalPoolWeight"]
        out_json["familyCount"] = outcomes_payload["familyCount"]
        out_json["outcomes"] = outcomes_payload["outcomes"]
    if as_json or ctx_wants_json(ctx):
        emit_output(ctx, out_json, as_json=True)
        return
    from tabulate import tabulate

    rows = [
        [
            a.lever + (f" + {' + '.join(a.omens)}" if a.omens else ""),
            f"{a.step_cost_ex:.2f}",
            f"{a.hit_probability * 100:.3f}%",
            f"{a.collateral_risk * 100:.1f}%",
            a.weight_confidence.value,
            a.notes,
        ]
        for a in actions[:15]
    ]
    tbl = tabulate(rows, headers=["通货 + 预兆", "成本(Ex)", "命中率", "误伤率", "置信度", "说明"], tablefmt="github")
    if outcomes_payload is not None:
        tbl += "\n\n" + render_outcomes_md(outcomes_payload)
    emit_output(ctx, out_json, as_json=False, markdown_text=tbl)


@craft_group.command("plan")
@_reject_legacy_target_option
@click.option("--base", required=True, help="底材名或类别 slug（如 Attuned Wand / Vile Robe / Breach Ring）。")
@click.option("--ilvl", type=int, default=82, show_default=True, help="物品等级。")
@click.option("--want", "wants", multiple=True, required=True, help=_WANT_HELP)
@click.option("--base-cost", "base_cost_ex", type=float, default=2.0, show_default=True, help="白底单价(Ex)。")
@click.option("--buy-price", "buy_price_ex", type=float, default=None, help="可选市集现货价(Ex)，触发 Buy vs Craft 裁决。")
@click.option("--exclude", "excluded", multiple=True, help="屏蔽通货/预兆（适配独狼 SSF）。")
@click.option("--offline", is_flag=True, default=True, show_default=True, help="离线使用正数基准物价。")
@click.option("--sims", "n_sims", type=int, default=600, show_default=True, help="具体物品回放次数。")
@click.option("--seed", type=int, default=42, show_default=True, help="回放随机种子。")
@output_options
@click.pass_context
def cmd_craft_plan(
    ctx: click.Context,
    base: str,
    ilvl: int,
    wants: tuple[str, ...],
    base_cost_ex: float,
    buy_price_ex: float | None,
    excluded: tuple[str, ...],
    offline: bool,
    n_sims: int,
    seed: int,
    as_json: bool,
) -> None:
    """运行压缩状态 MDP 策略迭代 + 具体物品回放，输出分步中文剧本与 P50/P80/P95 造价。"""
    from .craft import CraftMdpSolver, fetch_live_price_book

    solver = CraftMdpSolver()
    pb = fetch_live_price_book(offline=offline, excluded=excluded)
    try:
        plan = solver.plan_craft(
            base=base,
            ilvl=ilvl,
            target_mods=list(wants),
            price_book=pb,
            base_cost_ex=base_cost_ex,
            buy_price_ex=buy_price_ex,
            n_sims=n_sims,
            seed=seed,
        )
    except Exception as exc:
        handle_craft_or_inspect_error(ctx, exc, as_json=as_json)
        return
    emit_output(
        ctx,
        plan.to_dict(),
        as_json=as_json,
        markdown_text=_render_plan_md(plan, title="PoE2 最优制作工艺路线（MDP 动态策略）"),
    )


@craft_group.command("next")
@_reject_legacy_target_option
@click.option("--text", "item_text", default=None, help="半成品 Ctrl+C 文本。")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, allow_dash=True), default=None, help="半成品文本文件（`-` 表示 stdin）。")
@click.option("--base", "base_override", default=None, help="可选底材覆盖。")
@click.option("--ilvl", "ilvl_override", type=int, default=None, help="可选物品等级覆盖。")
@click.option("--want", "wants", multiple=True, required=True, help=_WANT_HELP)
@click.option("--protect-existing", is_flag=True, default=False, help="锁定当前装备全部已有词缀并计算误伤率。")
@click.option("--base-cost", "base_cost_ex", type=float, default=2.0, show_default=True, help="重开白底单价(Ex)。")
@click.option("--buy-price", "buy_price_ex", type=float, default=None, help="可选成品现货价(Ex)。")
@click.option("--exclude", "excluded", multiple=True, help="屏蔽通货/预兆。")
@click.option("--offline", is_flag=True, default=True, show_default=True, help="离线使用正数基准物价。")
@output_options
@click.pass_context
def cmd_craft_next(
    ctx: click.Context,
    item_text: str | None,
    file_path: str | None,
    base_override: str | None,
    ilvl_override: int | None,
    wants: tuple[str, ...],
    protect_existing: bool,
    base_cost_ex: float,
    buy_price_ex: float | None,
    excluded: tuple[str, ...],
    offline: bool,
    as_json: bool,
) -> None:
    """半成品接力：评估当前半成品下一手最优操作、削切误伤风险与止损判定。"""
    from .craft import CraftMdpSolver, ModPoolEngine, fetch_live_price_book

    raw = read_item_source(item_text, file_path)
    eng = ModPoolEngine()
    solver = CraftMdpSolver(eng)
    pb = fetch_live_price_book(offline=offline, excluded=excluded)
    try:
        rep = inspect_item_text(raw, eng, base_override=base_override, ilvl_override=ilvl_override)
        assert rep.state is not None
        plan = solver.plan_craft(
            base=rep.state.base,
            ilvl=rep.state.ilvl,
            target_mods=list(wants),
            starting_state=rep.state,
            protect_existing=protect_existing,
            price_book=pb,
            base_cost_ex=base_cost_ex,
            buy_price_ex=buy_price_ex,
            n_sims=400,
            seed=42,
        )
    except Exception as exc:
        handle_craft_or_inspect_error(ctx, exc, as_json=as_json)
        return
    out = plan.to_dict()
    out["inspection"] = rep.to_dict()
    md_text = None
    if not (as_json or ctx_wants_json(ctx)):
        md_text = render_inspect_md(rep) + "\n\n" + _render_plan_md(plan, title="半成品下一手接力与止损评估")
    emit_output(ctx, out, as_json=as_json, markdown_text=md_text)


__all__ = ["craft_group", "item_group"]
