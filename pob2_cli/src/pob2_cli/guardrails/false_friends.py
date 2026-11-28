# SPDX-License-Identifier: GPL-3.0-only
"""Provenance-backed PoE2 0.5.5 false-friend advisories and 0.1->0.5.5 version-drift blocks (L0)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .matcher import GuardrailScope

S = GuardrailScope


@dataclass(frozen=True)
class FalseFriendRule:
    rule_id: str
    scopes: frozenset[GuardrailScope]
    source_ref: str
    category: str = "false_friend"  # "false_friend" | "legacy_patch"
    patch_version: str | None = None
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    en_exact: tuple[str, ...] = ()
    zh_exact: tuple[str, ...] = ()
    exclude_en_re: re.Pattern[str] | None = None
    exclude_zh_compounds: tuple[str, ...] = ()
    summary: str = ""
    poe1_behavior: str = ""
    poe2_behavior: str = ""


@dataclass(frozen=True)
class VersionDriftBlockRule:
    """Hard-blocking rules for PoE2 levers removed in 0.4/0.5 (exits 1 via LegacyPatchTrapError)."""

    rule_id: str
    patch_version: str
    scopes: frozenset[GuardrailScope]
    source_ref: str
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    error: str = ""
    remediation: str = ""
    equivalent_command: str | None = None


VERSION_DRIFT_BLOCKS_POE2: tuple[VersionDriftBlockRule, ...] = (
    VersionDriftBlockRule(
        rule_id="POE2_REMOVED_HOMOGENISING_OMENS",
        patch_version="0.4.0",
        scopes=frozenset({S.DATA_QUERY, S.STAT_QUERY, S.ITEM_RAW, S.COMMAND}),
        source_ref="PoE2 Patch 0.4.0 Notes; pob2_cli/data/weights_overrides.json",
        en_words=(
            "omen of homogenising exaltation",
            "omen of homogenizing exaltation",
            "omen of homogenising coronation",
            "omen of homogenizing coronation",
        ),
        zh_phrases=("同质化崇高预兆", "同质化富豪预兆", "同质化预兆"),
        error="Omen of Homogenising Exaltation & Coronation were removed from drop pools in PoE2 Patch 0.4.0.",
        remediation="In 0.5.5, use Greater/Perfect Exalt + Sinistral/Dextral Exaltation, Perfect Essences/Alloys (max 1 Crafted), or Abyssal Desecration.",
        equivalent_command="pob2 craft plan --base <base> --want <mod>",
    ),
    VersionDriftBlockRule(
        rule_id="POE2_REMOVED_RECOMBINATOR",
        patch_version="0.5.0",
        scopes=frozenset({S.DATA_QUERY, S.STAT_QUERY, S.ITEM_RAW, S.COMMAND}),
        source_ref="PoE2 Patch 0.5.0 Notes; pob2_cli/data/weights_overrides.json",
        en_words=("recombinator", "omen of recombination"),
        zh_phrases=("重组器", "重组预兆", "重組器"),
        error="Recombinators and Omen of Recombination were phased out in PoE2 Patch 0.5.0.",
        remediation="In 0.5.5, use 4-mod Fracturing Orb checkpoints + Verisium Alloys / Perfect Essences (max 1 Crafted) + Abyssal Desecration.",
        equivalent_command="pob2 craft plan --base <base> --want <mod>",
    ),
)

SAME_NAME_FALSE_FRIENDS_POE2: tuple[FalseFriendRule, ...] = (
    FalseFriendRule(
        rule_id="FF_CHARGES_ZERO_PASSIVE_BONUS",
        scopes=frozenset({S.CONFIG_VAR, S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        source_ref="PoE2/Modules/CalcSetup.lua",
        en_words=("endurance charge", "endurance charges", "frenzy charge", "frenzy charges", "power charge", "power charges"),
        zh_phrases=("耐力球", "红球", "狂怒球", "绿球", "暴击球", "蓝球"),
        summary="In PoE2, Endurance/Frenzy/Power Charges grant ZERO inherent passive stats per charge (consumed by skills or explicit 'per Charge' mods).",
        poe1_behavior="4% Phys/Ele DR per Endurance; 4% more dmg/speed per Frenzy; +50% Crit per Power.",
        poe2_behavior="0% passive DR/Damage/Crit (`CalcSetup.lua` only sets base Max=3).",
    ),
    FalseFriendRule(
        rule_id="FF_DAMAGE_CONVERSION_POST_ONLY",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        source_ref="PoE2/Modules/CalcOffence.lua; RePoE keywords.min.json[Conversion]",
        en_words=("damage conversion", "converted to", "convert to", "avatar of fire"),
        zh_phrases=("伤害转化", "转化为闪电", "转化为冰霜", "转化为火焰", "转化为混沌", "火之化身"),
        summary="PoE2 Damage Conversion is strictly 2-step (Skill -> Gear/Tree) and scales ONLY with the FINAL damage type's global `% increased/% more`!",
        poe1_behavior="Converted damage scales with both source and target `% increased`.",
        poe2_behavior="Pre-conversion global `% increased Physical` contributes 0% to converted Elemental damage.",
    ),
    FalseFriendRule(
        rule_id="FF_CI_ES_BLEED_POISON",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        source_ref="PoE2/Modules/CalcDefence.lua:4075-4076; RePoE keywords.min.json[EnergyShield,ChaosInoculation]",
        en_exact=("ci",),
        en_words=("chaos inoculation", "bleed", "bleeding", "poison", "chaos damage"),
        zh_phrases=("异灵之体", "流血", "中毒"),
        summary="PoE2 inverted ES bypass rules: non-poison Chaos hits remove 2x ES (do NOT bypass ES); BOTH Poison and Bleeding bypass ES directly to Life; CI grants Chaos + Bleed immunity.",
        poe1_behavior="Chaos bypasses ES; Bleed damages ES first.",
        poe2_behavior="Chaos hits deal 2x damage to ES; Bleed & Poison bypass ES; CI is immune to both Chaos and Bleeding.",
    ),
    # CORRECTED 0.3+/0.5.5 SUPPORT GEM RULE (fixes stale 0.1 global-limit-1 false positive!)
    FalseFriendRule(
        rule_id="FF_SUPPORT_GEM_055_RULES",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY}),
        source_ref="PoE2/Classes/SkillsTab.lua:1593 (`isLineage`); PoE2/Data/Gems.lua (`gemFamily`); Patch 0.3.0 Notes",
        category="legacy_patch",
        patch_version="0.3.0->0.5.5",
        en_words=("support gem", "awakened", "level 21 support", "one support per character", "lineage support"),
        zh_phrases=("辅助宝石", "强辅", "觉醒辅助", "全局限一", "血脉辅助"),
        summary=(
            "Since Patch 0.3.0 (live in 0.5.5), standard Support Gems (Tier I/II/III) are NO LONGER globally limited to 1 per character! "
            "Only `isLineage` Lineage Supports and global-benefit reservation supports are capped at 1 per character; "
            "within the SAME skill group, supports are mutually exclusive by `gemFamily`."
        ),
        poe1_behavior="PoE1 supports level 1..20 (Awakened 1..5); PoE2 0.1.0 limited all supports to 1 per character.",
        poe2_behavior="0.5.5: Standard supports can repeat across different skills; max 1 per `gemFamily` per skill; max 1 per character ONLY for `isLineage` & global reservation supports.",
    ),
    # CORRECTED ARMOUR BEFORE ELEMENTAL RESISTANCES RULE
    FalseFriendRule(
        rule_id="FF_ARMOUR_BEFORE_RESISTANCE_AND_BLOCK_50",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        source_ref="PoE2/Modules/CalcDefence.lua:61-63,2364-2367; PoE2/Data/Misc.lua:150 (`ArmourRatio=10`, Block cap 50%)",
        en_words=("armour", "block chance", "raise shield", "armour applies to elemental"),
        zh_phrases=("护甲", "格挡几率", "架盾", "护甲作用于元素"),
        summary=(
            "PoE2 Armour uses `A / (A + 10 * RawHit)` and mitigates BEFORE Elemental/Chaos Resistances when "
            "`% of Armour also applies to Elemental Damage` is active (`CalcDefence.lua:2364-2367`) — compare Armour vs Resistance against the pre-resistance raw hit! "
            "Base passive Block cap is 50% (`Misc.lua:150`)."
        ),
        poe1_behavior="PoE1 Armour divisor is 5x and applies AFTER Elemental Resistance; base Block cap is 75%.",
        poe2_behavior="PoE2 Armour divisor is 10x and applies BEFORE Elemental Resistance (`Taken = Raw*(1-DR(A,10*Raw))*(1-Res)`); base passive Block cap is 50%.",
    ),
    FalseFriendRule(
        rule_id="FF_RESISTANCE_CAP_AND_OVERCAP_BUFFER",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        source_ref="PoE2/Modules/CalcDefence.lua:2360-2410; PoE2/Modules/ConfigOptions.lua (`resistancePenalty`)",
        en_words=("elemental resistance", "fire resistance", "cold resistance", "lightning resistance", "resistance overcap"),
        zh_phrases=("元素抗性", "三抗配平", "抗性配平", "抗性溢出"),
        summary=(
            "抗性目标永远是配满各自上限（`Res >= MaxRes`）并在条件允许时预留减抗图/诅咒/曝露超上限余量（Overcap Buffer），"
            "绝不强求三抗数值相等“配平”或惩罚健康溢出！"
        ),
        poe1_behavior="Overcap protects against curses/exposure (only Wise Oak cared about equal resists).",
        poe2_behavior="Cap each resist (`>= MaxRes`) and keep Overcap Buffer (`+15%~+30%`) when feasible; never trade away healthy overcap to equalize resistances.",
    ),
    FalseFriendRule(
        rule_id="FF_STALE_01_DRIFT_GRIM_FEAST_RUNES_TABLETS",
        scopes=frozenset({S.DATA_QUERY, S.GEM_NAME, S.STAT_QUERY, S.ITEM_RAW}),
        source_ref="PoE2/Data/ModRunes.lua (`isSocketBound`); RePoE keywords.min.json[Tablet,Augment]; Patch 0.2-0.5 Notes",
        category="legacy_patch",
        patch_version="0.1.0->0.5.5",
        en_words=("grim feast", "lost tower", "precursor tower", "tier 1 is worst", "runes are permanent"),
        zh_phrases=("冷酷盛宴", "失落之塔", "符文不可覆盖", "T1最差"),
        summary=(
            "Stale 0.1 PoE2 rule check: in 0.5.5, (1) Grim Feast no longer overflows ES (reworked into minion revival); "
            "(2) standard Runes/Soul Cores CAN be overwritten (only `isSocketBound` cannot); "
            "(3) Tablets go directly into the Map Device (not Lost Towers); (4) Tier 1 is BEST since Patch 0.2.1."
        ),
        poe1_behavior="PoE2 0.1.0 launch rules (Grim Feast ES overflow, non-overwritable runes, tower tablets, inverted T1).",
        poe2_behavior="PoE2 0.5.5 live rules verified in PoB2 Data/*.lua and RePoE 0.5.5.",
    ),
    FalseFriendRule(
        rule_id="FF_MOM_AND_EB_KEYSTONES",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY}),
        source_ref="RePoE keywords.min.json[MindOverMatter,EldritchBattery]",
        en_exact=("mom", "eb"),
        en_words=("mind over matter", "eldritch battery"),
        zh_phrases=("心灵升华", "心灵昇华", "异能魔力"),
        summary="PoE2 `Mind Over Matter` takes 100% of damage from Mana before Life with 50% less Mana Recovery; `Eldritch Battery` converts 100% of ES to Mana and doubles Mana costs.",
        poe1_behavior="MoM: 40% from Mana; EB: ES protects Mana.",
        poe2_behavior="MoM: 100% from Mana + 50% less Mana Recovery; EB: 100% ES -> Mana + 2x Mana Costs.",
    ),
    FalseFriendRule(
        rule_id="FF_SCEPTRE_AND_CHARM",
        scopes=frozenset({S.SLOT, S.CATEGORY, S.DATA_QUERY, S.ITEM_RAW}),
        source_ref="PoE2/Data/Bases/sceptre.lua; PoE2/Data/ModCharm.lua",
        en_words=("sceptre", "sceptres", "charm", "charms"),
        zh_phrases=("权杖", "短杖", "护符", "咒符"),
        exclude_zh_compounds=("琥珀护身符", "翠玉护身符", "海玉护身符", "帝金护身符", "护身符"),
        summary="PoE2 Sceptres (`权杖`) grant Spirit & Aura/Minion mods (NOT weapon attacks); Charms (`护符`/`咒符`) are Belt `Charm 1..3` auto-trigger utility slots.",
        poe1_behavior="PoE1 Sceptres are melee weapons; Charms were 3.23 Wildwood ascendancy passives.",
        poe2_behavior="PoE2 Sceptres are pure Spirit/Command hands; Charms replace Utility Flasks in Belt slots.",
    ),
)
