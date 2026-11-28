# SPDX-License-Identifier: GPL-3.0-only
"""CLI commands for `pob2 item <inspect|db>` and shared item-inspect/unique-ranking helpers (L4)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from .cli_common import (
    ctx_wants_json,
    daemon_call_cli,
    echo_utf8,
    emit_output,
    output_options,
    pob2_group,
    projection_options,
    resolve_target_slot,
    target_option,
)
from .craft.item_inspector import inspect_item_text
from .craft.models import RemovedLeverError, UnknownBaseError, UnknownTargetError
from .errors import (
    EXIT_REQUEST_ERROR,
    CrossGameHallucinationError,
    DaemonRequestError,
    LegacyPatchTrapError,
)
from .guardrails import check_input_item_raw, normalize_poe2_equipment_slot


def read_item_source(item_text: str | None, file_path: str | None) -> str:
    if file_path == "-":
        return sys.stdin.read()
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    if item_text:
        return item_text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise click.UsageError("请提供 --slot、--text、--file 或通过 stdin 粘贴 Ctrl+C 装备文本。")


def render_inspect_md(rep: Any, *, slot_label: str | None = None) -> str:
    slot_prefix = f"[{slot_label}] " if slot_label else ""
    lines = [
        f"### 装备词缀体检 [PoE2 {rep.patch}] — {slot_prefix}{rep.item_name} ({rep.base_type}, ilvl {rep.ilvl})",
        f"- 稀有度: `{rep.rarity.value}` | 类别槽位: `{rep.base_slug}` | "
        f"前缀 `{len(rep.prefixes)}/{rep.max_prefixes}` (空 {rep.open_prefixes}) | "
        f"后缀 `{len(rep.suffixes)}/{rep.max_suffixes}` (空 {rep.open_suffixes})",
        f"- `0.5.0+ Crafted` 槽位: `{rep.crafted_mod_count}/1`"
        + (" [破裂锁死]" if rep.fractured_crafted_locked else "")
        + f" | `Desecrated` 亵渎槽位: `{rep.desecrated_mod_count}/1` | 合法性: "
        + ("PASS" if rep.is_legal_0_5_5 else "FAIL"),
    ]
    rows = []
    for side_label, mlist in (("前缀", rep.prefixes), ("后缀", rep.suffixes)):
        for m in mlist:
            tags = []
            if m.fractured:
                tags.append("破裂")
            if m.is_crafted:
                tags.append("Crafted")
            if m.is_desecrated:
                tags.append("亵渎")
            rows.append([
                side_label,
                f"T{m.display_tier}" if m.resolved else "?",
                m.required_level if m.resolved else "?",
                m.source_pool,
                ",".join(tags) or "-",
                m.weight_confidence.value,
                m.raw_text.replace("\n", " / "),
            ])
    if rows:
        from tabulate import tabulate

        lines.append("")
        lines.append(
            tabulate(
                rows,
                headers=["位", "阶", "req_lvl", "词缀池", "标记", "权重置信度", "词缀文本"],
                tablefmt="github",
            )
        )
    if rep.whittling_preview:
        lines.append(f"\n- 削切预兆（Omen of Whittling）预览: {rep.whittling_preview.summary}")
    for iss in rep.legality_issues:
        lines.append(f"- [违例] {iss}")
    for w in rep.warnings:
        lines.append(f"- [提示] {w}")
    return "\n".join(lines)


def raise_removed_as_trap(exc: RemovedLeverError) -> None:
    raise LegacyPatchTrapError(
        trigger=exc.lever_name,
        patch_version="0.4/0.5",
        error=exc.reason,
        remediation=exc.alternatives,
        equivalent_command="pob2 craft plan --base <base> --want <mod>",
        rule_id="CRAFT_REMOVED_LEVER",
    ) from exc


def handle_craft_or_inspect_error(ctx: click.Context, exc: Exception, *, as_json: bool) -> None:
    """Handle domain/input craft & inspect errors (`rc=1`) while letting unexpected faults hit `rc=5`."""
    if isinstance(exc, (CrossGameHallucinationError, LegacyPatchTrapError, DaemonRequestError)):
        raise exc
    if isinstance(exc, RemovedLeverError):
        raise_removed_as_trap(exc)
    if isinstance(exc, (UnknownBaseError, UnknownTargetError)):
        raise exc
    if isinstance(exc, ValueError):
        if as_json or ctx_wants_json(ctx):
            emit_output(
                ctx,
                {
                    "ok": False,
                    "game": "poe2",
                    "errorKind": "invalid_input",
                    "code": "POE2-INVALID-INPUT",
                    "error": str(exc),
                },
                as_json=True,
                ok=False,
                ignore_ctx_out=True,
                skip_projection=True,
            )
        else:
            echo_utf8(f"Error: {exc}", err=True)
        ctx.exit(EXIT_REQUEST_ERROR)
    raise exc


def _run_base_only_inspect(
    ctx: click.Context,
    *,
    base_name: str,
    ilvl: int,
    as_json: bool,
) -> None:
    """Inspect a PoE2 crafting base's slot structure and eligible mod pool without requiring item text."""
    from .craft import ModPoolEngine
    from .craft.models import CraftingState, ItemRarity
    from .craft.pool_outcomes import compute_next_step_outcomes, render_outcomes_md

    eng = ModPoolEngine()
    try:
        base = eng.resolve_base(base_name)
        state = CraftingState(base=base, ilvl=ilvl, rarity=ItemRarity.NORMAL)
        outcomes_payload = compute_next_step_outcomes(eng, state)
    except Exception as exc:
        handle_craft_or_inspect_error(ctx, exc, as_json=as_json)
        return

    payload: dict[str, Any] = {
        "game": "poe2",
        "patch": eng.patch,
        "mode": "base_pool",
        "rarity": ItemRarity.NORMAL.value,
        "itemName": base.name,
        "baseType": base.name,
        "baseSlug": base.poe2db_slug,
        "ilvl": ilvl,
        "slots": {
            "prefixes": 0,
            "maxPrefixes": base.max_prefixes,
            "openPrefixes": base.max_prefixes,
            "suffixes": 0,
            "maxSuffixes": base.max_suffixes,
            "openSuffixes": base.max_suffixes,
        },
        "craftedSlot": {"used": 0, "limit": 1, "fracturedLocked": False, "canAddCrafted": True},
        "desecratedSlot": {"used": 0, "limit": 1, "canDesecrate": True},
        "isLegal055": True,
        "totalPoolWeight": outcomes_payload["totalPoolWeight"],
        "familyCount": outcomes_payload["familyCount"],
        "weightConfidence": outcomes_payload["weightConfidence"],
        "outcomes": outcomes_payload["outcomes"],
    }
    md_header = (
        f"### 底材词缀池体检 [PoE2 {eng.patch}] — {base.name} (`{base.poe2db_slug}`, ilvl {ilvl})\n"
        f"- 稀有槽位上限: 前缀 `0/{base.max_prefixes}` | 后缀 `0/{base.max_suffixes}` | "
        f"`Crafted` `0/1` | `Desecrated` `0/1`\n\n"
    )
    emit_output(
        ctx,
        payload,
        as_json=as_json,
        markdown_text=md_header + render_outcomes_md(outcomes_payload),
    )


