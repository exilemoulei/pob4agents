# SPDX-License-Identifier: GPL-3.0-only
"""Click CLI entry points for `pob2 rune` and `pob2 instill` (L4)."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    emit_output,
    output_options,
    pob2_group,
    resolve_target_slot,
    target_option,
)
from .daemon import call_daemon, read_daemon_state
from .errors import CrossGameHallucinationError, DaemonRequestError, LegacyPatchTrapError
from .game_profile import stamp_game
from .guardrails import GuardrailScope, check_input, normalize_poe2_equipment_slot
from .rune import (
    load_rune_instill_catalog,
    normalize_emotion_name,
    normalize_emotion_with_warning,
    price_instill_recipe,
    rank_instillations,
    rank_runes_for_slot,
    resolve_rune_price_ex,
)


def _try_live_daemon_slot(action: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Attempt live CoW ranking against an already-running PoB2 daemon slot without spawning a cold process."""
    if read_daemon_state() is None:
        return None, "[live-eval-degraded] No running PoB2 daemon found; fell back to offline heuristic ranking."
    try:
        return call_daemon(action, params, auto_start=False, timeout=90.0), None
    except (CrossGameHallucinationError, LegacyPatchTrapError):
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"[live-eval-degraded] Live daemon slot unavailable ({exc}); fell back to offline heuristic ranking."


@pob2_group("rune")
def rune_group() -> None:
    """Inspect and Pareto-rank PoE2 0.5.5 runes, soul cores, and idols with Item.lua socket parity."""


@rune_group.command("inspect")
@click.argument("name", required=True)
@output_options
@click.pass_context
def cmd_rune_inspect(ctx: click.Context, name: str, as_json: bool) -> None:
    """Inspect a PoE2 rune, soul core, or idol (slots, req_level, isSocketBound, Bonded mods, price)."""
    check_input(name, GuardrailScope.DATA_QUERY, expected_game="poe2")
    cat = load_rune_instill_catalog()
    entry = cat.lookup_rune(name)
    if entry is None:
        raise DaemonRequestError(f"Unknown PoE2 rune/soul core/idol: {name!r}")
    px, src = resolve_rune_price_ex(entry)
    payload = {
        "ok": True,
        "patch": cat.version,
        "name": entry.name,
        "zhCn": entry.zh_cn,
        "zhTw": entry.zh_tw,
        "kind": entry.kind,
        "reqLevel": entry.req_level,
        "isSocketBound": entry.is_socket_bound,
        "priceEx": round(px, 3),
        "priceSource": src,
        "slots": {
            k: {
                "kind": v.kind,
                "reqLevel": v.req_level,
                "isSocketBound": v.is_socket_bound,
                "stats": list(v.stats),
                "bonded": list(v.bonded),
            }
            for k, v in sorted(entry.slots.items())
        },
    }
    bound_tag = " [isSocketBound=true: 不可覆盖]" if entry.is_socket_bound else ""
    lines = [f"{entry.name} ({entry.zh_cn}) | {entry.kind} | ReqLv={entry.req_level} | ~{px:.1f} Ex{bound_tag}"]
    for skey, sval in sorted(entry.slots.items()):
        lines.append(f"  [{skey}] {'; '.join(sval.stats)}")
        if sval.bonded:
            lines.append(f"    (Shaman) {'; '.join(sval.bonded)}")
    emit_output(ctx, stamp_game(payload), as_json=as_json, markdown_text="\n".join(lines))


_UNIQUE_TYPE_TO_SLOT: dict[str, str] = {
    "body armour": "Body Armour",
    "body armor": "Body Armour",
    "chest": "Body Armour",
    "helmet": "Helmet",
    "helm": "Helmet",
    "gloves": "Gloves",
    "boots": "Boots",
    "amulet": "Amulet",
    "belt": "Belt",
}


