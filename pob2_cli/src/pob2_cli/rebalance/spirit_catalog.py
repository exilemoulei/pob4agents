# SPDX-License-Identifier: GPL-3.0-only
"""Static PoE2 0.5.5 Spirit-reserving skill catalog, campaign Spirit quests, and offline pack options (L2)."""

from __future__ import annotations

from typing import Any

from ..rune.catalog import RuneInstillCatalog, load_rune_instill_catalog

CAMPAIGN_SPIRIT_QUESTS: tuple[tuple[str, str, int], ...] = (
    ("questAct 1FreythornKing In The Mists", "Act 1 Freythorn: King in the Mists", 30),
    ("questAct 3Azak BogIgnagduk", "Act 3 Azak Bog: Ignagduk", 30),
    ("questInterlude 3Kriar VillageLythara", "Interlude 3 Kriar Village: Lythara", 40),
)

# All 67 PoE2 0.5.5 active skills carrying `spiritReservationFlat` / `spiritReservationPercent` in Data/Skills/*.lua
SPIRIT_ACTIVE_NAMES: frozenset[str] = frozenset(
    {
        "alchemist's boon", "archmage", "arctic armour", "attrition", "barkskin", "berserk",
        "blasphemy", "blink", "briarpatch", "charge regulation", "combat frenzy", "convalescence",
        "defiance banner", "dread banner", "elemental conflux", "eternal rage",
        "explosive transmutation", "ghost dance", "grim feast", "herald of ash", "herald of blood",
        "herald of ice", "herald of plague", "herald of thunder", "iron ward", "leylines",
        "lingering illusion", "magma barrier", "mana remnants", "barrier invocation",
        "cast on critical", "cast on dodge", "cast on elemental ailment", "cast on melee kill",
        "cast on melee stun", "cast on minion death", "elemental invocation", "feral invocation",
        "mirage archer", "reaper's invocation", "remnants of kalguur", "overwhelming presence",
        "plague bearer", "raging spirits", "ravenous swarm", "rhoa mount", "sacrifice",
        "savage fury", "scavenged plating", "shard scavenger", "siphon elements",
        "skeletal arsonist", "skeletal arsonist minion", "skeletal brute", "skeletal brute minion",
        "skeletal cleric", "skeletal cleric minion", "skeletal frost mage", "skeletal frost mage minion",
        "skeletal reaver", "skeletal reaver minion", "skeletal sniper", "skeletal sniper minion",
        "skeletal storm mage", "skeletal storm mage minion", "skeletal warrior", "skeletal warrior minion",
        "bind spectre", "time of need", "trail of caltrops", "trinity",
        "verisium manifestations", "war banner", "wind dancer", "withering presence", "wolf pack",
    }
)

SPIRIT_ACTIVE_IDS: frozenset[str] = frozenset(
    {
        "alchemistsboonplayer", "archmageplayer", "arcticarmourplayer", "attritionplayer",
        "barkskinplayer", "berserkplayer", "blasphemyplayer", "blinkreservationplayer",
        "briarpatchplayer", "chargeregulationplayer", "combatfrenzyplayer", "convalescenceplayer",
        "defiancebannerreservationplayer", "dreadbannerreservationplayer", "elementalconfluxplayer",
        "eternalrageplayer", "explosivetransmutationplayer", "ghostdanceplayer", "grimfeastplayer",
        "heraldofashplayer", "heraldofbloodplayer", "heraldoficeplayer", "heraldofplagueplayer",
        "heraldofthunderplayer", "ironwardplayer", "leylinesplayer", "lingeringillusionplayer",
        "magmabarrierplayer", "manaremnantsplayer", "metabarrierinvocationplayer",
        "metacastoncritplayer", "metacastondodgeplayer", "metacastonelementalailmentplayer",
        "metacastonmeleekillplayer", "metacastonmeleestunplayer", "metacastonminiondeathplayer",
        "metaelementalinvocationplayer", "metaferalinvocationplayer", "metamiragearcherplayer",
        "metareapersinvocationplayer", "metaremnantsofkalguurplayer", "overwhelmingpresenceplayer",
        "plaguebearerplayer", "ragingspiritsplayer", "ravenousswarmplayer", "rhoamountplayer",
        "sacrificeplayer", "savagefuryplayer", "scavengedplatingplayer", "shardscavengerplayer",
        "siphonelementsplayer", "summonskeletalarsonistsplayer", "summonskeletalbrutesplayer",
        "summonskeletalclericsplayer", "summonskeletalfrostmagesplayer", "summonskeletalreaversplayer",
        "summonskeletalsnipersplayer", "summonskeletalstormmagesplayer", "summonskeletalwarriorsplayer",
        "summonspectreplayer", "timeofneedplayer", "trailofcaltropsplayer", "trinityplayer",
        "verisiummanifestationplayer", "warbannerreservationplayer", "winddancerplayer",
        "witheringpresenceplayer", "wolfpackplayer",
    }
)

