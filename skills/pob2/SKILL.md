---
name: pob-poe2
description: Path of Exile 2 (PoE2 0.5.x / 流放之路2 / 流放之路：降临) headless PoB2 diagnosis, crafting odds & MDP routes (`pob2 craft`), WeGame share-link import, instillation & rune Pareto ranking, resistance cap+overcap fixing, and Spirit knapsack via `pob2`. Trigger on PoE2, PoB2, pob2, 流放之路2, 流放之路：降临, WeGame 分享链接, 精魂, 护符, 涂灌, 蒸馏情感, 液态情感, 灵魂核心, 符文, 神像, 瓦尔宝珠, 做装, 削切, 预兆, 渎灵, 神圣化, 双武器组, Waystone, Spirit, Charm, Soul Core, Instill, Omen, Desecrated, Sanctified.
---

# PoE2 0.5.5+ Headless PoB2 Skill (`pob24agents`, command `pob2`)

## 1. Provenance, Isolation & PoE2 Domain Iron Laws
1. **Measured Numbers Only** — Every DPS, EHP, max-hit, resistance, Spirit, and crafting probability MUST come from `pob2` output. Never estimate from memory or import PoE1 mechanics into PoE2.
2. **PoE2 `0.5.5+` Rule Boundaries (Never Confuse With PoE1)**:
   - **Defenses**: Armour applies **BEFORE** Elemental Resistances; 50% base Block cap; Deflection; Chaos deals **2x** vs ES; Bleed & Poison bypass ES.
   - **Conversion & Quality**: Two-step final-damage-type conversion only (no multi-stage scaling); Quality = **1% MORE**.
   - **Skills, Spirit & Weapon Sets**: `gemFamily` enforces 1-copy per skill; normal supports stack across skills while only Lineage (`isLineage`) and global reservation supports are 1-copy; Meta Energy replaces fixed trigger-tick breakpoints; Spirit pool (`Spirit - SpiritUnreserved`), `Charm 1..3`, and Dual Weapon Sets (`allocMode 0|1|2`, `set1`/`set2`) are first-class.
   - **Item & Crafting Legality**: Max **1 `Crafted` mod slot** per item (fractured still occupies it; `Desecrated` max 1 independently). Scouring, Alteration, Homogenising Omens, and Recombinator do not exist in `0.5.5`. Respect `0.5.5` resistance override lines and `Sanctified` (`0.78x–1.22x`) roll bounds.
3. **Ground Uncertain Mechanics First** — Call `pob2 ground "<kw>"` (`<15ms`, daemon-free) before judging unfamiliar PoE2 uniques, keystones, or skills. If PoB2 genuinely does not model a mechanic, tag it explicitly as **`[PoB2未模拟]`** instead of guessing.
4. **Resistance Iron Law (Cap + Overcap, Never Equalize)** — Target elemental resistance **cap + overcap headroom** (`--overcap 20` default, `--chaos-floor`/`--chaos-target` for Chaos) against exposure/curse/minus-max maps. **Never** waste affixes trying to equalize Fire/Cold/Lightning ("三抗配平").
5. **Output & Credential Hygiene** — Markdown is default; pass `--json` (`--compact`, `--out <file>`) for structured data. Core read commands accept `--section <s1,s2>` and `--fields <p1,p2>` projection (invalid paths fail fast with exit `1`). Never read, print, or persist cookies/tokens/secrets.

## 2. Core Workflow & Command Reference (D1=A CLI Contract)

Workflow: **`load` / `import` -> `diagnose` / `audit` -> `what-if` / solvers (`fix-gaps`, `spirit`, `instill`, `rune`, `item`, `craft`) -> `--apply` (or `undo`/`redo`) -> `export`**.

### Build I/O & Inspection (`--section` / `--fields` supported on core reads)
```powershell
pob2 load <source> [--target main|ref]                   # pobb.in, poe2db.tw, poe.ninja/poe2, maxroll, WeGame share link, XML, PoB2 code
pob2 import <source> [--load] [--xml-out out.xml]        # Decode/convert build (incl. passwordless CN WeGame share link)
pob2 diagnose [--target main] [--overcap 20] [--chaos-floor 0] [--node-power] [--section S] [--fields F]
pob2 summary [--target main] [--section S] [--fields F]  # Fast build snapshot with PoB2 version/patch stamp
pob2 audit [--target main] [--overcap 20] [--chaos-floor 0]  # 4-dim dehydration + [PoB2未模拟] warnings
pob2 compare [REF_SOURCE] [--ref-target ref] [--target main] # 5-dim Compare Power Report
pob2 diff <source> [--target main]                       # Structural diff against another build source
pob2 items [--slot S] | pob2 skills | pob2 tree | pob2 config | pob2 notes | pob2 export [--xml]
pob2 ground "<kw>" [--category C] [--limit 10]           # <15ms daemon-free PoE2 entity & mechanic grounding
```

