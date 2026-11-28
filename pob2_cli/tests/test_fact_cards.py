# SPDX-License-Identifier: GPL-3.0-only
"""Unit tests for T2.1 provenance-enforced 0.5.5 fact cards and `<15ms` `pob2 ground`."""

from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from pob2_cli.cli import main as cli_main
from pob2_cli.errors import CrossGameHallucinationError
from pob2_cli.game_data import (
    FactCard,
    MissingProvenanceError,
    ground_query,
    load_fact_cards,
    validate_fact_provenance,
)


def test_all_bundled_fact_cards_pass_strict_provenance_validation() -> None:
    cards = load_fact_cards()
    assert len(cards) >= 50
    slugs = {c.slug for c in cards}
    assert "crafting_single_crafted_mod_cap_0_5" in slugs
    assert "resistances_cap_and_overcap_buffer" in slugs
    assert "support_gems_0_3_tiered_freedom" in slugs
    assert "armour_formula_and_before_resistances" in slugs

    for card in cards:
        validate_fact_provenance(card)
        assert card.patch_verified == "0.5.5"

    # Verify resistance card respects owner's overcap rule and NEVER promotes equalizing
    res_card = next(c for c in cards if c.slug == "resistances_cap_and_overcap_buffer")
    assert "Overcap Buffer" in res_card.poe2_live_rule
    assert "Never seek equal numerical values" in res_card.poe2_live_rule


def test_unsourced_fact_card_is_rejected() -> None:
    bad_card = FactCard(
        slug="unverified_rumour",
        title="Rumor",
        category="crafting",
        tags=("rumour",),
        poe1_or_stale_trap="none",
        poe2_live_rule="unverified claim",
        formula_expr="",
        source_ref="trust me bro",
        patch_verified="0.5.5",
    )
    with pytest.raises(MissingProvenanceError):
        validate_fact_provenance(bad_card)


def test_ground_query_latency_under_15ms_and_bilingual_hits() -> None:
    # Warm LRU cache once, then measure repeated queries strictly < 15ms
    _ = load_fact_cards()
    for q, expected_slug in (
        ("armour", "armour_formula_and_before_resistances"),
        ("护甲", "armour_formula_and_before_resistances"),
        ("support gems", "support_gems_0_3_tiered_freedom"),
        ("精魂", "spirit_reservation_and_weapon_sets"),
        ("crafted essence limit", "crafting_single_crafted_mod_cap_0_5"),
        ("抗性", "resistances_cap_and_overcap_buffer"),
    ):
        t0 = time.perf_counter()
        res = ground_query(q, limit=5, emit_stderr=False)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < 15.0, f"ground_query({q!r}) took {elapsed_ms:.2f}ms >= 15ms"
        assert res.elapsed_ms < 15.0
        assert any(c.slug == expected_slug for c in res.cards), f"{expected_slug} not in {[c.slug for c in res.cards]}"


def test_ground_blocks_poe1_terms_and_cli_smoke() -> None:
    with pytest.raises(CrossGameHallucinationError):
        ground_query("Spell Suppression", emit_stderr=False)

    runner = CliRunner()
    ok = runner.invoke(cli_main, ["ground", "deflection", "--json"])
    assert ok.exit_code == 0, ok.output
    data = json.loads(ok.output)
    assert data["game"] == "poe2"
    assert data["elapsedMs"] < 15.0
    assert any(c["slug"] == "deflection_0_5_formula" for c in data["cards"])

    blocked = runner.invoke(cli_main, ["ground", "法术压制", "--json"])
    assert blocked.exit_code == 1


def test_ground_zh_recall_and_noise_rejection() -> None:
    res_ws = ground_query("单武器双天赋")
    assert any(c.slug == "spirit_reservation_and_weapon_sets" for c in res_ws.cards)

    res_armour = ground_query("护甲")
    assert any(c.slug == "armour_formula_and_before_resistances" for c in res_armour.cards)

    # Unrelated / fuzzy noise queries must return 0 hits
    for noisy in ("Kaom's", "War Cry", "Break Frost", "战吼"):
        res = ground_query(noisy)
        assert len(res.cards) == 0, f"Expected 0 hits for {noisy!r}, got {[c.slug for c in res.cards]}"
