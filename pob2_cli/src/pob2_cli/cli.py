"""Click CLI entry point for ``pob2`` (lazy-loaded with optional extension mounting)."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from typing import Any

if len(sys.argv) > 1 and sys.argv[1] == "_lua-host":
    from .updater.win_lua_host import main as _lua_host_main

    raise SystemExit(_lua_host_main(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == "_daemon":
    from .daemon.client import main as _daemon_main

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    _daemon_main()
    raise SystemExit(0)

import click

from . import __version__
from .cli_common import Pob2Group, ctx_wants_json, echo_utf8, emit_output, output_options
from .game_profile import (
    CLI_NAME,
    ENV_OFFLINE,
    GAME_ID,
    GAME_NAME,
    LICENSE_SPDX,
    PACKAGE_NAME,
    POE2BRAIN_ATTRIBUTION,
    TOKEN_HEADER,
    resolve_runtime_dir,
    stamp_game,
)
from .locale_cfg import SUPPORTED_LANGS, get_active_lang, set_active_lang

# Core engine/lifecycle subcommands registered lazily (must exist when listed here).
_LAZY_SUBCOMMANDS: dict[str, tuple[str, str, str]] = {
    "help": (".cli_help", "cmd_help", "Show compact 1-screen PoE2 CLI cheat-sheet (--brief) or command help."),
    "bootstrap": (".cli_lifecycle", "cmd_bootstrap", "Install or seed the headless PoB2 Slim LuaJIT engine."),
    "status": (".cli_lifecycle", "cmd_status", "Show PoB2 daemon status, capabilities, and loaded slots."),
    "stop": (".cli_lifecycle", "cmd_stop", "Stop the running local PoB2 daemon cleanly."),
    "serve": (".cli_lifecycle", "cmd_serve", "Run the PoB2 headless HTTP daemon in the foreground."),
    "update": (".cli_lifecycle", "cmd_update", "Check for or hot-reload PoB2 / PobTools-zh updates."),
    "load": (".cli_build_io", "cmd_load", "Load a PoE2 build (code, XML, pobb.in, poe2db, poe.ninja, maxroll, WeGame)."),
    "export": (".cli_build_io", "cmd_export", "Export the current PoE2 build as a share code or XML."),
    "notes": (".cli_build_io", "cmd_notes", "Show author build Notes with PoB colour codes stripped."),
    "undo": (".cli_build_io", "cmd_undo", "Undo the last committed edit on the target build slot."),
    "redo": (".cli_build_io", "cmd_redo", "Redo the previously undone edit on the target build slot."),
    "summary": (".cli_inspect", "cmd_summary", "Show PoE2 build summary with version & patch stamp."),
    "audit": (".cli_inspect", "cmd_audit", "Run S4/T3.4 PoE2 deep audit, 4-dim dehydration, and [PoB2未模拟] red cards."),
    "diagnose": (".cli_inspect", "cmd_diagnose", "One-shot PoE2 build diagnosis (summary + deep audit + notes)."),
    "compare": (".cli_inspect", "cmd_compare", "5-dimension Compare Power Report against a reference build."),
    "items": (".cli_inspect", "cmd_items", "Inspect equipped PoE2 items and slots."),
    "skills": (".cli_inspect", "cmd_skills", "Inspect PoE2 skill groups, gems, and weapon-set bindings."),
    "tree": (".cli_inspect", "cmd_tree", "Inspect allocated PoE2 passive tree nodes and attribute overrides."),
    "config": (".cli_inspect", "cmd_config", "Inspect PoB2 Configuration toggles and custom modifiers."),
    "what-if": (".cli_mutate", "what_if_group", "Run CoW what-if mutations by domain (equip|tree|skill|config|apply)."),
    "diff": (".cli_mutate", "cmd_diff", "Diff the active build against another PoE2 build source."),
}

# Optional extension subcommands discovered dynamically when their module is present
# in the package tree (enables conflict-free parallel feature branch merges).
_OPTIONAL_SUBCOMMANDS: dict[str, tuple[str, str, str]] = {
    "craft": (".cli_craft", "craft_group", "PoE2 crafting odds, MDP solver, and step-by-step plans."),
    "item": (".cli_item", "item_group", "Inspect PoE2 items (--slot/--text/--file) and rank uniques against loaded build (item db)."),
    "ground": (".cli_ground", "cmd_ground", "Fast (<15ms, daemon-free) PoE2 entity & mechanic grounding."),
    "import": (".cli_import", "cmd_import", "Import PoE2 builds (PoB2 code, share URL, CN WeGame link)."),
    "rune": (".cli_rune", "rune_group", "Rank PoE2 runes, soul cores, and idols with socket parity."),
    "instill": (".cli_rune", "instill_group", "Lookup, bridge, and Pareto-rank 875 PoE2 amulet instillations."),
    "fix-gaps": (".cli_rebalance", "cmd_fix_gaps", "5-stage zero-buy attribute & resistance cap/overcap gap solver."),
    "spirit": (".cli_rebalance", "cmd_spirit", "PoE2 discrete Spirit reservation & support-tier knapsack solver."),
}

_HIDDEN_LAZY_SUBCOMMANDS: dict[str, tuple[str, str]] = {}


def _module_exists(mod_path: str) -> bool:
    full_name = importlib.util.resolve_name(mod_path, __package__)
    try:
        return importlib.util.find_spec(full_name) is not None
    except (ImportError, ValueError):
        return False


class ExtensibleLazyGroup(Pob2Group):
    """Click Group with lazy core subcommands, optional extension auto-mount, and 0/1/5 exit codes."""

    def __init__(
        self,
        *args: Any,
        lazy_subcommands: dict[str, tuple[str, str, str]] | None = None,
        optional_subcommands: dict[str, tuple[str, str, str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._lazy_subcommands: dict[str, tuple[str, str, str]] = dict(lazy_subcommands or {})
        self._optional_subcommands: dict[str, tuple[str, str, str]] = dict(optional_subcommands or {})

    def register_lazy(self, name: str, module_path: str, attr_name: str, help_text: str) -> None:
        """Dynamically mount a lazy subcommand onto the CLI root group."""
        self._lazy_subcommands[name] = (module_path, attr_name, help_text)

    def _active_optional(self) -> dict[str, tuple[str, str, str]]:
        return {
            name: spec
            for name, spec in self._optional_subcommands.items()
            if name not in self.commands and name not in self._lazy_subcommands and _module_exists(spec[0])
        }

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = set(super().list_commands(ctx))
        names.update(self._lazy_subcommands.keys())
        names.update(self._active_optional().keys())
        return sorted(names)

    def _load_spec(self, cmd_name: str, mod_path: str, attr_name: str) -> click.Command | None:
        if not _module_exists(mod_path):
            return None
        mod = importlib.import_module(mod_path, __package__)
        loaded = getattr(mod, attr_name, None)
        if not isinstance(loaded, click.Command):
            return None
        self.commands[cmd_name] = loaded
        return loaded

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        spec = self._lazy_subcommands.get(cmd_name) or self._optional_subcommands.get(cmd_name)
        if spec is not None:
            return self._load_spec(cmd_name, spec[0], spec[1])
        hidden = _HIDDEN_LAZY_SUBCOMMANDS.get(cmd_name)
        if hidden is not None:
            return self._load_spec(cmd_name, hidden[0], hidden[1])
        return None

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        opt = self._active_optional()
        commands: list[tuple[str, str]] = []
        for subcommand in self.list_commands(ctx):
            if subcommand in self._lazy_subcommands and subcommand not in self.commands:
                commands.append((subcommand, self._lazy_subcommands[subcommand][2]))
                continue
            if subcommand in opt and subcommand not in self.commands:
                commands.append((subcommand, opt[subcommand][2]))
                continue
            cmd = super().get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            commands.append((subcommand, cmd.get_short_help_str(limit=formatter.width)))

        if commands:
            limit = formatter.width - 6 - max(len(c[0]) for c in commands)
            rows = []
            for subcommand, help_text in commands:
                if len(help_text) > limit > 3:
                    help_text = help_text[: limit - 3] + "..."
                rows.append((subcommand, help_text))
            with formatter.section("Commands"):
                formatter.write_dl(rows)


@click.group(
    cls=ExtensibleLazyGroup,
    lazy_subcommands=_LAZY_SUBCOMMANDS,
    optional_subcommands=_OPTIONAL_SUBCOMMANDS,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "-V", "--version", prog_name=CLI_NAME)
@click.option("--offline", is_flag=True, help=f"Skip network update checks (sets {ENV_OFFLINE}=1).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.option("--compact", is_flag=True, default=False, help="Emit compact single-line JSON for --json.")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write output to PATH instead of stdout.",
)
@click.pass_context
def main(ctx: click.Context, offline: bool, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Headless Path of Building (PoE2) CLI for build analysis, what-if testing, and crafting."""
    if offline:
        os.environ[ENV_OFFLINE] = "1"
    ctx.ensure_object(dict)
    ctx.obj["as_json"] = as_json
    ctx.obj["compact"] = compact
    ctx.obj["out"] = out_path