### CoW Zero-Residue What-If (Rolls back by default; pass `--apply` to persist, `pob2 undo` / `pob2 redo` to revert)
```powershell
pob2 what-if equip --slot "Weapon 1" (--item-text "<txt>" | --item-file f | --unequip) [--weapon-swap] [--apply]  # or `--unequip "Body Armour"`
pob2 what-if tree --tree-node <id> [--alloc-mode 0|1|2] [--attribute-node <id> --attribute str|dex|int] [--apply]
pob2 what-if skill --gem-group G [--gem-index I] [--gem-level N] [--gem-quality Q] [--gem-enabled/--no-gem-enabled] [--set1/--set2] [--apply]
pob2 what-if config [--level N] [--config-key K --config-value V] [--custom-mods "<txt>"] [--apply]
pob2 what-if apply --spec '{"config":{"level":95,"enemyLevel":82},"tree":{"tree_node":331}}' [--apply]  # Flat or nested JSON / `--spec-file`
```

### Solvers: Gear, Instillations, Runes, Gaps & Spirit
```powershell
pob2 item inspect [--slot S | --text "<txt>" | --file f] [--base B] [--ilvl 82] # Mod pool, open slots, 0.5.5 legality
pob2 item db --slot S [--sort CombinedDPS|TotalEHP] [--limit 15] [--no-sanctified] # Unique ranking (JSON key: results)
pob2 instill rank [--metric M] [--budget N] [--limit 15] # Pareto cost-efficiency across 875 ordered amulet instillations
pob2 instill lookup "<kw>" | pob2 instill bridge "<emotion>"  # Ordered [Slot 1->2->3] & Distilled/Liquid emotion bridge
pob2 rune rank --slot S [--type T] [--base B] [--shaman] | pob2 rune inspect "<name>" # `--type` auto-maps unique armour slots; pass `--slot` for weapons/rings
pob2 fix-gaps [--overcap 20] [--chaos-target 0] [--apply]     # 5-stage zero-buy attribute & res cap/overcap solver
pob2 spirit [--budget 220] [--apply]                          # Discrete Spirit deficit rescue or forward buff knapsack
```

### PoE2 `0.5.5` Crafting Solver (Always use `--want`, NEVER `--target`)
```powershell
pob2 craft inspect [--text "<txt>" | --file f | --base B [--ilvl 82]]  # `--base` works standalone for base mod-pool summary
pob2 craft odds --base "<Base>" --ilvl 82 (--want "T1 Spell Damage" [--want ...] | --outcomes) [--lever L] [--omen O]
pob2 craft plan --base "<Base>" --ilvl 82 --want "<mod>" [--want ...] [--base-cost 2] [--buy-price P] [--exclude X]
pob2 craft next (--text "<txt>" | --file f) --want "<mod>" [--want ...] [--protect-existing] [--buy-price P]
```

### Lifecycle & Language
```powershell
pob2 bootstrap [--proxy URL] | pob2 status | pob2 lang [zh-CN|zh-TW|en] | pob2 update [--check] | pob2 stop | pob2 help --brief
```

## 3. Slot Contract, Exit Codes & Error Handling
- **Slot Disambiguation**: `--target SLUG` selects the daemon build slot (`main`, `ref`; defaults to `POB2_SLOT` or `main`). `--slot SLOT` selects an equipment slot (`Weapon 1..2`, `Weapon 1..2 Swap`, `Helmet`, `Body Armour`, `Gloves`, `Boots`, `Amulet`, `Ring 1..3`, `Belt`, `Flask 1..2`, `Charm 1..3`). Crafting goals use repeatable `--want`.
- **Exit Codes**:
  - `0` — Success.
  - `1` — Request, parameter, guardrail, or invalid `--section`/`--fields` error. Under `--json`, stdout emits a structured JSON error envelope and `stderr` is empty.
  - `5` — Local PoB2 daemon or LuaJIT engine fault. Inspect `pob2 status`; recover with `pob2 stop` or `pob2 bootstrap`.
