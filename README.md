# Fantasy Basketball Dashboard

Local, read-only decision support for the Big Baller League (Yahoo 2026–27, 10-team, nine-category H2H). The dashboard forecasts deterministic regular-season results, recommends waiver strategies and screens trades. It never submits transactions to Yahoo.

## Quick start

Use Python 3.11+ from this repository root. The source inputs in `data/inputs/` and saved Yahoo captures in `data/reports/` must be present.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/fantasy-dashboard --port 8766
```

Open <http://127.0.0.1:8766/>. The server binds to localhost and validates every input at startup; restart it after changing inputs. There is no frontend build: the server serves `src/fantasy_dashboard/web/static/`.

`.venv/bin/python -m fantasy_dashboard` is equivalent. Pass `--actual-state /path/to/state.json` to supply verified completed-week records and current-week volumes by hand (schema in [CURRENT_STATE.md](docs/CURRENT_STATE.md#inputs-and-calculation-contracts)).

## What is available

- **Overview and Matchups.** Scheduled 18-week category W-L-T, projected seeds, a weekly matchup explorer and a league-relative category heatmap. Source choice: Hashtag, LeagueWinner, or a weighted blend.
- **Waiver Advisor.** Ranks up to five distinct strategies (streams, swaps, permanent upgrades) against HOLD, with timing, category wins gained, rest-of-season effect, adds used, verification status and a per-strategy audit. See [WAIVER_ADVISOR.md](docs/WAIVER_ADVISOR.md).
- **Trade Lab.** 1-for-1, 2-for-1, 1-for-2 and 2-for-2 trades replayed for both teams, with market value from Yahoo ADP and FantasyPros consensus kept apart from category benefit. See [TRADE_LAB.md](docs/TRADE_LAB.md).

Suggestions stay provisional until Yahoo ownership, budget, claims and timing are verified together.

## Refreshing Yahoo data

```sh
.venv/bin/pip install -e '.[browser]'
.venv/bin/fantasy-yahoo-refresh
```

This opens a dedicated Chrome profile; sign in once. It can also be triggered from the page (**Refresh Yahoo**). Captures under `data/reports/` and the profile under `data/cache/` are private and Git ignored; never share browser session data. Refresh alone does not verify claims, effective transaction times or player-level restrictions. Details: [YAHOO_DATA.md](docs/YAHOO_DATA.md).

The import audit re-checks the workbook and saved captures and writes QA reports to `data/reports/`:

```sh
.venv/bin/fantasy-import-audit --browser-capture data/reports/yahoo-browser-capture.json --browser-players-capture data/reports/yahoo-browser-players-capture.json
```

## Repository layout

```
src/fantasy_dashboard/   installable package (src layout)
  league.py, inputs.py   league vocabulary; input paths and load_inputs()
  sources/               workbook, settings, schedule and matchup parsing
  yahoo/                 Yahoo capture loaders, transaction evidence, refresh, market captures
  model/                 lineups, day engine, deterministic season forecast
  advisor/               Waiver Advisor search and audit
  trades/                Trade Lab: config, replay, market, ranking, cards, search
  web/                   HTTP server, presentation, static/ page assets
tests/                   unittest suite; pinned_capture.py, support.py, fixtures/
scripts/                 run_tests.py (parallel runner), advisor_snapshot.py, trade_benchmark.py
data/
  inputs/                read-only source inputs: workbook, league settings, schedules, H2H matchups, reviewed ID links
  reports/               private Yahoo captures and QA reports (Git ignored)
  cache/                 private signed-in browser profile (Git ignored)
docs/                    current state and feature reference
pyproject.toml           packaging, console scripts, ruff config
PROJECT_SPEC.md          product scope and roadmap
```

## Tests

```sh
.venv/bin/python scripts/run_tests.py
```

Runs each test module in its own process (about 4 minutes). The tests need the private archived capture `data/reports/archive-2026-10-20T1411Z`. Without it, modules that read Yahoo captures skip themselves. The single-process equivalent is `.venv/bin/python -m unittest discover -s tests -v`. Lint with `.venv/bin/ruff check .`; CI runs both on every push and pull request.

Start with [CURRENT_STATE.md](docs/CURRENT_STATE.md) for modules, routes, contracts and known gaps, and [PROJECT_SPEC.md](PROJECT_SPEC.md) for product scope.