@main.command("about")
@output_options
@click.pass_context
def cmd_about(ctx: click.Context, as_json: bool) -> None:
    """Show version, license, runtime isolation profile, and GPLv3 attribution notices."""
    lang, lang_src = get_active_lang()
    info = stamp_game(
        {
            "package": PACKAGE_NAME,
            "cli": CLI_NAME,
            "version": __version__,
            "gameName": GAME_NAME,
            "license": LICENSE_SPDX,
            "tokenHeader": TOKEN_HEADER,
            "runtimeDir": str(resolve_runtime_dir()),
            "lang": lang,
            "langSource": lang_src,
            "attribution": [
                POE2BRAIN_ATTRIBUTION,
                "PathOfBuildingCommunity/PathOfBuilding-PoE2 (MIT)",
                "ackness/pobr (MIT)",
                "Hsiung-Shao/PobTools-zh",
                "exilemoulei/pob4agents contributors",
            ],
        }
    )
    lines = [
        f"{PACKAGE_NAME} ({CLI_NAME}) v{__version__} — {GAME_NAME} [{GAME_ID}]",
        f"License: {LICENSE_SPDX}",
        f"Runtime: {info['runtimeDir']} (lang={lang}, source={lang_src}, header={TOKEN_HEADER})",
        f"Attribution: {POE2BRAIN_ATTRIBUTION}",
        "Upstream & references: PathOfBuilding-PoE2 (MIT), ackness/pobr (MIT), PobTools-zh, pob4agents contributors",
    ]
    emit_output(ctx, info, as_json=as_json, markdown_text="\n".join(lines))


