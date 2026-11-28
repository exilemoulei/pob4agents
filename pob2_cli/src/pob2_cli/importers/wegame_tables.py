# SPDX-License-Identifier: GPL-3.0-only
"""Static PoE2 0.5.5 reference tables for CN WeGame share-link import (L1).

Sources:
  - PathOfBuildingCommunity/PathOfBuilding-PoE2 (MIT):
    TreeData/0_5/tree.lua (classes, ascendancies, jewelSlots[1..31]),
    Data/QuestRewards.lua (17 useConfig=true campaign reward keys),
    Classes/ImportTab.lua (equipment/flask/charm/incursion slot mapping).
  - ackness/pobr (MIT): WeGame wire protocol field conventions.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..han_fold import fold_han

# Exact 31-row PassiveJewelSlots mapping from PoE2/TreeData/0_5/tree.lua jewelSlots[row + 1].
# Row index (0-based, as emitted in WeGame `PassiveJewel_<n>`) -> PoB2 passive tree nodeId.
PASSIVE_JEWEL_SLOTS: dict[int, int] = {
    0: 26725,
    1: 36634,
    2: 33989,
    3: 41263,
    4: 60735,
    5: 61834,
    6: 31683,
    7: 28475,
    8: 6230,
    9: 48768,
    10: 34483,
    11: 7960,
    12: 46882,
    13: 55190,
    14: 61419,
    15: 2491,
    16: 54127,
    17: 32763,
    18: 26196,
    19: 33631,
    20: 21984,
    21: 59740,
    22: 63132,
    23: 36044,
    24: 17788,
    25: 62152,
    26: 26178,
    27: 23960,
    28: 39087,
    29: 3367,
    30: 11184,
}


@dataclass(frozen=True)
class ClassSpec:
    """Resolved PoE2 0.5.5 class and optional ascendancy identity for PoB2 XML."""

    class_name: str
    ascendancy_name: str
    class_id: int
    ascend_id: int
    ascendancy_internal_id: str


# All 8 PoE2 0.5.5 base classes and 23 ascendancies from PoE2/TreeData/0_5/tree.lua.
CLASS_SPECS: tuple[ClassSpec, ...] = (
    # Ranger (integerId=2)
    ClassSpec("Ranger", "", 2, 0, ""),
    ClassSpec("Ranger", "Deadeye", 2, 1, "Ranger1"),
    ClassSpec("Ranger", "Pathfinder", 2, 2, "Ranger3"),
    # Huntress (integerId=8)
    ClassSpec("Huntress", "", 8, 0, ""),
    ClassSpec("Huntress", "Amazon", 8, 1, "Huntress1"),
    ClassSpec("Huntress", "Spirit Walker", 8, 2, "Huntress2"),
    ClassSpec("Huntress", "Ritualist", 8, 3, "Huntress3"),
    # Warrior (integerId=6)
    ClassSpec("Warrior", "", 6, 0, ""),
    ClassSpec("Warrior", "Titan", 6, 1, "Warrior1"),
    ClassSpec("Warrior", "Warbringer", 6, 2, "Warrior2"),
    ClassSpec("Warrior", "Smith of Kitava", 6, 3, "Warrior3"),
    # Mercenary (integerId=9)
    ClassSpec("Mercenary", "", 9, 0, ""),
    ClassSpec("Mercenary", "Tactician", 9, 1, "Mercenary1"),
    ClassSpec("Mercenary", "Witchhunter", 9, 2, "Mercenary2"),
    ClassSpec("Mercenary", "Gemling Legionnaire", 9, 3, "Mercenary3"),
    # Druid (integerId=11)
    ClassSpec("Druid", "", 11, 0, ""),
    ClassSpec("Druid", "Oracle", 11, 1, "Druid1"),
    ClassSpec("Druid", "Shaman", 11, 2, "Druid2"),
    # Witch (integerId=1)
    ClassSpec("Witch", "", 1, 0, ""),
    ClassSpec("Witch", "Infernalist", 1, 1, "Witch1"),
    ClassSpec("Witch", "Blood Mage", 1, 2, "Witch2"),
    ClassSpec("Witch", "Lich", 1, 3, "Witch3"),
    ClassSpec("Witch", "Abyssal Lich", 1, 4, "Witch3b"),
    # Sorceress (integerId=7)
    ClassSpec("Sorceress", "", 7, 0, ""),
    ClassSpec("Sorceress", "Stormweaver", 7, 1, "Sorceress1"),
    ClassSpec("Sorceress", "Chronomancer", 7, 2, "Sorceress2"),
    ClassSpec("Sorceress", "Disciple of Varashta", 7, 3, "Sorceress3"),
    # Monk (integerId=10)
    ClassSpec("Monk", "", 10, 0, ""),
    ClassSpec("Monk", "Martial Artist", 10, 1, "Monk1"),
    ClassSpec("Monk", "Invoker", 10, 2, "Monk2"),
    ClassSpec("Monk", "Acolyte of Chayula", 10, 3, "Monk3"),
)

# CN/TW display aliases for WeGame class_name -> canonical English class/ascendancy name.
CLASS_ZH_TO_EN: dict[str, str] = {
    "游侠": "Ranger",
    "锐眼": "Deadeye",
    "追猎者": "Pathfinder",
    "女猎手": "Huntress",
    "女猎人": "Huntress",
    "亚马逊": "Amazon",
    "灵行者": "Spirit Walker",
    "仪祭师": "Ritualist",
    "仪式师": "Ritualist",
    "战士": "Warrior",
    "泰坦": "Titan",
    "战争使者": "Warbringer",
    "奇塔弗铁匠": "Smith of Kitava",
    "奇塔弗的铁匠": "Smith of Kitava",
    "雇佣兵": "Mercenary",
    "佣兵": "Mercenary",
    "战术家": "Tactician",
    "猎巫人": "Witchhunter",
    "宝石军团": "Gemling Legionnaire",
    "宝石军团士兵": "Gemling Legionnaire",
    "德鲁伊": "Druid",
    "先知": "Oracle",
    "神谕者": "Oracle",
    "萨满": "Shaman",
    "女巫": "Witch",
    "狱术师": "Infernalist",
    "地狱师": "Infernalist",
    "血法师": "Blood Mage",
    "巫妖": "Lich",
    "深渊巫妖": "Abyssal Lich",
    "女术士": "Sorceress",
    "魔巫": "Sorceress",
    "风暴编织者": "Stormweaver",
    "时空术士": "Chronomancer",
    "时光术士": "Chronomancer",
    "瓦拉什塔信徒": "Disciple of Varashta",
    "瓦拉什塔的信徒": "Disciple of Varashta",
    "武僧": "Monk",
    "武道家": "Martial Artist",
    "祈求者": "Invoker",
    "夏乌拉侍僧": "Acolyte of Chayula",
    "夏乌拉的侍僧": "Acolyte of Chayula",
}

_CLASS_BY_KEY: dict[str, ClassSpec] = {}
for _spec in CLASS_SPECS:
    _key = (_spec.ascendancy_name or _spec.class_name).lower()
    _CLASS_BY_KEY[_key] = _spec
    if _spec.ascendancy_internal_id:
        _CLASS_BY_KEY[_spec.ascendancy_internal_id.lower()] = _spec


def resolve_class_spec(raw_class_name: str) -> ClassSpec | None:
    """Resolve English, CN, TW, or internalId class/ascendancy name into a ClassSpec."""
    cleaned = raw_class_name.strip()
    if not cleaned:
        return None
    direct = _CLASS_BY_KEY.get(cleaned.lower())
    if direct is not None:
        return direct
    folded = fold_han(cleaned)
    en = CLASS_ZH_TO_EN.get(folded) or CLASS_ZH_TO_EN.get(cleaned)
    if en:
        return _CLASS_BY_KEY.get(en.lower())
    return None


# Equipment inventoryId -> PoB2 XML Slot name (PoE2/Classes/ImportTab.lua).
EQUIPMENT_SLOT_MAP: dict[str, str] = {
    "Weapon": "Weapon 1",
    "Offhand": "Weapon 2",
    "Weapon2": "Weapon 1 Swap",
    "Offhand2": "Weapon 2 Swap",
    "Helm": "Helmet",
    "BodyArmour": "Body Armour",
    "Gloves": "Gloves",
    "Boots": "Boots",
    "Amulet": "Amulet",
    "Belt": "Belt",
    "Ring": "Ring 1",
    "Ring2": "Ring 2",
    "Ring3": "Ring 3",
    "IncursionArmRight": "Arm 1",
    "IncursionArmLeft": "Arm 2",
    "IncursionLegRight": "Leg 1",
    "IncursionLegLeft": "Leg 2",
}

# All 17 PoE2 0.5.5 QuestRewards.lua entries with useConfig = true.
# Boolean quests (Stat) must be set to boolean="false" when replaced by WeGame quest_stats.
DEFAULT_QUEST_BOOLEAN_KEYS: tuple[str, ...] = (
    "questAct 1ClearfellBeira",
    "questAct 1FreythornKing In The Mists",
    "questAct 1Ogham ManorCandlemass",
    "questAct 2Spires of DesharSisters of Garukhan Shrine",
    "questAct 3Azak BogIgnagduk",
    "questAct 3Jiquani's MachinariumBlackjaw",
    "questAct 4Eye of HinekoraSilent Hall",
    "questInterlude 2Khari CrossingMolten Shrine",
    "questInterlude 3Kriar VillageLythara",
)

# Option quests (Options) must be set to string="" when replaced by WeGame quest_stats.
DEFAULT_QUEST_OPTION_KEYS: tuple[str, ...] = (
    "questAct 2Valley of the TitansMedallion",
    "questAct 3Venom CryptsVenom Draught",
    "questAct 4Eye of HinekoraTribal Medicine",
    "questAct 4Halls Of The DeadTawhoa's Test",
    "questAct 4Halls Of The DeadTasalio's Test",
    "questAct 4Halls Of The DeadNgamahu's Test",
    "questAct 4Abandoned PrisonGoddess of Justice",
    "questInterlude 2QimahSeven Pillars",
)

# Catalyst quality modifier category CN -> PoB2 EN label.
CATALYST_ZH_TO_EN: dict[str, str] = {
    "火焰": "Fire",
    "冰霜": "Cold",
    "冰冷": "Cold",
    "闪电": "Lightning",
    "混沌": "Chaos",
    "物理": "Physical",
    "攻击": "Attack",
    "施法者": "Caster",
    "施法": "Caster",
    "法术": "Caster",
    "防御": "Defence",
    "生命": "Life",
    "魔力": "Mana",
    "属性": "Attribute",
    "能力": "Attribute",
    "速度": "Speed",
    "暴击": "Critical",
    "抗性": "Resistance",
    "元素": "Elemental",
    "能量护盾": "Energy Shield",
    "闪避": "Evasion",
    "护甲": "Armour",
}

# Direct CN -> EN rune overrides where Tencent CN diverges from folded zh-rTW names.
RUNE_CN_OVERRIDES: dict[str, str] = {
    "高级冰川符文": "Greater Glacial Rune",
    "低级冰川符文": "Lesser Glacial Rune",
    "高级沙漠符文": "Greater Desert Rune",
    "低级沙漠符文": "Lesser Desert Rune",
    "高级暴风符文": "Greater Storm Rune",
    "低级暴风符文": "Lesser Storm Rune",
    "高级锻铁符文": "Greater Iron Rune",
    "高级铁符文": "Greater Iron Rune",
    "低级铁符文": "Lesser Iron Rune",
    "铁符文": "Iron Rune",
    "完美铁符文": "Perfect Iron Rune",
    "法瑞尔的狩猎符文": "Farrul's Rune of the Hunt",
    "法瑞尔的优雅符文": "Farrul's Rune of Grace",
    "法瑞尔的追击符文": "Farrul's Rune of the Chase",
    "萨奇沃的苍空符文": "Saqawal's Rune of the Sky",
    "萨奇沃的天空符文": "Saqawal's Rune of the Sky",
    "萨奇沃的记忆符文": "Saqawal's Rune of Memory",
    "萨奇沃的侵蚀符文": "Saqawal's Rune of Erosion",
    "费努姆斯的痛苦符文": "Fenumus' Rune of Agony",
    "费努姆斯的编织符文": "Fenumus' Rune of Spinning",
    "费努姆斯的吸取符文": "Fenumus' Rune of Draining",
    "克雷森的守卫符文": "Craiceann's Rune of Warding",
    "克雷森的复苏符文": "Craiceann's Rune of Recovery",
    "妙尔尼尔的遗产": "Legacy of Mjolner",
    "雷神之锤的遗产": "Legacy of Mjolner",
}

# Systematic Tencent CN -> folded zh-rTW lexical bridges for all 287 ModRunes entries.
RUNE_CN_TO_TW_BRIDGES: tuple[tuple[str, str], ...] = (
    ("高级", "高阶"),
    ("低级", "低阶"),
    ("法瑞尔", "费尔罗"),
    ("萨奇沃", "斯卡沃"),
    ("苍空", "天空"),
    ("灵核", "灵魂核心"),
    ("铁符文", "锻铁符文"),
)

# Exact Metadata/Items/Jewels/* id -> PoE2 0.5.5 Bases/jewel.lua canonical base name.
JEWEL_METADATA_ID_TO_BASE: dict[str, str] = {
    "metadata/items/jewels/jewelstr": "Ruby",
    "metadata/items/jewels/jeweldex": "Emerald",
    "metadata/items/jewels/jewelint": "Sapphire",
    "metadata/items/jewels/jeweldiamond": "Diamond",
    "metadata/items/jewels/jeweltimeless": "Timeless Jewel",
    "metadata/items/jewels/jewelstrradius": "Time-Lost Ruby",
    "metadata/items/jewels/jewelradiusstr": "Time-Lost Ruby",
    "metadata/items/jewels/jeweltimeloststr": "Time-Lost Ruby",
    "metadata/items/jewels/jeweldexradius": "Time-Lost Emerald",
    "metadata/items/jewels/jewelradiusdex": "Time-Lost Emerald",
    "metadata/items/jewels/jeweltimelostdex": "Time-Lost Emerald",
    "metadata/items/jewels/jewelintradius": "Time-Lost Sapphire",
    "metadata/items/jewels/jewelradiusint": "Time-Lost Sapphire",
    "metadata/items/jewels/jeweltimelostint": "Time-Lost Sapphire",
    "metadata/items/jewels/jeweldiamondradius": "Time-Lost Diamond",
    "metadata/items/jewels/jewelradiusdiamond": "Time-Lost Diamond",
    "metadata/items/jewels/jeweltimelostdiamond": "Time-Lost Diamond",
}


def resolve_jewel_base_from_metadata_id(raw_id: str | None) -> str | None:
    """Resolve WeGame GetJewels `jewel.id` metadata path to a valid PoE2 0.5.5 jewel base."""
    if not raw_id or not isinstance(raw_id, str):
        return None
    norm = raw_id.strip().replace("\\", "/").lower()
    if norm in JEWEL_METADATA_ID_TO_BASE:
        return JEWEL_METADATA_ID_TO_BASE[norm]
    if norm.startswith("metadata/items/jewels/"):
        tail = norm.rsplit("/", 1)[-1]
        is_radius = "radius" in tail or "timelost" in tail
        if "diamond" in tail:
            return "Time-Lost Diamond" if is_radius else "Diamond"
        if "timeless" in tail:
            return "Timeless Jewel"
        if "str" in tail or "ruby" in tail:
            return "Time-Lost Ruby" if is_radius else "Ruby"
        if "dex" in tail or "emerald" in tail:
            return "Time-Lost Emerald" if is_radius else "Emerald"
        if "int" in tail or "sapphire" in tail:
            return "Time-Lost Sapphire" if is_radius else "Sapphire"
    return None


# Common PoE2 0.5.5 concrete itemBases (every value MUST exist in PoE2/Data/Bases/*.lua).
BUNDLED_COMMON_BASES: dict[str, str] = {
    # Jewels (CN & TW folded variants)
    "红玉": "Ruby",
    "红宝石": "Ruby",
    "翡翠": "Emerald",
    "绿宝石": "Emerald",
    "翠绿珠宝": "Emerald",
    "蓝玉": "Sapphire",
    "蓝宝石": "Sapphire",
    "宝钻": "Diamond",
    "钻石": "Diamond",
    "失落的红玉": "Time-Lost Ruby",
    "时迭红宝石": "Time-Lost Ruby",
    "时光消逝红宝石": "Time-Lost Ruby",
    "失落的翡翠": "Time-Lost Emerald",
    "时迭绿宝石": "Time-Lost Emerald",
    "时光消逝翠绿珠宝": "Time-Lost Emerald",
    "失落的蓝玉": "Time-Lost Sapphire",
    "时迭蓝宝石": "Time-Lost Sapphire",
    "时光消逝蓝宝石": "Time-Lost Sapphire",
    "失落的宝钻": "Time-Lost Diamond",
    "时迭钻石": "Time-Lost Diamond",
    "失落时光钻石": "Time-Lost Diamond",
    # Flasks & Charms
    "终极生命药剂": "Ultimate Life Flask",
    "终极魔力药剂": "Ultimate Mana Flask",
    "巨大魔力药剂": "Gargantuan Mana Flask",
    "抗毒咒符": "Antidote Charm",
    "解毒咒符": "Antidote Charm",
    "解毒护符": "Antidote Charm",
    "根基咒符": "Grounding Charm",
    "接地咒符": "Grounding Charm",
    "接地护符": "Grounding Charm",
    "解冻咒符": "Thawing Charm",
    "解冻护符": "Thawing Charm",
    "黄金咒符": "Golden Charm",
    # Jewellery & Belts
    "紫晶戒指": "Amethyst Ring",
    "裂隙戒指": "Breach Ring",
    "裂痕戒指": "Breach Ring",
    "棱光戒指": "Prismatic Ring",
    "红玉戒指": "Ruby Ring",
    "蓝玉戒指": "Sapphire Ring",
    "黄玉戒指": "Topaz Ring",
    "活力戒指": "Vitalic Ring",
    "生机戒指": "Vitalic Ring",
    "维生之戒": "Vitalic Ring",
    "日曜项链": "Solar Amulet",
    "太阳项链": "Solar Amulet",
    "日耀项链": "Solar Amulet",
    "恒星项链": "Stellar Amulet",
    "琥珀项链": "Amber Amulet",
    "翠玉项链": "Jade Amulet",
    "海玉项链": "Lapis Amulet",
    "血石项链": "Bloodstone Amulet",
    "万用腰带": "Utility Belt",
    "实用腰带": "Utility Belt",
    "重革腰带": "Heavy Belt",
    # Weapons, Quivers, Shields, Body Armours
    "双弦弓": "Dualstring Bow",
    "双联战弩": "Dyad Crossbow",
    "双生十字弓": "Dyad Crossbow",
    "轰鸣十字弓": "Dyad Crossbow",
    "双灵战弩": "Gemini Crossbow",
    "双子十字弓": "Gemini Crossbow",
    "围攻十字弓": "Bombard Crossbow",
    "攻城十字弓": "Siege Crossbow",
    "调谐法杖": "Attuned Wand",
    "调和法杖": "Attuned Wand",
    "精绝法杖": "Attuned Wand",
    "虹吸法杖": "Siphoning Wand",
    "挥发法杖": "Volatile Wand",
    "宽头箭袋": "Broadhead Quiver",
    "火羽箭袋": "Fire Quiver",
    "加固塔盾": "Braced Tower Shield",
    "塔赫亚塔盾": "Tawhoan Tower Shield",
    "潜行外套": "Swiftstalker Coat",
    "猎鹰外套": "Falconer's Jacket",
    "先祖长袍": "Vile Robe",
    # Incursion Limbs (PoE2/Data/Bases/incursionlimb.lua)
    "守护之臂": "Guarding Arm",
    "守卫之臂": "Guarding Arm",
    "战斗之臂": "Combat Arm",
    "施法之臂": "Caster Arm",
    "衰老之臂": "Decrepifying Arm",
    "偏转之臂": "Deflective Arm",
    "指挥之臂": "Commanding Arm",
    "闪避之腿": "Evasive Leg",
    "疾跑之腿": "Sprinters Leg",
    "稳健之腿": "Sturdy Leg",
    "稳步之腿": "Surefooted Leg",
    "沉着之腿": "Calm Leg",
    "恢复之腿": "Restorative Leg",
}

BUNDLED_COMMON_UNIQUES: dict[str, str] = {
    "无尽之钻": "From Nothing",
    "虚无": "From Nothing",
    "独创": "Ingenuity",
}

_BUNDLED_GZ_PATH = Path(__file__).resolve().parent.parent / "data" / "wegame_runes_gems.json.gz"


@lru_cache(maxsize=1)
def load_bundled_runes_gems() -> dict[str, Any]:
    """Load bundled PoE2 0.5.5 Bases + ModRunes + Gems lookup payload (~47 KB gz)."""
    if not _BUNDLED_GZ_PATH.is_file():
        return {"version": "0.5.5", "bases": [], "runes": [], "gems": []}
    raw = gzip.decompress(_BUNDLED_GZ_PATH.read_bytes()).decode("utf-8")
    data = json.loads(raw)
    return data if isinstance(data, dict) else {"version": "0.5.5", "bases": [], "runes": [], "gems": []}


__all__ = [
    "BUNDLED_COMMON_BASES",
    "BUNDLED_COMMON_UNIQUES",
    "CATALYST_ZH_TO_EN",
    "CLASS_SPECS",
    "CLASS_ZH_TO_EN",
    "DEFAULT_QUEST_BOOLEAN_KEYS",
    "DEFAULT_QUEST_OPTION_KEYS",
    "EQUIPMENT_SLOT_MAP",
    "JEWEL_METADATA_ID_TO_BASE",
    "PASSIVE_JEWEL_SLOTS",
    "RUNE_CN_OVERRIDES",
    "RUNE_CN_TO_TW_BRIDGES",
    "ClassSpec",
    "load_bundled_runes_gems",
    "resolve_class_spec",
    "resolve_jewel_base_from_metadata_id",
]
