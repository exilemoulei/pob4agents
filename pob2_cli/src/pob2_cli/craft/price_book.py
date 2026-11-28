# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Anti-poisoning crafting price book backed by GGG CX API, poe.ninja/poe2, and positive baselines."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .models import LeverEntry
from .weights_loader import norm_key

GGG_CX_URL_TEMPLATE = "https://web.poecdn.com/api/currency-exchange/poe2/{hour_ts}"
POENINJA_EXCHANGE_URL = "https://poe.ninja/poe2/api/economy/exchange/current/overview"
POENINJA_CRAFT_TYPES: tuple[str, ...] = ("Currency", "Essences", "Ritual", "Abyss", "Verisium", "Delirium")

MIN_TRUSTED_VOLUME_DIV: float = 0.08
DEFAULT_DIVINE_RATE_EX: float = 450.0

# Strictly positive Patch 0.5.5 Exalted-equivalent baseline prices.
# Used when offline or when live feeds lack depth/quotes; NO lever may ever price <= 0 Ex!
DEFAULT_CRAFTING_PRICES_EX: dict[str, float] = {
    "Exalted Orb": 1.0,
    "Greater Exalted Orb": 4.5,
    "Perfect Exalted Orb": 38.0,
    "Orb of Transmutation": 0.05,
    "Greater Orb of Transmutation": 0.3,
    "Perfect Orb of Transmutation": 2.5,
    "Orb of Augmentation": 0.05,
    "Greater Orb of Augmentation": 0.3,
    "Perfect Orb of Augmentation": 2.5,
    "Regal Orb": 0.2,
    "Greater Regal Orb": 1.0,
    "Perfect Regal Orb": 6.5,
    "Chaos Orb": 12.0,
    "Greater Chaos Orb": 30.0,
    "Perfect Chaos Orb": 95.0,
    "Orb of Alchemy": 0.25,
    "Orb of Annulment": 18.0,
    "Fracturing Orb": 180.0,
    "Divine Orb": 450.0,
    "Vaal Orb": 0.5,
    # Active Omens
    "Omen of Sinistral Exaltation": 14.0,
    "Omen of Dextral Exaltation": 14.0,
    "Omen of Greater Exaltation": 22.0,
    "Omen of Catalysing Exaltation": 16.0,
    "Omen of Sinistral Coronation": 6.0,
    "Omen of Dextral Coronation": 6.0,
    "Omen of Sinistral Alchemy": 4.0,
    "Omen of Dextral Alchemy": 4.0,
    "Omen of Sinistral Annulment": 40.0,
    "Omen of Dextral Annulment": 40.0,
    "Omen of Greater Annulment": 45.0,
    "Omen of Sinistral Erasure": 35.0,
    "Omen of Dextral Erasure": 35.0,
    "Omen of Whittling": 140.0,
    "Omen of Sinistral Crystallisation": 32.0,
    "Omen of Dextral Crystallisation": 32.0,
    "Omen of Sinistral Necromancy": 18.0,
    "Omen of Dextral Necromancy": 18.0,
    "Omen of Light": 45.0,
    "Omen of Abyssal Echoes": 55.0,
    "Omen of the Liege": 65.0,
    "Omen of the Blackblooded": 65.0,
    "Omen of the Sovereign": 65.0,
    "Omen of Sanctification": 90.0,
    "Omen of Corruption": 12.0,
    # Desecration Bones
    "Gnawed Jawbone": 0.5,
    "Preserved Jawbone": 4.0,
    "Ancient Jawbone": 32.0,
    "Gnawed Rib": 0.5,
    "Preserved Rib": 3.5,
    "Ancient Rib": 28.0,
    "Gnawed Collarbone": 0.8,
    "Preserved Collarbone": 5.0,
    "Ancient Collarbone": 36.0,
    # Corrupted Essences
    "Essence of the Abyss": 45.0,
    "Essence of Horror": 80.0,
    "Essence of Hysteria": 80.0,
    "Essence of Insanity": 80.0,
    "Essence of Delirium": 80.0,
    "Essence of the Breach": 65.0,
}

_TIERED_CHAINS: tuple[tuple[str, str, str], ...] = (
    ("orb_of_transmutation", "greater_orb_of_transmutation", "perfect_orb_of_transmutation"),
    ("orb_of_augmentation", "greater_orb_of_augmentation", "perfect_orb_of_augmentation"),
    ("regal_orb", "greater_regal_orb", "perfect_regal_orb"),
    ("exalted_orb", "greater_exalted_orb", "perfect_exalted_orb"),
    ("chaos_orb", "greater_chaos_orb", "perfect_chaos_orb"),
)

