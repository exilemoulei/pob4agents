# SPDX-License-Identifier: GPL-3.0-only
"""Bidirectional PoE1/PoE2 anti-hallucination, version-drift, and CN mistranslation guardrails (L0)."""

from __future__ import annotations

import re

from ..errors import (
    CrossGameHallucinationError,
    DaemonRequestError,
    HallucinationWarning,
    LegacyPatchTrapError,
)
from .false_friends import (
    SAME_NAME_FALSE_FRIENDS_POE2,
    VERSION_DRIFT_BLOCKS_POE2,
    FalseFriendRule,
    VersionDriftBlockRule,
)
from .gates import (
    POE1_ONLY_CLASSES,
    POE2_ONLY_CLASSES,
    URL_GATE_RULES,
    VALID_POE2_EQUIPMENT_SLOTS,
    resolve_poe2_slot_name,
    validate_build_xml,
    validate_command,
    validate_engine_root,
    validate_url,
)
from .matcher import (
    ALL_QUERY_SCOPES,
    GuardrailScope,
    emit_stderr_warnings_from_dicts,
    extract_scannable_item_parts,
    get_active_warning_dicts,
    get_active_warnings,
    match_phrase_zh,
    match_word_en,
    normalize_guardrail_text,
    normalize_zh_compact,
    record_warning,
    reset_cli_warnings,
    strip_scope_suffix,
    warning_context,
)
from .redact import (
    get_auth_env_status,
    redact,
    redact_secrets,
    redact_url_credentials,
)
from .traps import (
    POE1_ONLY_TRAPS_FOR_POB2,
    POE2_ONLY_REVERSE_TRAPS_FOR_POB1,
    TrapRule,
)
from .zh_mistranslations import (
    POE1_ZH_TO_POE2_ALIAS,
    ZH_COLLISION_ADVISORIES,
    ZH_MISTRANSLATION_TRAPS,
    ZhMistranslationTrap,
    lookup_compact_alias,
    resolve_poe1_zh_alias,
)

_POB_ESC_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")


def _match_trap(
    raw: str, norm: str, exact_en: str, exact_zh: str, rule: TrapRule
) -> str | None:
    if rule.en_exact and exact_en in rule.en_exact:
        if rule.exclude_en_re is None or not rule.exclude_en_re.search(norm):
            return raw.strip()
    if rule.zh_exact:
        zh_targets = {normalize_zh_compact(z) for z in rule.zh_exact}
        if exact_zh in zh_targets and not any(
            normalize_zh_compact(c) in exact_zh for c in rule.exclude_zh_compounds
        ):
            return raw.strip()
    for phrase in rule.en_words:
        hit = match_word_en(norm, phrase, rule.exclude_en_re)
        if hit is not None:
            return hit
    for zh_p in rule.zh_phrases:
        hit = match_phrase_zh(raw, zh_p, rule.exclude_zh_compounds)
        if hit is not None:
            return hit
    if rule.regex is not None:
        m = rule.regex.search(_POB_ESC_RE.sub("", raw))
        if m is not None and (rule.exclude_en_re is None or not rule.exclude_en_re.search(norm)):
            return m.group(0).strip()
    return None


def _match_ff(
    raw: str, norm: str, exact_en: str, exact_zh: str, rule: FalseFriendRule
) -> str | None:
    if rule.en_exact and exact_en in rule.en_exact:
        if rule.exclude_en_re is None or not rule.exclude_en_re.search(norm):
            return raw.strip()
    if rule.zh_exact:
        zh_targets = {normalize_zh_compact(z) for z in rule.zh_exact}
        if exact_zh in zh_targets and not any(
            normalize_zh_compact(c) in exact_zh for c in rule.exclude_zh_compounds
        ):
            return raw.strip()
    for phrase in rule.en_words:
        hit = match_word_en(norm, phrase, rule.exclude_en_re)
        if hit is not None:
            return hit
    for zh_p in rule.zh_phrases:
        hit = match_phrase_zh(raw, zh_p, rule.exclude_zh_compounds)
        if hit is not None:
            return hit
    return None


