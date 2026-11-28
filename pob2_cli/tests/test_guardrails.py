# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for T2.1 bidirectional PoE1/PoE2 guardrails, 38 CN mistranslations, and 0-false-positive whitelist."""

from __future__ import annotations

import pytest

from pob2_cli.errors import CrossGameHallucinationError, LegacyPatchTrapError
from pob2_cli.guardrails import (
    POE1_ONLY_TRAPS_FOR_POB2,
    POE2_ONLY_REVERSE_TRAPS_FOR_POB1,
    SAME_NAME_FALSE_FRIENDS_POE2,
    VERSION_DRIFT_BLOCKS_POE2,
    ZH_MISTRANSLATION_TRAPS,
    GuardrailScope,
    check_input,
    check_input_item_raw,
    resolve_poe1_zh_alias,
    validate_build_xml,
    validate_command,
    validate_url,
)

# ---------------------------------------------------------------------------
# 1. 100% PoE1 XML / URL / Command / Term Rejection (Exit Code 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "xml_snippet,expected_rule",
    [
        ('<PathOfBuilding><Build className="Witch"/></PathOfBuilding>', "XML_POE1_ROOT_IN_POB2"),
        ('<PathOfBuilding2><Tree><Spec treeVersion="3_26"/></Tree></PathOfBuilding2>', "XML_POE1_TREE_IN_POB2"),
        ('<PathOfBuilding2><Build className="Templar"/></PathOfBuilding2>', "XML_POE1_CLASS_IN_POB2"),
        ('<PathOfBuilding2><Items><ItemSet><Slot name="Flask 4" itemId="1"/></ItemSet></Items></PathOfBuilding2>', "XML_POE1_SLOT_IN_POB2"),
    ],
)
def test_poe1_xml_hard_blocked_with_exit_code_1(xml_snippet: str, expected_rule: str) -> None:
    with pytest.raises(CrossGameHallucinationError) as exc:
        validate_build_xml(xml_snippet, expected_game="poe2")
    assert exc.value.exit_code == 1
    assert exc.value.rule_id == expected_rule
    assert exc.value.detected_game == "poe1"


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://poedb.tw/us/Gems",
        "https://poe.ninja/builds/settlers",
        "https://www.pathofexile.com/trade/search/Settlers",
        "https://www.pathofexile.com/api/leagues?realm=poe2",
        "https://poe.game.qq.com/character-window/get-items",
        "https://www.poewiki.net/wiki/Determination",
        "https://maxroll.gg/poe/build-guides/rf",
        "pob://pobb.in/abc123",
    ],
)
def test_poe1_urls_hard_blocked_in_pob2(bad_url: str) -> None:
    with pytest.raises(CrossGameHallucinationError) as exc:
        validate_url(bad_url, expected_game="poe2")
    assert exc.value.exit_code == 1


@pytest.mark.parametrize(
    "good_poe2_url",
    [
        "https://poe2db.tw/us/Wands",
        "https://poe.ninja/poe2/builds",
        "https://www.pathofexile.com/trade2/search/poe2/Standard",
        "https://www.poe2wiki.net/wiki/Spirit",
        "https://maxroll.gg/poe2/build-guides",
        "https://www.wegame.com.cn/helper/poe2/#/share/AbCdEfGh1234567890",
        "pob2://pobb.in/xyz999",
    ],
)
def test_valid_poe2_urls_allowed_and_blocked_in_poe1(good_poe2_url: str) -> None:
    validate_url(good_poe2_url, expected_game="poe2")
    with pytest.raises(CrossGameHallucinationError):
        validate_url(good_poe2_url, expected_game="poe1")


@pytest.mark.parametrize("poe1_cmd", ["pob2 bench suggest", "pob2 map-regex", "pob2 tattoo list", "pob2 item anoint"])
def test_poe1_commands_hard_blocked(poe1_cmd: str) -> None:
    with pytest.raises(CrossGameHallucinationError) as exc:
        validate_command(poe1_cmd, expected_game="poe2")
    assert exc.value.exit_code == 1