_THIN_BOOK_FLOORS: dict[str, float] = {
    "omen_of_the_liege": 25.0,
    "omen_of_the_blackblooded": 25.0,
    "omen_of_the_sovereign": 25.0,
    "omen_of_whittling": 50.0,
    "fracturing_orb": 50.0,
}

_CX_ID_TO_KEY: dict[str, str] = {
    "exalted": "exalted_orb",
    "divine": "divine_orb",
    "chaos": "chaos_orb",
    "regal": "regal_orb",
    "annul": "orb_of_annulment",
    "alch": "orb_of_alchemy",
    "transmute": "orb_of_transmutation",
    "aug": "orb_of_augmentation",
    "vaal": "vaal_orb",
}


@dataclass(slots=True)
class PriceQuote:
    key: str
    exalted: float
    volume_div: float | None = None
    source: str = "baseline"


@dataclass(slots=True)
class PriceBook:
    prices_by_key: dict[str, float]
    divine_rate_ex: float = DEFAULT_DIVINE_RATE_EX
    used_fallback: bool = True
    source_summary: str = "offline_baseline"
    excluded_keys: frozenset[str] = field(default_factory=frozenset)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        currency_prices_ex: dict[str, float] | None = None,
        *,
        volumes_div: dict[str, float] | None = None,
        use_fallback_prices: bool = True,
        excluded: list[str] | tuple[str, ...] | None = None,
        source_summary: str | None = None,
        extra_warnings: list[str] | tuple[str, ...] = (),
    ) -> PriceBook:
        """Construct a sanitized price book guaranteeing every priced lever is strictly > 0 Ex."""
        table: dict[str, float] = {}
        warns: list[str] = list(extra_warnings)
        used_fb = False
        if use_fallback_prices:
            for name, val in DEFAULT_CRAFTING_PRICES_EX.items():
                if val > 0:
                    table[norm_key(name)] = float(val)
            used_fb = currency_prices_ex is None or len(currency_prices_ex) < 10

        vol_map = {norm_key(k): float(v) for k, v in (volumes_div or {}).items() if v is not None}

        if currency_prices_ex:
            for raw_k, raw_v in currency_prices_ex.items():
                key = norm_key(raw_k)
                key = _CX_ID_TO_KEY.get(key, key)
                if raw_v is None or float(raw_v) <= 0.0:
                    warns.append(f"Ignored non-positive quote for {raw_k!r} ({raw_v!r})")
                    continue
                price = float(raw_v)
                vol = vol_map.get(key)
                is_thin = vol is not None and vol < MIN_TRUSTED_VOLUME_DIV
                if is_thin:
                    floor_p = _thin_floor_for_key(key)
                    if floor_p is not None and price < floor_p:
                        warns.append(
                            f"Thin-volume ({vol:.3f} Div) quote for {raw_k!r} ({price:.2f} Ex) "
                            f"clamped to anti-poisoning floor {floor_p:.2f} Ex"
                        )
                        price = floor_p
                table[key] = price

        _enforce_tiered_monotonicity(table, vol_map)
        table.setdefault("exalted_orb", 1.0)
        div_rate = table.get("divine_orb") or DEFAULT_DIVINE_RATE_EX
        if div_rate <= 0:
            div_rate = DEFAULT_DIVINE_RATE_EX

        excl = frozenset(norm_key(x) for x in (excluded or ()) if x and x.strip())
        src = source_summary or ("offline_baseline" if used_fb else "live_override")
        return cls(
            prices_by_key=table,
            divine_rate_ex=div_rate,
            used_fallback=used_fb,
            source_summary=src,
            excluded_keys=excl,
            warnings=warns,
        )

    def is_excluded(self, lever: LeverEntry) -> bool:
        if not self.excluded_keys:
            return False
        keys = (lever.lever_id, norm_key(lever.name), norm_key(lever.category))
        return any(k in self.excluded_keys for k in keys)

    def price_of(self, lever: LeverEntry, *, use_category_fallback: bool = True) -> float | None:
        direct = self.prices_by_key.get(lever.lever_id) or self.prices_by_key.get(norm_key(lever.name))
        if direct is not None and direct > 0.0:
            return direct
        if not use_category_fallback:
            return None
        if lever.category == "essence":
            if lever.name.startswith("Lesser "):
                return 0.2
            if lever.name.startswith("Greater "):
                return 3.5
            return 1.0
        if lever.category == "perfect_or_corrupted_essence":
            return 42.0
        if lever.category == "alloy":
            return 55.0
        if lever.category == "liquid_emotion":
            return 15.0
        return None

    def action_cost(
        self,
        lever: LeverEntry,
        omens: tuple[LeverEntry, ...] | list[LeverEntry],
        *,
        use_category_fallback: bool = True,
    ) -> float:
        """Compute strictly positive total cost in Exalted Orbs, or raise ValueError."""
        base_p = self.price_of(lever, use_category_fallback=use_category_fallback)
        if base_p is None or base_p <= 0.0:
            raise ValueError(
                f"Missing positive price for {lever.name!r}; refusing to price unknown currencies at 0 Ex."
            )
        total = base_p
        for om in omens:
            op = self.price_of(om, use_category_fallback=use_category_fallback)
            if op is None or op <= 0.0:
                raise ValueError(
                    f"Missing positive price for {om.name!r}; refusing to price unknown currencies at 0 Ex."
                )
            total += op
        return total


