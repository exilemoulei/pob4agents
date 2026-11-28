# SPDX-License-Identifier: GPL-3.0-only
"""Human-readable Markdown & table formatters for PoE2 build summary, audit, uniques, compare, and what-if (L2)."""

from __future__ import annotations

from typing import Any

from tabulate import tabulate


def _fmt_num(val: Any) -> str:
    if isinstance(val, bool) or val is None:
        return "-"
    if isinstance(val, (int, float)):
        if abs(val) >= 1_000_000:
            return f"{val / 1_000_000:.2f}M"
        if abs(val) >= 10_000:
            return f"{val / 1_000:.1f}k"
        return f"{val:.0f}" if val == int(val) else f"{val:.2f}"
    return str(val)


def _version_banner(payload: dict[str, Any]) -> str:
    pob_v = payload.get("pob2Version") or "unknown"
    patch = payload.get("gamePatch") or "0.5.5"
    flag = f" {payload['reviewFlag']}" if payload.get("reviewFlag") else ""
    return f"PoB2 v{pob_v} | PoE2 {patch}{flag}"


def format_summary_md(payload: dict[str, Any]) -> str:
    b = payload.get("build") or {}
    pts = b.get("points") or {}
    lines = [
        f"## PoE2 Build Summary ({_version_banner(payload)})",
        f"- Class: {b.get('className') or '?'} / {b.get('ascendClassName') or 'None'} | Lv {b.get('level') or '?'} | Tree {b.get('treeVersion') or '?'}",
        f"- Primary DPS ({payload.get('primaryDpsKey')}): {_fmt_num(payload.get('primaryDps'))} | TotalEHP: {_fmt_num(payload.get('totalEhp'))} | Life: {_fmt_num(payload.get('life'))} | Spirit: {_fmt_num(payload.get('spirit'))}",
    ]
    if pts:
        lines.append(f"- Passives: {pts.get('used', 0)}/{pts.get('usedMax', '?')} (Asc {pts.get('ascUsed', 0)}/{pts.get('ascMax', '?')})")
    ws = payload.get("weaponSetState") or {}
    if ws:
        lines.append(
            f"- Weapon Set: active WS{2 if ws.get('useSecondWeaponSet') else 1} | allocMode={ws.get('allocMode', 0)} | WS1={ws.get('weaponSet1Used', 0)} WS2={ws.get('weaponSet2Used', 0)}"
        )
    warns = payload.get("warnings") or []
    if warns:
        lines.append("- Warnings: " + "; ".join(str(w) for w in warns[:6]))
    return "\n".join(lines)


def format_audit_md(payload: dict[str, Any]) -> str:
    lines = [f"## PoE2 Deep Audit ({_version_banner(payload)})"]

    red_cards = payload.get("unsimulatedRedCards") or []
    if red_cards:
        lines.append("\n### [PoB2未模拟] Honest Red Cards")
        for rc in red_cards:
            lines.append(f"- **[PoB2未模拟]** `{rc.get('skillName')}` ({rc.get('issue')}): {rc.get('reason')}")

    deh = payload.get("dehydration") or {}
    sc = deh.get("scenarios") or {}
    if sc:
        rows = [
            [
                nm,
                _fmt_num(st.get("dps")),
                _fmt_num(st.get("totalEhp")),
                _fmt_num(st.get("physMaxHit")),
                _fmt_num(st.get("eleMinMaxHit")),
                f"{_fmt_num(st.get('spiritUnreserved'))}/{_fmt_num(st.get('spirit'))}",
            ]
            for nm, st in sc.items()
        ]
        lines.append("\n### 4-Dim Honest Dehydration (Quest* & ResistancePenalty Preserved)")
        lines.append(tabulate(rows, headers=["Scenario", "DPS", "TotalEHP", "PhysMaxHit", "EleMinMaxHit", "Spirit"], tablefmt="github"))

    res = (payload.get("resistances") or {}).get("elements") or {}
    if res:
        r_rows = [
            [e, f"{v.get('final', 0):.0f}%", f"{v.get('cap', 75):.0f}%", f"{v.get('overcapBuffer', 0):+.0f}%", v.get("status")]
            for e, v in res.items()
        ]
        lines.append("\n### Resistances (Cap + Overcap Buffer Policy)")
        lines.append(tabulate(r_rows, headers=["Element", "Final", "Cap", "Overcap", "Status"], tablefmt="github"))

    arm = payload.get("armour") or {}
    if arm.get("physicalLadder"):
        a_rows = [[r["hitDamage"], f"{r['mitigationPct']:.1f}%"] for r in arm["physicalLadder"]]
        lines.append(f"\n### Armour Mitigation Ladder (Armour={_fmt_num(arm.get('armour'))}, ArmourRatio={arm.get('armourRatio', 10)})")
        lines.append(tabulate(a_rows, headers=["Raw Hit", "Phys Mitigation"], tablefmt="github"))
        if arm.get("hasElementalArmour"):
            lines.append(f"- Errata: {arm.get('tooltipErrata')}")

    defl = payload.get("deflection") or {}
    if defl:
        lines.append(
            f"\n### Deflection & Block\n- Deflection: {defl.get('chance', 0):.0f}% (cap {defl.get('chanceCap', 95)}%) | "
            f"TotalEHP={_fmt_num(defl.get('totalEhp'))} vs Raw MaxHit Phys={_fmt_num(defl.get('rawPhysMaxHit'))} / Ele={_fmt_num(defl.get('rawEleMinMaxHit'))}"
        )

    findings = payload.get("findings") or []
    if findings:
        lines.append("\n### Audit Findings")
        for f in findings:
            lines.append(f"- [{f.get('severity', 'info').upper()}] {f.get('message')}")
    return "\n".join(lines)