def _match_drift_block(raw: str, norm: str, rule: VersionDriftBlockRule) -> str | None:
    for phrase in rule.en_words:
        hit = match_word_en(norm, phrase)
        if hit is not None:
            return hit
    for zh_p in rule.zh_phrases:
        hit = match_phrase_zh(raw, zh_p)
        if hit is not None:
            return hit
    return None


def check_input(
    text: str | None,
    scope: GuardrailScope,
    *,
    expected_game: str = "poe2",
    emit_stderr: bool = True,
) -> list[HallucinationWarning]:
    """Validate input against hard-blocking PoE1/0.4/0.5 traps and non-blocking PoE2 warnings."""
    if not text or not str(text).strip():
        return []
    raw = str(text)
    if scope == GuardrailScope.COMMAND:
        validate_command(raw, expected_game=expected_game)

    norm = normalize_guardrail_text(raw)
    stripped = strip_scope_suffix(raw, scope)
    exact_en = normalize_guardrail_text(stripped)
    exact_zh = normalize_zh_compact(stripped)

    # 1. Cross-game hard-blocking table
    trap_table = POE1_ONLY_TRAPS_FOR_POB2 if expected_game == "poe2" else POE2_ONLY_REVERSE_TRAPS_FOR_POB1
    detected = "poe1" if expected_game == "poe2" else "poe2"
    for rule in trap_table:
        if scope not in rule.scopes:
            continue
        trig = _match_trap(raw, norm, exact_en, exact_zh, rule)
        if trig is not None:
            raise CrossGameHallucinationError(
                game=expected_game, detected_game=detected, trigger=trig,
                error=rule.error, remediation=rule.remediation,
                equivalent_command=rule.equivalent_command, rule_id=rule.rule_id,
            )

    if expected_game != "poe2":
        return []

    # 2. PoE2 0.4/0.5 removed mechanics hard-block
    for d_rule in VERSION_DRIFT_BLOCKS_POE2:
        if scope not in d_rule.scopes:
            continue
        trig = _match_drift_block(raw, norm, d_rule)
        if trig is not None:
            raise LegacyPatchTrapError(
                trigger=trig, patch_version=d_rule.patch_version,
                error=d_rule.error, remediation=d_rule.remediation,
                equivalent_command=d_rule.equivalent_command, rule_id=d_rule.rule_id,
            )

    warnings: list[HallucinationWarning] = []
    seen: set[str] = set()

    # 3. Same-name false friends & 0.1->0.5.5 version drift advisories
    for ff in SAME_NAME_FALSE_FRIENDS_POE2:
        if scope not in ff.scopes:
            continue
        trig = _match_ff(raw, norm, exact_en, exact_zh, ff)
        if trig is not None and ff.rule_id not in seen:
            seen.add(ff.rule_id)
            w = HallucinationWarning(
                rule_id=ff.rule_id, game="poe2", category=ff.category, trigger=trig,
                summary=ff.summary, poe1_behavior=ff.poe1_behavior, poe2_behavior=ff.poe2_behavior,
                patch_version=ff.patch_version, source_ref=ff.source_ref,
            )
            record_warning(w, emit_stderr=emit_stderr)
            warnings.append(w)

    # 4. CN mistranslation collision & PoE1-ZH alias advisories
    if scope in (
        GuardrailScope.GEM_NAME, GuardrailScope.DATA_QUERY, GuardrailScope.STAT_QUERY,
        GuardrailScope.CALC_QUERY, GuardrailScope.NOTABLE_OR_OIL, GuardrailScope.SLOT,
    ):
        for adv in ZH_COLLISION_ADVISORIES:
            if scope not in adv.scopes:
                continue
            for zh_p in adv.zh_phrases:
                hit = match_phrase_zh(raw, zh_p, adv.exclude_zh_compounds)
                if hit is not None and adv.rule_id not in seen:
                    seen.add(adv.rule_id)
                    w = HallucinationWarning(
                        rule_id=adv.rule_id, game="poe2", category="zh_mistranslation", trigger=hit,
                        summary=adv.summary, poe1_behavior=adv.poe1_behavior, poe2_behavior=adv.poe2_behavior,
                        canonical_term=adv.canonical_term, source_ref=adv.source_ref,
                    )
                    record_warning(w, emit_stderr=emit_stderr)
                    warnings.append(w)
                    break

        alias_hit = lookup_compact_alias(exact_zh)
        if alias_hit is not None:
            poe1_zh, en_canon, poe2_zh = alias_hit
            slug = re.sub(r"[^A-Z0-9]+", "_", en_canon.upper()).strip("_")
            rid = f"ZH_ALIAS_{slug}"
            if rid not in seen:
                seen.add(rid)
                w = HallucinationWarning(
                    rule_id=rid, game="poe2", category="zh_mistranslation", trigger=raw.strip(),
                    summary=f"PoE1 CN term `{poe1_zh}` was renamed in PoE2 to `{poe2_zh}` (`{en_canon}`).",
                    poe1_behavior=f"PoE1 CN: `{poe1_zh}`", poe2_behavior=f"PoE2 CN: `{poe2_zh}` (EN: `{en_canon}`)",
                    canonical_term=f"{en_canon} / {poe2_zh}",
                    source_ref="Data/poe2/zh-rCN/{gems,items,passives,stats,uniques}.json",
                )
                record_warning(w, emit_stderr=emit_stderr)
                warnings.append(w)
    return warnings


