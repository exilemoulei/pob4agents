# SPDX-License-Identifier: GPL-3.0-only
"""CLI lifecycle commands: bootstrap, status, stop, serve, update (L4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from .cli_common import daemon_call_cli, emit_output, output_options, pob2_command
from .daemon import run_daemon_forever, stop_daemon
from .errors import (
    BootstrapMissingError,
    CrossGameHallucinationError,
    DaemonRequestError,
    LegacyPatchTrapError,
)
from .game_profile import GAME_ID
from .updater import OK_STATES, bootstrap_engine


@pob2_command("bootstrap")
@click.option("--from-dir", "from_dir", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None, help="Seed engine from a local PoB2 / PobTools directory (read-only copy).")
@click.option("--proxy", type=str, default=None, help="Optional HTTP/SOCKS proxy URL.")
@click.option("--mirror", type=str, default=None, help="Optional GitHub mirror prefix.")
@click.option("--force", is_flag=True, default=False, help="Force reinstall even if valid engine exists.")
@output_options
@click.pass_context
def cmd_bootstrap(
    ctx: click.Context,
    from_dir: Path | None,
    proxy: str | None,
    mirror: str | None,
    force: bool,
    as_json: bool,
) -> None:
    """Install or seed the headless PoB2 Slim LuaJIT engine."""
    try:
        res = bootstrap_engine(from_dir=from_dir, proxy=proxy, mirror=mirror, force=force)
    except (CrossGameHallucinationError, LegacyPatchTrapError, DaemonRequestError, BootstrapMissingError):
        raise
    except (ValueError, FileNotFoundError) as exc:
        raise DaemonRequestError(f"Bootstrap failed: {exc}") from exc
    if res.get("state") not in OK_STATES:
        msg = str(res.get("userHint") or res.get("networkError") or "PoB2 engine bootstrap failed.")
        raise BootstrapMissingError(msg)
    payload = {"game": GAME_ID, **dict(res)}
    md = f"PoB2 engine bootstrap: state={payload.get('state')} engineDir={payload.get('engineDir')}"
    emit_output(ctx, payload, as_json=as_json, markdown_text=md)


@pob2_command("status")
@click.option("--no-start", is_flag=True, default=False, help="Do not auto-start the daemon if stopped.")
@output_options
@click.pass_context
def cmd_status(ctx: click.Context, no_start: bool, as_json: bool) -> None:
    """Show PoB2 daemon status, capabilities, language, and loaded build slots."""
    res = daemon_call_cli("status", {}, auto_start=not no_start)
    main_slot = res.get("main") or {}
    caps = res.get("caps") or {}
    md = "\n".join(
        [
            f"## PoB2 Daemon Status (pid={res.get('pid')}, game={res.get('game')})",
            f"- Engine: {res.get('engineDir')}",
            f"- Runtime: {res.get('runtimeDir')} | lang={res.get('lang')} ({res.get('langSource')})",
            f"- Main slot: alive={main_slot.get('alive')} loaded={main_slot.get('hasLoadedBuild')} rev={main_slot.get('rev')}",
            f"- Caps: {', '.join(sorted(k for k, v in caps.items() if v is True))}",
        ]
    )
    emit_output(ctx, res, as_json=as_json, markdown_text=md)


@pob2_command("stop")
@output_options
@click.pass_context
def cmd_stop(ctx: click.Context, as_json: bool) -> None:
    """Stop the running local PoB2 daemon cleanly."""
    res: dict[str, Any] = stop_daemon()
    md = "PoB2 daemon stopped." if res.get("stopped") else "PoB2 daemon was not running."
    emit_output(ctx, res, as_json=as_json, markdown_text=md)


@pob2_command("serve")
@click.option("--pob-dir", type=Path, default=None, help="Explicit PoB2 engine directory.")
@click.option("--data-dir", type=Path, default=None, help="Explicit PoE2 localization data directory.")
@click.option("--port", type=int, default=0, help="Loopback TCP port (0 = ephemeral).")
@click.option("--skip-update-check", is_flag=True, default=False, help="Skip startup update check.")
@output_options
def cmd_serve(
    pob_dir: Path | None,
    data_dir: Path | None,
    port: int,
    skip_update_check: bool,
    as_json: bool = False,
) -> None:
    """Run the PoB2 headless HTTP daemon in the foreground."""
    _ = as_json
    run_daemon_forever(pob_dir=pob_dir, data_dir=data_dir, port=port, skip_update_check=skip_update_check)


@pob2_command("update")
@click.option("--check", "check_only", is_flag=True, default=False, help="Check remote versions without applying.")
@click.option("--force", is_flag=True, default=False, help="Force engine update even if current.")
@click.option("--proxy", type=str, default=None, help="Optional proxy URL.")
@click.option("--mirror", type=str, default=None, help="Optional GitHub mirror prefix.")
@output_options
@click.pass_context
def cmd_update(
    ctx: click.Context,
    check_only: bool,
    force: bool,
    proxy: str | None,
    mirror: str | None,
    as_json: bool,
) -> None:
    """Check for or hot-reload PoB2 / PobTools-zh updates."""
    res = daemon_call_cli("update", {"check_only": check_only, "force": force, "proxy": proxy, "mirror": mirror})
    md = f"PoB2 update: state={res.get('state')} pob={res.get('pobVersion')} pobtools={res.get('pobtoolsVersion')}"
    emit_output(ctx, res, as_json=as_json, markdown_text=md)


__all__ = ["cmd_bootstrap", "cmd_serve", "cmd_status", "cmd_stop", "cmd_update"]
