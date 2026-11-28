# SPDX-License-Identifier: GPL-3.0-only
"""Unified Click Command/Group exit-code (`0`/`1`/`5`) and zero-stderr `--json` error envelope boundary (L4)."""

from __future__ import annotations

import contextvars
import re
import sys
from typing import Any

import click

from .errors import (
    EXIT_DAEMON_ERROR,
    EXIT_REQUEST_ERROR,
    BootstrapMissingError,
    CrossGameHallucinationError,
    DaemonRequestError,
    InternalBugError,
    LegacyPatchTrapError,
)

_BRACKET_CODE_RE = re.compile(r"^(?:Error:\s*)?\[([A-Za-z0-9_.-]+)\]")
_RAW_ARGV_VAR: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("pob2_raw_argv", default=())


def _sanitize_err(message: str) -> str:
    from .bridge_client import sanitize_bridge_error

    return sanitize_bridge_error(message)


def _single_line(text: str) -> str:
    collapsed = " ".join(part.strip() for part in str(text or "").splitlines() if part.strip())
    if collapsed.startswith("Error:"):
        collapsed = collapsed[len("Error:") :].strip()
    return collapsed or "Request failed."


def _classify_daemon_request_error(msg: str) -> tuple[str, str]:
    m_code = _BRACKET_CODE_RE.match(msg)
    code = m_code.group(1) if m_code else None
    low = msg.lower()
    if code == "POE2-INVALID-SLOT":
        return "invalid_slot", code
    if code == "POE2-INVALID-SECTION":
        return "invalid_section", code
    if code == "POE2-INVALID-FIELD":
        return "invalid_field", code
    if code == "POE2-INVALID-SPEC":
        return "invalid_spec", code
    if code == "POE2-NO-OP":
        return "no_op", code
    if code == "POE2-FETCH-BLOCKED":
        return "fetch_blocked", code
    if code == "POE2-FETCH-HTTP-ERROR":
        return "fetch_http_error", code
    if code == "POE2-EMPTY-HISTORY" or "stack is empty" in low:
        return "empty_history", code or "POE2-EMPTY-HISTORY"
    if "has no build loaded" in low or "is empty in build slot" in low:
        return "empty_slot", code or "POE2-EMPTY-SLOT"
    if "file not found" in low or "no such file" in low:
        return "file_not_found", code or "POE2-FILE-NOT-FOUND"
    if "against itself" in low or ("both `--target" in low and "`--slot" in low):
        return "same_slot", code or "POE2-SAME-SLOT"
    return "request_error", code or "POE2-REQUEST-ERROR"


def _emit_error_and_resolve_code(ctx: click.Context | None, exc: BaseException) -> int:
    from .cli_common import ctx_wants_json, echo_utf8, emit_output

    wants_json = ctx_wants_json(ctx)
    if isinstance(exc, (CrossGameHallucinationError, LegacyPatchTrapError)):
        if wants_json:
            payload = dict(exc.to_dict())
            payload.setdefault(
                "code",
                "POE2-CROSS-GAME-BLOCK" if isinstance(exc, CrossGameHallucinationError) else "POE2-PATCH-BLOCK",
            )
            payload["error"] = _single_line(str(payload.get("error") or exc))
            emit_output(ctx, payload, as_json=True, ok=False, ignore_ctx_out=True, skip_projection=True)
        else:
            echo_utf8(exc.format_human(), err=True)
        return EXIT_REQUEST_ERROR

    if isinstance(exc, (InternalBugError, AttributeError, TypeError, KeyError)):
        single = _single_line(_sanitize_err(f"{exc.__class__.__name__}: {exc}" if not isinstance(exc, InternalBugError) else str(exc)))
        if wants_json:
            emit_output(
                ctx,
                {"errorKind": "internal_error", "code": "POE2-INTERNAL-ERROR", "error": single},
                as_json=True,
                ok=False,
                ignore_ctx_out=True,
                skip_projection=True,
            )
        else:
            echo_utf8(f"Error: {single}", err=True)
        return EXIT_REQUEST_ERROR

    if isinstance(exc, DaemonRequestError):
        exit_code = int(getattr(exc, "exit_code", EXIT_REQUEST_ERROR))
        to_dict_fn = getattr(exc, "to_dict", None)
        single = _single_line(str(exc))
        if wants_json:
            if callable(to_dict_fn):
                payload = dict(to_dict_fn())
                m_code = _BRACKET_CODE_RE.match(single)
                payload.setdefault("code", m_code.group(1) if m_code else "POE2-REQUEST-ERROR")
                payload["error"] = _single_line(str(payload.get("error") or single))
            else:
                kind, code = _classify_daemon_request_error(single)
                payload = {"errorKind": kind, "code": code, "error": single}
            emit_output(ctx, payload, as_json=True, ok=False, ignore_ctx_out=True, skip_projection=True)
        else:
            echo_utf8(f"Error: {single}", err=True)
        return exit_code

    if isinstance(exc, BootstrapMissingError):
        single = _single_line(str(exc))
        if wants_json:
            emit_output(
                ctx,
                {"errorKind": "bootstrap_missing", "code": "POE2-BOOTSTRAP-MISSING", "error": single},
                as_json=True,
                ok=False,
                ignore_ctx_out=True,
                skip_projection=True,
            )
        else:
            echo_utf8(f"Error: {single}", err=True)
        return EXIT_REQUEST_ERROR

    if isinstance(exc, FileNotFoundError):
        single = _single_line(str(exc))
        if wants_json:
            emit_output(
                ctx,
                {"errorKind": "file_not_found", "code": "POE2-FILE-NOT-FOUND", "error": single},
                as_json=True,
                ok=False,
                ignore_ctx_out=True,
                skip_projection=True,
            )
        else:
            echo_utf8(f"Error: {single}", err=True)
        return EXIT_REQUEST_ERROR

    if isinstance(exc, click.ClickException):
        exc.exit_code = EXIT_REQUEST_ERROR
        if wants_json:
            single = _single_line(exc.format_message())
            kind = "usage_error" if isinstance(exc, click.UsageError) else "cli_error"
            code = "POE2-USAGE-ERROR" if isinstance(exc, click.UsageError) else "POE2-CLI-ERROR"
            emit_output(
                ctx,
                {"errorKind": kind, "code": code, "error": single},
                as_json=True,
                ok=False,
                ignore_ctx_out=True,
                skip_projection=True,
            )
        else:
            exc.show()
        return EXIT_REQUEST_ERROR

    single = _single_line(_sanitize_err(str(exc)))
    if wants_json:
        emit_output(
            ctx,
            {"errorKind": "daemon_error", "code": "POE2-DAEMON-ERROR", "error": single},
            as_json=True,
            ok=False,
            ignore_ctx_out=True,
            skip_projection=True,
        )
    else:
        echo_utf8(f"Error: {single}", err=True)
    return EXIT_DAEMON_ERROR