def check_input_item_raw(
    raw_text: str | None,
    *,
    expected_game: str = "poe2",
    emit_stderr: bool = True,
) -> list[HallucinationWarning]:
    """Scan raw PoB item text after discarding random Rare/Magic Line-1 names & flavour text."""
    if not raw_text or not str(raw_text).strip():
        return []
    scannable = extract_scannable_item_parts(raw_text)["scannable_text"]
    if not scannable:
        return []
    return check_input(scannable, GuardrailScope.ITEM_RAW, expected_game=expected_game, emit_stderr=emit_stderr)


def normalize_poe2_equipment_slot(
    raw_slot: str | None,
    *,
    command: str = "uniques",
    allow_empty: bool = False,
) -> str:
    """Validate and canonicalize a PoE2 equipment slot name before daemon/build checks."""
    raw = str(raw_slot or "").strip()
    if not raw:
        if allow_empty:
            return ""
        raise DaemonRequestError(
            f"`{command}` requires `--slot <slot>` (e.g. Ring 1, Amulet, Body Armour, Flask 1, Charm 1)."
        )
    check_input(raw, GuardrailScope.SLOT, expected_game="poe2", emit_stderr=False)
    canon = resolve_poe2_slot_name(raw)
    if canon is None:
        raise DaemonRequestError(
            f"[POE2-INVALID-SLOT] Invalid PoE2 equipment slot {raw!r} for `{command}`. "
            f"Valid slots: {', '.join(VALID_POE2_EQUIPMENT_SLOTS)}."
        )
    return canon


__all__ = [
    "ALL_QUERY_SCOPES",
    "POE1_ONLY_CLASSES",
    "POE1_ONLY_TRAPS_FOR_POB2",
    "POE1_ZH_TO_POE2_ALIAS",
    "POE2_ONLY_CLASSES",
    "POE2_ONLY_REVERSE_TRAPS_FOR_POB1",
    "SAME_NAME_FALSE_FRIENDS_POE2",
    "URL_GATE_RULES",
    "VALID_POE2_EQUIPMENT_SLOTS",
    "VERSION_DRIFT_BLOCKS_POE2",
    "ZH_COLLISION_ADVISORIES",
    "ZH_MISTRANSLATION_TRAPS",
    "FalseFriendRule",
    "GuardrailScope",
    "TrapRule",
    "VersionDriftBlockRule",
    "ZhMistranslationTrap",
    "check_input",
    "check_input_item_raw",
    "emit_stderr_warnings_from_dicts",
    "extract_scannable_item_parts",
    "get_active_warning_dicts",
    "get_active_warnings",
    "get_auth_env_status",
    "match_phrase_zh",
    "match_word_en",
    "normalize_guardrail_text",
    "normalize_poe2_equipment_slot",
    "normalize_zh_compact",
    "redact",
    "redact_secrets",
    "redact_url_credentials",
    "reset_cli_warnings",
    "resolve_poe1_zh_alias",
    "resolve_poe2_slot_name",
    "validate_build_xml",
    "validate_command",
    "validate_engine_root",
    "validate_url",
    "warning_context",
]
