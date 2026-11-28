# SPDX-License-Identifier: GPL-3.0-only
"""Stage 3: PoE2 13 Breach Catalysts quality solver for uncorrupted Jewellery & Belts (L2).

Strict parity with `PoE2/Classes/Item.lua` `catalystList` & `getCatalystScalar`:
  - Exactly 13 PoE2 catalysts (NO PoE1 Prismatic all-res catalyst!).
  - Skips `{unscalable}` mod lines (`getCatalystScalar` returns 1).
  - Skips Corrupted, Sanctified, and Mirrored items.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..craft.price_book import PriceBook
from ..craft.weights_loader import norm_key
from ..rune.instill_bridge import DEFAULT_CATALYST_PRICES_EX
from .net_gap import EquippedSlotAudit


@dataclass(frozen=True, slots=True)
class CatalystSpec:
    """Single PoE2 Breach Catalyst from Item.lua `catalystList`."""

    name: str
    descriptor: str
    zh_cn: str
    tags: tuple[str, ...]
    quality_header: str


# Exact 13 PoE2 Breach Catalysts (Item.lua `catalystList` / `catalystDescriptorList` / `catalystTags`).
POE2_CATALYSTS: tuple[CatalystSpec, ...] = (
    CatalystSpec("Flesh Catalyst", "Life", "血肉催化剂", ("life",), "Quality (Life Modifiers)"),
    CatalystSpec("Neural Catalyst", "Mana", "神经催化剂", ("mana",), "Quality (Mana Modifiers)"),
    CatalystSpec("Carapace Catalyst", "Defence", "甲壳催化剂", ("defences", "armour", "evasion", "energyshield"), "Quality (Defence Modifiers)"),
    CatalystSpec("Uul-Netol's Catalyst", "Physical", "乌尔尼多的催化剂", ("physical",), "Quality (Physical Modifiers)"),
    CatalystSpec("Xoph's Catalyst", "Fire", "索伏的催化剂", ("fire",), "Quality (Fire Modifiers)"),
    CatalystSpec("Tul's Catalyst", "Cold", "托沃的催化剂", ("cold",), "Quality (Cold Modifiers)"),
    CatalystSpec("Esh's Catalyst", "Lightning", "艾许的催化剂", ("lightning",), "Quality (Lightning Modifiers)"),
    CatalystSpec("Chayula's Catalyst", "Chaos", "夏乌拉的催化剂", ("chaos",), "Quality (Chaos Modifiers)"),
    CatalystSpec("Reaver Catalyst", "Attack", "掠夺者催化剂", ("attack",), "Quality (Attack Modifiers)"),
    CatalystSpec("Sibilant Catalyst", "Caster", "嘶鸣催化剂", ("caster",), "Quality (Caster Modifiers)"),
    CatalystSpec("Skittering Catalyst", "Speed", "疾行催化剂", ("speed",), "Quality (Speed Modifiers)"),
    CatalystSpec("Adaptive Catalyst", "Attribute", "适应催化剂", ("attribute",), "Quality (Attribute Modifiers)"),
    CatalystSpec("Necrotic Catalyst", "Minion", "死灵催化剂", ("minion",), "Quality (Minion Modifiers)"),
)

_CATALYST_BY_NAME: dict[str, CatalystSpec] = {c.name: c for c in POE2_CATALYSTS}

# Which stat gaps each PoE2 catalyst can scale on Jewellery / Belts
_STAT_MATCHERS: dict[str, tuple[tuple[re.Pattern[str], tuple[str, ...]], ...]] = {
    "Xoph's Catalyst": (
        (re.compile(r"\+(\d+)%\s+to\s+Fire\s+Resistance", re.IGNORECASE), ("fire",)),
        (re.compile(r"\+(\d+)%\s+to\s+Fire\s+and\s+Cold\s+Resistances", re.IGNORECASE), ("fire", "cold")),
        (re.compile(r"\+(\d+)%\s+to\s+Fire\s+and\s+Lightning\s+Resistances", re.IGNORECASE), ("fire", "lightning")),
        (re.compile(r"\+(\d+)%\s+to\s+Fire\s+and\s+Chaos\s+Resistances", re.IGNORECASE), ("fire", "chaos")),
        (re.compile(r"\+(\d+)%\s+to\s+all\s+Elemental\s+Resistances", re.IGNORECASE), ("fire", "cold", "lightning")),
    ),
    "Tul's Catalyst": (
        (re.compile(r"\+(\d+)%\s+to\s+Cold\s+Resistance", re.IGNORECASE), ("cold",)),
        (re.compile(r"\+(\d+)%\s+to\s+Fire\s+and\s+Cold\s+Resistances", re.IGNORECASE), ("fire", "cold")),
        (re.compile(r"\+(\d+)%\s+to\s+Cold\s+and\s+Lightning\s+Resistances", re.IGNORECASE), ("cold", "lightning")),
        (re.compile(r"\+(\d+)%\s+to\s+Cold\s+and\s+Chaos\s+Resistances", re.IGNORECASE), ("cold", "chaos")),
        (re.compile(r"\+(\d+)%\s+to\s+all\s+Elemental\s+Resistances", re.IGNORECASE), ("fire", "cold", "lightning")),
    ),
    "Esh's Catalyst": (
        (re.compile(r"\+(\d+)%\s+to\s+Lightning\s+Resistance", re.IGNORECASE), ("lightning",)),
        (re.compile(r"\+(\d+)%\s+to\s+Fire\s+and\s+Lightning\s+Resistances", re.IGNORECASE), ("fire", "lightning")),
        (re.compile(r"\+(\d+)%\s+to\s+Cold\s+and\s+Lightning\s+Resistances", re.IGNORECASE), ("cold", "lightning")),
        (re.compile(r"\+(\d+)%\s+to\s+Lightning\s+and\s+Chaos\s+Resistances", re.IGNORECASE), ("lightning", "chaos")),
        (re.compile(r"\+(\d+)%\s+to\s+all\s+Elemental\s+Resistances", re.IGNORECASE), ("fire", "cold", "lightning")),
    ),
    "Chayula's Catalyst": (
        (re.compile(r"\+(\d+)%\s+to\s+Chaos\s+Resistance", re.IGNORECASE), ("chaos",)),
        (re.compile(r"\+(\d+)%\s+to\s+(?:Fire|Cold|Lightning)\s+and\s+Chaos\s+Resistances", re.IGNORECASE), ("chaos",)),
    ),
    "Adaptive Catalyst": (
        (re.compile(r"\+(\d+)\s+to\s+Strength\b", re.IGNORECASE), ("str",)),
        (re.compile(r"\+(\d+)\s+to\s+Dexterity\b", re.IGNORECASE), ("dex",)),
        (re.compile(r"\+(\d+)\s+to\s+Intelligence\b", re.IGNORECASE), ("int",)),
        (re.compile(r"\+(\d+)\s+to\s+Strength\s+and\s+Dexterity\b", re.IGNORECASE), ("str", "dex")),
        (re.compile(r"\+(\d+)\s+to\s+Strength\s+and\s+Intelligence\b", re.IGNORECASE), ("str", "int")),
        (re.compile(r"\+(\d+)\s+to\s+Dexterity\s+and\s+Intelligence\b", re.IGNORECASE), ("dex", "int")),
        (re.compile(r"\+(\d+)\s+to\s+all\s+Attributes\b", re.IGNORECASE), ("str", "dex", "int")),
    ),
}

_EXISTING_QUAL_RE = re.compile(r"^Quality(?:\s*\([^)]+\))?:\s*\+?(\d+)", re.IGNORECASE)
_CATALYST_HDR_RE = re.compile(r"^Catalyst(?:Quality)?:", re.IGNORECASE)
_IMPLICITS_RE = re.compile(r"^Implicits:\s*\d+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CatalystUpgradeAction:
    """Planned +20% Breach Catalyst application on one Jewellery/Belt slot."""

    slot_name: str
    item_name: str
    catalyst_name: str
    descriptor: str
    zh_cn: str
    quality_pct: int
    total_cost_ex: float
    gains: dict[str, int]
    purpose: str  # "hard_floor" | "overcap_buffer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot_name,
            "item": self.item_name,
            "catalyst": self.catalyst_name,
            "descriptor": self.descriptor,
            "zhCn": self.zh_cn,
            "qualityPct": self.quality_pct,
            "totalCostEx": round(self.total_cost_ex, 2),
            "gains": {k: v for k, v in self.gains.items() if v > 0},
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class Stage3CatalystPlan:
    """Result of Stage 3 Breach Catalyst solver."""

    actions: tuple[CatalystUpgradeAction, ...]
    total_cost_ex: float
    remaining_hard: dict[str, int]
    remaining_overcap: dict[str, int]
    skipped_unscalable_lines: int = 0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": 3,
            "name": "breach_catalysts_13",
            "actionCount": len(self.actions),
            "actions": [a.to_dict() for a in self.actions],
            "totalCostEx": round(self.total_cost_ex, 2),
            "remainingHardDeficits": dict(self.remaining_hard),
            "remainingOvercapNeeds": dict(self.remaining_overcap),
            "skippedUnscalableLines": self.skipped_unscalable_lines,
            "warnings": list(self.warnings),
        }


def estimate_catalyst_gains(raw_text: str, catalyst_name: str, *, quality_pct: int = 20) -> tuple[dict[str, int], int]:
    """Compute exact integer stat gains of applying *catalyst_name* at *quality_pct*%, skipping `{unscalable}`."""
    gains = {k: 0 for k in ("str", "dex", "int", "fire", "cold", "lightning", "chaos")}
    skipped_unscalable = 0
    matchers = _STAT_MATCHERS.get(catalyst_name, ())
    if not matchers:
        return gains, 0
    for line in (raw_text or "").splitlines():
        st = line.strip()
        if not st:
            continue
        if "{unscalable}" in st.lower():
            skipped_unscalable += 1
            continue
        if "{enchant}" in st.lower() or "{rune}" in st.lower():
            continue
        cleaned = re.sub(r"\{[^}]+\}", "", st).strip()
        for pat, keys in matchers:
            for m in pat.finditer(cleaned):
                delta = int(int(m.group(1)) * quality_pct // 100)
                if delta > 0:
                    for k in keys:
                        gains[k] += delta
    return gains, skipped_unscalable


def apply_catalyst_to_item_raw(raw_text: str, catalyst_name: str, *, quality_pct: int = 20) -> str:
    """Inject PoB2 `Catalyst: <Descriptor>` + `CatalystQuality: <pct>` onto an uncorrupted jewellery/belt item."""
    spec = _CATALYST_BY_NAME.get(catalyst_name)
    if spec is None:
        raise ValueError(f"Unknown PoE2 catalyst: {catalyst_name!r}")
    lines = [
        ln
        for ln in (raw_text or "").splitlines()
        if not _EXISTING_QUAL_RE.match(ln.strip()) and not _CATALYST_HDR_RE.match(ln.strip())
    ]
    cat_lines = [f"Catalyst: {spec.descriptor}", f"CatalystQuality: {quality_pct}"]
    imp_idx = next((i for i, ln in enumerate(lines) if _IMPLICITS_RE.match(ln.strip())), len(lines))
    lines[imp_idx:imp_idx] = cat_lines
    return "\n".join(lines)


def _catalyst_20q_cost_ex(catalyst_name: str, price_book: PriceBook | None) -> float:
    unit = DEFAULT_CATALYST_PRICES_EX.get(catalyst_name, 8.0)
    if price_book is not None:
        v = price_book.prices_by_key.get(norm_key(catalyst_name))
        if v is not None and float(v) > 0.0:
            unit = float(v)
    # 10 catalysts on Rare/Unique jewellery to reach 20% quality (2% per catalyst in PoE2)
    return max(1.0, unit * 10.0)


def solve_catalyst_upgrades(
    slots: tuple[EquippedSlotAudit, ...],
    hard_deficits: dict[str, int],
    overcap_needs: dict[str, int],
    *,
    price_book: PriceBook | None = None,
    max_total_ex: float = 200.0,
) -> Stage3CatalystPlan:
    """Choose minimum-cost PoE2 Breach Catalysts on eligible Jewellery/Belts for remaining hard/overcap gaps."""
    rem_hard = {k: max(0, int(hard_deficits.get(k, 0))) for k in ("str", "dex", "int", "fire", "cold", "lightning", "chaos")}
    rem_over = {k: max(0, int(overcap_needs.get(k, 0))) for k in ("fire", "cold", "lightning")}
    eligible_slots = [s for s in slots if s.catalyst_eligible]
    actions: list[CatalystUpgradeAction] = []
    used_slots: set[str] = set()
    total_ex = 0.0
    total_unscalable = 0

    for phase in ("hard_floor", "overcap_buffer"):
        while True:
            need_map = rem_hard if phase == "hard_floor" else rem_over
            if not any(need_map.values()):
                break
            best: tuple[float, EquippedSlotAudit, CatalystSpec, float, dict[str, int]] | None = None
            for sl in eligible_slots:
                if sl.slot_name in used_slots:
                    continue
                for cat_name in _STAT_MATCHERS:
                    gains, unscaled = estimate_catalyst_gains(sl.raw_text, cat_name, quality_pct=20)
                    total_unscalable += unscaled
                    covered = sum(min(need_map[k], gains[k]) for k in need_map)
                    if covered <= 0:
                        continue
                    cost = _catalyst_20q_cost_ex(cat_name, price_book)
                    if total_ex + cost > max_total_ex:
                        continue
                    score = cost / covered
                    if best is None or score < best[0]:
                        best = (score, sl, _CATALYST_BY_NAME[cat_name], cost, gains)
            if best is None:
                break
            _score, sl, spec, cost, gains = best
            used_slots.add(sl.slot_name)
            total_ex += cost
            actions.append(
                CatalystUpgradeAction(
                    slot_name=sl.slot_name,
                    item_name=sl.name,
                    catalyst_name=spec.name,
                    descriptor=spec.descriptor,
                    zh_cn=spec.zh_cn,
                    quality_pct=20,
                    total_cost_ex=cost,
                    gains=gains,
                    purpose=phase,
                )
            )
            for k in rem_hard:
                rem_hard[k] = max(0, rem_hard[k] - gains[k])
            for k in rem_over:
                rem_over[k] = max(0, rem_over[k] - gains[k])

    return Stage3CatalystPlan(
        actions=tuple(actions),
        total_cost_ex=total_ex,
        remaining_hard=rem_hard,
        remaining_overcap=rem_over,
        skipped_unscalable_lines=total_unscalable,
    )


def apply_catalyst_plan_live(
    worker: Any,
    slots: tuple[EquippedSlotAudit, ...],
    plan: Stage3CatalystPlan,
) -> None:
    """Apply planned Stage 3 catalysts to live `BridgeWorker` inside a `BuildTransaction`."""
    slot_map = {s.slot_name: s for s in slots}
    for act in plan.actions:
        sl = slot_map.get(act.slot_name)
        if sl is None:
            continue
        rendered = apply_catalyst_to_item_raw(sl.raw_text, act.catalyst_name, quality_pct=act.quality_pct)
        worker.call("add_item", {"raw": rendered, "slotName": sl.slot_name})


__all__ = [
    "POE2_CATALYSTS",
    "CatalystSpec",
    "CatalystUpgradeAction",
    "Stage3CatalystPlan",
    "apply_catalyst_plan_live",
    "apply_catalyst_to_item_raw",
    "estimate_catalyst_gains",
    "solve_catalyst_upgrades",
]
