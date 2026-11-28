# SPDX-License-Identifier: GPL-3.0-only
"""Verified 38-group CN PoE1 vs PoE2 mistranslation correction table and alias resolver (L0)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import HallucinationWarning
from .matcher import GuardrailScope, normalize_zh_compact, record_warning

S = GuardrailScope


@dataclass(frozen=True)
class ZhMistranslationTrap:
    en_term: str
    poe1_zh: str
    poe2_zh: str
    source_files: str
    hazard: str  # "collision" | "renamed"
    notes: str


# 38 verified PoE1 vs PoE2 CN localization divergences (sourced from PobTools-zh Data/poe1 vs Data/poe2)
ZH_MISTRANSLATION_TRAPS: tuple[ZhMistranslationTrap, ...] = (
    ZhMistranslationTrap("Arrows Pierce {0} additional Target", "箭矢贯穿 {0} 个额外目标", "箭矢穿透 {0} 个额外目标", "poe1/stats.json vs poe2/stats.json", "collision", "PoE2 CN translates Projectile Pierce as `穿透`, colliding with Resistance Penetration!"),
    ZhMistranslationTrap("Fork / Arrows Fork", "分裂(辅) / 箭矢分裂", "分叉 / 箭矢分叉", "poe1/gems.json vs poe2/gems.json", "collision", "PoE2 distinguishes `Fork`=`分叉` vs `Split`=`分裂`."),
    ZhMistranslationTrap("Bloodlust", "嗜血辅助", "血怒", "poe1/gems.json vs poe2/gems.json", "collision", "PoE2 `Bloodlust` support is named `血怒`, colliding with PoE1 slang for Blood Rage (`鲜血狂怒`)!"),
    ZhMistranslationTrap("Always inflict Brittle", "必定造成易碎", "必定造成脆弱", "poe1/stats.json vs poe2/stats.json", "collision", "PoE2 translates `Brittle` as `脆弱`, colliding identically with `Vulnerability` curse!"),
    ZhMistranslationTrap("Immolate", "牺牲辅助", "献祭", "poe1/gems.json vs poe2/gems.json", "collision", "PoE2 `Immolate`=`献祭`, while `Sacrifice`=`替代牺牲`."),
    ZhMistranslationTrap("Sacrifice", "牺牲（辅）", "替代牺牲", "poe1/gems.json vs poe2/gems.json", "collision", "PoE2 Spirit minion gem `替代牺牲`."),
    ZhMistranslationTrap("Lifetap", "活栓辅助", "赤炼", "poe1/gems.json vs poe2/gems.json", "renamed", "Searching `活栓` misses PoE2 `Lifetap` (`赤炼`)."),
    ZhMistranslationTrap("Reap", "绝命之镰", "赤炼收割", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE1 `绝命之镰` renamed `赤炼收割` in PoE2."),
    ZhMistranslationTrap("Essence Drain", "灵魂吸取", "精华吸取", "poe1/gems.json vs poe2/gems.json", "renamed", "Iconic Chaos DoT spell `灵魂吸取` is `精华吸取` in PoE2 CN."),
    ZhMistranslationTrap("Contagion", "顽疾", "瘟疫", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE2 standardizes `Contagion` on `瘟疫`."),
    ZhMistranslationTrap("Unearth", "亵渎之矛", "开掘", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE1 `亵渎之矛`/`尸术矢` is `开掘` in PoE2."),
    ZhMistranslationTrap("Innervate", "亢奋辅助", "闪电支配", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE1 `亢奋` renamed `闪电支配` in PoE2."),
    ZhMistranslationTrap("Blasphemy", "渎神（辅）", "诅咒光环", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE2 Spirit Meta-Gem `Blasphemy` is named `诅咒光环`."),
    ZhMistranslationTrap("Plague Bearer", "灾难使徒", "瘟疫使者", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE1 `病疫之难`/`灾难使徒` is `瘟疫使者` in PoE2."),
    ZhMistranslationTrap("Culling Strike", "扑杀", "终结(辅)", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE2 `终结` thresholds: 35% Normal / 20% Magic / 10% Rare / 5% Unique (`Misc.lua:104-107`)."),
    ZhMistranslationTrap("Blind", "致盲辅助", "盲目（辅）", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE1 `致盲` vs PoE2 `盲目`/`目盲`."),
    ZhMistranslationTrap("Swift Affliction", "极速苦痛辅助", "极速腐化(辅)", "poe1/gems.json vs poe2/gems.json", "renamed", "PoE1 `极速苦痛` renamed `极速腐化` in PoE2."),
    ZhMistranslationTrap("Ghost Dance", "鬼舞", "幽灵舞步", "poe1/passives.json vs poe2/gems.json", "renamed", "Moved from PoE1 Keystone (`鬼舞`) to PoE2 Spirit Gem (`幽灵舞步`)."),
    ZhMistranslationTrap("Wind Dancer", "风魔舞者", "风舞者", "poe1/passives.json vs poe2/gems.json", "renamed", "Moved from PoE1 Keystone (`风魔舞者`) to PoE2 Spirit Gem (`风舞者`)."),
    ZhMistranslationTrap("Assassin's Mark", "刺客印记", "暗影印记", "poe1/gems.json vs poe2/gems.json", "renamed", "`刺客印记` renamed `暗影印记` in PoE2."),
    ZhMistranslationTrap("Far Shot", "狙击", "远射", "poe1/passives.json vs poe2/passives.json", "collision", "PoE2 `远射`=`Far Shot`; `狙击`=`Snipe`."),
    ZhMistranslationTrap("Glancing Blows", "侧身之击", "斗转星移", "poe1/passives.json vs poe2/passives.json", "renamed", "PoE2 `斗转星移` makes Evade Unlucky and Deflect Lucky (NOT Block!)."),
    ZhMistranslationTrap("The Agnostic", "不可知论", "惘信者", "poe1/passives.json vs poe2/passives.json", "renamed", "Keystone `不可知论` is `惘信者` in PoE2."),
    ZhMistranslationTrap("Crimson Dance", "绯红舞蹈", "玫红之舞", "poe1/passives.json vs poe2/passives.json", "renamed", "Keystone `绯红舞蹈` is `玫红之舞` in PoE2."),
    ZhMistranslationTrap("Tailwind", "流顺之风", "提速尾流", "poe1/stats.json vs poe2/stats.json", "renamed", "Deadeye buff `流顺之风` is `提速尾流` in PoE2."),
    ZhMistranslationTrap("Always Sap Enemies", "必定造成力竭", "必定造成精疲力尽", "poe1/stats.json vs poe2/stats.json", "renamed", "Lightning alt-ailment `Sap`: `力竭` (PoE1) vs `精疲力尽` (PoE2)."),
    ZhMistranslationTrap("Arsonist", "焚烧", "纵火狂", "poe1/passives.json vs poe2/passives.json", "collision", "PoE2 `纵火狂`=`Arsonist`/`Skeletal Arsonist` (in PoE1 it meant Pyromaniac)."),
    ZhMistranslationTrap("Bloodletting / Flow of Life", "出血 / 放血", "放血 / 生命之流", "poe1/passives.json vs poe2/passives.json", "collision", "Exact swap: `放血` meant Flow of Life in PoE1, now means Bloodletting in PoE2!"),
    ZhMistranslationTrap("Composite Bow", "合成弓", "复合弓", "poe1/items.json vs poe2/items.json", "collision", "PoE2 `复合弓`=`Composite Bow` (PoE1 `复合弓` was Compound Bow)."),
    ZhMistranslationTrap("Ochre Sceptre", "赤色短杖", "赤色权杖", "poe1/items.json vs poe2/items.json", "collision", "Sceptres shifted from `短杖` to `权杖` in PoE2 `items.json`."),
    ZhMistranslationTrap("Amber / Jade / Lapis / Gold Amulet", "琥珀/翠玉/海玉/帝金护身符", "琥珀/翠玉/海玉/帝金项链", "poe1/items.json vs poe2/items.json", "renamed", "Amulet bases shifted from `护身符` (PoE1) to `项链` (PoE2)."),
    ZhMistranslationTrap("Heroic Tragedy", "英勇悲剧", "英雄的悲歌", "poe1/uniques.json vs poe2/uniques.json", "renamed", "Timeless jewel `Heroic Tragedy` is `英雄的悲歌` in PoE2."),
    ZhMistranslationTrap("Thunderfist", "轰天雷", "雷拳", "poe1/uniques.json vs poe2/uniques.json", "renamed", "Unique gloves `轰天雷` (PoE1) vs `雷拳` (PoE2)."),
    ZhMistranslationTrap("Tidebreaker", "碎潮者", "局势逆转者", "poe1/uniques.json vs poe2/uniques.json", "renamed", "Unique 2H Mace `碎潮者` (PoE1) vs `局势逆转者` (PoE2)."),
    ZhMistranslationTrap("Charm", "咒符", "护符 (ui.json) / 咒符 (items.json)", "poe2/ui.json vs poe2/items.json", "collision", "Internal CN split: `ui.json` uses `护符`, `items.json` uses `咒符` — `pob2` accepts both!"),
    ZhMistranslationTrap("Area of Effect / Cooldown Recovery Rate / ES Recharge Rate", "影响范围 / 冷却回复率 / 能量护盾充能率", "效果区域 / 冷却回复速度 / 能量护盾充能速度", "poe1/passives.json vs poe2/passives.json", "renamed", "Core stat wording shifted (`率` -> `速度`, `影响范围` -> `效果区域`)."),
    ZhMistranslationTrap("Malice vs Malevolence", "怨毒 (Malevolence 光环)", "怨毒 (Malice 权杖暴击弱点光环)", "poe1/gems.json vs poe2/gems.json", "collision", "PoE2 `怨毒` is `Malice` Sceptre aura (NOT PoE1 DoT `Malevolence`)."),
    ZhMistranslationTrap("Deflection Rating", "(无)", "偏转值 (zh-CN) / 偏斜值 (zh-TW)", "poe2/zh-rCN vs poe2/zh-rTW", "renamed", "PoE2 0.5 defence layer: accept both `偏转` (CN) and `偏斜` (TW)."),
)

POE1_ZH_TO_POE2_ALIAS: dict[str, tuple[str, str]] = {
    "灵魂吸取": ("Essence Drain", "精华吸取"),
    "亵渎之矛": ("Unearth", "开掘"),
    "尸术矢": ("Unearth", "开掘"),
    "活栓": ("Lifetap", "赤炼"),
    "活栓辅助": ("Lifetap", "赤炼"),
    "绝命之镰": ("Reap", "赤炼收割"),
    "亢奋辅助": ("Innervate", "闪电支配"),
    "牺牲辅助": ("Immolate", "献祭"),
    "嗜血辅助": ("Bloodlust", "血怒"),
    "分裂辅助": ("Fork", "分叉"),
    "渎神": ("Blasphemy", "诅咒光环"),
    "渎神辅助": ("Blasphemy", "诅咒光环"),
    "病疫之难": ("Plague Bearer", "瘟疫使者"),
    "灾难使徒": ("Plague Bearer", "瘟疫使者"),
    "扑杀": ("Culling Strike", "终结"),
    "极速苦痛": ("Swift Affliction", "极速腐化"),
    "极速苦痛辅助": ("Swift Affliction", "极速腐化(辅)"),
    "鬼舞": ("Ghost Dance", "幽灵舞步"),
    "风魔舞者": ("Wind Dancer", "风舞者"),
    "刺客印记": ("Assassin's Mark", "暗影印记"),
    "侧身之击": ("Glancing Blows", "斗转星移"),
    "不可知论": ("The Agnostic", "惘信者"),
    "绯红舞蹈": ("Crimson Dance", "玫红之舞"),
    "流顺之风": ("Tailwind", "提速尾流"),
    "英勇悲剧": ("Heroic Tragedy", "英雄的悲歌"),
    "轰天雷": ("Thunderfist", "雷拳"),
    "碎潮者": ("Tidebreaker", "局势逆转者"),
    "破浪者": ("Tidebreaker", "局势逆转者"),
    "琥珀护身符": ("Amber Amulet", "琥珀项链"),
    "翠玉护身符": ("Jade Amulet", "翠玉项链"),
    "海玉护身符": ("Lapis Amulet", "海玉项链"),
    "帝金护身符": ("Gold Amulet", "帝金项链"),
    "顽疾": ("Contagion", "瘟疫"),
    "致盲辅助": ("Blind", "盲目（辅）"),
}

_COMPACT_ALIAS_LOOKUP: dict[str, tuple[str, str, str]] = {
    normalize_zh_compact(k): (k, en, zh2) for k, (en, zh2) in POE1_ZH_TO_POE2_ALIAS.items()
}


@dataclass(frozen=True)
class ZhCollisionAdvisory:
    rule_id: str
    scopes: frozenset[GuardrailScope]
    zh_phrases: tuple[str, ...]
    exclude_zh_compounds: tuple[str, ...]
    canonical_term: str
    source_ref: str
    summary: str
    poe1_behavior: str
    poe2_behavior: str


ZH_COLLISION_ADVISORIES: tuple[ZhCollisionAdvisory, ...] = (
    ZhCollisionAdvisory("ZH_COLLISION_BLOODLUST", frozenset({S.GEM_NAME, S.DATA_QUERY}), ("血怒",), ("鲜血狂怒",), "Bloodlust / 血怒", "Data/poe2/zh-rCN/gems.json", "In PoE2 CN, `血怒` is `Bloodlust` support (NOT PoE1 Blood Rage / `鲜血狂怒`)!", "`血怒` = Blood Rage slang.", "`血怒` = `Bloodlust` support."),
    ZhCollisionAdvisory("ZH_COLLISION_PIERCE_PENETRATION", frozenset({S.GEM_NAME, S.STAT_QUERY, S.DATA_QUERY}), ("箭矢穿透", "穿透(辅)", "穿透辅助"), ("元素穿透", "火焰穿透", "冰霜穿透", "闪电穿透", "混沌穿透", "抗性穿透"), "Pierce vs Penetration", "Data/poe2/zh-rCN/stats.json", "In PoE2 CN, Projectile `Pierce` is translated as `穿透`, colliding with Elemental `Penetration`!", "`贯穿`=Pierce; `穿透`=Penetration.", "Both share `穿透`; verify EN text."),
    ZhCollisionAdvisory("ZH_COLLISION_BRITTLE_VULNERABILITY", frozenset({S.GEM_NAME, S.STAT_QUERY, S.DATA_QUERY, S.CALC_QUERY}), ("脆弱",), (), "Brittle vs Vulnerability", "Data/poe2/zh-rCN/stats.json", "In PoE2 CN, `脆弱` is used for BOTH `Brittle` (Cold crit ailment) and `Vulnerability` curse!", "`易碎`=Brittle; `脆弱`=Vulnerability.", "Both share `脆弱`; verify EN term."),
    ZhCollisionAdvisory("ZH_COLLISION_FORK_SPLIT", frozenset({S.GEM_NAME, S.STAT_QUERY, S.DATA_QUERY}), ("箭矢分裂", "分裂辅助", "分裂(辅)"), (), "Fork (分叉) vs Split (分裂)", "Data/poe2/zh-rCN/gems.json", "In PoE2 CN, `Fork`=`分叉` while `Split`=`分裂` (PoE1 used `分裂` for both).", "Both `分裂`.", "`Fork`=`分叉`, `Split`=`分裂`."),
    ZhCollisionAdvisory("ZH_COLLISION_BLOODLETTING_FLOW", frozenset({S.DATA_QUERY, S.NOTABLE_OR_OIL}), ("放血",), (), "Bloodletting (放血) vs Flow of Life (生命之流)", "Data/poe2/zh-rCN/passives.json", "In PoE2 CN, `放血` swapped meaning to `Bloodletting` (`Flow of Life` is `生命之流`)!", "`放血`=Flow of Life.", "`放血`=Bloodletting."),
    ZhCollisionAdvisory("ZH_COLLISION_ARSONIST", frozenset({S.DATA_QUERY, S.NOTABLE_OR_OIL, S.GEM_NAME}), ("纵火狂",), (), "Arsonist (纵火狂)", "Data/poe2/zh-rCN/passives.json", "In PoE2 CN, `纵火狂` translates `Arsonist` / `Skeletal Arsonist` (not Pyromaniac).", "`纵火狂`=Pyromaniac.", "`纵火狂`=Arsonist."),
    ZhCollisionAdvisory("ZH_COLLISION_FAR_SHOT_SNIPE", frozenset({S.DATA_QUERY, S.NOTABLE_OR_OIL, S.GEM_NAME}), ("狙击",), ("狙击印记",), "Far Shot (远射) vs Snipe (狙击)", "Data/poe2/zh-rCN/passives.json", "In PoE2 CN, `Far Shot`=`远射`, while `狙击`=`Snipe`!", "`狙击`=Far Shot.", "`远射`=Far Shot; `狙击`=Snipe."),
    ZhCollisionAdvisory("ZH_COLLISION_MALICE_MALEVOLENCE", frozenset({S.GEM_NAME, S.DATA_QUERY, S.STAT_QUERY}), ("怨毒",), ("怨毒光环",), "Malice (PoE2 怨毒) vs Malevolence (PoE1)", "Data/poe2/zh-rCN/gems.json", "In PoE2 CN, `怨毒` is `Malice` (Omen Sceptre aura), NOT PoE1 `Malevolence`!", "`怨毒`=Malevolence.", "`怨毒`=Malice Sceptre aura."),
    ZhCollisionAdvisory("ZH_COLLISION_CHARM_HU_ZHOU", frozenset({S.SLOT, S.CATEGORY, S.DATA_QUERY}), ("护符", "咒符"), ("琥珀护身符", "翠玉护身符", "海玉护身符", "帝金护身符"), "Charm (护符/咒符)", "Data/poe2/zh-rCN/{ui,items}.json", "In PoE2 CN, `Charm` appears as both `护符` (`ui.json`) and `咒符` (`items.json`).", "PoE1 3.23 Wildwood `咒符`.", "PoE2 Belt `Charm 1..3` (`护符`/`咒符`)."),
)


def lookup_compact_alias(exact_zh: str) -> tuple[str, str, str] | None:
    return _COMPACT_ALIAS_LOOKUP.get(exact_zh)


def resolve_poe1_zh_alias(query: str, *, emit_warning: bool = True) -> tuple[str, str] | None:
    """Translate a legacy PoE1 Simplified Chinese term into `(en_canonical, poe2_zh_canonical)`."""
    if not query or not str(query).strip():
        return None
    raw = str(query).strip()
    compact = normalize_zh_compact(raw)
    hit = _COMPACT_ALIAS_LOOKUP.get(compact)
    if hit is None:
        stripped = re.sub(r"(?:辅助|辅)$", "", compact)
        if stripped and stripped != compact:
            hit = _COMPACT_ALIAS_LOOKUP.get(stripped)
    if hit is None:
        return None
    poe1_zh, en_canon, poe2_zh = hit
    if emit_warning:
        en_slug = re.sub(r"[^A-Z0-9]+", "_", en_canon.upper()).strip("_")
        warn = HallucinationWarning(
            rule_id=f"ZH_ALIAS_{en_slug}",
            game="poe2",
            category="zh_mistranslation",
            trigger=raw,
            summary=f"PoE1 CN term `{poe1_zh}` was renamed in PoE2 to `{poe2_zh}` (`{en_canon}`).",
            poe1_behavior=f"PoE1 CN: `{poe1_zh}`",
            poe2_behavior=f"PoE2 CN: `{poe2_zh}` (EN: `{en_canon}`)",
            canonical_term=f"{en_canon} / {poe2_zh}",
            source_ref="Data/poe2/zh-rCN/{gems,items,passives,stats,uniques}.json",
        )
        record_warning(warn, emit_stderr=True)
    return en_canon, poe2_zh
