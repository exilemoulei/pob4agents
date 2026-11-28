# pob4agents (`pob_cli`)

pob4agents is a command-line tool, run as `pob`, that lets AI agents inspect, test and optimize Path of Exile 1 builds with the real Path of Building engine, run headless. It is a Python package (`pob_cli/`, Python 3.11+) whose daemon on `127.0.0.1` drives PobTools-zh's `pob-zh.exe` on Windows and PoB on a pinned LuaJIT runtime on macOS and Linux.

> **Analyzing, comparing, upgrading, or pricing a player's PoE1 build?** Use the `pob` CLI directly via [skills/pob/SKILL.md](../skills/pob/SKILL.md) (see §1.B for `compare` + `trade appraise` + `sweep` `op:"borrow"` A->B staged upgrades), [docs/guides/commands.md](../docs/guides/commands.md), or `pob help --brief` — no need to inspect `pob_cli/` source; this `AGENTS.md` is for developing `pob4agents` itself.

**Read [docs/architecture.md](../docs/architecture.md) before changing code.** It covers how a command reaches the engine, the module layers, engine paths and runtime state, environment variables, Chinese locale matching, the `R1`..`R13` constraints, adding a command, verification and CI.

## Commands

```bash
uv sync --project pob_cli --extra dev                 # dev env in pob_cli/.venv, locked to CI's versions; run the rest inside it
python pob_cli/scripts/check.py --fast                # ruff + architecture lint, under 3 s
python pob_cli/scripts/check.py --self-test           # proves every lint rule still fires
python pob_cli/scripts/check.py                       # full gate: fast gate + pytest with a pass/skip ratchet (see Tests)
POB_OFFLINE=1 python -m pytest pob_cli/tests -q       # what PR CI runs: no engine, no network
pob bootstrap                                         # managed engine in .runtime/engine, for real-engine tests
pob_cli/packaging/build_posix_runtime.sh ./runtime-dist  # macOS/Linux: LuaJIT + lua-utf8; copy its bin/ to .runtime/engine/bin before bootstrap
pyinstaller --noconfirm --clean pob_cli/packaging/pob.spec  # dist/pob; on macOS/Linux set POB_BUNDLE_RUNTIME=./runtime-dist first
python pob_cli/scripts/gen_han_fold.py Unihan.zip     # regenerate game_data/tables_han.py from Unicode's Unihan
python pob_cli/scripts/update_bench_costs.py          # regenerate data/bench_costs.json (needs an engine; online unless --from-local-poedb)
python pob_cli/scripts/update_map_mods.py --apt-dir <dir>  # regenerate data/map_mods_3_30.json from MIT awakened-poe-trade data
python pob_cli/scripts/update_apt_data.py --apt-dir <dir>  # regenerate data/apt_*.json.gz from an awakened-poe-trade checkout (offline)
gh workflow run engine.yml -R exilemoulei/pob4agents  # the real-engine gate on Windows, macOS and Linux
```

