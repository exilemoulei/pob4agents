# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 0.5.5 LiquidEmotions legacy<->live name bridge, Ancient-jewel exclusion, and positive pricing (L2).

Bridges PoB2 tree.lua / NotableDBControl legacy names (`Distilled <X>`) to live 0.4/0.5 market names
(`Diluted / Liquid / Concentrated / Potent Liquid <X>`) from `PoE2/Data/LiquidEmotions.lua`, while
strictly excluding the 13 `radiusJewel == true` `Ancient ...` Time-Lost Jewel emotions.
"""

from __future__ import annotations

from typing import Any

from ..craft.price_book import PriceBook
from ..craft.weights_loader import norm_key
from ..han_fold import fold_han
from .catalog import EmotionEntry, InstillRecipe, RuneEntry, RuneInstillCatalog, load_rune_instill_catalog

# Strictly positive Patch 0.5.5 Exalted-equivalent baseline prices for the 13 amulet Liquid Emotions.
# Guarantees zero 0-Ex price poisoning even when offline or when live feeds lack depth.
DEFAULT_EMOTION_PRICES_EX: dict[str, float] = {
    "Ire": 0.5,
    "Guilt": 1.0,
    "Greed": 1.5,
    "Paranoia": 4.0,
    "Envy": 8.0,
    "Disgust": 16.0,
    "Despair": 32.0,
    "Fear": 65.0,
    "Suffering": 120.0,
    "Isolation": 220.0,
    "Melancholy": 90.0,
    "Ferocity": 140.0,
    "Contempt": 190.0,
}

# Strictly positive Exalted-equivalent baselines for notable Runes, Soul Cores, and Idols.
DEFAULT_RUNE_PRICES_EX: dict[str, float] = {
    "Soul Core of Azcapa": 160.0,
    "Soul Core of Citaqualotl": 22.0,
    "Soul Core of Puhuarte": 14.0,
    "Soul Core of Tacati": 18.0,
    "Soul Core of Jiquani": 16.0,
    "Soul Core of Zalatl": 18.0,
    "Soul Core of Xopec": 12.0,
    "Soul Core of Opiloti": 12.0,
    "Soul Core of Topotante": 12.0,
    "Soul Core of Quipolatl": 20.0,
    "Soul Core of Ticaba": 15.0,
    "Soul Core of Atmohua": 14.0,
    "Soul Core of Cholotl": 12.0,
    "Soul Core of Zantipi": 12.0,
    "Soul Core of Tzamoto": 16.0,
    "Farrul's Rune of the Hunt": 95.0,
    "Farrul's Rune of Grace": 65.0,
    "Farrul's Rune of the Chase": 75.0,
    "Saqawal's Rune of the Sky": 85.0,
    "Hedgewitch Assandra's Rune of Wisdom": 55.0,
    "The Greatwolf's Rune of Claws": 90.0,
    "The Greatwolf's Rune of Willpower": 90.0,
    "Amanamu's Gaze": 110.0,
    "Kurgal's Gaze": 95.0,
    "Tecrod's Gaze": 120.0,
    "Ulaman's Gaze": 110.0,
}

# Strictly positive Exalted-equivalent baselines for PoE2 13 Breach Catalysts (T3.6 Step 3).
DEFAULT_CATALYST_PRICES_EX: dict[str, float] = {
    "Flesh Catalyst": 8.0,
    "Neural Catalyst": 4.0,
    "Carapace Catalyst": 5.0,
    "Uul-Netol's Catalyst": 14.0,
    "Xoph's Catalyst": 6.0,
    "Tul's Catalyst": 6.0,
    "Esh's Catalyst": 6.0,
    "Chayula's Catalyst": 18.0,
    "Reaver Catalyst": 16.0,
    "Sibilant Catalyst": 12.0,
    "Skittering Catalyst": 10.0,
    "Adaptive Catalyst": 15.0,
    "Necrotic Catalyst": 8.0,
}


def is_excluded_jewel_emotion(name: str, *, catalog: RuneInstillCatalog | None = None) -> bool:
    """Return True if *name* is one of the 13 Time-Lost Jewel `Ancient ...` (`radiusJewel=true`) emotions."""
    s = (name or "").strip().lower()
    if not s:
        return False
    if s.startswith("ancient ") and "liquid " in s:
        return True
    cat = catalog or load_rune_instill_catalog()
    return any(s == ex.lower() for ex in cat.excluded_jewel_emotions)


_LIVE_EMOTION_PREFIXES: tuple[str, ...] = (
    "concentrated liquid ",
    "diluted liquid ",
    "potent liquid ",
    "liquid ",
    "distilled ",
    "浓缩液态",
    "稀释的液态",
    "稀释液态",
    "增幅液态",
    "液态",
    "精炼的",
    "精炼",
)


def normalize_emotion_name(
    raw: str,
    *,
    catalog: RuneInstillCatalog | None = None,
) -> EmotionEntry:
    """Bridge short (`Suffering`), legacy (`Distilled Suffering`), any live 0.5.5 tier name
    (`Diluted Liquid <X>`, `Liquid <X>`, `Concentrated Liquid <X>`, `Potent Liquid <X>`),
    or CN/TW emotion names into canonical `EmotionEntry`.

    Strictly rejects the 13 `Ancient ...` (`radiusJewel=true`) Time-Lost Jewel emotions.
    """
    cat = catalog or load_rune_instill_catalog()
    if is_excluded_jewel_emotion(raw, catalog=cat):
        raise ValueError(
            f"Emotion `{raw}` is a Time-Lost Jewel-only emotion (`radiusJewel=true`, Ancient prefix) "
            "and cannot be used for amulet instillation."
        )
    key = fold_han((raw or "").strip()).lower()
    found = cat.emotions_by_key.get(key)
    if found is None:
        for pref in _LIVE_EMOTION_PREFIXES:
            if key.startswith(pref):
                tail = key[len(pref) :].strip()
                if tail in cat.emotions_by_key:
                    found = cat.emotions_by_key[tail]
                    break
    if found is None:
        raise ValueError(
            f"Unknown amulet Liquid Emotion: {raw!r}. "
            "Accepted forms: short `<X>`, `Distilled <X>`, `Diluted Liquid <X>`, `Liquid <X>`, "
            "`Concentrated Liquid <X>`, `Potent Liquid <X>`, or CN/TW name."
        )
    return found


def normalize_emotion_with_warning(
    raw: str,
    *,
    catalog: RuneInstillCatalog | None = None,
) -> tuple[EmotionEntry, str | None]:
    """Normalize an amulet emotion and emit `[tier-coerced]` if a mismatched or legacy tier prefix was coerced."""
    cat = catalog or load_rune_instill_catalog()
    found = normalize_emotion_name(raw, catalog=cat)
    cleaned = fold_han((raw or "").strip()).lower()
    exact_forms = {
        found.short.lower(),
        found.live_name.lower(),
        fold_han(found.zh_cn).lower(),
        fold_han(found.zh_tw).lower(),
    }
    if cleaned in exact_forms:
        return found, None
    return (
        found,
        f"[tier-coerced] Input `{(raw or '').strip()}` was coerced to PoE2 0.5.5 tier "
        f"`{found.live_name}` (`{found.zh_cn}`, tierLevel={found.tier_level}).",
    )


def legacy_to_live_emotion(name: str, *, catalog: RuneInstillCatalog | None = None) -> str:
    """Convert `Distilled <X>` or short `<X>` to live 0.5.5 market name."""
    return normalize_emotion_name(name, catalog=catalog).live_name


def live_to_legacy_emotion(name: str, *, catalog: RuneInstillCatalog | None = None) -> str:
    """Convert live `Diluted/Liquid/Concentrated/Potent Liquid <X>` to PoB2 legacy `Distilled <X>`."""
    return normalize_emotion_name(name, catalog=catalog).legacy_name


def format_ordered_recipe(
    recipe: tuple[str, str, str],
    *,
    lang: str = "en",
    catalog: RuneInstillCatalog | None = None,
) -> str:
    """Render an amulet instillation in strict `[Slot 1 -> Slot 2 -> Slot 3]` order."""
    cat = catalog or load_rune_instill_catalog()
    parts: list[str] = []
    for short in recipe:
        em = normalize_emotion_name(short, catalog=cat)
        if lang.lower().startswith("zh") and em.zh_cn:
            parts.append(f"{em.zh_cn} ({em.live_name})")
        else:
            parts.append(em.live_name)
    return f"[Slot 1: {parts[0]} -> Slot 2: {parts[1]} -> Slot 3: {parts[2]}]"


def resolve_emotion_price_ex(
    emotion: EmotionEntry,
    price_book: PriceBook | None = None,
) -> tuple[float, str]:
    """Return strictly positive `(price_ex, source)` for an amulet Liquid Emotion."""
    if price_book is not None:
        for cand in (emotion.live_name, emotion.legacy_name):
            val = price_book.prices_by_key.get(norm_key(cand))
            if val is not None and float(val) > 0.0:
                return float(val), price_book.source_summary
    base = DEFAULT_EMOTION_PRICES_EX.get(emotion.short, 5.0)
    return max(0.25, float(base)), "baseline_positive"


def resolve_rune_price_ex(
    rune: RuneEntry,
    price_book: PriceBook | None = None,
) -> tuple[float, str]:
    """Return strictly positive `(price_ex, source)` for a Rune / Soul Core / Idol."""
    if price_book is not None:
        val = price_book.prices_by_key.get(norm_key(rune.name))
        if val is not None and float(val) > 0.0:
            return float(val), price_book.source_summary
    if rune.name in DEFAULT_RUNE_PRICES_EX:
        return DEFAULT_RUNE_PRICES_EX[rune.name], "baseline_positive"

    nl = rune.name.lower()
    if rune.is_socket_bound:
        return 120.0, "baseline_positive"
    if nl.startswith("lesser "):
        return 0.5, "baseline_positive"
    if nl.startswith("greater "):
        return 5.0, "baseline_positive"
    if nl.startswith("perfect "):
        return 25.0, "baseline_positive"
    if nl.startswith("ancient "):
        return 60.0, "baseline_positive"
    if nl.startswith("legacy of "):
        return 35.0, "baseline_positive"
    if rune.kind == "SoulCore":
        return 16.0, "baseline_positive"
    if rune.kind == "Idol":
        return 22.0, "baseline_positive"
    if rune.req_level >= 50:
        return 30.0, "baseline_positive"
    if rune.req_level >= 30:
        return 6.0, "baseline_positive"
    return 1.5, "baseline_positive"


def price_instill_recipe(
    instill: InstillRecipe,
    *,
    price_book: PriceBook | None = None,
    catalog: RuneInstillCatalog | None = None,
) -> dict[str, Any]:
    """Return ordered `[Slot 1 -> Slot 2 -> Slot 3]` cost breakdown (strictly > 0 Ex)."""
    cat = catalog or load_rune_instill_catalog()
    slots: list[dict[str, Any]] = []
    total_ex = 0.0
    sources: set[str] = set()
    for idx, short in enumerate(instill.recipe, 1):
        em = normalize_emotion_name(short, catalog=cat)
        px, src = resolve_emotion_price_ex(em, price_book=price_book)
        total_ex += px
        sources.add(src)
        slots.append(
            {
                "slot": idx,
                "short": em.short,
                "liveName": em.live_name,
                "legacyName": em.legacy_name,
                "zhCn": em.zh_cn,
                "priceEx": round(px, 3),
                "priceSource": src,
            }
        )
    return {
        "nodeId": instill.node_id,
        "name": instill.name,
        "zhCn": instill.zh_cn,
        "orderedSlots": slots,
        "orderedDisplayEn": format_ordered_recipe(instill.recipe, lang="en", catalog=cat),
        "orderedDisplayZh": format_ordered_recipe(instill.recipe, lang="zh-CN", catalog=cat),
        "totalCostEx": round(max(0.01, total_ex), 3),
        "priceSources": sorted(sources),
    }


__all__ = [
    "DEFAULT_CATALYST_PRICES_EX",
    "DEFAULT_EMOTION_PRICES_EX",
    "DEFAULT_RUNE_PRICES_EX",
    "format_ordered_recipe",
    "is_excluded_jewel_emotion",
    "legacy_to_live_emotion",
    "live_to_legacy_emotion",
    "normalize_emotion_name",
    "normalize_emotion_with_warning",
    "price_instill_recipe",
    "resolve_emotion_price_ex",
    "resolve_rune_price_ex",
]