def _invoke_with_exit_contract(ctx: click.Context, fn: Any) -> Any:
    """Enforce unified `0 / 1 / 5` exit-code and `--json` error envelope contract."""
    try:
        return fn()
    except (click.exceptions.Exit, click.Abort, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        ctx.exit(_emit_error_and_resolve_code(ctx, exc))


class _Pob2CliMixin:
    """Shared Click Command/Group mixin tracking raw CLI args and unifying error envelopes."""

    def main(  # type: ignore[misc]
        self,
        args: Any = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        raw_tuple = tuple(str(a) for a in (args if args is not None else sys.argv[1:]))
        tok = _RAW_ARGV_VAR.set(raw_tuple)
        try:
            return super().main(  # type: ignore[misc]
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                **extra,
            )
        except (click.exceptions.Exit, click.Abort, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if not standalone_mode:
                raise
            code = _emit_error_and_resolve_code(click.get_current_context(silent=True), exc)
            raise SystemExit(code) from exc
        finally:
            _RAW_ARGV_VAR.reset(tok)


class Pob2Command(_Pob2CliMixin, click.Command):
    """Click Command enforcing `0 / 1 / 5` exit codes and recording raw CLI args."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        raw_copy = list(args)
        try:
            ctx = super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as exc:
            exc.exit_code = EXIT_REQUEST_ERROR
            raise
        ctx.meta["pob2_raw_args"] = raw_copy
        return ctx

    def invoke(self, ctx: click.Context) -> Any:
        return _invoke_with_exit_contract(ctx, lambda: super(Pob2Command, self).invoke(ctx))


class Pob2Group(_Pob2CliMixin, click.Group):
    """Click Group defaulting child commands to `Pob2Command` and enforcing `0 / 1 / 5` exit codes."""

    command_class = Pob2Command

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        raw_copy = list(args)
        try:
            ctx = super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as exc:
            exc.exit_code = EXIT_REQUEST_ERROR
            raise
        ctx.meta["pob2_raw_args"] = raw_copy
        return ctx

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            exc.exit_code = EXIT_REQUEST_ERROR
            raise

    def invoke(self, ctx: click.Context) -> Any:
        return _invoke_with_exit_contract(ctx, lambda: super(Pob2Group, self).invoke(ctx))


def pob2_command(name: str | None = None, **attrs: Any) -> Any:
    attrs.setdefault("cls", Pob2Command)
    return click.command(name, **attrs)


def pob2_group(name: str | None = None, **attrs: Any) -> Any:
    attrs.setdefault("cls", Pob2Group)
    return click.group(name, **attrs)


__all__ = [
    "_RAW_ARGV_VAR",
    "Pob2Command",
    "Pob2Group",
    "_classify_daemon_request_error",
    "_emit_error_and_resolve_code",
    "_invoke_with_exit_contract",
    "pob2_command",
    "pob2_group",
]
