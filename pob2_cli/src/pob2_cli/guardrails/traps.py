# SPDX-License-Identifier: GPL-3.0-only
"""Provenance-backed Table A (PoE1 traps in pob2) and Table D (PoE2 traps in pob1) (L0)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .matcher import GuardrailScope

S = GuardrailScope


@dataclass(frozen=True)
class TrapRule:
    rule_id: str
    scopes: frozenset[GuardrailScope]
    source_ref: str
    en_exact: tuple[str, ...] = ()
    zh_exact: tuple[str, ...] = ()
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    regex: re.Pattern[str] | None = None
    exclude_en_re: re.Pattern[str] | None = None
    exclude_zh_compounds: tuple[str, ...] = ()
    error: str = ""
    remediation: str = ""
    equivalent_command: str | None = None


POE1_ONLY_TRAPS_FOR_POB2: tuple[TrapRule, ...] = (
    TrapRule(
        rule_id="POE1_SPELL_SUPPRESSION",
        scopes=frozenset({S.STAT_QUERY, S.CONFIG_VAR, S.ITEM_RAW, S.CALC_QUERY, S.DATA_QUERY}),
        source_ref="PoE2/Data/Misc.lua:111 (BasePercentDamageDeflected=40); PoE2/Data/ModItem.lua",
        en_words=("spell suppression", "suppress spell damage", "suppressed spell damage", "chance to suppress spell damage"),
        zh_phrases=("法术压制", "法术压抑", "法压"),
        error="Spell Suppression (`法术压制`/`法压`) does not exist in PoE2.",
        remediation="PoE2 replaced Spell Suppression with Deflection Rating (`偏转值`, base 40% damage prevented, 95% cap).",
        equivalent_command='pob2 ground "deflection"',
    ),
    TrapRule(
        rule_id="POE1_CRAFTING_BENCH",
        scopes=frozenset({S.STAT_QUERY, S.DATA_QUERY, S.ITEM_RAW, S.CONFIG_VAR}),
        source_ref="PoE2 Patch 0.1-0.5.5; poe2db.tw/us/Crafting",
        en_words=(
            "crafting bench", "bench craft", "benchcrafted", "master craft",
            "can have up to 3 crafted modifiers", "prefixes cannot be changed", "suffixes cannot be changed",
            "orb of scouring", "orb of alteration",
        ),
        zh_phrases=(
            "工艺台", "藏身处工艺", "大师工艺", "附魔工艺台",
            "最多可以有 3 个工艺词缀", "前缀无法被改变", "后缀无法被改变", "重铸石", "改造石",
        ),
        error="PoE2 has no Hideout Crafting Bench, no Orb of Scouring, and no Orb of Alteration.",
        remediation="Use PoE2 0.5.5 tiered orbs, Essences/Alloys (max 1 Crafted mod), Desecration Bones (max 1), and Ritual Omens via `pob2 craft`.",
        equivalent_command="pob2 craft plan --base <base> --want <mod>",
    ),
    TrapRule(
        rule_id="POE1_FLASK_SLOTS_3_5",
        scopes=frozenset({S.SLOT}),
        source_ref="PoE2/Data/Bases/flask.lua; PoE2/Classes/ImportTab.lua",
        en_exact=("flask 3", "flask 4", "flask 5", "flask3", "flask4", "flask5"),
        zh_exact=("药剂 3", "药剂 4", "药剂 5", "药剂3", "药剂4", "药剂5"),
        error="PoE2 only has 2 flask slots (`Flask 1` Life, `Flask 2` Mana) plus belt `Charm 1..3`.",
        remediation="Use `Flask 1..2` and `Charm 1..3` (`护符`/`咒符`).",
        equivalent_command='pob2 ground "charms"',
    ),
    TrapRule(
        rule_id="POE1_UTILITY_FLASKS",
        scopes=frozenset({S.ITEM_RAW, S.DATA_QUERY, S.STAT_QUERY}),
        source_ref="PoE2/Data/Bases/flask.lua; RePoE keywords.min.json[Charm]",
        en_words=(
            "quicksilver flask", "silver flask", "granite flask", "jade flask", "quartz flask",
            "amethyst flask", "diamond flask", "bismuth flask", "stibnite flask", "sulphur flask",
            "basalt flask", "progenesis", "oriath's end", "dying sun", "bottled faith",
            "taste of hate", "cinderswallow urn", "magic utility flasks constantly apply",
        ),
        zh_phrases=(
            "水银药剂", "跑速药", "真银药剂", "坚岩药剂", "翠玉药剂", "石英药剂",
            "紫晶药剂", "宝钻药剂", "硫磺药剂", "石化药剂", "源生", "瓶中信仰", "恨意",
        ),
        exclude_en_re=re.compile(
            r"legacy\s+of\s+(?:quicksilver|amethyst|basalt|bismuth|diamond|granite|jade|silver|stibnite|sulphur)",
            re.IGNORECASE,
        ),
        error="PoE1 Utility Flasks (`Quicksilver`/`Granite`/`Progenesis`, etc.) do not exist in PoE2.",
        remediation="Use Life/Mana Flasks and Belt Charms (`Charm 1..3`).",
        equivalent_command='pob2 ground "charms"',
    ),
    TrapRule(
        rule_id="POE1_BLIGHT_OILS",
        scopes=frozenset({S.NOTABLE_OR_OIL, S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY}),
        source_ref="PoE2/Data/LiquidEmotions.lua",
        en_words=(
            "clear oil", "sepia oil", "amber oil", "verdant oil", "teal oil", "azure oil",
            "indigo oil", "violet oil", "crimson oil", "black oil", "opalescent oil",
            "silver oil", "golden oil", "tainted oil", "reflective oil", "prismatic oil", "blight oil", "cassia",
        ),
        zh_phrases=(
            "清澈圣油", "墨色圣油", "琥珀圣油", "翠绿圣油", "白银圣油", "金色圣油",
            "金油", "银油", "腐化圣油", "圣油配方",
        ),
        error="PoE1 Blight Oils (`圣油`) do not exist in PoE2.",
        remediation="PoE2 amulets are Instilled (`涂灌`) with Delirium Liquid Emotions (`PoE2/Data/LiquidEmotions.lua`).",
        equivalent_command='pob2 ground "liquid emotions"',
    ),
    TrapRule(
        rule_id="POE1_MANA_RESERVATION_AND_AURAS",
        scopes=frozenset({S.GEM_NAME, S.STAT_QUERY, S.CONFIG_VAR, S.CALC_QUERY, S.DATA_QUERY}),
        source_ref="RePoE keywords.min.json[Spirit]; PoE2/Data/Gems.lua",
        en_exact=(
            "determination", "grace", "hatred", "zealotry", "wrath",
            "anger", "haste", "pride", "malevolence", "purity of elements", "divine blessing",
        ),
        zh_exact=("坚定", "优雅", "憎恨", "狂热", "雷霆", "愤怒", "迅捷", "尊严", "元素净化", "神圣祝福"),
        en_words=(
            "mana reservation efficiency", "increased mana reservation efficiency",
            "purity of elements", "divine blessing support",
            "determination aura", "grace aura", "hatred aura", "zealotry aura", "malevolence aura",
        ),
        zh_phrases=("魔力保留效能", "法力保留效能", "元素净化光环", "坚定光环", "优雅光环", "憎恨光环", "怨毒光环"),
        exclude_en_re=re.compile(r"essence\s+of\s+haste|thundergod's\s+wrath|wrath\s+sceptre|pride\s+of\s+sekhema", re.I),
        error="PoE1 Mana Reservation Efficiency and PoE1 reservation auras (`Determination`/`Grace`/`Hatred`/etc.) do not exist in PoE2.",
        remediation="PoE2 persistent buffs, heralds, banners, meta-gems, and permanent minions reserve Spirit (`精魂`), not Mana.",
        equivalent_command='pob2 ground "spirit"',
    ),
    TrapRule(
        rule_id="POE1_ELDRITCH_IMPLICITS",
        scopes=frozenset({S.ITEM_RAW, S.STAT_QUERY, S.DATA_QUERY, S.CONFIG_VAR}),
        source_ref="PoE2/Data/ModRunes.lua; PoE2/Data/ModCorrupted.lua",
        en_words=("searing exarch", "eater of worlds", "eldritch implicit", "eldritch ichor", "eldritch ember"),
        zh_phrases=("焚界者", "噬界者", "红王基底", "蓝王基底", "红蓝王", "古灵溶液", "古灵余烬"),
        regex=re.compile(r"(?m)^\{(?:exarch|eater)\}"),
        error="PoE1 Eldritch Implicits (`Searing Exarch`/`Eater of Worlds`) do not exist in PoE2.",
        remediation="PoE2 equipment uses Augment sockets (`{rune}`: Runes, Soul Cores, Idols) and Vaal corruptions.",
        equivalent_command='pob2 ground "runes"',
    ),
    TrapRule(
        rule_id="POE1_INFLUENCED_GEAR",
        scopes=frozenset({S.ITEM_RAW, S.STAT_QUERY, S.DATA_QUERY}),
        source_ref="PoE2/Data/ModItem.lua; poe2db.tw/us/Modifiers",
        en_words=("shaper item", "elder item", "crusader item", "redeemer item", "hunter item", "warlord item", "synthesised item", "awakener's orb"),
        zh_phrases=("塑界之器", "裂界之器", "塑界底子", "裂界底子", "圣战者物品", "救赎者物品", "狩猎者物品", "督军物品", "觉醒者之石", "忆境物品"),
        regex=re.compile(r"(?m)^(?:Shaper|Elder|Crusader|Redeemer|Hunter|Warlord|Synthesised) Item\b|^\{(?:shaper|elder|crusader|redeemer|hunter|warlord)\}"),
        error="PoE1 Atlas Influence bases (`Shaper`/`Elder`/`Conqueror`/`Synthesised`) do not exist in PoE2.",
        remediation="Use PoE2 normal, Essence, Verisium Alloy, Desecrated, and Corrupted modifier pools.",
        equivalent_command="pob2 craft odds --base <base> --want <mod>",
    ),
    TrapRule(
        rule_id="POE1_SIX_LINK_ARMOUR_SOCKETS",
        scopes=frozenset({S.ITEM_RAW, S.STAT_QUERY, S.DATA_QUERY, S.CATEGORY}),
        source_ref="RePoE base_items.min.json[CurrencyAddSkillGemSocket3..5]",
        en_words=("6-link body armour", "six-link body armour", "6l body armour", "orb of fusing", "chromatic orb"),
        zh_phrases=("六连胸甲", "6连胸甲", "六连衣服", "链接石", "幻色石"),
        regex=re.compile(r"(?m)^Sockets:\s*(?:[RGBWAD][ -]){1,5}[RGBWAD]\s*$"),
        error="PoE2 equipment does NOT hold coloured skill-gem links (`R-G-B-W` / Orb of Fusing / Chromatic Orb).",
        remediation="Active Skill Gems hold up to 5 Support sockets upgraded via Lesser/Greater/Perfect Jeweller's Orbs; gear `Sockets: S` hold Runes/Soul Cores.",
        equivalent_command='pob2 ground "jewellers orb"',
    ),
    TrapRule(
        rule_id="POE1_GUARD_SKILLS_AND_FORTIFY_SUPPORT",
        scopes=frozenset({S.GEM_NAME, S.CONFIG_VAR, S.STAT_QUERY}),
        source_ref="PoE2/Data/Gems.lua",
        en_exact=("molten shell", "vaal molten shell", "steelskin", "immortal call", "arcane cloak", "fortify support", "phase run", "withering step"),
        zh_exact=("熔岩护盾", "钢铁皮肤", "不朽怒嚎", "奥术斗篷", "护体辅助", "暗影迷踪", "凋零步"),
        en_words=("molten shell", "steelskin", "immortal call", "arcane cloak", "fortify support"),
        zh_phrases=("熔岩护盾", "钢铁皮肤", "不朽怒嚎", "奥术斗篷", "护体辅助"),
        error="PoE1 Guard/Fortify skills (`Molten Shell`/`Steelskin`/`Immortal Call`/`Arcane Cloak`/`Fortify Support`) do not exist in PoE2.",
        remediation="Use PoE2 defences: `Ghost Dance`, `Wind Dancer`, `Raise Shield`, `Fortifying Cry`, Deflection, and Belt Charms.",
        equivalent_command='pob2 ground "defence"',
    ),
    TrapRule(
        rule_id="POE1_CLUSTER_JEWELS_AND_TATTOOS",
        scopes=frozenset({S.CATEGORY, S.ITEM_RAW, S.DATA_QUERY, S.STAT_QUERY}),
        source_ref="PoE2/Data/Bases/jewel.lua",
        en_exact=("cluster", "cluster_jewel", "clusterjewel", "jewel.cluster", "tattoo"),
        en_words=("small cluster jewel", "medium cluster jewel", "large cluster jewel", "cluster jewel", "tattoo of the", "honoured tattoo"),
        zh_phrases=("星团珠宝", "小型星团珠宝", "中型星团珠宝", "大型星团珠宝", "团簇珠宝", "天赋纹身", "纹身："),
        exclude_en_re=re.compile(r"can\s+tattoo\s+runes\s+onto\s+your\s+body", re.IGNORECASE),
        error="PoE1 Cluster Jewels (`星团珠宝`) and Passive-Tree Tattoos (`天赋纹身`) do not exist in PoE2.",
        remediation="PoE2 uses Ruby/Emerald/Sapphire/Diamond, Time-Lost radius jewels, and Unique jewels (`From Nothing`, `Against the Darkness`, `Controlled Metamorphosis`, `Prism of Belief`).",
        equivalent_command='pob2 ground "jewel"',
    ),
)

POE2_ONLY_REVERSE_TRAPS_FOR_POB1: tuple[TrapRule, ...] = (
    TrapRule(
        rule_id="POE2_UNCUT_GEMS",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY}),
        source_ref="PoE2/Data/Gems.lua",
        en_words=("uncut skill gem", "uncut spirit gem", "uncut support gem"),
        zh_phrases=("未切割的技能宝石", "未切割的精魂宝石", "未切割的辅助宝石", "精魂宝石"),
        error="Uncut Skill/Spirit/Support Gems belong to PoE2.",
        remediation="Use `pob2` for PoE2 gems.",
        equivalent_command='pob2 ground "uncut gem"',
    ),
    TrapRule(
        rule_id="POE2_DISTILLED_EMOTIONS",
        scopes=frozenset({S.NOTABLE_OR_OIL, S.DATA_QUERY, S.ITEM_RAW}),
        source_ref="PoE2/Data/LiquidEmotions.lua",
        en_words=("distilled ire", "distilled isolation", "diluted liquid ire", "concentrated liquid isolation", "instilled modifier"),
        zh_phrases=("涂灌", "精炼的孤立", "精炼的愤怒"),
        exclude_en_re=re.compile(r"distilled\s+perfection", re.IGNORECASE),
        error="Distilled / Liquid Emotions (`涂灌`) are PoE2 Delirium currencies.",
        remediation="PoE1 uses Blight Oils (`圣油`); use `pob2` for PoE2.",
        equivalent_command='pob2 ground "liquid emotions"',
    ),
    TrapRule(
        rule_id="POE2_WAYSTONE_AND_CHARMS",
        scopes=frozenset({S.SLOT, S.CATEGORY, S.DATA_QUERY, S.ITEM_RAW}),
        source_ref="PoE2/Data/ModMap.lua; PoE2/Data/ModCharm.lua",
        en_exact=("waystone", "waystones", "charm 1", "charm 2", "charm 3", "focus", "quarterstaff", "crossbow"),
        en_words=("waystone tier", "thawing charm", "soul core", "maximum spirit", "spirit reservation"),
        zh_phrases=("引路石", "灵魂核心", "最大精魂", "精魂保留", "融冰咒符"),
        exclude_en_re=re.compile(r"spirit\s+shield|raging\s+spirit", re.IGNORECASE),
        error="Waystones, Charms, Spirit reservation, Soul Cores, Focus, Crossbow, and Quarterstaff belong to PoE2.",
        remediation="Use `pob2` for PoE2 queries.",
        equivalent_command="pob2 ground <query>",
    ),
)
