# Fantasy Basketball Dashboard

Local, read-only decision support for the Big Baller League (Yahoo 2026–27, 10-team, nine-category H2H). The dashboard forecasts deterministic regular-season results and explores provisional waiver strategies. It does not submit transactions.

## Quick start

Use Python 3.11+ from this repository root. The supplied workbook, league settings, schedule, matchup CSV, reviewed ID mappings, and saved Yahoo captures must be present.

```sh
python3 -m venv .venv
.venv/bin/pip install -e backend
PYTHONPATH=backend .venv/bin/python -m app.dashboard --port 8766
```

Open <http://127.0.0.1:8766/>. The server binds to localhost and validates source inputs at startup. Restart it after changing inputs. There is no separate frontend build; the Python server serves the interface.

Optional read-only import and capture checks:

```sh
PYTHONPATH=backend .venv/bin/python -m app.cli --browser-capture data/reports/yahoo-browser-capture.json --browser-players-capture data/reports/yahoo-browser-players-capture.json
```

For a browser refresh, install `backend[browser]` and run `PYTHONPATH=backend .venv/bin/python -m app.browser_refresh` from a signed-in local Chrome profile. Captures and reports under `data/reports/` and the browser profile under `data/cache/` are private and Git ignored. Never share browser session data. Refresh alone does not verify claims, effective transaction times, or player-level restrictions.

## What is available

- **Standings:** scheduled 18-week category W-L-T, projected seeds, weekly matchup explorer, and league-relative category heatmap. Source choice: Hashtag, LeagueWinner, or weighted blend.
- **Waiver Advisor:** one panel that ranks up to five distinct strategies (streams, swaps, permanent upgrades) against HOLD. Each shows timing, category wins gained, categories helped and hurt, rest-of-season effect, adds used and verification status. When HOLD is best, it shows the closest alternatives and why they fall short. The planning horizon and diagnostics sit under Advanced. Suggestions stay provisional until Yahoo ownership, budget, claims and timing are verified together.
- **Saved-state updates:** `--actual-state /path/to/state.json` accepts verified completed-week records and current-week active-lineup volumes. The schema and freshness rules are in [CURRENT_STATE.md](docs/CURRENT_STATE.md).

The older Monte Carlo backend and `/api/forecast` route are preserved for future research; the page does not show odds. Trade optimization and live Yahoo integration are not implemented.

## Tests and navigation

```sh
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests -v
```

Read [CURRENT_STATE.md](docs/CURRENT_STATE.md) for the active modules, contracts, limitations, and Phase 3D handoff. Read [PROJECT_SPEC.md](PROJECT_SPEC.md) for product scope and roadmap. The original input files and private captures are source material, not generated documentation.
