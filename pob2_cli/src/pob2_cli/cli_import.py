# SPDX-License-Identifier: GPL-3.0-only
"""Click CLI subcommand `pob2 import` for multi-source PoE2 URLs, WeGame shares, and PoB2 codes/XML (L4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from .game_profile import stamp_game
from .importers import resolve_poe2_build_source


@pob2_command("import")
@click.argument("source", required=True)
@click.option(
    "--xml-out",
    "explicit_xml_out",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write imported PoB2 XML to a dedicated UTF-8 file.",
)
@click.option(
    "--data-dir",
    "data_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional Data/poe2 localization directory (zh-rCN / zh-rTW).",
)
@click.option("--load", "load_slot", is_flag=True, default=False, help="Also load the imported build into the active PoB2 daemon slot.")
@target_option(allow_slot_alias=True, help_text="Target daemon slot when --load is set (default: main).")
@click.option("--timeout", type=float, default=15.0, show_default=True, help="HTTP timeout per endpoint (s).")
@output_options
@click.pass_context
def cmd_import(
    ctx: click.Context,
    source: str,
    explicit_xml_out: str | None,
    data_dir: Path | None,
    load_slot: bool,
    target: str,
    timeout: float,
    as_json: bool,
) -> None:
    """Import a PoE2 build from pobb.in, poe2db.tw, poe.ninja/poe2, maxroll.gg/poe2, WeGame, XML, or PoB2 code."""
    eff_json = bool(as_json or ctx_wants_json(ctx))
    _eff_compact, eff_out = resolve_output_flags(ctx)

    resolved = resolve_poe2_build_source(source=source, data_dir=data_dir, timeout=timeout)
    summary: dict[str, Any] = {**resolved.summary, "warnings": list(resolved.warnings), "stats": dict(resolved.stats)}

    if load_slot:
        load_res = daemon_call_cli("load", {"xml_code": resolved.code, "target": resolve_target_slot(target)})
        summary["loaded"] = load_res

    _deliver_import_result(
        ctx,
        resolved.xml,
        summary=summary,
        source_kind=resolved.source_kind,
        as_json=eff_json,
        explicit_xml_out=Path(explicit_xml_out) if explicit_xml_out else None,
        eff_out=eff_out,
    )


def _deliver_import_result(
    ctx: click.Context,
    xml: str,
    *,
    summary: dict[str, Any],
    source_kind: str,
    as_json: bool,
    explicit_xml_out: Path | None,
    eff_out: Path | None,
) -> None:
    if not as_json:
        xml_target = explicit_xml_out or eff_out
        if xml_target is not None:
            xml_target.parent.mkdir(parents=True, exist_ok=True)
            xml_target.write_text(xml, encoding="utf-8", newline="\n")
            echo_utf8(f"Wrote PoB2 XML ({len(xml)} bytes) to {xml_target}")
            for w in summary.get("warnings") or []:
                echo_utf8(f"[import] {w}", err=True)
            return
        echo_utf8(xml, err=False)
        for w in summary.get("warnings") or []:
            echo_utf8(f"[import] {w}", err=True)
        return

    out_is_xml = eff_out is not None and eff_out.suffix.lower() in (".xml", ".pob", ".pob2")
    xml_dest = explicit_xml_out or (eff_out if out_is_xml else None)
    written_path: str | None = None
    if xml_dest is not None:
        xml_dest.parent.mkdir(parents=True, exist_ok=True)
        xml_dest.write_text(xml, encoding="utf-8", newline="\n")
        written_path = str(xml_dest)

    payload = stamp_game(
        {
            "ok": True,
            "sourceKind": source_kind,
            "xmlBytes": len(xml.encode("utf-8")),
            "xmlPath": written_path,
            **summary,
            **({} if written_path else {"xml": xml}),
        }
    )
    emit_output(
        ctx,
        payload,
        as_json=True,
        ignore_ctx_out=(out_is_xml and explicit_xml_out is None),
    )


__all__ = ["cmd_import"]