@pytest.mark.parametrize(
    "term,scope",
    [
        ("法术压制", GuardrailScope.STAT_QUERY),
        ("Spell Suppression", GuardrailScope.CALC_QUERY),
        ("Quicksilver Flask", GuardrailScope.DATA_QUERY),
        ("水银药剂", GuardrailScope.ITEM_RAW),
        ("Golden Oil", GuardrailScope.NOTABLE_OR_OIL),
        ("Determination", GuardrailScope.GEM_NAME),
        ("魔力保留效能", GuardrailScope.STAT_QUERY),
        ("Searing Exarch", GuardrailScope.ITEM_RAW),
        ("Shaper Item", GuardrailScope.ITEM_RAW),
        ("Orb of Fusing", GuardrailScope.DATA_QUERY),
        ("Molten Shell", GuardrailScope.GEM_NAME),
        ("星团珠宝", GuardrailScope.DATA_QUERY),
        ("Orb of Scouring", GuardrailScope.DATA_QUERY),
    ],
)
def test_poe1_terms_hard_blocked(term: str, scope: GuardrailScope) -> None:
    with pytest.raises(CrossGameHallucinationError) as exc:
        check_input(term, scope, expected_game="poe2", emit_stderr=False)
    assert exc.value.exit_code == 1


# ---------------------------------------------------------------------------
# 2. 0.1->0.5.5 Version Drift, Support Gem Fix, Armour Order, Resistance Rule
# ---------------------------------------------------------------------------


def test_version_drift_and_corrected_055_mechanics() -> None:
    # 0.4/0.5 removed levers hard-blocked with LegacyPatchTrapError (exit 1)
    for removed in ("Omen of Homogenising Exaltation", "Recombinator", "同质化预兆", "重组器"):
        with pytest.raises(LegacyPatchTrapError) as exc:
            check_input(removed, GuardrailScope.DATA_QUERY, emit_stderr=False)
        assert exc.value.exit_code == 1

    # Corrected 0.5.5 support gem rule: NO global 1-copy limit on normal supports!
    w_sup = check_input("support gem", GuardrailScope.DATA_QUERY, emit_stderr=False)
    sup_rule = next(w for w in w_sup if w.rule_id == "FF_SUPPORT_GEM_055_RULES")
    assert "isLineage" in sup_rule.summary
    assert "gemFamily" in sup_rule.summary
    assert "NO LONGER globally limited to 1" in sup_rule.summary

    # Corrected armour before elemental resistances rule
    w_arm = check_input("armour applies to elemental", GuardrailScope.CALC_QUERY, emit_stderr=False)
    arm_rule = next(w for w in w_arm if w.rule_id == "FF_ARMOUR_BEFORE_RESISTANCE_AND_BLOCK_50")
    assert "BEFORE Elemental" in arm_rule.summary
    assert "10 * RawHit" in arm_rule.summary

    # Owner's resistance rule: cap + overcap buffer, never equalize
    w_res = check_input("三抗配平", GuardrailScope.CALC_QUERY, emit_stderr=False)
    res_rule = next(w for w in w_res if w.rule_id == "FF_RESISTANCE_CAP_AND_OVERCAP_BUFFER")
    assert "Overcap Buffer" in res_rule.summary
    assert "绝不强求三抗数值相等" in res_rule.summary


# ---------------------------------------------------------------------------
# 3. 36+ Verified CN Mistranslation Traps & Alias Resolution
# ---------------------------------------------------------------------------


