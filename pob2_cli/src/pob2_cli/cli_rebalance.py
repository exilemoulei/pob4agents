# SPDX-License-Identifier: GPL-3.0-only
"""Click CLI commands `pob2 fix-gaps` and `pob2 spirit` (L4)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

from . import updater
from .bridge_client import BridgeWorker
from .cli_common import (
    daemon_call_cli,
    emit_output,
    output_options,
    pob2_command,
    resolve_target_slot,
    target_option,
)
from .errors import DaemonRequestError
from .game_profile import stamp_game
from .importers.share_urls import resolve_poe2_build_source
from .rebalance import (
    SpiritKnapsackPlan,
    build_offline_pack_catalog_options,
    solve_fix_gaps_live,
    solve_forward_spirit_knapsack,
    solve_live_spirit_deficit,
)


def _resolve_engine_dir(engine_dir: Path | None) -> tuple[Path, Path | None]:
    raw_eng = (
        str(engine_dir)
        if engine_dir
        else (os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip())
    )
    raw_data = os.environ.get("POB2_DATA_DIR", "").strip()
    if not raw_eng:
        raise DaemonRequestError("Live PoB2 engine required: set `POB2_ENGINE_DIR` or pass `--engine-dir`.")
    eng = Path(raw_eng)
    data = Path(raw_data) if raw_data else None
    if not updater.is_valid_engine_dir(eng, data_dir=data):
        raise DaemonRequestError(f"Invalid PoB2 engine directory: {eng}")
    return eng, data


def _load_source_into_worker(worker: BridgeWorker, build_source: str, *, data_dir: Path | None = None) -> None:
    resolved = resolve_poe2_build_source(build_source, data_dir=data_dir)
    worker.call("import_code", {"code": resolved.code, "mode": "replace"})


@pob2_command("fix-gaps")
@click.option(
    "--build",
    "build_source",
    default=None,
    help="Optional standalone PoE2 build source (XML file, share code, pobb.in, poe2db, WeGame). Omit to use active daemon slot.",
)
@target_option(allow_slot_alias=True)
@click.option(
    "--overcap",
    "overcap_target",
    type=int,
    default=20,
    show_default=True,
    help="Soft overcap buffer target above each element's MaxRes (never equalizes healthy overcap).",
)
@click.option(
    "--chaos-target",
    "chaos_target",
    type=int,
    default=0,
    show_default=True,
    help="Hard floor target for Chaos Resistance.",
)
@click.option(
    "--apply",
    "apply_flag",
    is_flag=True,
    default=False,
    help="Commit Stages 1-4 edits to the active daemon slot (undoable via `pob2 undo`).",
)
@click.option(
    "--engine-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional PoB2 engine root directory for standalone `--build` runs (defaults to POB2_ENGINE_DIR).",
)
@output_options
@click.pass_context
def cmd_fix_gaps(
    ctx: click.Context,
    build_source: str | None,
    target: str,
    overcap_target: int,
    chaos_target: int,
    apply_flag: bool,
    engine_dir: Path | None,
    as_json: bool,
) -> None:
    """5-stage zero-buy PoE2 attribute, resistance cap+overcap, and Spirit deficit solver."""
    if build_source is None:
        data_out = daemon_call_cli(
            "fix_gaps",
            {
                "target": resolve_target_slot(target),
                "overcap": overcap_target,
                "chaos_target": chaos_target,
                "apply": apply_flag,
            },
        )
    else:
        if apply_flag:
            raise DaemonRequestError(
                "`--apply` commits to the running daemon slot; omit `--build` (load first with `pob2 load <source>`)."
            )
        eng, data = _resolve_engine_dir(engine_dir)
        worker = BridgeWorker(slot_name="fix_gaps_cli", pob_dir=eng, data_dir=data)
        try:
            worker.start()
            _load_source_into_worker(worker, build_source, data_dir=data)
            report = solve_fix_gaps_live(
                worker,
                overcap_target=overcap_target,
                chaos_target=chaos_target,
                apply=False,
            )
        finally:
            worker.stop()
        data_out = stamp_game(report.to_dict())
        data_out["evalMode"] = "live_cow"
        data_out["applied"] = False

    emit_output(
        ctx,
        stamp_game(data_out),
        as_json=as_json,
        markdown_text=_format_fix_gaps_human(data_out),
    )


@pob2_command("spirit")
@click.option(
    "--budget",
    type=int,
    default=None,
    help="Forward packing mode: total Spirit budget (e.g. --budget 220) to pack optimal buffs/supports offline.",
)
@click.option(
    "--build",
    "build_source",
    default=None,
    help="Optional standalone PoE2 build source (XML file, share code, URL) for live CoW Spirit deficit rescue.",
)
@target_option(allow_slot_alias=True, help_text="Target daemon build slot when rescuing active slot (default: main).")
@click.option(
    "--apply",
    "apply_flag",
    is_flag=True,
    default=False,
    help="Commit Spirit deficit rescue edits to the active daemon slot (undoable via `pob2 undo`).",
)
@click.option("--metric", default="CombinedDPS", show_default=True, help="Target metric (CombinedDPS | TotalEHP).")
@click.option(
    "--engine-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional PoB2 engine root directory for standalone `--build` runs (defaults to POB2_ENGINE_DIR).",
)
@output_options
@click.pass_context
def cmd_spirit(
    ctx: click.Context,
    budget: int | None,
    build_source: str | None,
    target: str,
    apply_flag: bool,
    metric: str,
    engine_dir: Path | None,
    as_json: bool,
) -> None:
    """PoE2 discrete Spirit knapsack solver: rescue active slot Spirit deficit or pack optimal buffs within `--budget`."""
    if budget is not None:
        if apply_flag:
            raise DaemonRequestError("`--apply` cannot be combined with offline `--budget` forward packing.")
        if budget <= 0:
            raise DaemonRequestError("`--budget` must be a positive integer (e.g. `--budget 220`).")
        opts = build_offline_pack_catalog_options(metric=metric)
        chosen, used = solve_forward_spirit_knapsack(opts, budget)
        plan = SpiritKnapsackPlan(
            mode="pack",
            target_spirit=budget,
            achieved_spirit=used,
            remaining_deficit=max(0, budget - used),
            selected=tuple(chosen),
            all_evaluated=tuple(opts),
        )
        out = stamp_game(plan.to_dict())
        out["evalMode"] = "catalog_knapsack"
        out["applied"] = False
    elif build_source is not None:
        if apply_flag:
            raise DaemonRequestError(
                "`--apply` commits to the running daemon slot; omit `--build` (load first with `pob2 load <source>`)."
            )
        eng, data = _resolve_engine_dir(engine_dir)
        worker = BridgeWorker(slot_name="spirit_cli", pob_dir=eng, data_dir=data)
        try:
            worker.start()
            _load_source_into_worker(worker, build_source, data_dir=data)
            plan = solve_live_spirit_deficit(worker)
        finally:
            worker.stop()
        out = stamp_game(plan.to_dict())
        out["evalMode"] = "live_cow"
        out["applied"] = False
    else:
        out = daemon_call_cli(
            "spirit",
            {
                "target": resolve_target_slot(target),
                "metric": metric,
                "apply": apply_flag,
            },
        )

    emit_output(
        ctx,
        stamp_game(out),
        as_json=as_json,
        markdown_text=_format_spirit_human(out),
    )


def _format_fix_gaps_human(data: dict[str, Any]) -> str:
    ig_r = (data.get("currentInGame") or data["before"])["resistances"]
    wc_r = (data.get("withoutConditionalAndCharm") or data["before"])["resistances"]
    bn_r = data["bareNet"]["resistances"]
    after_r = data["afterVerified"]["resistances"]
    after_a = data["afterVerified"]["attributes"]
    after_s = data["afterVerified"]["spirit"]
    applied_tag = f" | Applied(rev={data.get('rev')})" if data.get("applied") else ""
    lines = [
        f"PoE2 {data['patch']} 5-Stage Fix-Gaps | HardFloorsMet={data['allHardFloorsMet']} | "
        f"Gold={data['totalGoldCost']} | EstCost={data['totalExCost']:.1f} Ex | OvercapTarget=+{data['overcapTarget']}%{applied_tag}",
        f"  CurrentInGame Res: Fire {ig_r['fire']}/{ig_r['fireMax']}, Cold {ig_r['cold']}/{ig_r['coldMax']}, Light {ig_r['lightning']}/{ig_r['lightningMax']}, Chaos {ig_r['chaos']}",
        f"  WithoutConditionalAndCharm Res: Fire {wc_r['fire']}/{wc_r['fireMax']}, Cold {wc_r['cold']}/{wc_r['coldMax']}, Light {wc_r['lightning']}/{wc_r['lightningMax']}, Chaos {wc_r['chaos']}",
        f"  BareNet Res (gear+quests kept, replaceable runes stripped): Fire {bn_r['fire']}/{bn_r['fireMax']}, Cold {bn_r['cold']}/{bn_r['coldMax']}, Light {bn_r['lightning']}/{bn_r['lightningMax']}, Chaos {bn_r['chaos']}",
        f"  After Attrs: Str {after_a['str']}/{after_a['reqStr']}, Dex {after_a['dex']}/{after_a['reqDex']}, Int {after_a['int']}/{after_a['reqInt']}",
        f"  After Res: Fire {after_r['fire']}/{after_r['fireMax']} (+{after_r['fireOvercap']}), "
        f"Cold {after_r['cold']}/{after_r['coldMax']} (+{after_r['coldOvercap']}), "
        f"Light {after_r['lightning']}/{after_r['lightningMax']} (+{after_r['lightningOvercap']}), Chaos {after_r['chaos']}",
        f"  After Spirit: {after_s['reserved']}/{after_s['total']} (Unreserved={after_s['unreserved']})",
    ]
    s1 = data["stages"]["stage1Attributes"]
    if s1["swapCount"]:
        lines.append(f"  [Stage 1 Gold Respec] {s1['swapCount']} swaps ({s1['goldCost']} Gold)")
    s2 = data["stages"]["stage2Runes"]
    if s2["assignmentCount"]:
        lines.append(f"  [Stage 2 Runes] {s2['assignmentCount']} socket augments (~{s2['totalCostEx']:.1f} Ex)")
    s3 = data["stages"]["stage3Catalysts"]
    if s3["actionCount"]:
        lines.append(f"  [Stage 3 Catalysts] {s3['actionCount']} jewellery upgrades (~{s3['totalCostEx']:.1f} Ex)")
    s4 = data["stages"]["stage4Spirit"]
    if s4["selected"]:
        lines.append(f"  [Stage 4 Spirit] freed {s4['achievedSpirit']} Spirit via {len(s4['selected'])} change(s)")
    s5 = data["stages"]["stage5Fallback"]
    if s5["needed"]:
        lines.append(f"  [Stage 5 Fallback] replace `{s5['targetSlot']}` for remaining hard gaps: {s5['requiredStats']}")
    for w in data.get("warnings") or ():
        if "[res-override-line]" in str(w):
            lines.append(f"  [Warning] {w}")
    return "\n".join(lines)


def _format_spirit_human(data: dict[str, Any]) -> str:
    applied_tag = f" | Applied(rev={data.get('rev')})" if data.get("applied") else ""
    lines = [
        f"PoE2 Spirit Knapsack ({data['mode']}) | target={data['targetSpirit']} | "
        f"achieved={data['achievedSpirit']} | remaining={data['remainingDeficit']}{applied_tag}"
    ]
    for item in data["selected"]:
        lines.append(f"  - {item['label']} (dDPS={item['deltaDps']:+.1f}, dEHP={item['deltaEhp']:+.1f})")
    return "\n".join(lines)


__all__ = [
    "cmd_fix_gaps",
    "cmd_spirit",
]