@rune_group.command("rank")
@click.option(
    "--type",
    "item_type",
    default="",
    metavar="ITEM_TYPE",
    help="Optional item type (Wand, Quarterstaff, Buckler, Body Armour, Helmet, Gloves, Boots, Shield, Focus). Omit to read equipped `--slot` from live daemon.",
)
@click.option(
    "--slot",
    "slot_name",
    default=None,
    help="Equipped slot label (default: 'Weapon 1', or auto-mapped when `--type` uniquely identifies an equipment slot).",
)
@click.option("--base", "base_name", default="", help="Optional item base name (for Buckler/subtype parity).")
@click.option(
    "--level",
    "char_level",
    type=int,
    default=None,
    help="Character level cap (auto-read from live daemon build, or 90 offline).",
)
@click.option(
    "--shaman/--no-shaman",
    "is_shaman",
    default=None,
    help="Include Shaman-only Bonded rune bonuses (auto-detected from live daemon build when omitted).",
)
@target_option(allow_slot_alias=False)
@click.option(
    "--offline-heuristic",
    is_flag=True,
    default=False,
    help="Skip live daemon CoW evaluation and run deterministic offline heuristic ranking.",
)
@click.option("--metric", default="CombinedDPS", show_default=True, help="Target metric (CombinedDPS | TotalEHP | Spirit).")
@click.option("--budget", "max_budget_ex", type=float, default=None, help="Optional max price cap in Exalted Orbs.")
@click.option("--limit", type=int, default=12, show_default=True, help="Maximum top candidates to output.")
@click.option(
    "--top-live",
    "top_live_eval",
    type=int,
    default=28,
    show_default=True,
    help="Max shortlisted runes evaluated via live CoW (keeps weapon-slot ranking <3s).",
)
@output_options
@click.pass_context
def cmd_rune_rank(
    ctx: click.Context,
    item_type: str,
    slot_name: str | None,
    base_name: str,
    char_level: int | None,
    is_shaman: bool | None,
    target: str,
    offline_heuristic: bool,
    metric: str,
    max_budget_ex: float | None,
    limit: int,
    top_live_eval: int,
    as_json: bool,
) -> None:
    """Rank legal runes/soul cores/idols for a slot (live daemon CoW by default, offline fallback with `--type`)."""
    if item_type:
        check_input(item_type, GuardrailScope.CATEGORY, expected_game="poe2")
    if slot_name is None:
        if item_type:
            mapped_slot = _UNIQUE_TYPE_TO_SLOT.get(item_type.strip().lower())
            if mapped_slot:
                slot_name = mapped_slot
            elif offline_heuristic:
                slot_name = "Weapon 1"
            else:
                raise click.UsageError(
                    f"`--type {item_type}` cannot be uniquely mapped to a single equipment slot; "
                    'pass `--slot <SLOT>` explicitly (e.g. `--slot "Weapon 1"`, `--slot "Weapon 2"`, or `--slot "Ring 1"`).'
                )
        else:
            slot_name = "Weapon 1"
    slot_name = normalize_poe2_equipment_slot(slot_name, command="rune rank")

    data_out: dict[str, Any] | None = None
    fallback_note: str | None = None
    if not offline_heuristic:
        rpc_params: dict[str, Any] = {
            "target": resolve_target_slot(target),
            "slot": slot_name,
            "item_type": item_type,
            "base_name": base_name,
            "metric": metric,
            "budget": max_budget_ex,
            "limit": limit,
            "top_live_eval": top_live_eval,
        }
        if char_level is not None:
            rpc_params["level"] = char_level
        if is_shaman is not None:
            rpc_params["shaman"] = is_shaman
        data_out, fallback_note = _try_live_daemon_slot("rune_rank", rpc_params)

    if data_out is None:
        if not item_type:
            reason = fallback_note or "Offline heuristic mode requires `--type`."
            raise DaemonRequestError(
                f"{reason} Load a build via `pob2 load <source>` or specify `--type <ItemType>` (e.g. `--type Wand`)."
            )
        report = rank_runes_for_slot(
            item_type=item_type,
            slot_name=slot_name,
            base_name=base_name,
            char_level=char_level if char_level is not None else 90,
            is_shaman=bool(is_shaman),
            metric=metric,
            max_budget_ex=max_budget_ex,
            limit=limit,
        )
        data_out = report.to_dict()
        if fallback_note and not offline_heuristic:
            data_out["warnings"] = [*list(data_out.get("warnings") or []), fallback_note]

    data_out.setdefault("slot", slot_name)
    if item_type:
        data_out.setdefault("itemType", item_type)
    elif "itemType" not in data_out:
        data_out["itemType"] = None

    emit_output(
        ctx,
        stamp_game(data_out),
        as_json=as_json,
        markdown_text=_format_pareto_human(data_out),
    )


@pob2_group("instill")
def instill_group() -> None:
    """Lookup and bridge PoE2 0.5.5 ordered amulet instillations & Liquid Emotions."""


