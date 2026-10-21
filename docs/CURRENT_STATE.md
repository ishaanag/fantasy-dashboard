# Current state

**As of October 21, 2026.** Start here. The application is a local, read-only Python dashboard for the Big Baller League (Yahoo 2026–27, ten teams, nine-category H2H). It forecasts the regular season, recommends waiver strategies and screens trades. It never submits anything to Yahoo. There is no frontend build: the Python server serves one static page and its CSS and JS.

Feature reference:

- [WAIVER_ADVISOR.md](WAIVER_ADVISOR.md): search, objective, audit and page.
- [TRADE_LAB.md](TRADE_LAB.md): replay, market value, tiers, labels, search and page.
- [YAHOO_DATA.md](YAHOO_DATA.md): browser refresh, snapshot validation, in-season actual state, injury rule and market-signal captures.

## Modules

The installable package is `fantasy_dashboard` in `src/` (src layout). Imports are absolute (`from fantasy_dashboard.model.forecast import forecast`). Dependencies point downward: `league` ← `sources` ← `yahoo` loaders and `inputs` ← `model` ← `advisor`, `trades` ← `web`.

| Module | Responsibility |
|---|---|
| `league.py` | League vocabulary: lineup slots, the nine categories and volume keys, Yahoo status sets, `RosterPlayer`, slot eligibility, UTC parsing and formatting. |
| `inputs.py` | Input locations (`data/inputs/`, `data/reports/`, the browser profile) and `load_inputs()`, which revalidates the workbook, settings, schedule, reviewed ID links and saved Yahoo captures on every launch. |
| `parallel.py` | Ordered map over forked worker processes, shared by the advisor and Trade Lab searches. |
| `cli.py` | Projection import and capture audit; writes QA reports to `data/reports/`. |
| `sources/` | `projections` and `blending` (both workbook sheets with each source's own makes/attempts; volumes blended before percentages), `matching` and `reviewed_matches` (name/team normalization and the 35 reviewed site-ID decisions), `settings` (league-settings reconciliation), `schedule` (dated NBA fixtures and Yahoo periods) and `matchups` (the 18-week, 90-pairing H2H schedule). |
| `yahoo/` | `roster_capture` and `player_capture` (saved capture loaders), `transactions` (read-only page scripts; builds and validates the transaction snapshot and actual state; `resolve_evidence`), `evidence` (pool eligibility, the user-evidence fallback, evidence gaps, plan eligibility classes), `refresh` (signed-in Playwright refresh) and `market_capture` (Yahoo ADP and FantasyPros ECR). |
| `model/` | `lineups` (legal daily lineups, expected starts by GP/82, the imminent-game injury rule, `rank_league`), `day_engine` (`DayEngine`, the cached daily lineup engine the advisor, audit and Trade Lab share), `forecast` (`forecast()`, Yahoo seeding, Min-Max strengths, and the shared actual-state, category and margin helpers). |
| `advisor/` | `search` (`AdvisorConfig`, `advise`) and `audit` (`Auditor`, the per-strategy recommendation audit). |
| `trades/` | `config` (`TradeConfig`, package shapes, trade rules), `replay` (two-team replay context, screen, side metrics, model value), `market` (market value and gaps), `ranking` (tiers, labels, rank score, Pareto pruning), `cards` (card text) and `lab` (`trade_lab`, the search pipeline). |
| `web/` | `server` (localhost HTTP server and routes), `presentation` (plain-language `display` and `brief` blocks built only from computed values) and `static/` (`index.html`, `app.css`, `app.js`; vanilla JS, no build). |

Console scripts: `fantasy-dashboard`, `fantasy-yahoo-refresh`, `fantasy-market-capture` and `fantasy-import-audit`; `python -m fantasy_dashboard` also starts the server.

## Routes

| Route | Purpose |
|---|---|
| `GET /` | The page: Overview, Matchups, Waiver Advisor and Trade Lab views (hash-routed). |
| `GET /api/deterministic?hashtag_weight=&as_of_utc=` | Cached season forecast plus `my_team_id` and `league_name`. |
| `GET /api/advisor?hashtag_weight=&horizon=auto\|1\|2\|3` | Waiver Advisor result with an additive `display` block. |
| `GET /api/trades?hashtag_weight=&partners=&protect=&types=1-1,2-1,1-2,2-2` | Trade Lab result with an additive `brief` per card; up to 16 combinations cached; `verification` recomputed per request. |
| `GET /api/rankings?hashtag_weight=&week=regular\|all\|N&as_of_utc=` | Descriptive power-score ranking for a period (not used by the page). |
| `GET /api/yahoo/status`, `POST /api/yahoo/refresh` | Snapshot status; signed-in refresh that reloads inputs and clears caches, keeping the previous data if capture fails. |

Forecast and advisor errors name the actual-state rejection reason when the captured state was refused.

## Inputs and calculation contracts

Read-only source inputs live in `data/inputs/`: `player-projections-2026-27.xlsx`, `league-settings.md`, `H2H-Matchups.csv`, `nba_2026_27_team_schedule.csv`, `YAHOO_FANTASY_CALENDAR_AND_LINEUP_LOCKS_2026-27.md`, `NBA_CUP_Rules.md`, the manual review decisions (`player_matching_manual_review_35_decisions.csv`) and the applied mapping (`player_site_matches.csv`). Private dated captures are under Git-ignored `data/reports/`; the Chrome profile is under Git-ignored `data/cache/`. Never publish credentials or session data.

- **Projections.** 527 LeagueWinner rows and 400 Hashtag rows (397 unique normalized names); 35 player-ID decisions were reviewed manually. LeagueWinner supplies numeric makes/attempts; Hashtag embeds them in its shooting cells. Source-only and unmatched players stay visible in QA reports rather than receiving invented values.
- **Schedule.** 1,200 dated NBA games (80 per team); 30 Cup/replacement games are undated and omitted. Yahoo has 18 regular-season scoring periods; Weeks 7 and 17 last 14 days.
- **Categories.** FG% = total FGM / total FGA; FT% = total FTM / total FTA; lower TO wins. Each team has 162 category decisions. Projected win percentage is `(W + 0.5·T)/(W+L+T)`. Seeds use Yahoo's tiebreak: the most recent week's win percentage, then earlier weeks in reverse order.
- **Heatmap.** Min-Max scores: 100 best, 0 worst, 50 when a category is tied league-wide. These are not probabilities.
- **Lineups.** Each day's starters are a maximum-weight legal assignment over the PG, SG, G, SF, PF, F, C, C, Util, Util slots; expected appearances are scheduled games × GP/82. Started players keep their captured slots on the captured lineup date.
- **Actual state.** Post-start forecasts require actual state, captured automatically or supplied with `--actual-state`: `as_of_utc`, `completed_weeks`, `records` for all ten team IDs, reciprocal `weekly_records` with nine decisions per team per completed week, and `current_week_volumes` (`fgm`, `fga`, `ftm`, `fta`, `three_ptm`, `pts`, `reb`, `ast`, `stl`, `blk`, `turnovers`) for every team after the period's first tipoff. The as-of time cannot predate the roster capture. A lineup snapshot from another day is refused only once a game on the as-of date has tipped off (overnight, Yahoo still shows the previous day's lineup). Completed weeks' W-L-T is carried once; their category stat totals are not part of the contract and are not reconstructed.
- **Eligibility classes.** Rows and plans carry `verified_current` (verified snapshot under 15 minutes old, no gaps), `provisional` (feasible with named gaps), `conditional_future` (future-dated or waiver steps), `hold` or `infeasible`.