def test_36_plus_cn_mistranslation_traps_and_aliases() -> None:
    assert len(ZH_MISTRANSLATION_TRAPS) >= 36
    assert resolve_poe1_zh_alias("活栓", emit_warning=False) == ("Lifetap", "赤炼")
    assert resolve_poe1_zh_alias("灵魂吸取", emit_warning=False) == ("Essence Drain", "精华吸取")
    assert resolve_poe1_zh_alias("扑杀", emit_warning=False) == ("Culling Strike", "终结")

    for zh_term, expected_rule in (
        ("箭矢穿透", "ZH_COLLISION_PIERCE_PENETRATION"),
        ("脆弱", "ZH_COLLISION_BRITTLE_VULNERABILITY"),
        ("血怒", "ZH_COLLISION_BLOODLUST"),
        ("护符", "ZH_COLLISION_CHARM_HU_ZHOU"),
        ("咒符", "ZH_COLLISION_CHARM_HU_ZHOU"),
    ):
        ws = check_input(zh_term, GuardrailScope.DATA_QUERY, emit_stderr=False)
        assert any(w.rule_id == expected_rule for w in ws), f"Expected {expected_rule} for {zh_term}"


# ---------------------------------------------------------------------------
# 4. Valid PoE2 Terminology Whitelist (ZERO False-Positive Blocks!)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid_poe2_input,scope",
    [
        ("Spark", GuardrailScope.GEM_NAME),
        ("Herald of Ice", GuardrailScope.GEM_NAME),
        ("Archmage", GuardrailScope.GEM_NAME),
        ("Purity of Fire", GuardrailScope.GEM_NAME),
        ("Discipline", GuardrailScope.GEM_NAME),
        ("Fortifying Cry", GuardrailScope.GEM_NAME),
        ("Deflection Rating", GuardrailScope.STAT_QUERY),
        ("偏转值", GuardrailScope.STAT_QUERY),
        ("Spirit", GuardrailScope.CALC_QUERY),
        ("精魂", GuardrailScope.CALC_QUERY),
        ("Waystone", GuardrailScope.DATA_QUERY),
        ("Attuned Wand", GuardrailScope.ITEM_RAW),
        ("Legacy of Quicksilver", GuardrailScope.ITEM_RAW),
        ("Essence of Haste", GuardrailScope.DATA_QUERY),
        ("Can tattoo Runes onto your body", GuardrailScope.ITEM_RAW),
        ("Charm 1", GuardrailScope.SLOT),
        ("Flask 1", GuardrailScope.SLOT),
        ("Flask 2", GuardrailScope.SLOT),
        ("Arm 1", GuardrailScope.SLOT),
        ("Leg 2", GuardrailScope.SLOT),
        ("Weapon 1 Swap", GuardrailScope.SLOT),
        ("元素穿透", GuardrailScope.STAT_QUERY),
        ("鲜血狂怒", GuardrailScope.DATA_QUERY),
    ],
)
def test_valid_poe2_whitelist_zero_false_positive_blocks(
    valid_poe2_input: str, scope: GuardrailScope
) -> None:
    check_input(valid_poe2_input, scope, expected_game="poe2", emit_stderr=False)


def test_rare_line1_random_name_discarded_no_false_block() -> None:
    # Random rare title "Determination Crown" or "Wrath Knot" must NOT trigger aura trap!
    rare_item = (
        "Rarity: RARE\nDetermination Knot\nTopaz Ring\n--------\n"
        "Item Level: 82\n--------\n+35% to Lightning Resistance\n+85 to maximum Life\n"
    )
    check_input_item_raw(rare_item, expected_game="poe2", emit_stderr=False)


def test_all_guardrail_rules_carry_verifiable_source_ref() -> None:
    for r in (*POE1_ONLY_TRAPS_FOR_POB2, *POE2_ONLY_REVERSE_TRAPS_FOR_POB1):
        assert r.source_ref.strip(), f"{r.rule_id} missing source_ref"
    for r in (*SAME_NAME_FALSE_FRIENDS_POE2, *VERSION_DRIFT_BLOCKS_POE2):
        assert r.source_ref.strip(), f"{r.rule_id} missing source_ref"