def _thin_floor_for_key(key: str) -> float | None:
    if key in _THIN_BOOK_FLOORS:
        return _THIN_BOOK_FLOORS[key]
    if key.endswith("_alloy"):
        return 25.0
    if key.startswith("perfect_essence_of_"):
        return 15.0
    return None


def _enforce_tiered_monotonicity(
    table: dict[str, float],
    volumes_div: dict[str, float],
) -> None:
    """Prevent illiquid thin-book inversions where Greater < Basic or Perfect < Greater."""
    for low_k, mid_k, high_k in _TIERED_CHAINS:
        p_low = table.get(low_k)
        p_mid = table.get(mid_k)
        p_high = table.get(high_k)
        if p_low is not None and p_mid is not None and p_mid < p_low:
            if volumes_div.get(mid_k, 0.0) < 0.5:
                p_mid = round(p_low * 1.25, 4)
                table[mid_k] = p_mid
        if p_mid is not None and p_high is not None and p_high < p_mid:
            if volumes_div.get(high_k, 0.0) < 0.5:
                table[high_k] = round(p_mid * 1.5, 4)


def parse_ggg_cx_snapshot(
    payload: dict[str, Any],
    *,
    league: str | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Parse official unauthenticated GGG Currency Exchange snapshot into `(prices_ex, volumes_div)`."""
    markets = payload.get("markets")
    if not isinstance(markets, list):
        return {}, {}
    prices: dict[str, float] = {"exalted_orb": 1.0}
    volumes_div: dict[str, float] = {}
    div_per_ex: float | None = None

    # Pass 1: find direct exalted pairs & divine/exalted reference
    for m in markets:
        if not isinstance(m, dict):
            continue
        if league and str(m.get("league", "")).lower() != league.lower():
            continue
        pair = str(m.get("market_id") or "")
        if "|" not in pair:
            continue
        left, right = [p.strip().lower() for p in pair.split("|", 1)]
        vol_map = m.get("volume_traded") if isinstance(m.get("volume_traded"), dict) else {}
        hi = m.get("highest_ratio") if isinstance(m.get("highest_ratio"), dict) else {}
        lo = m.get("lowest_ratio") if isinstance(m.get("lowest_ratio"), dict) else {}
        v_left = float(vol_map.get(left) or 0.0)
        v_right = float(vol_map.get(right) or 0.0)
        if v_left <= 0 or v_right <= 0:
            continue
        # Volume-weighted implied price from executed volumes
        ratio_r_per_l = v_right / v_left
        if ratio_r_per_l <= 0:
            continue
        # Sanity cross-check against highest/lowest if present
        if hi and lo and left in hi and right in hi and float(hi[left] or 0) > 0:
            pass
        if right == "exalted":
            k = _CX_ID_TO_KEY.get(norm_key(left), norm_key(left))
            prices[k] = ratio_r_per_l
            if left == "divine":
                div_per_ex = ratio_r_per_l
            volumes_div[k] = v_right / (div_per_ex or DEFAULT_DIVINE_RATE_EX)
        elif left == "exalted":
            k = _CX_ID_TO_KEY.get(norm_key(right), norm_key(right))
            prices[k] = v_left / v_right
            if right == "divine":
                div_per_ex = v_left / v_right
            volumes_div[k] = v_left / (div_per_ex or DEFAULT_DIVINE_RATE_EX)

    return prices, volumes_div


def parse_poeninja_exchange_payload(
    payload: dict[str, Any],
    *,
    divine_rate_ex: float = DEFAULT_DIVINE_RATE_EX,
) -> tuple[dict[str, float], dict[str, float]]:
    """Parse a `poe.ninja/poe2` Currency Exchange JSON overview into `(prices_ex, volumes_div)`."""
    lines = payload.get("lines")
    items = payload.get("items")
    if not isinstance(lines, list):
        return {}, {}
    core = payload.get("core") if isinstance(payload.get("core"), dict) else {}
    rates = core.get("rates") if isinstance(core.get("rates"), dict) else {}
    ex_per_primary = float(rates.get("exalted") or divine_rate_ex)
    id_to_name: dict[str, str] = {}
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("id") and it.get("name"):
                id_to_name[str(it["id"])] = str(it["name"])

    prices: dict[str, float] = {}
    vols: dict[str, float] = {}
    for row in lines:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "")
        primary_val = row.get("primaryValue")
        if not cid or primary_val is None or float(primary_val) <= 0:
            continue
        ex_val = float(primary_val) * ex_per_primary
        vol_primary = float(row.get("volumePrimaryValue") or 0.0)
        key = _CX_ID_TO_KEY.get(norm_key(cid), norm_key(cid))
        prices[key] = ex_val
        vols[key] = vol_primary
        if cid in id_to_name:
            nkey = norm_key(id_to_name[cid])
            prices[nkey] = ex_val
            vols[nkey] = vol_primary
    return prices, vols


def fetch_live_price_book(
    league: str = "Standard",
    *,
    offline: bool = False,
    excluded: list[str] | tuple[str, ...] | None = None,
    timeout_s: float = 8.0,
) -> PriceBook:
    """Fetch live GGG CX + poe.ninja/poe2 prices with automatic anti-poisoning & offline fallback."""
    if offline:
        return PriceBook.build(
            None, use_fallback_prices=True, excluded=excluded, source_summary="offline_baseline"
        )

    try:
        import httpx
    except ImportError:
        return PriceBook.build(
            None, use_fallback_prices=True, excluded=excluded, source_summary="offline_no_httpx"
        )

    merged_prices: dict[str, float] = {}
    merged_vols: dict[str, float] = {}
    sources: list[str] = []
    fetch_warnings: list[str] = []
    headers = {"User-Agent": "pob24agents/0.1 (+https://github.com/exilemoulei/pob4agents)"}

    # 1. Official unauthenticated GGG Currency Exchange hourly snapshot
    hour_ts = int(time.time() // 3600) * 3600 - 3600
    try:
        with httpx.Client(timeout=timeout_s, headers=headers) as client:
            resp = client.get(GGG_CX_URL_TEMPLATE.format(hour_ts=hour_ts))
            if resp.status_code == 200:
                p_cx, v_cx = parse_ggg_cx_snapshot(resp.json(), league=league)
                if len(p_cx) > 1:
                    merged_prices.update(p_cx)
                    merged_vols.update(v_cx)
                    sources.append("ggg_cx_api")
            # 2. poe.ninja/poe2 exchange overviews
            div_est = merged_prices.get("divine_orb") or DEFAULT_DIVINE_RATE_EX
            for cat in POENINJA_CRAFT_TYPES:
                url = f"{POENINJA_EXCHANGE_URL}?league={quote(league, safe='')}&type={cat}"
                r2 = client.get(url)
                if r2.status_code == 200:
                    p_n, v_n = parse_poeninja_exchange_payload(r2.json(), divine_rate_ex=div_est)
                    for k, val in p_n.items():
                        merged_prices.setdefault(k, val)
                    for k, vol in v_n.items():
                        merged_vols.setdefault(k, vol)
                    if p_n and "poe.ninja" not in sources:
                        sources.append("poe.ninja")
    except Exception as exc:
        fetch_warnings.append(
            f"[price-book-degraded] Live currency feed fetch failed ({exc}); fell back to offline baseline prices."
        )

    return PriceBook.build(
        merged_prices or None,
        volumes_div=merged_vols,
        use_fallback_prices=True,
        excluded=excluded,
        source_summary="+".join(sources) if sources else "offline_baseline",
        extra_warnings=fetch_warnings,
    )