Releases are cut by bumping `__version__` in `pob_cli/src/pob_cli/__init__.py` and pushing a `v*` tag; `release.yml` gates on `engine.yml`, builds, signs in the `release` environment (which holds `RELEASE_SIGNING_KEY`, limited to `v*` tags; the architecture guide lists the owner's setup), verifies a draft and publishes. `gh workflow run release.yml -R exilemoulei/pob4agents` (on a branch) is a dry run that uploads nothing. Tag only when a release is asked for.

## Layout

- `pob_cli/src/pob_cli/`: the package, five layers that import only downward. L0 foundation (`errors`, `ticks`, `os_util`, `compat`, `json_io`, `locale_cfg`, `guardrails/`); L1 engine and data sources (`updater/`, `bridge_client`, `importers`, `profile_import/`, `game_data/`, `external_data/`, `poedb_client/`); L2 domain (`analyzer/`, `mutators/`, `snapshot`, `comparator/`, `formatters/`, `bench/`, `trade_*`, `timeless/`); L3 `daemon/`; L4 the `cli*` entry points. The full table is in the architecture guide.
- `pob_cli/src/pob_cli/data/`, `lua/`: bundled `bench_costs.json` and `headless_runner.lua` (the macOS/Linux engine host).
- `pob_cli/tests/`: pytest. `engine_paths.py` finds a real engine; `support.py` holds helpers for timing and network-failure tests.
- `pob_cli/scripts/`: `check.py` and `arch_lint.py` (the gate) and the generators.
- `pob_cli/packaging/`: `pob.spec` (PyInstaller), `build_posix_runtime.sh`, `smoke_frozen.sh`, `release_tools.py` (asset names, packing, sums, signing).
- `pob_cli/uv.lock`: the locked dev and build environment.
- `skills/pob/`: the agent skill shipped to users (`SKILL.md`, `references/`).
- `install.ps1`, `install.sh`: one-line installers. `bucket/pob.json`: the Scoop manifest, updated by `release.yml`.
- `docs/`: [docs/README.md](../docs/README.md) indexes the guides (`docs/guides/`) and design notes (`docs/design/`, historical, in Chinese).
- `.github/workflows/`: `ci.yml` (PRs, engine-less), `engine.yml` (real engine: nightly via `engine-nightly.yml`, on dispatch, as the release gate), `release.yml`, `codeql.yml`.

## Hard rules

- **PoE1 only, and the guardrails stay.** `guardrails/` blocks PoE2 builds, URLs, terms and commands on purpose, because agents mix the two games up. Never weaken it, and never let it block a PoE1-legal term (the list is in the architecture guide).
- **Engine and network red lines.** Never commit the PobTools pack (1 GB, with unlicensed data). Never run stock LuaJIT on PoB >= 2.68 (its `+=` syntax fails there). Never call `api.github.com` or get past Cloudflare on poe.ninja. Official profile import (`pob profile` / `pob load` / `pob diagnose`) reads public `pathofexile.com/character-window/*` without cookies (using env `POESESSID`/`POE_COOKIE` only for private profiles) and `poe.game.qq.com` using env `POE_CN_SESSID`/`POE_CN_COOKIE`. Trade is read-only, and `POESESSID` / `POE_COOKIE` / `POE_CN_SESSID` / `POE_CN_COOKIE` live only in the environment, never in arguments, logs or git.
- **Layers and budgets.** Imports only point down (L4 -> L0), with no cycles; files stay at or under 650 lines and 24,000 bytes, functions at or under 200 lines. Split a module before it hits the limit rather than raising it; `check.py --fast` fails otherwise.

## Conventions

- **Generated files are never edited by hand:** `game_data/tables_han.py`, `data/bench_costs.json`, `data/map_mods_3_30.json`, `data/apt_*.json.gz`, `uv.lock` (change it with `uv add` / `uv lock`) and `bucket/pob.json`. Change the generator or its input instead.
- **Tests.** Unit tests run offline on fakes. Real-engine tests skip when no engine is found. The full gate's ratchet depends on the engine: with no `--min-passed/--max-skipped` flags, `pob_cli/scripts/check.py` auto-selects machine-appropriate floors and prints an INFO line; against a bootstrapped engine, pass the explicit `--min-passed` / `--max-skipped` flags defined in `.github/workflows/engine.yml` (each OS skips the tests written for the other). Under the A+B+C fast-track rule, regular feature PRs and bugfixes must NOT edit `.github/workflows/engine.yml` ratchets — ratchets are raised once before a release to avoid cross-PR merge collisions. Timing assertions scale with `POB_TEST_PERF_SLACK`.
- **Chinese text.** The display language defaults to Simplified (`zh-CN`) and can be switched to Traditional (`zh-TW`) or English with `pob lang` / `POB_LANG`, while queries and slang are mostly Simplified. Compare Chinese labels only after `game_data.fold_han`, and fall back to the English name from `game_data.english_for_zh`. Keep `--json` fields English; localize only human output.
- **Exit codes are a contract:** `1` for a bad request (including `pob trade` usage errors, same-slot `compare`/`borrow`/`tree diff`, and strict field-type or pre-run validation failures in `pob batch` and mutation RPCs), `5` for a daemon or engine fault with a clean `Error: ...` line, `0`..`6` for trade (`2` = no results, `3` = cookie required, `4` = rate-limited). Agents branch on them.
- **Docs move with the code.** A changed command or flag updates `docs/guides/commands.md`, both READMEs (`README.md` in Chinese, `README_EN.md`) and `skills/pob/SKILL.md` when it touches a recipe. A new doc goes into `docs/` and its index.
- **Supply chain.** Every `uses:` in a workflow is pinned to a commit SHA with a `# vX.Y.Z` comment; Dependabot bumps Actions and the uv lockfile. Anything pob or an installer downloads from a release is trusted only through the Ed25519-signed `SHA256SUMS.txt` (`updater/signature.py`, `install.ps1`, `install.sh`); keep the three public-key lists equal. Source tarballs a build downloads are pinned by SHA-256.

## Commits

Conventional Commits, `type(scope): description`. Types: `feat fix perf refactor test docs ci build chore`. Scopes are areas: `cli`, `daemon`, `bridge`, `updater`, `game-data`, `analyzer`, `mutators`, `comparator`, `trade`, `bench`, `timeless`, `guardrails`, `research`, `external-apis`, `packaging`, `posix`, `deps`, `release`, `scoop`, `docs`, `workspace` (root files and scripts). Combine them as `trade,bench`.

Pull requests fill in every section of [.github/pull_request_template.md](../.github/pull_request_template.md), the AI disclosure included. To add a command, follow "Adding a command" in the architecture guide.
