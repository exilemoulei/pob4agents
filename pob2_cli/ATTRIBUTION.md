# Attribution & Third-Party Notices (`pob24agents` / `pob2_cli`)

`pob24agents` (`pob2_cli/`) is licensed under **GNU General Public License v3.0 only (`GPL-3.0-only`)**.
See the repository root `LICENSE` for the full text of the GNU General Public License version 3.

---

## 1. `lifan-builds/poe2brain` (`GPL-3.0-only`, GPLv3 §7(b) Attribution)

> **Based on poe2brain by lifan-builds (https://github.com/lifan-builds/poe2brain)**

- **Copyright**: Copyright (c) 2026 lifan-builds
- **License**: GNU General Public License v3.0 only (`GPL-3.0-only`)
- **Derived & Modified Assets (2026-11-26)**:
  - Crafting modifier weight tables (`modsview_weights.json.gz`, `weights_overrides.json`) and crafting
    domain pool/rules ported to stdlib `dataclass` modules with Path of Exile 2 `0.5.0+` single-Crafted-mod
    enforcement and clean-room Bellman policy iteration.
  - Path of Exile 2 `0.5.5` verified mechanics fact cards (`mechanics_facts.json`) and version-drift guardrails.
  - Pure-stdlib `poe.ninja` Protobuf / `NDIC\x02` decoding and waystone regex reference designs.
- **Compliance Note**: `pob2_cli/` carries no code from AGPL-3.0 or unlicensed third-party repositories, and
  no `poe2brain`-derived code is copied back into `pob_cli/` (`GPL-3.0-or-later`).

---

## 2. `PathOfBuildingCommunity/PathOfBuilding-PoE2` (`MIT`)

`pob2_cli` drives the upstream Path of Building Community Fork (PoE2) calculation engine headless at runtime
(fetched on demand by `pob2 bootstrap` / `pob2 update`, not redistributed inside this git repository) and
interoperates with its Lua data structures, `manifest.xml`, and `0.5.5+` calculation modules.

- **Repository**: https://github.com/PathOfBuildingCommunity/PathOfBuilding-PoE2
- **License**: MIT License (Copyright (c) 2016 David Gowor and Path of Building Community contributors)

---

## 3. `ackness/pobr` (`MIT`)

The Path of Exile 2 CN WeGame character share-link import protocol (public read-only `wegame.pallas.poe2.Profile/*`
endpoints without cookies) references endpoint schemas documented in `ackness/pobr`.

- **Repository**: https://github.com/ackness/pobr
- **License**: MIT License

---

## 4. `Hsiung-Shao/PobTools-zh` (Runtime Bilingual Bridge & Dictionaries)

Runtime Simplified/Traditional Chinese translations (`Data/poe2/zh-rCN`, `Data/poe2/zh-rTW`) and bridge scripts
(`Data/bridge/bridge.lua`) are downloaded on demand at user bootstrap time from `PobTools-zh` releases and are
never committed into this repository.

- **Repository**: https://github.com/Hsiung-Shao/PobTools-zh

---

## 5. Additional Upstream Open-Source Data & Runtime Components

- **LuaJIT** (`2460b3ff93a1c955de3d62cfc825de7d68dc272e`, MIT License) & **starwing/luautf8** (`0.1.6`, MIT License):
  Pinned POSIX runtime matching PoB2 `+=` / `..=` LuaJIT syntax.
- **Unicode Unihan Database** (`Unihan_Variants.txt`, Unicode License v3):
  Source for Traditional-to-Simplified `HAN_FOLD` character folding in `han_fold.py`.
- **Upstream Community Data References**: `Kvan7/Exiled-Exchange-2` (MIT) and `repoe-fork/poe2` (MIT) as
  referenced by `poe2brain` data pipelines.

---

## 6. `pob4agents` Contributors

`pob2_cli` shares the five-layer architecture, slim LuaJIT host pattern, CoW transaction design, and CI
supply-chain standards pioneered by `exilemoulei/pob4agents` and its contributors, including:

- `@exilemoulei`
- `@restartgr`
- `@Capoouo`
- `@luyahan` (`Lu Yahan`)
- `@looperhuang`
