# SPDX-License-Identifier: GPL-3.0-only
"""Compact 1-screen pure-PoE2 CLI cheat-sheet (`pob2 help --brief`) auto-generated from Click commands (L4)."""

from __future__ import annotations

import sys
from typing import Any

import click

from .cli_common import (
    SLOT_ALIAS_PARAM,
    ctx_wants_json,
    emit_output,
    emit_text,
    output_options,
    pob2_command,
)
from .game_profile import CLI_NAME

_GLOBAL_NOISE_OPTS = frozenset({"--help", "-h", "--compact", "--out", "--offline"})
_BRIEF_UNCUT = frozenset({"diagnose", "what-if"})


def _param_token(param: click.Parameter) -> str | None:
    if getattr(param, "hidden", False) or param.name == SLOT_ALIAS_PARAM:
        return None
    if isinstance(param, click.Argument):
        label = (param.metavar or param.name or "ARG").strip("[]").upper()
        suffix = "..." if param.nargs == -1 else ""
        return f"<{label}{suffix}>" if param.required else f"[{label}{suffix}]"
    if isinstance(param, click.Option):
        long_opts = [o for o in param.opts if o.startswith("--")]
        opt = long_opts[0] if long_opts else (param.opts[0] if param.opts else "")
        if not opt or opt in _GLOBAL_NOISE_OPTS:
            return None
        if param.secondary_opts:
            sec_long = [o for o in param.secondary_opts if o.startswith("--")]
            if sec_long:
                return f"{opt}/{sec_long[0]}"
        if param.is_flag:
            return opt
        if param.metavar:
            return f"{opt} {param.metavar}"
        return opt
    return None


def _command_signature(cmd: click.Command, *, max_opts: int = 12) -> str:
    tokens: list[str] = []
    for p in cmd.params:
        tok = _param_token(p)
        if tok and tok not in tokens:
            tokens.append(tok)
    if len(tokens) > max_opts:
        return " ".join(tokens[:max_opts]) + f" (+{len(tokens) - max_opts})"
    return " ".join(tokens)


def build_brief_entries(root: click.Group, root_ctx: click.Context) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name in root.list_commands(root_ctx):
        cmd = root.get_command(root_ctx, name)
        if cmd is None or cmd.hidden:
            continue
        short = (cmd.get_short_help_str(limit=96) or "").strip()
        if isinstance(cmd, click.Group):
            sub_ctx = click.Context(cmd, info_name=name, parent=root_ctx)
            subs: list[dict[str, str]] = []
            for sub_name in cmd.list_commands(sub_ctx):
                sub_cmd = cmd.get_command(sub_ctx, sub_name)
                if sub_cmd is None or sub_cmd.hidden:
                    continue
                subs.append(
                    {
                        "name": sub_name,
                        "command": f"{name} {sub_name}",
                        "flags": _command_signature(sub_cmd, max_opts=10),
                    }
                )
            entries.append({"name": name, "kind": "group", "summary": short, "subcommands": subs})
        else:
            entries.append(
                {
                    "name": name,
                    "kind": "command",
                    "summary": short,
                    "flags": _command_signature(cmd, max_opts=sys.maxsize if name in _BRIEF_UNCUT else 10),
                }
            )
    return entries


def format_brief_help(root: click.Group, root_ctx: click.Context) -> str:
    entries = build_brief_entries(root, root_ctx)
    lines = [
        f"{CLI_NAME} — Headless Path of Exile 2 (0.5.5+) CLI (run `{CLI_NAME} <cmd> --help` for full details)",
        "Global: [--offline] [--json] [--compact] [--out PATH (writes file instead of stdout)] | Daemon build slot: [--target SLUG] (default POB2_SLOT or 'main')",
        "Equipment slot: [--slot SLOT] (Weapon 1..2, Weapon 1..2 Swap, Helmet, Body Armour, Gloves, Boots, Amulet, Ring 1..3, Belt, Flask 1..2, Charm 1..3)",
        "Unique ranking: `pob2 item db --slot <SLOT>` ranks PoE2 uniques against the loaded build in `--target` (JSON key: `results`)",
        "Instill ranking: `pob2 instill rank` Pareto-ranks all 875 ordered amulet instillations (`instill lookup|bridge|rank`)",
        "Read projection: core read commands accept `--section <s1,s2>` and `--fields <p1,p2>` to slice output payloads",
        "Crafting target mods: use `--want <mod>` (repeatable) on `pob2 craft odds|plan|next` (`--target` is strictly for daemon build slots)",
    ]
    rows: list[tuple[str, str]] = []
    for ent in entries:
        if ent["kind"] == "group":
            for sub in ent["subcommands"]:
                rows.append((sub["command"], sub["flags"]))
        else:
            rows.append((ent["name"], ent["flags"] or f"# {ent['summary']}"))

    pad = max((len(label) for label, _ in rows), default=14)
    for label, flags in rows:
        lines.append(f"  {label.ljust(pad)}  {flags}".rstrip())
    return "\n".join(lines)


@pob2_command("help")
@click.argument("topic", nargs=-1)
@click.option("-b", "--brief", is_flag=True, default=False, help="Compact 1-screen pure-PoE2 cheat-sheet of all commands and flags.")
@output_options
@click.pass_context
def cmd_help(
    ctx: click.Context,
    topic: tuple[str, ...],
    brief: bool,
    as_json: bool,
) -> None:
    """Show compact 1-screen PoE2 CLI cheat-sheet (`--brief`) or help for a subcommand."""
    eff_json = bool(as_json or ctx_wants_json(ctx))
    root_ctx = ctx.find_root()
    root_cmd = root_ctx.command
    if not isinstance(root_cmd, click.Group):
        emit_text(ctx, root_ctx.get_help())
        return

    if topic:
        cur_cmd: click.Command = root_cmd
        cur_ctx = root_ctx
        for part in topic:
            if not isinstance(cur_cmd, click.Group):
                raise click.UsageError(f"Command `{cur_ctx.command_path}` has no subcommands.")
            nxt = cur_cmd.get_command(cur_ctx, part)
            if nxt is None:
                raise click.UsageError(f"No such command {' '.join(topic)!r}.")
            cur_ctx = click.Context(nxt, info_name=part, parent=cur_ctx)
            cur_cmd = nxt
        cmd_label = " ".join((CLI_NAME, *topic))
        if eff_json:
            emit_output(
                ctx,
                {
                    "command": cmd_label,
                    "flags": _command_signature(cur_cmd, max_opts=50),
                    "help": cur_ctx.get_help(),
                },
                as_json=True,
            )
            return
        if brief:
            emit_text(ctx, f"{cmd_label} {_command_signature(cur_cmd, max_opts=50)}".rstrip())
            return
        emit_text(ctx, cur_ctx.get_help())
        return

    if eff_json:
        emit_output(ctx, {"commands": build_brief_entries(root_cmd, root_ctx)}, as_json=True)
        return
    if brief:
        emit_text(ctx, format_brief_help(root_cmd, root_ctx))
        return
    emit_text(
        ctx,
        root_ctx.get_help() + f"\n\nTip: run `{CLI_NAME} help --brief` for a compact 1-screen PoE2 option cheat-sheet.",
    )


__all__ = ["build_brief_entries", "cmd_help", "format_brief_help"]