OFFLINE_SPIRIT_BUFF_PRESETS: tuple[tuple[str, str, int, float, float], ...] = (
    ("Wind Dancer", "buff_wind_dancer", 30, 8.0, 26.0),
    ("Ghost Dance", "buff_ghost_dance", 30, 0.0, 28.0),
    ("Herald of Ice", "buff_hoi", 30, 22.0, 4.0),
    ("Herald of Thunder", "buff_hot", 30, 24.0, 2.0),
    ("Herald of Ash", "buff_hoa", 30, 18.0, 0.0),
    ("Archmage", "buff_archmage", 100, 75.0, 0.0),
    ("Blasphemy", "buff_blasphemy", 60, 28.0, 18.0),
    ("Cast on Critical", "buff_coc", 100, 68.0, 0.0),
    ("Grim Feast", "buff_grim_feast", 30, 0.0, 32.0),
    ("Charge Regulation", "buff_charge_reg", 30, 20.0, 12.0),
)


def is_spirit_active_gem(gem: dict[str, Any]) -> bool:
    """Return True if *gem* is one of the 67 PoE2 Spirit-reserving active skills."""
    sid = str(gem.get("skillId") or "").strip().lower()
    if sid and sid in SPIRIT_ACTIVE_IDS:
        return True
    name = str(gem.get("nameSpec") or gem.get("name") or "").strip().lower()
    return name in SPIRIT_ACTIVE_NAMES or name.startswith(("spectre:", "herald of ", "skeletal "))


def iter_offline_pack_entries(
    *,
    metric: str = "CombinedDPS",
    catalog: RuneInstillCatalog | None = None,
) -> list[tuple[str, str, str, int, float, float, float]]:
    """Yield `(option_id, choice_group, label, cost, dps, ehp, value)` tuples for offline forward packing."""
    cat = catalog or load_rune_instill_catalog()
    is_ehp = any(k in metric.lower() for k in ("ehp", "def", "life", "surv"))
    rows: list[tuple[str, str, str, int, float, float, float]] = []
    for label, grp, cost, dps_v, ehp_v in OFFLINE_SPIRIT_BUFF_PRESETS:
        rows.append((grp, grp, f"{label} ({cost} Spirit)", cost, dps_v, ehp_v, ehp_v if is_ehp else dps_v))

    by_family: dict[str, list[Any]] = {}
    for sp in cat.spirit_supports:
        fam = sp.name.rsplit(" ", 1)[0] if sp.name.endswith((" I", " II", " III")) else sp.name
        by_family.setdefault(fam, []).append(sp)
    for fam, tiers in sorted(by_family.items()):
        for sp in tiers:
            low = sp.name.lower()
            dps_v = 9.0 if ("precision" in low or "direstrike" in low or "herald" in low) else 3.0
            ehp_v = (
                11.0
                if any(k in low for k in ("vitality", "cannibalism", "herbalism", "clarity", "strong", "warm", "cool"))
                else 4.0
            )
            if sp.name.endswith(" II"):
                dps_v *= 1.5
                ehp_v *= 1.5
            rows.append(
                (
                    f"sup_{sp.skill_id}",
                    f"sup_{fam}",
                    f"{sp.name} ({sp.spirit_cost} Spirit)",
                    sp.spirit_cost,
                    dps_v,
                    ehp_v,
                    ehp_v if is_ehp else dps_v,
                )
            )
    return rows


__all__ = [
    "CAMPAIGN_SPIRIT_QUESTS",
    "OFFLINE_SPIRIT_BUFF_PRESETS",
    "SPIRIT_ACTIVE_IDS",
    "SPIRIT_ACTIVE_NAMES",
    "is_spirit_active_gem",
    "iter_offline_pack_entries",
]