@main.command("lang")
@click.argument("value", required=False, default=None)
@output_options
@click.pass_context
def cmd_lang(ctx: click.Context, value: str | None, as_json: bool) -> None:
    """Get or set display language (`zh-CN`, `zh-TW`, `en`)."""
    if value is None:
        lang, source = get_active_lang()
        payload = stamp_game({"lang": lang, "source": source, "supported": list(SUPPORTED_LANGS)})
    else:
        try:
            saved = set_active_lang(value)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        payload = stamp_game({"lang": saved["lang"], "source": "config", "configPath": saved["configPath"]})

    emit_output(ctx, payload, as_json=as_json, markdown_text=f"lang={payload['lang']} (source={payload['source']})")


def register_lazy_subcommand(name: str, module_path: str, attr_name: str, help_text: str) -> None:
    """Public hook for optional extension modules to mount a lazy subcommand on ``main``."""
    main.register_lazy(name, module_path, attr_name, help_text)


def register_command(command: click.Command, name: str | None = None) -> None:
    """Public hook to attach an already-constructed Click command/group to ``main``."""
    main.add_command(command, name=name)


__all__ = [
    "_HIDDEN_LAZY_SUBCOMMANDS",
    "_LAZY_SUBCOMMANDS",
    "_OPTIONAL_SUBCOMMANDS",
    "ExtensibleLazyGroup",
    "cmd_about",
    "cmd_lang",
    "ctx_wants_json",
    "echo_utf8",
    "main",
    "register_command",
    "register_lazy_subcommand",
]

if __name__ == "__main__":
    main()