def format_uniques_md(payload: dict[str, Any]) -> str:
    lines = [
        f"## PoE2 Unique Ranking — {payload.get('slot')} ({_version_banner(payload)})",
        f"- Stat: `{payload.get('stat')}` | Base DPS: {_fmt_num(payload.get('baseDps'))} | Base EHP: {_fmt_num(payload.get('baseEhp'))} | Scanned: {payload.get('scannedCount', 0)}",
    ]
    rows = []
    for i, r in enumerate(payload.get("results") or [], 1):
        sanc = r.get("sanctifiedRollRange") or {}
        sanc_str = f"{sanc.get('maxDpsUpsidePct', 0):+.1f}% DPS / {sanc.get('maxEhpUpsidePct', 0):+.1f}% EHP" if sanc else "-"
        rows.append([i, r.get("name"), r.get("baseName"), f"{r.get('deltaDpsPct', 0):+.1f}%", f"{r.get('deltaEhpPct', 0):+.1f}%", sanc_str])
    if rows:
        lines.append(tabulate(rows, headers=["#", "Unique", "Base", "dDPS%", "dEHP%", "Sanctified 1.22x"], tablefmt="github"))
    return "\n".join(lines)


def format_compare_md(payload: dict[str, Any]) -> str:
    m, r, g = payload.get("main") or {}, payload.get("ref") or {}, payload.get("gap") or {}
    lines = [
        f"## PoE2 5-Dimension Compare Report ({_version_banner(payload)})",
        f"- Main: {m.get('className')} Lv{m.get('level')} (DPS {_fmt_num(m.get('primaryDps'))}, EHP {_fmt_num(m.get('totalEhp'))})",
        f"- Ref ({payload.get('reference')}): {r.get('className')} Lv{r.get('level')} (DPS {_fmt_num(r.get('primaryDps'))}, EHP {_fmt_num(r.get('totalEhp'))})",
        f"- Total Gap: dDPS {g.get('deltaDpsPct', 0):+.1f}% | dEHP {g.get('deltaEhpPct', 0):+.1f}% | Top Bottleneck: `{payload.get('topBottleneck')}`",
    ]
    rows = [
        [d.get("rank"), f"{d.get('label')} ({d.get('labelZh')})", f"{d.get('deltaDpsPct', 0):+.1f}%", f"{d.get('deltaEhpPct', 0):+.1f}%"]
        for d in payload.get("dimensions") or []
    ]
    if rows:
        lines.append(tabulate(rows, headers=["Rank", "Dimension", "dDPS%", "dEHP%"], tablefmt="github"))
    return "\n".join(lines)


def format_what_if_md(payload: dict[str, Any]) -> str:
    b, a = payload.get("before") or {}, payload.get("after") or {}
    lines = [
        f"## PoE2 What-If / Diff ({payload.get('mode', 'DIFF')} | {_version_banner(payload)})",
        f"- DPS: {_fmt_num(b.get('primaryDps'))} -> {_fmt_num(a.get('primaryDps'))} | EHP: {_fmt_num(b.get('totalEhp'))} -> {_fmt_num(a.get('totalEhp'))} | Life: {_fmt_num(b.get('life'))} -> {_fmt_num(a.get('life'))}",
    ]
    deltas = (payload.get("diff") or {}).get("deltas") or []
    if deltas:
        rows = [[d.get("label"), d.get("beforeStr"), d.get("afterStr"), d.get("deltaStr")] for d in deltas[:15]]
        lines.append(tabulate(rows, headers=["Stat", "Before", "After", "Delta"], tablefmt="github"))
    return "\n".join(lines)


def format_notes_md(payload: dict[str, Any]) -> str:
    hdr = f"## PoE2 Author Notes ({_version_banner(payload)})"
    txt = payload.get("text") or ""
    return f"{hdr}\n\n{txt}" if txt else f"{hdr}\n\n(No notes in this build.)"


__all__ = [
    "format_audit_md",
    "format_compare_md",
    "format_notes_md",
    "format_summary_md",
    "format_uniques_md",
    "format_what_if_md",
]