def run_inspect_command(
    ctx: click.Context,
    *,
    item_text: str | None,
    file_path: str | None,
    base_override: str | None,
    ilvl_override: int | None,
    as_json: bool,
    slot_name: str | None = None,
    target_slot: str | None = None,
) -> None:
    """Unified execution pipeline for `craft inspect` and `item inspect` (`--slot` / `--text` / `--file` / `--base`)."""
    if item_text is not None and file_path is not None:
        raise click.UsageError("Use either `--text` or `--file`, not both.")
    if slot_name and (item_text is not None or file_path is not None):
        raise click.UsageError(
            "Cannot combine `--slot` with `--text`/`--file`; pass `--slot` for equipped build items or `--text`/`--file` for pasted text."
        )

    canon_slot: str | None = None
    fallback_base: str | None = None
    if slot_name:
        canon_slot = normalize_poe2_equipment_slot(slot_name, command="item inspect")
        resolved_target = resolve_target_slot(target_slot)
        loaded = daemon_call_cli(
            "items",
            {"target": resolved_target, "slot": canon_slot, "include_raw": True},
        )
        items_list = loaded.get("items") or []
        first_meta = items_list[0] if items_list and isinstance(items_list[0], dict) else {}
        raw_val = loaded.get("raw") or first_meta.get("raw")
        if not raw_val or not str(raw_val).strip():
            raise DaemonRequestError(
                f"Slot {canon_slot!r} is empty in build slot {resolved_target!r}."
            )
        raw = str(raw_val)
        fallback_base = str(first_meta.get("baseName") or "").strip() or None
        check_input_item_raw(raw, expected_game="poe2", emit_stderr=not (as_json or ctx_wants_json(ctx)))
    elif item_text is None and file_path is None and base_override:
        _run_base_only_inspect(
            ctx,
            base_name=base_override,
            ilvl=ilvl_override if ilvl_override is not None else 82,
            as_json=as_json,
        )
        return
    else:
        raw = read_item_source(item_text, file_path)

    try:
        try:
            rep = inspect_item_text(raw, base_override=base_override, ilvl_override=ilvl_override)
        except UnknownBaseError:
            if base_override is None and fallback_base:
                rep = inspect_item_text(raw, base_override=fallback_base, ilvl_override=ilvl_override)
            else:
                raise
    except Exception as exc:
        handle_craft_or_inspect_error(ctx, exc, as_json=as_json)
        return

    payload = rep.to_dict()
    if canon_slot is not None:
        payload["slot"] = canon_slot
        payload["targetSlot"] = resolve_target_slot(target_slot)
    emit_output(
        ctx,
        payload,
        as_json=as_json,
        markdown_text=render_inspect_md(rep, slot_label=canon_slot),
    )


