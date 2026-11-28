# SPDX-License-Identifier: GPL-3.0-only
"""Bidirectional hard-blocking gates for XML, URLs, engine root dirs, and PoE1 CLI commands (L0)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import CrossGameHallucinationError

_XML_NOTES_STRIP_RE = re.compile(r"<Notes\b[^>]*>[\s\S]*?</Notes>", re.IGNORECASE)
_XML_ROOT_TAG_RE = re.compile(r"<\s*(PathOfBuilding2?)\b")
_XML_TREE_VER_RE = re.compile(r'\btreeVersion\s*=\s*["\']([^"\']+)["\']')
_XML_TARGET_VER_RE = re.compile(r'\btargetVersion\s*=\s*["\']([^"\']+)["\']')
_XML_CLASS_RE = re.compile(r'<\s*Build\b[^>]*\bclassName\s*=\s*["\']([^"\']+)["\']')
_XML_SLOT_RE = re.compile(r'<\s*Slot\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']')

POE1_ONLY_CLASSES: frozenset[str] = frozenset({"marauder", "duelist", "shadow", "templar", "scion"})
POE2_ONLY_CLASSES: frozenset[str] = frozenset({"warrior", "monk", "mercenary", "sorceress", "huntress", "druid"})


def validate_build_xml(
    xml_text: str,
    *,
    expected_game: str = "poe2",
    source_hint: str | None = None,
) -> None:
    """Hard-block cross-game PoB1 vs PoB2 XML builds (exit code 1)."""
    cleaned = _XML_NOTES_STRIP_RE.sub("", (xml_text or "").lstrip("\ufeff").strip())
    if not cleaned.startswith("<"):
        return

    root_m = _XML_ROOT_TAG_RE.search(cleaned[:1024])
    root_tag = root_m.group(1) if root_m else None
    tree_versions = _XML_TREE_VER_RE.findall(cleaned)
    target_versions = _XML_TARGET_VER_RE.findall(cleaned)
    class_m = _XML_CLASS_RE.search(cleaned[:4096])
    class_name = class_m.group(1).strip().lower() if class_m else ""
    slots = _XML_SLOT_RE.findall(cleaned)
    src_arg = f' "{source_hint}"' if source_hint else " <source>"

    if expected_game == "poe2":
        if root_tag == "PathOfBuilding":
            tv = f" (treeVersion={tree_versions[0]!r})" if tree_versions else ""
            raise CrossGameHallucinationError(
                game="poe2", detected_game="poe1", trigger=f"<PathOfBuilding>{tv}",
                error=f"Build XML uses PoE1 root `<PathOfBuilding>`{tv}; PoB2 strictly requires `<PathOfBuilding2>`.",
                remediation="Load PoE1 builds with `pob`, or export a PoE2 build from PathOfBuilding-PoE2.",
                equivalent_command=f"pob load{src_arg}", rule_id="XML_POE1_ROOT_IN_POB2",
            )
        for tv in tree_versions:
            if re.match(r"^(?:2|3)_\d+", tv.strip()):
                raise CrossGameHallucinationError(
                    game="poe2", detected_game="poe1", trigger=f'treeVersion="{tv}"',
                    error=f"Build spec uses PoE1 passive tree version `{tv}` (PoE2 uses `0_1`..`0_5+`).",
                    remediation="Use `pob` for PoE1 3.x builds.",
                    equivalent_command=f"pob load{src_arg}", rule_id="XML_POE1_TREE_IN_POB2",
                )
        if class_name in POE1_ONLY_CLASSES:
            raise CrossGameHallucinationError(
                game="poe2", detected_game="poe1", trigger=f'className="{class_name}"',
                error=f"Build uses PoE1 class `{class_name}` (not playable in PoE2 0.5.5).",
                remediation="Use `pob` for PoE1 builds, or PoE2 classes (Warrior/Monk/Mercenary/Sorceress/Witch/Ranger/Huntress/Druid).",
                equivalent_command=f"pob load{src_arg}", rule_id="XML_POE1_CLASS_IN_POB2",
            )
        for sl in slots:
            if sl in ("Flask 3", "Flask 4", "Flask 5", "Graft 1", "Graft 2") or "Abyssal Socket" in sl:
                raise CrossGameHallucinationError(
                    game="poe2", detected_game="poe1", trigger=f'Slot name="{sl}"',
                    error=f"Build XML contains PoE1-only equipment slot `{sl}`.",
                    remediation="Use `pob` for PoE1 builds (PoE2 has Flask 1..2 + Charm 1..3).",
                    equivalent_command=f"pob load{src_arg}", rule_id="XML_POE1_SLOT_IN_POB2",
                )
    elif expected_game == "poe1":
        if root_tag == "PathOfBuilding2":
            tv = f" (treeVersion={tree_versions[0]!r})" if tree_versions else ""
            raise CrossGameHallucinationError(
                game="poe1", detected_game="poe2", trigger=f"<PathOfBuilding2>{tv}",
                error=f"Build XML uses PoE2 root `<PathOfBuilding2>`{tv}; `pob` only supports PoE1 `<PathOfBuilding>`.",
                remediation="Load PoE2 builds with `pob2`.",
                equivalent_command=f"pob2 import{src_arg}", rule_id="XML_POE2_ROOT_IN_POB1",
            )
        for tv in (*tree_versions, *target_versions):
            if re.match(r"^0_\d+", tv.strip()):
                raise CrossGameHallucinationError(
                    game="poe1", detected_game="poe2", trigger=f'treeVersion="{tv}"',
                    error=f"Build uses PoE2 version `{tv}` (PoE1 uses `3_*`).",
                    remediation="Load PoE2 builds with `pob2`.",
                    equivalent_command=f"pob2 import{src_arg}", rule_id="XML_POE2_TREE_IN_POB1",
                )
        if class_name in POE2_ONLY_CLASSES:
            raise CrossGameHallucinationError(
                game="poe1", detected_game="poe2", trigger=f'className="{class_name}"',
                error=f"Build uses PoE2-only class `{class_name}`.",
                remediation="Load PoE2 builds with `pob2`.",
                equivalent_command=f"pob2 import{src_arg}", rule_id="XML_POE2_CLASS_IN_POB1",
            )
        for sl in slots:
            if sl in ("Charm 1", "Charm 2", "Charm 3"):
                raise CrossGameHallucinationError(
                    game="poe1", detected_game="poe2", trigger=f'Slot name="{sl}"',
                    error=f"Build XML contains PoE2 belt slot `{sl}`.",
                    remediation="Load PoE2 builds with `pob2`.",
                    equivalent_command=f"pob2 import{src_arg}", rule_id="XML_POE2_SLOT_IN_POB1",
                )


@dataclass(frozen=True)
class UrlGateRule:
    rule_id: str
    belongs_to: str
    pattern: re.Pattern[str]
    label: str
    counterpart_hint: str
    source_ref: str
    counterpart_command: str | None = None


_COUNTERPART_SUBCOMMAND_MAP: dict[tuple[str, str], str] = {
    ("pob", "import"): "load",
    ("pob2", "profile"): "import",
}


URL_GATE_RULES: tuple[UrlGateRule, ...] = (
    # PoE1 URLs blocked in pob2
    UrlGateRule(
        "URL_POE1_POEDB", "poe1",
        re.compile(r"^(?:https?://)?(?:www\.|cdn\.)?poedb\.tw(?=[/?#:]|$)", re.IGNORECASE),
        "poedb.tw (PoE1 Database)", "Use `https://poe2db.tw/...` for PoE2.", "poe2db.tw",
    ),
    UrlGateRule(
        "URL_POE1_NINJA", "poe1",
        re.compile(r"^(?:https?://)?(?:www\.)?poe\.ninja/(?:poe1|builds|pob|economy|api/data)(?=[/?#]|$)", re.IGNORECASE),
        "poe.ninja PoE1 endpoint", "Use `https://poe.ninja/poe2/...` for PoE2.", "poe.ninja/poe2",
    ),
    UrlGateRule(
        "URL_POE1_TRADE", "poe1",
        re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)?pathofexile\.com/(?:api/trade|trade|api/leagues)(?=[/?#]|$)", re.IGNORECASE),
        "pathofexile.com/trade or /api/leagues (PoE1)",
        "PoE2 uses `/trade2/`, `/api/trade2/search/poe2/<league>`, and `/api/trade2/data/leagues`.",
        "pathofexile.com/trade2",
    ),
    UrlGateRule(
        "URL_POE1_CN_PROFILE", "poe1",
        re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)?poe\.game\.qq\.com(?=[/?#:]|$)", re.IGNORECASE),
        "poe.game.qq.com (PoE1 CN Official Domain)",
        "PoE2 CN uses cookie-free WeGame share URLs `https://www.wegame.com.cn/helper/poe2/#/share/<code>` via `pob2 import`.",
        "wegame.com.cn/helper/poe2",
    ),
    UrlGateRule(
        "URL_POE1_WIKI", "poe1",
        re.compile(r"^(?:https?://)?(?:www\.)?(?:poewiki\.net|pathofexile\.fandom\.com)(?=[/?#:]|$)", re.IGNORECASE),
        "poewiki.net (PoE1 Wiki)", "Use `https://www.poe2wiki.net` for PoE2.", "poe2wiki.net",
    ),
    UrlGateRule(
        "URL_POE1_MAXROLL", "poe1",
        re.compile(r"^(?:https?://)?(?:www\.)?maxroll\.gg/poe(?=[/?#]|$)", re.IGNORECASE),
        "maxroll.gg/poe (PoE1)", "PoE2 Maxroll URLs use `https://maxroll.gg/poe2/...`.", "maxroll.gg/poe2",
    ),
    UrlGateRule(
        "URL_POE1_MOBALYTICS", "poe1",
        re.compile(r"^(?:https?://)?(?:www\.)?mobalytics\.gg/poe(?=[/?#]|$)", re.IGNORECASE),
        "mobalytics.gg/poe (PoE1)", "PoE2 Mobalytics URLs use `https://mobalytics.gg/poe-2/...`.", "mobalytics.gg/poe-2",
    ),
    UrlGateRule(
        "URL_POE1_URI_SCHEME", "poe1",
        re.compile(r"^pob:(?://|[/\\])", re.IGNORECASE),
        "pob:// protocol URI (PoE1)", "PoE2 uses `pob2://` (`PoE2/Modules/BuildSiteTools.lua`).", "PoE2/Modules/BuildSiteTools.lua",
    ),
    UrlGateRule(
        "URL_POE1_POBCODES", "poe1",
        re.compile(r"^(?:https?://)?(?:api\.)?pob\.codes(?=[/?#:]|$)", re.IGNORECASE),
        "pob.codes (PoE1-only share site)", "Use `pobb.in`, `poe2db.tw/pob`, `poe.ninja/poe2/pob`, or WeGame share URLs.", "PoE2/Modules/BuildSiteTools.lua",
    ),
    # PoE2 URLs blocked in pob1
    UrlGateRule(
        "URL_POE2_POE2DB", "poe2",
        re.compile(r"^(?:https?://)?(?:www\.|cdn\.)?poe2db\.tw(?=[/?#:]|$)", re.IGNORECASE),
        "poe2db.tw (PoE2 Database)", "Use `pob2` for PoE2DB links or `poedb.tw` for PoE1.", "poe2db.tw",
    ),
    UrlGateRule(
        "URL_POE2_NINJA", "poe2",
        re.compile(r"^(?:https?://)?(?:(?:www\.)?poe\.ninja/poe2|(?:www\.)?poe2\.ninja)(?=[/?#:]|$)", re.IGNORECASE),
        "poe.ninja/poe2 (PoE2)", "Use `pob2` for PoE2 ninja links.", "poe.ninja/poe2",
    ),
    UrlGateRule(
        "URL_POE2_TRADE2", "poe2",
        re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)?pathofexile\.com/(?:api/)?trade2(?=[/?#]|$)", re.IGNORECASE),
        "pathofexile.com/trade2 (PoE2 Trade)", "Use `pob2` for PoE2 `/trade2/`.", "pathofexile.com/trade2",
    ),
    UrlGateRule(
        "URL_POE2_WEGAME", "poe2",
        re.compile(r"^(?:https?://)?www\.wegame\.com\.cn/helper/poe2(?=[/?#]|$)", re.IGNORECASE),
        "wegame.com.cn/helper/poe2 (PoE2 CN WeGame Share)", "Use `pob2 import <wegame-url>` for PoE2 CN builds.", "wegame.com.cn/helper/poe2",
    ),
    UrlGateRule(
        "URL_POE2_WIKI", "poe2",
        re.compile(r"^(?:https?://)?(?:www\.)?poe2wiki\.net(?=[/?#:]|$)", re.IGNORECASE),
        "poe2wiki.net (PoE2 Wiki)", "Use `pob2` for PoE2 Wiki or `poewiki.net` for PoE1.", "poe2wiki.net",
    ),
    UrlGateRule(
        "URL_POE2_MAXROLL", "poe2",
        re.compile(r"^(?:https?://)?(?:www\.)?maxroll\.gg/poe2(?=[/?#]|$)", re.IGNORECASE),
        "maxroll.gg/poe2 (PoE2 Maxroll)", "Use `pob2` for PoE2 Maxroll links or `maxroll.gg/poe` for PoE1.", "maxroll.gg/poe2",
    ),
    UrlGateRule(
        "URL_POE2_MOBALYTICS", "poe2",
        re.compile(r"^(?:https?://)?(?:www\.)?mobalytics\.gg/poe-2(?=[/?#]|$)", re.IGNORECASE),
        "mobalytics.gg/poe-2 (PoE2 Mobalytics)", "Use `pob2` for PoE2 Mobalytics links.", "mobalytics.gg/poe-2",
    ),
    UrlGateRule(
        "URL_POE2_URI_SCHEME", "poe2",
        re.compile(r"^pob2:(?://|[/\\])", re.IGNORECASE),
        "pob2:// protocol URI (PoE2)", "Use `pob2` for `pob2://` URIs.", "PoE2/Modules/BuildSiteTools.lua",
    ),
)


def validate_url(
    text: str,
    *,
    expected_game: str = "poe2",
    cli_subcommand: str = "import",
) -> None:
    """Block cross-game URLs bidirectionally."""
    cand = (text or "").strip()
    if not cand:
        return
    for rule in URL_GATE_RULES:
        if rule.belongs_to != expected_game and rule.pattern.search(cand):
            other_cli = "pob" if expected_game == "poe2" else "pob2"
            subcmd = rule.counterpart_command or _COUNTERPART_SUBCOMMAND_MAP.get(
                (other_cli, cli_subcommand), cli_subcommand
            )
            raise CrossGameHallucinationError(
                game=expected_game, detected_game=rule.belongs_to, trigger=cand,
                error=f"URL matches {rule.label}, which belongs to {rule.belongs_to.upper()}.",
                remediation=rule.counterpart_hint,
                equivalent_command=f'{other_cli} {subcmd} "{cand}"',
                rule_id=rule.rule_id,
            )


def validate_engine_root(
    path: Path | str | None,
    *,
    expected_game: str = "poe2",
    source_label: str = "POB2_ENGINE_DIR",
) -> None:
    """Reject a PoE1-only engine directory in `pob2` or a PoE2-only engine directory in `pob`."""
    if path is None:
        return
    p = Path(path)
    if not p.exists():
        return
    has_poe1 = (p / "PoE1").is_dir()
    has_poe2 = (p / "PoE2").is_dir()
    if expected_game == "poe2" and has_poe1 and not has_poe2:
        raise CrossGameHallucinationError(
            game="poe2", detected_game="poe1", trigger=f"{source_label}={p.name}",
            error="Engine directory contains `PoE1/` only and has no `PoE2/` subdirectory.",
            remediation="Set `POB2_ENGINE_DIR` to a PathOfBuilding-PoE2 / PobTools PoE2 directory containing `PoE2/`.",
            equivalent_command="pob2 about", rule_id="ENV_POE1_ENGINE_IN_POB2",
        )
    if expected_game == "poe1" and has_poe2 and not has_poe1:
        raise CrossGameHallucinationError(
            game="poe1", detected_game="poe2", trigger=f"{source_label}={p.name}",
            error="Engine directory contains `PoE2/` only and has no `PoE1/` subdirectory.",
            remediation="Point `POB_ROOT` to a PoE1 engine directory.",
            equivalent_command="pob bootstrap", rule_id="ENV_POE2_ENGINE_IN_POB1",
        )


_POE1_ONLY_CMD_RE: tuple[tuple[str, re.Pattern[str], str, str], ...] = (
    (
        "CMD_POE1_BENCH",
        re.compile(r"(?:^|\s)(?:pob2?\s+)?bench(?:\s|$)", re.IGNORECASE),
        "PoE2 has no Hideout Crafting Bench (`bench` is PoE1-only).",
        "pob2 craft plan --base <base> --want <mod>",
    ),
    (
        "CMD_POE1_MAP_REGEX",
        re.compile(r"(?:^|\s)(?:pob2?\s+)?map-regex(?:\s|$)", re.IGNORECASE),
        "`map-regex` is PoE1-only (PoE2 endgame uses Waystones, not PoE1 Maps).",
        "pob2 ground waystone",
    ),
    (
        "CMD_POE1_TATTOO",
        re.compile(r"(?:^|\s)(?:pob2?\s+)?tattoo(?:\s|$)", re.IGNORECASE),
        "Passive-tree `tattoo` commands belong to PoE1 (PoE2 uses Runes/Soul Cores in equipment sockets).",
        "pob2 ground runes",
    ),
    (
        "CMD_POE1_ANOINT",
        re.compile(r"(?:^|\s)(?:pob2?\s+)?(?:item\s+)?anoint(?:\s|$)", re.IGNORECASE),
        "PoE1 Blight Oil `anoint` does not exist in PoE2 (PoE2 uses Delirium Liquid Emotions to Instill amulets).",
        "pob2 ground liquid emotions",
    ),
)


def validate_command(command_text: str, *, expected_game: str = "poe2") -> None:
    """Block PoE1-only CLI subcommands sent to `pob2`."""
    if expected_game != "poe2" or not command_text:
        return
    for rule_id, pat, err, equiv in _POE1_ONLY_CMD_RE:
        m = pat.search(command_text.strip())
        if m:
            raise CrossGameHallucinationError(
                game="poe2", detected_game="poe1", trigger=m.group(0).strip(),
                error=err, remediation=f"Use the PoE2 workflow instead: `{equiv}`.",
                equivalent_command=equiv, rule_id=rule_id,
            )


VALID_POE2_EQUIPMENT_SLOTS: tuple[str, ...] = (
    "Weapon 1",
    "Weapon 2",
    "Weapon 1 Swap",
    "Weapon 2 Swap",
    "Helmet",
    "Body Armour",
    "Gloves",
    "Boots",
    "Amulet",
    "Ring 1",
    "Ring 2",
    "Ring 3",
    "Belt",
    "Flask 1",
    "Flask 2",
    "Charm 1",
    "Charm 2",
    "Charm 3",
    "Arm 1",
    "Arm 2",
    "Leg 1",
    "Leg 2",
)

_JEWEL_SOCKET_PARENTS: frozenset[str] = frozenset(
    {
        "Weapon 1",
        "Weapon 2",
        "Weapon 1 Swap",
        "Weapon 2 Swap",
        "Helmet",
        "Gloves",
        "Body Armour",
        "Boots",
        "Belt",
        "Ring 1",
        "Ring 2",
        "Ring 3",
    }
)
_PASSIVE_JEWEL_SLOT_RE = re.compile(r"^jewel\s+(\d+)$", re.IGNORECASE)
_EQUIP_JEWEL_SOCKET_RE = re.compile(r"^(.+?)\s+jewel\s+socket\s+([1-6])$", re.IGNORECASE)

_POE2_SLOT_LOOKUP: dict[str, str] = {
    **{s.lower(): s for s in VALID_POE2_EQUIPMENT_SLOTS},
    **{s.lower().replace(" ", ""): s for s in VALID_POE2_EQUIPMENT_SLOTS},
    "weapon": "Weapon 1",
    "mainhand": "Weapon 1",
    "main hand": "Weapon 1",
    "offhand": "Weapon 2",
    "off hand": "Weapon 2",
    "weapon swap 1": "Weapon 1 Swap",
    "weaponswap1": "Weapon 1 Swap",
    "weapon swap 2": "Weapon 2 Swap",
    "weaponswap2": "Weapon 2 Swap",
    "offhand swap": "Weapon 2 Swap",
    "helm": "Helmet",
    "body": "Body Armour",
    "chest": "Body Armour",
    "glove": "Gloves",
    "boot": "Boots",
    "ring": "Ring 1",
    "flask": "Flask 1",
    "charm": "Charm 1",
    "主手": "Weapon 1",
    "副手": "Weapon 2",
    "头盔": "Helmet",
    "胸甲": "Body Armour",
    "手套": "Gloves",
    "鞋子": "Boots",
    "项链": "Amulet",
    "护身符": "Amulet",
    "戒指": "Ring 1",
    "戒指1": "Ring 1",
    "戒指 1": "Ring 1",
    "戒指2": "Ring 2",
    "戒指 2": "Ring 2",
    "戒指3": "Ring 3",
    "戒指 3": "Ring 3",
    "腰带": "Belt",
    "药剂": "Flask 1",
    "药剂1": "Flask 1",
    "药剂 1": "Flask 1",
    "药剂2": "Flask 2",
    "药剂 2": "Flask 2",
    "护符": "Charm 1",
    "咒符": "Charm 1",
    "护符1": "Charm 1",
    "护符 1": "Charm 1",
    "咒符1": "Charm 1",
    "咒符 1": "Charm 1",
    "护符2": "Charm 2",
    "护符 2": "Charm 2",
    "咒符2": "Charm 2",
    "咒符 2": "Charm 2",
    "护符3": "Charm 3",
    "护符 3": "Charm 3",
    "咒符3": "Charm 3",
    "咒符 3": "Charm 3",
}


def resolve_poe2_slot_name(raw_slot: str) -> str | None:
    """Return canonical PoB2 slot name if *raw_slot* is a valid PoE2 slot or alias, else ``None``."""
    s = (raw_slot or "").strip()
    if not s:
        return None
    norm = re.sub(r"\s+", " ", s.lower().replace("-", " ").replace("_", " ")).strip()
    hit = _POE2_SLOT_LOOKUP.get(norm) or _POE2_SLOT_LOOKUP.get(norm.replace(" ", ""))
    if hit is not None:
        return hit
    jm = _PASSIVE_JEWEL_SLOT_RE.match(norm)
    if jm and int(jm.group(1)) >= 100:
        return f"Jewel {int(jm.group(1))}"
    em = _EQUIP_JEWEL_SOCKET_RE.match(norm)
    if em:
        parent = _POE2_SLOT_LOOKUP.get(em.group(1).strip()) or _POE2_SLOT_LOOKUP.get(
            em.group(1).replace(" ", "")
        )
        if parent in _JEWEL_SOCKET_PARENTS:
            return f"{parent} Jewel Socket {em.group(2)}"
    return None
