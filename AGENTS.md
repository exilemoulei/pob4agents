# pob4agents — Dual-Track Monorepo Router

This repository hosts two physically isolated, self-contained headless Path of Building CLI packages:

| Track | Game | CLI | Package | License | Developer Guide |
| --- | --- | --- | --- | --- | --- |
| PoE1 | Path of Exile 1 | `pob` | `pob_cli/` | `GPL-3.0-or-later` | [pob_cli/AGENTS.md](pob_cli/AGENTS.md) |
| PoE2 | Path of Exile 2 (`0.5.5+`) | `pob2` | `pob2_cli/` | `GPL-3.0-only` | [pob2_cli/AGENTS.md](pob2_cli/AGENTS.md) |

> **Analyzing a build as a user?** Use `pob` via [skills/pob/SKILL.md](skills/pob/SKILL.md) (PoE1) or `pob2 --help` (PoE2) — no need to inspect source.

## Isolation & License Hard Rules

- **Zero cross-package imports:** `pob_cli/` and `pob2_cli/` are physically self-contained and MUST NEVER import each other or share runtime/env namespaces (`POB_*` vs `POB2_*`).
- **One-way license boundary:** NEVER copy `pob2_cli/` (`GPL-3.0-only`) code back into `pob_cli/` (`GPL-3.0-or-later`).
- **Read the track-specific `AGENTS.md` before editing code** in either package.

## Quick Gate Commands

```bash
# PoE1 track
python pob_cli/scripts/check.py --fast    # ruff + arch lint (<3 s)
python pob_cli/scripts/check.py           # full offline/engine gate

# PoE2 track
python pob2_cli/scripts/check.py --fast   # ruff + AST arch/isolation/leak lint (<3 s)
python pob2_cli/scripts/check.py          # fast gate + isolated offline pytest
```

## Workflow & Ratchets (A+B+C Fast-Track)

- Regular feature PRs and bugfixes must **not** edit `.github/workflows/engine.yml` ratchets; ratchets are raised once before a release to avoid cross-PR merge collisions.
- Never commit local machine paths, working notes (`PROGRESS.md`, `*_SPEC.md`, `*_PLAN.md`), engine binaries, or cookies/tokens.