@instill_group.command("lookup")
@click.argument("query", required=False, default="")
@click.option(
    "--emotions",
    "emotions_csv",
    default=None,
    metavar="E1,E2,E3",
    help=(
        "Exact ordered 3 emotions `Slot1,Slot2,Slot3` (accepts short `<X>`, legacy `Distilled <X>`, "
        "all 0.5.5 live tier names `Diluted Liquid <X>` / `Liquid <X>` / `Concentrated Liquid <X>` / "
        "`Potent Liquid <X>`, or CN/TW names)."
    ),
)
@click.option("--limit", type=int, default=10, show_default=True, help="Max matches for substring queries.")
@output_options
@click.pass_context
def cmd_instill_lookup(
    ctx: click.Context,
    query: str,
    emotions_csv: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """Lookup ordered `[Slot 1 -> Slot 2 -> Slot 3]` amulet instillation recipe and live emotion cost."""
    cat = load_rune_instill_catalog()
    warnings: list[str] = []
    reason: str | None = None
    if emotions_csv:
        parts = [p.strip() for p in emotions_csv.split(",") if p.strip()]
        if len(parts) != 3:
            raise DaemonRequestError("`--emotions` requires exactly 3 comma-separated emotions in Slot1,Slot2,Slot3 order.")
        try:
            normed = [normalize_emotion_with_warning(p, catalog=cat) for p in parts]
        except ValueError as exc:
            raise DaemonRequestError(str(exc)) from exc
        for _em, warn in normed:
            if warn:
                warnings.append(warn)
        e1, e2, e3 = (item[0] for item in normed)
        rec = cat.lookup_instill_by_recipe(e1.short, e2.short, e3.short)
        matches = [rec] if rec is not None else []
        if not matches:
            target_bag = sorted((e1.short, e2.short, e3.short))
            perm_hits = [ins for ins in cat.instills if sorted(ins.recipe) == target_bag]
            if perm_hits:
                alts = "; ".join(
                    f"{ins.name} ({ins.recipe[0]},{ins.recipe[1]},{ins.recipe[2]})" for ins in perm_hits[:3]
                )
                reason = (
                    f"No PoE2 0.5.5 amulet instillation matches exact slot order [{e1.short} -> {e2.short} -> {e3.short}]. "
                    f"Instillation order is permutation-sensitive; valid order(s) using these emotions: {alts}."
                )
            else:
                reason = (
                    f"No PoE2 0.5.5 amulet instillation recipe is assigned to ordered triplet "
                    f"[{e1.short} -> {e2.short} -> {e3.short}]."
                )
    else:
        if query:
            check_input(query, GuardrailScope.NOTABLE_OR_OIL, expected_game="poe2")
        matches = cat.search_instills(query, limit=limit)
        if not matches:
            reason = (
                f"No PoE2 0.5.5 amulet instillation notable name, node ID, or modifier text matched {query!r}. "
                "Use `--emotions Slot1,Slot2,Slot3` to look up by Liquid Emotions."
            )

    priced = [
        {**price_instill_recipe(inst, catalog=cat), "stats": list(inst.stats)}
        for inst in matches
    ]
    if not priced and reason:
        warnings.append(f"[no-match] {reason}")
    payload: dict[str, Any] = {
        "ok": True,
        "patch": cat.version,
        "count": len(priced),
        "instills": priced,
        "warnings": warnings,
    }
    if reason is not None:
        payload["reason"] = reason
    if not priced:
        md_lines = [reason or "No matching PoE2 amulet instillation found."]
    else:
        md_lines = [
            f"{item['name']} ({item['zhCn']}) [node={item['nodeId']}] | ~{item['totalCostEx']:.1f} Ex\n"
            f"  Order: {item['orderedDisplayEn']}\n"
            f"  Stats: {'; '.join(item['stats'])}"
            for item in priced
        ]
    for w in warnings:
        if not (reason and w.startswith("[no-match]")):
            md_lines.append(f"Warning: {w}")
    emit_output(ctx, stamp_game(payload), as_json=as_json, markdown_text="\n".join(md_lines))


@instill_group.command("bridge")
@click.argument("emotion", required=True)
@output_options
@click.pass_context
def cmd_instill_bridge(ctx: click.Context, emotion: str, as_json: bool) -> None:
    """Bridge short `<X>`, legacy `Distilled <X>`, and live 0.5.5 `Diluted/Liquid/Concentrated/Potent Liquid <X>` (rejects Ancient jewel emotions)."""
    cat = load_rune_instill_catalog()
    try:
        em = normalize_emotion_name(emotion, catalog=cat)
    except ValueError as exc:
        raise DaemonRequestError(str(exc)) from exc
    payload = {
        "ok": True,
        "short": em.short,
        "legacyName": em.legacy_name,
        "liveName": em.live_name,
        "tierLevel": em.tier_level,
        "zhCn": em.zh_cn,
        "zhTw": em.zh_tw,
        "radiusJewel": False,
    }
    md = f"{em.legacy_name} <-> {em.live_name} ({em.zh_cn}, tierLevel={em.tier_level}, amulet-valid)"
    emit_output(ctx, stamp_game(payload), as_json=as_json, markdown_text=md)


@instill_group.command("rank")
@click.option("--metric", default="CombinedDPS", show_default=True, help="Target metric (CombinedDPS | TotalEHP).")
@click.option("--budget", "max_budget_ex", type=float, default=None, help="Optional max instillation cost in Exalted Orbs.")
@click.option("--limit", type=int, default=15, show_default=True, help="Maximum top candidates to output.")
@target_option(allow_slot_alias=True)
@click.option(
    "--offline-heuristic",
    is_flag=True,
    default=False,
    help="Skip live daemon CoW evaluation and run deterministic offline heuristic ranking across all 875 notables.",
)
@click.option(
    "--top-live",
    "top_live_eval",
    type=int,
    default=20,
    show_default=True,
    help="Max offline-shortlisted notables verified via live PoB2 calc (<2.5s).",
)
@output_options
@click.pass_context
def cmd_instill_rank(
    ctx: click.Context,
    metric: str,
    max_budget_ex: float | None,
    limit: int,
    target: str,
    offline_heuristic: bool,
    top_live_eval: int,
    as_json: bool,
) -> None:
    """Pareto cost-efficiency ranking for all 875 ordered PoE2 amulet instillations (live daemon CoW when loaded)."""
    data_out: dict[str, Any] | None = None
    fallback_note: str | None = None
    if not offline_heuristic:
        data_out, fallback_note = _try_live_daemon_slot(
            "rank_instill",
            {
                "target": resolve_target_slot(target),
                "metric": metric,
                "budget": max_budget_ex,
                "limit": limit,
                "top_live_eval": top_live_eval,
            },
        )

    if data_out is None:
        report = rank_instillations(metric=metric, max_budget_ex=max_budget_ex, limit=limit)
        data_out = report.to_dict()
        if fallback_note and not offline_heuristic:
            data_out["warnings"] = [*list(data_out.get("warnings") or []), fallback_note]

    emit_output(
        ctx,
        stamp_game(data_out),
        as_json=as_json,
        markdown_text=_format_pareto_human(data_out),
    )


def _format_pareto_human(data: dict[str, Any]) -> str:
    mode = data.get("evalMode", "offline_heuristic")
    shortlist_tag = f"/{data['shortlistedFrom']}" if data.get("shortlistedFrom") else ""
    slot_tag = f" | slot={data['slot']}" if data.get("slot") else ""
    type_tag = f" | itemType={data['itemType']}" if data.get("itemType") else ""
    lines = [
        f"PoE2 {data['patch']} Pareto Ranking ({data['kind']} | mode={mode}{slot_tag}{type_tag} | "
        f"metric={data['metric']} | evaluated={data['evaluatedCount']}{shortlist_tag})"
    ]
    bis = data.get("bisAnchor")
    if bis:
        lines.append(f"  [BiS Anchor] {bis['name']} ({bis['zhCn']}) | gain={bis['gain']:.2f} | cost={bis['costEx']:.1f} Ex")
    champ = data.get("budgetChampion")
    if champ:
        lines.append(
            f"  [Budget Pareto] {champ['name']} ({champ['zhCn']}) | {champ['pctOfBis']:.1f}% of BiS | cost={champ['costEx']:.1f} Ex"
        )
    frontier = data.get("paretoFrontier") or []
    for row in frontier:
        order = f" | {row['orderedDisplayEn']}" if "orderedDisplayEn" in row else ""
        lines.append(f"  - {row['name']}: gain={row['gain']:.2f} ({row['pctOfBis']:.1f}% BiS), cost={row['costEx']:.1f} Ex{order}")
    if not frontier and int(data.get("evaluatedCount") or 0) > 0:
        lines.append(
            f"  [Summary] All {data['evaluatedCount']} evaluated candidates had <= 0 gain for {data['metric']} "
            "(no positive upgrade over current setup)."
        )
        for row in (data.get("topCandidates") or [])[:5]:
            lines.append(f"  - {row['name']}: gain={row['gain']:+.2f}, cost={row['costEx']:.1f} Ex")
    for warn in data.get("warnings") or []:
        lines.append(f"  [note] {warn}")
    return "\n".join(lines)


__all__ = [
    "cmd_instill_rank",
    "instill_group",
    "rune_group",
]
