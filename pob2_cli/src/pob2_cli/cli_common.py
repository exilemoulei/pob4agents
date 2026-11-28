# SPDX-License-Identifier: GPL-3.0-only
"""Shared CLI decorators, unified JSON envelope, `--section`/`--fields` projection, and daemon RPC wrapper (L4)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import click
from click.core import ParameterSource

from .cli_errors import (
    _RAW_ARGV_VAR,
    Pob2Command,
    Pob2Group,
    pob2_command,
    pob2_group,
)
from .compat import ensure_utf8
from .errors import DaemonRequestError
from .game_profile import GAME_ID, default_slot

_SLOT_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")
_SLOT_ALIAS_META_KEY = "pob2_build_slot_alias"
SLOT_ALIAS_PARAM = "pob2_build_slot_alias"


def _option_tokens_before_ddash(raw_args: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for tok in raw_args:
        if tok == "--":
            break
        out.append(tok)
    return tuple(out)


def echo_utf8(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def ctx_wants_json(ctx: click.Context | None = None) -> bool:
    """Return True if `--json` was passed on root group, leaf subcommand, or raw CLI args."""
    cur = ctx
    while cur is not None:
        if cur.params.get("as_json") or cur.meta.get("pob2_as_json"):
            return True
        if isinstance(cur.obj, dict) and cur.obj.get("as_json"):
            return True
        raw_args = cur.meta.get("pob2_raw_args")
        if isinstance(raw_args, (list, tuple)) and "--json" in _option_tokens_before_ddash(raw_args):
            return True
        cur = cur.parent
    active_raw = _RAW_ARGV_VAR.get()
    return bool(active_raw and "--json" in _option_tokens_before_ddash(active_raw))


def _extract_raw_opt_value(raw_args: list[str] | tuple[str, ...], flag: str) -> str | None:
    opts = _option_tokens_before_ddash(raw_args)
    for idx, tok in enumerate(opts):
        if tok == flag and idx + 1 < len(opts):
            return opts[idx + 1]
        if tok.startswith(f"{flag}="):
            return tok.split("=", 1)[1]
    return None


def resolve_output_flags(
    ctx: click.Context | None,
    *,
    compact: bool = False,
    out_path: str | Path | None = None,
    ignore_ctx_out: bool = False,
) -> tuple[bool, Path | None]:
    """Resolve effective `(compact, out_path)` across leaf options, eager raw args, and root `ctx.obj`."""
    eff_compact = bool(compact)
    eff_out: str | Path | None = out_path
    cur = ctx
    while cur is not None:
        if cur.params.get("compact") or cur.meta.get("pob2_compact"):
            eff_compact = True
        if not ignore_ctx_out and eff_out is None:
            cand = cur.params.get("out_path") or cur.meta.get("pob2_out")
            if cand is not None:
                eff_out = cand
        if isinstance(cur.obj, dict):
            if cur.obj.get("compact"):
                eff_compact = True
            if not ignore_ctx_out and eff_out is None and cur.obj.get("out") is not None:
                eff_out = cur.obj.get("out")
        raw_args = cur.meta.get("pob2_raw_args")
        if isinstance(raw_args, (list, tuple)):
            if "--compact" in _option_tokens_before_ddash(raw_args):
                eff_compact = True
            if not ignore_ctx_out and eff_out is None:
                raw_out = _extract_raw_opt_value(raw_args, "--out")
                if raw_out:
                    eff_out = raw_out
        cur = cur.parent
    active_raw = _RAW_ARGV_VAR.get()
    if active_raw:
        if "--compact" in _option_tokens_before_ddash(active_raw):
            eff_compact = True
        if not ignore_ctx_out and eff_out is None:
            raw_out = _extract_raw_opt_value(active_raw, "--out")
            if raw_out:
                eff_out = raw_out
    return eff_compact, (Path(eff_out) if eff_out is not None else None)


def resolve_projection_flags(ctx: click.Context | None) -> tuple[str | None, str | None]:
    """Resolve effective `(section, fields)` projection strings from Click context."""
    sec: str | None = None
    flds: str | None = None
    cur = ctx
    while cur is not None:
        if sec is None:
            sec = cur.meta.get("pob2_section") or cur.params.get("section")
        if flds is None:
            flds = cur.meta.get("pob2_fields") or cur.params.get("fields")
        cur = cur.parent
    return sec, flds


def normalize_json_envelope(payload: dict[str, Any], *, ok: bool | None = None) -> dict[str, Any]:
    """Ensure every CLI JSON payload carries top-level `ok` (bool) and `game: 'poe2'`."""
    existing_ok = payload.get("ok")
    resolved_ok = existing_ok if isinstance(existing_ok, bool) else (True if ok is None else ok)
    out: dict[str, Any] = {
        "ok": resolved_ok,
        "game": str(payload.get("game") or GAME_ID),
    }
    for key, val in payload.items():
        if key not in ("ok", "game"):
            out[key] = val
    return out


def emit_output(
    ctx: click.Context | None,
    payload: dict[str, Any],
    *,
    as_json: bool = False,
    compact: bool = False,
    markdown_text: str | None = None,
    out_path: str | Path | None = None,
    ok: bool | None = None,
    ignore_ctx_out: bool = False,
    skip_projection: bool = False,
) -> None:
    """Unified CLI output sink for both JSON envelopes and human/Markdown text."""
    eff_json = bool(as_json or ctx_wants_json(ctx))
    eff_compact, eff_out = resolve_output_flags(
        ctx, compact=compact, out_path=out_path, ignore_ctx_out=ignore_ctx_out
    )
    eff_payload = payload
    eff_md = markdown_text
    if not skip_projection:
        sec_raw, fields_raw = resolve_projection_flags(ctx)
        if sec_raw or fields_raw:
            from .formatters.projection import project_payload, render_projected_markdown

            eff_payload = project_payload(
                normalize_json_envelope(payload, ok=ok),
                sections_raw=sec_raw,
                fields_raw=fields_raw,
            )
            if not eff_json:
                eff_md = render_projected_markdown(
                    eff_payload,
                    sections_raw=sec_raw,
                    fields_raw=fields_raw,
                )

    if eff_json or eff_md is None:
        normalized = normalize_json_envelope(eff_payload, ok=ok)
        text = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":") if eff_compact else None,
            indent=None if eff_compact else 2,
        )
    else:
        text = eff_md

    if eff_out is not None:
        eff_out.parent.mkdir(parents=True, exist_ok=True)
        eff_out.write_text(text + "\n", encoding="utf-8", newline="\n")
        echo_utf8(f"Wrote {eff_out}", err=True)
        return
    echo_utf8(text)


def emit_text(
    ctx: click.Context | None,
    text: str,
    *,
    out_path: str | Path | None = None,
) -> None:
    """Write plain/Markdown text to `--out` if configured, otherwise stdout."""
    _eff_compact, eff_out = resolve_output_flags(ctx, out_path=out_path)
    if eff_out is not None:
        eff_out.parent.mkdir(parents=True, exist_ok=True)
        eff_out.write_text(text + "\n", encoding="utf-8", newline="\n")
        echo_utf8(f"Wrote {eff_out}", err=True)
        return
    echo_utf8(text)


def _json_opt_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> bool:
    root_obj = (ctx.find_root().obj or {}) if ctx is not None else {}
    eff = bool(value or root_obj.get("as_json"))
    if eff and not ctx.resilient_parsing:
        ctx.meta["pob2_as_json"] = True
    return eff


def _compact_opt_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> bool:
    if value and not ctx.resilient_parsing:
        ctx.meta["pob2_compact"] = True
    return value


def _out_opt_callback(ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is not None and not ctx.resilient_parsing:
        ctx.meta["pob2_out"] = value
    return value


def _section_opt_callback(ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is not None and not ctx.resilient_parsing:
        ctx.meta["pob2_section"] = value
    return value


def _fields_opt_callback(ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is not None and not ctx.resilient_parsing:
        ctx.meta["pob2_fields"] = value
    return value


def projection_options(fn: Any) -> Any:
    """Attach `--section` and `--fields` payload projection options to a read Click command."""
    fn = click.option(
        "--fields",
        "fields_opt",
        type=str,
        default=None,
        expose_value=False,
        metavar="PATHS",
        callback=_fields_opt_callback,
        help="Comma-separated dot-paths to project JSON/text output (e.g. offense.totalDps,defense.life).",
    )(fn)
    fn = click.option(
        "--section",
        "section_opt",
        type=str,
        default=None,
        expose_value=False,
        metavar="SECTIONS",
        callback=_section_opt_callback,
        help="Comma-separated top-level sections to keep (e.g. offense,defense).",
    )(fn)
    return fn


def output_options(fn: Any) -> Any:
    """Attach trailing `--json`, `--compact`, and `--out <path>` options to a leaf Click command."""
    fn = click.option(
        "--out",
        "out_path",
        type=click.Path(dir_okay=False),
        default=None,
        expose_value=False,
        callback=_out_opt_callback,
        help="Write output to PATH instead of stdout.",
    )(fn)
    fn = click.option(
        "--compact",
        is_flag=True,
        default=False,
        expose_value=False,
        callback=_compact_opt_callback,
        help="Emit compact single-line JSON when `--json` is set.",
    )(fn)
    fn = click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        callback=_json_opt_callback,
        help="Emit machine-readable JSON.",
    )(fn)
    return fn


def _validate_build_slot_slug(raw: str, *, ctx: click.Context | None = None, param: click.Parameter | None = None) -> str:
    s = (raw or "").strip()
    if s in (".", "..") or not _SLOT_SLUG_RE.match(s):
        raise click.BadParameter(
            f"Invalid daemon build slot slug {raw!r}; expected 1-48 chars matching [a-zA-Z0-9._-].",
            ctx=ctx,
            param=param,
        )
    return s.lower()


def _slot_alias_callback(ctx: click.Context, param: click.Parameter, value: str | None) -> None:
    if value is None or ctx.resilient_parsing:
        return
    from .guardrails import GuardrailScope, check_input, resolve_poe2_slot_name

    raw = str(value).strip()
    check_input(raw, GuardrailScope.SLOT, expected_game="poe2", emit_stderr=False)
    if resolve_poe2_slot_name(raw) is not None:
        raise DaemonRequestError(
            f"`--slot {raw}` on `{ctx.command_path}` selects a daemon build slot, not an equipment slot; "
            "use `--target <build-slot>` for build slots."
        )
    ctx.meta[_SLOT_ALIAS_META_KEY] = _validate_build_slot_slug(raw, ctx=ctx, param=param)


def _target_opt_callback(ctx: click.Context, param: click.Parameter, value: str | None) -> str:
    if ctx.resilient_parsing:
        return resolve_target_slot(value)
    alias = ctx.meta.get(_SLOT_ALIAS_META_KEY)
    norm_val = _validate_build_slot_slug(value, ctx=ctx, param=param) if value else None
    if alias is not None:
        if (
            norm_val is not None
            and ctx.get_parameter_source(param.name or "target") == ParameterSource.COMMANDLINE
            and norm_val != alias
        ):
            raise DaemonRequestError(
                f"Both `--target {norm_val}` and `--slot {alias}` were given; pass only `--target`."
            )
        return str(alias)
    return norm_val or default_slot()


def target_option(*, allow_slot_alias: bool = True, help_text: str | None = None) -> Any:
    """Attach `--target` build-slot option (plus `--slot` alias ONLY when `allow_slot_alias=True`)."""
    desc = help_text or "Target daemon build slot slug (defaults to POB2_SLOT or 'main')."
    target_dec = click.option(
        "--target",
        "target",
        type=str,
        default=None,
        callback=_target_opt_callback,
        help=desc,
    )
    if not allow_slot_alias:
        return target_dec
    alias_dec = click.option(
        "--slot",
        SLOT_ALIAS_PARAM,
        type=str,
        default=None,
        expose_value=False,
        is_eager=True,
        metavar="SLUG",
        callback=_slot_alias_callback,
        help="Alias of `--target` for daemon build slot (equipment slot names are rejected).",
    )

    def _decorate(fn: Any) -> Any:
        return target_dec(alias_dec(fn))

    return _decorate


def resolve_target_slot(target: str | None) -> str:
    return (target or "").strip().lower() or default_slot()


def daemon_call_cli(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Invoke local PoE2 daemon (letting request errors exit 1 and daemon/transport faults exit 5)."""
    from .daemon.client import call_daemon

    return call_daemon(action, params or {}, auto_start=auto_start, timeout=timeout)


__all__ = [
    "SLOT_ALIAS_PARAM",
    "Pob2Command",
    "Pob2Group",
    "ctx_wants_json",
    "daemon_call_cli",
    "echo_utf8",
    "emit_output",
    "emit_text",
    "normalize_json_envelope",
    "output_options",
    "pob2_command",
    "pob2_group",
    "projection_options",
    "resolve_output_flags",
    "resolve_projection_flags",
    "resolve_target_slot",
    "target_option",
]