## Dashboard page

The page is one static HTML file with one dark design system and tabular numbers. A sticky header has the four view tabs, a projection-source chip, a Yahoo freshness pill (green under 15 minutes, amber stale, red unverified) and a settings drawer.

- **Overview.** Your projected seed, season category W-L-T, category win % (a share of decisions, not a probability) and this week's matchup. An 18-week season path; sortable standings with a top-six playoff line and the Yahoo-only actual record in its own column; the category-strength heatmap; a team profile with your scheduled meetings.
- **Matchups.** Week stepper and the week's five games. The selected game shows the category score, an ahead/close/behind summary and nine rows with exact totals, FG%/FT% makes/attempts and a margin bar. Close/Edge/Clear bands are display thresholds only (Close ≤0.5 pp FG%, ≤1 pp FT%, ≤5% of the average total elsewhere; Edge ≤1.5 pp, ≤3 pp, ≤12%). Winners always come from the engine's `outcomes`.
- **Waiver Advisor** and **Trade Lab** load only when their tab is opened, and again after a source change or Yahoo refresh.
- **Settings and data.** Source selector (Blend with weight, Hashtag, LeagueWinner) drives every view. Data health lists snapshot status and age, final results, current-week stats, schedule gap, player matching and missing evidence.

## Tests and tooling

```sh
.venv/bin/python scripts/run_tests.py            # modules in parallel, about 4 minutes
.venv/bin/python -m unittest discover -s tests -v   # single process
.venv/bin/ruff check .                           # lint and import order (config in pyproject.toml)
```

Test modules that read Yahoo captures import `tests/pinned_capture.py`, which pins the archived capture `data/reports/archive-2026-10-20T1411Z` through `FANTASY_REPORTS_DIR`. That archive is private; without it those modules are skipped, so CI (`.github/workflows/ci.yml`: ruff plus the fixture-based page and presentation tests) runs on a clean checkout. `tests/support.py` holds the repository paths, the served page source (`PAGE`) and `get_json` for calling routes in process. Saved engine outputs used as fixtures are in `tests/fixtures/`. Advisor behaviour tests use a small search budget (`FAST` in `test_advisor.py`). Starting the dashboard with `FANTASY_REPORTS_DIR` set makes every refresh fail with `snapshot_not_synchronized_with_player_capture`, because refresh writes to the live folder while inputs load from the archive. Editing rosters in a test requires dropping the forecast's `_plan_cache` from the copied inputs.

Page rendering is checked by markup markers and label tests plus one-off scripted Chrome checks, not automated browser tests.

## Known gaps and debt

- **Verification gaps.** Waiver clear-time and pending-claim row formats are unverified (no waiver players or claims existed at capture). The first completed-week rollover in this league has not been verified live; the parser rejects rather than invents. Live postponements and undated Cup games are not modelled. Candidate IL eligibility and direct-IL adds are not verified or modelled.
- **Modelling limits.** Yahoo out/inactive status affects only the imminent game window, not later injury duration. Advisor multi-move combinations are beam-limited plus a deterministic local-improvement budget (first moves are exhaustive); validated against a five-minute run, not proven optimal. The rest-of-season margin score and the deterministic flip count can disagree. The audit explains but does not re-rank. Trade Lab limits are listed in [TRADE_LAB.md](TRADE_LAB.md#limitations).
- **Heuristics.** Margin-score scales, the acquisition cost, fragility discount, matchup bands, trade market curve, labels and rank weights are declared heuristics, not calibrated.
- **Performance.** Searches use forked workers (default min(8, cores − 1)); without fork they run single-process (about 2 to 3 minutes each).
- **Not implemented.** Win, playoff or acceptance probabilities (the earlier uncalibrated Monte Carlo engine was removed in October 2026 maintenance), playoff bracket verification, three-team trades.

Keep public routes and saved snapshot formats stable.