def run_unique_slot_ranking(
    ctx: click.Context,
    *,
    slot: str,
    sort_stat: str | None,
    limit: int,
    no_sanctified: bool,
    target: str | None,
    as_json: bool,
    command_label: str,
) -> None:
    """Shared implementation for `pob2 item db` and `pob2 uniques` with 100% identical schema."""
    from .formatters import format_uniques_md

    canon_slot = normalize_poe2_equipment_slot(slot, command=command_label)
    raw_res = daemon_call_cli(
        "uniques",
        {
            "target": resolve_target_slot(target),
            "slot": canon_slot,
            "stat": sort_stat,
            "top": limit,
            "eval_sanctified": not no_sanctified,
        },
    )
    res = {k: v for k, v in raw_res.items() if k not in ("loopGuardWarning", "cached")}
    emit_output(ctx, res, as_json=as_json, markdown_text=format_uniques_md(res))


@pob2_group("item")
def item_group() -> None:
    """Inspect PoE2 items (`--slot`/`--text`/`--file`) and rank uniques against loaded `--target` build (`item db`)."""


@item_group.command("inspect")
@click.argument("slot_arg", metavar="[SLOT]", required=False, default=None)
@click.option("--slot", "slot_opt", default=None, help="Equipped PoE2 equipment slot to inspect from the loaded build.")
@click.option("--text", "item_text", default=None, help="Directly pass Ctrl+C PoE2 item text.")
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True, dir_okay=False, allow_dash=True),
    default=None,
    help="Read PoE2 item text from a file (or `-` for stdin).",
)
@click.option("--base", "base_override", default=None, help="Optional base item name or class slug override.")
@click.option("--ilvl", "ilvl_override", type=int, default=None, help="Optional item level override.")
@target_option(allow_slot_alias=False)
@projection_options
@output_options
@click.pass_context
def cmd_item_inspect(
    ctx: click.Context,
    slot_arg: str | None,
    slot_opt: str | None,
    item_text: str | None,
    file_path: str | None,
    base_override: str | None,
    ilvl_override: int | None,
    target: str,
    as_json: bool,
) -> None:
    """Inspect PoE2 item modifiers, open affix/Crafted/Desecrated slots, and 0.5.5 legality (`--slot`, `--text`, or `--file`)."""
    if slot_arg and slot_opt and slot_arg.strip().lower() != slot_opt.strip().lower():
        raise click.UsageError("Specify equipment slot either positionally or via `--slot`, not both.")
    run_inspect_command(
        ctx,
        item_text=item_text,
        file_path=file_path,
        base_override=base_override,
        ilvl_override=ilvl_override,
        as_json=as_json,
        slot_name=slot_opt or slot_arg,
        target_slot=target,
    )


@item_group.command("db")
@click.option("--slot", type=str, required=True, help="Target PoE2 equipment slot (including Flask 1..2 and Charm 1..3).")
@click.option("--sort", "--stat", "sort_stat", type=str, default=None, help="Ranking stat/metric (default: primary DPS; e.g. CombinedDPS, TotalEHP).")
@click.option("--limit", "--top", "limit", type=click.IntRange(min=1), default=15, show_default=True, help="Maximum top uniques to return.")
@click.option("--no-sanctified", "no_sanctified", is_flag=True, default=False, help="Skip 0.78x-1.22x Sanctified roll-range evaluation.")
@target_option(allow_slot_alias=False)
@projection_options
@output_options
@click.pass_context
def cmd_item_db(
    ctx: click.Context,
    slot: str,
    sort_stat: str | None,
    limit: int,
    no_sanctified: bool,
    target: str,
    as_json: bool,
) -> None:
    """Rank PoE2 unique items for `--slot` against the loaded build in `--target` (JSON output key: `results`)."""
    run_unique_slot_ranking(
        ctx,
        slot=slot,
        sort_stat=sort_stat,
        limit=limit,
        no_sanctified=no_sanctified,
        target=target,
        as_json=as_json,
        command_label="item db",
    )


__all__ = [
    "cmd_item_db",
    "cmd_item_inspect",
    "handle_craft_or_inspect_error",
    "item_group",
    "raise_removed_as_trap",
    "read_item_source",
    "render_inspect_md",
    "run_inspect_command",
    "run_unique_slot_ranking",
]
