# SPDX-License-Identifier: GPL-3.0-only
"""CLI build I/O commands: load, export, notes, undo, redo (L4)."""

from __future__ import annotations

import click

from .cli_common import (
    ctx_wants_json,
    daemon_call_cli,
    echo_utf8,
    emit_output,
    output_options,
    pob2_command,
    resolve_output_flags,
    resolve_target_slot,
    target_option,
)
from .formatters import format_notes_md


@pob2_command("load")
@click.argument("source", required=True)
@target_option(allow_slot_alias=True)
@output_options
@click.pass_context
def cmd_load(ctx: click.Context, source: str, target: str, as_json: bool) -> None:
    """Load a PoE2 build (code, XML file, pobb.in, poe2db.tw, poe.ninja/poe2, maxroll, WeGame) into a slot."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("load", {"source": source, "target": slot})
    info = res.get("buildInfo") or {}
    lines = [
        f"Loaded PoE2 build into slot `{slot}` ({res.get('sourceKind') or 'build'}): "
        f"{info.get('className') or '?'} / {info.get('ascendClassName') or 'None'} Lv{info.get('level') or '?'}"
    ]
    dropped_nodes = int(res.get("disconnectedNodesDropped") or 0)
    if dropped_nodes > 0:
        req_cnt = res.get("requestedNodeCount", "?")
        ld_cnt = res.get("loadedNodeCount", "?")
        warn_txt = (
            f"Warning: {dropped_nodes} passive node(s) from input XML were dropped on load "
            f"({req_cnt} requested -> {ld_cnt} allocated; disconnected from class start or invalid for treeVersion)."
        )
        echo_utf8(warn_txt, err=True)
        lines.append(f"- [tree-disconnected] {warn_txt}")
    for w in res.get("fixupWarnings") or []:
        if "[res-override-line]" in str(w):
            echo_utf8(f"Warning: {w}", err=True)
        lines.append(f"- {w}")
    for w in res.get("importWarnings") or []:
        lines.append(f"- [import] {w}")
    for d in res.get("droppedItems") or []:
        lines.append(f"- [{d.get('issue')}] {d.get('message')}")
    notes = res.get("notes") or {}
    if notes.get("hasNotes"):
        lines.append(f"- Author Notes ({notes.get('lineCount', 0)} lines) available via `pob2 notes`.")
    emit_output(ctx, res, as_json=as_json, markdown_text="\n".join(lines))


@pob2_command("export")
@target_option(allow_slot_alias=True)
@click.option("--xml", "include_xml", is_flag=True, default=False, help="Output decoded XML instead of share code.")
@output_options
@click.pass_context
def cmd_export(
    ctx: click.Context,
    target: str,
    include_xml: bool,
    as_json: bool,
) -> None:
    """Export the current PoE2 slot build as a PoB2 share code or XML."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("export", {"target": slot, "include_xml": include_xml})
    if as_json or ctx_wants_json(ctx):
        emit_output(ctx, res, as_json=True)
        return
    body = str(res.get("xml") if include_xml else res.get("code") or "")
    _compact, eff_out = resolve_output_flags(ctx)
    if eff_out is not None:
        eff_out.parent.mkdir(parents=True, exist_ok=True)
        eff_out.write_text(body + "\n", encoding="utf-8", newline="\n")
        echo_utf8(f"Wrote {eff_out}", err=True)
        return
    echo_utf8(body)


@pob2_command("notes")
@target_option(allow_slot_alias=True)
@output_options
@click.pass_context
def cmd_notes(ctx: click.Context, target: str, as_json: bool) -> None:
    """Display author build Notes with PoB colour escape codes stripped."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("notes", {"target": slot})
    emit_output(ctx, res, as_json=as_json, markdown_text=format_notes_md(res))


@pob2_command("undo")
@target_option(allow_slot_alias=True)
@output_options
@click.pass_context
def cmd_undo(ctx: click.Context, target: str, as_json: bool) -> None:
    """Undo the last committed edit on the target PoE2 build slot."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("undo", {"target": slot})
    md = f"Undone slot `{slot}` (undoDepth={res.get('undoDepth')}, redoDepth={res.get('redoDepth')})."
    emit_output(ctx, res, as_json=as_json, markdown_text=md)


@pob2_command("redo")
@target_option(allow_slot_alias=True)
@output_options
@click.pass_context
def cmd_redo(ctx: click.Context, target: str, as_json: bool) -> None:
    """Redo the previously undone edit on the target PoE2 build slot."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("redo", {"target": slot})
    md = f"Redone slot `{slot}` (undoDepth={res.get('undoDepth')}, redoDepth={res.get('redoDepth')})."
    emit_output(ctx, res, as_json=as_json, markdown_text=md)


__all__ = ["cmd_export", "cmd_load", "cmd_notes", "cmd_redo", "cmd_undo"]
