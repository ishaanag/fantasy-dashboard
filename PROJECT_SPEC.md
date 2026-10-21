# Fantasy Basketball Dashboard — Product Specification

**League:** Big Baller League, Yahoo Fantasy Basketball 2026–27. **Status (October 21, 2026):** the season forecast, read-only Yahoo integration, Waiver Advisor with recommendation audit, four-view page and market-driven Trade Lab are implemented. The app is local and read-only; it never submits Yahoo transactions.

## Purpose and rules

Support the manager's decisions using actual league rules, auditable projection inputs, dated NBA games, legal lineups, and scheduled Yahoo opponents. Optimize category results rather than generic player ranks. Show source, as-of time, assumptions, and missing verification with every analysis.

The league has ten teams and nine H2H categories: FG%, FT%, 3PTM, PTS, REB, AST, STL, BLK, and TO. Lower TO wins. Each category contributes a weekly W/L/T; a 6–3 matchup adds six wins and three losses. Daily lineup slots are PG, SG, G, SF, PF, F, C, C, Util, Util, with four bench and three IL slots. League settings record four acquisitions per Yahoo scoring period, a one-day standard rolling waiver, and a cannot-cut list. The regular season covers 18 scoring periods; Weeks 7 and 17 span 14 days. Playoffs are configured for six teams in Weeks 19–21.

## Implemented product

| Area | Current behavior |
|---|---|
| Inputs | Import Hashtag and LeagueWinner workbook sheets, validate source-specific shooting volumes, reconcile a complete dated Yahoo roster/player capture, and apply 35 reviewed player-ID decisions. Yahoo-displayed eligibility is used. |
| Deterministic core | Blend per-game makes, attempts, counting stats, and GP; assign legal daily starters; weight expected starts by projected GP/82; compute FG%/FT% from summed makes and attempts. |
| Season forecast | Validate all 90 regular-season pairings; forecast nine-category weekly results, cumulative 18-week W-L-T, win percentage, and seed. Display weekly results and Min-Max league-relative category strengths (100 best, 0 worst, 50 all tied). Completed Yahoo weeks are carried once. |
| Yahoo integration | Synchronized, authenticated, read-only capture of rosters, the player pool, transaction evidence, injury status, completed-week results and current-week volumes, with strict rejection of incomplete or inconsistent state and a manual actual-state fallback. |
| Waiver Advisor | One read-only workflow. Every available player is scored as a first move; multi-move streams and permanent upgrades are searched within weekly budgets; up to five distinct strategies are ranked against HOLD by short-term category wins, flip fragility and rest-of-season roster value. Can't Cut, legal lineups, locks and timing are enforced. These are conditional forecasts, not verified executable transactions. |
| Recommendation audit | Each shown strategy has a collapsed audit: each transaction's contribution (in order, left out, and alone), legal shorter plans, net permanent replacements with per-game stats, games, starts and scarcity, and Hashtag/LeagueWinner/Blended sensitivity with warnings. It explains ranking; it does not change it. |
| Trade Lab | Read-only trade ideas for my team only: 1-for-1, 2-for-1, 1-for-2 and 2-for-2 packages with every partner, replayed exactly for both teams with legal lineups, roster balancing and free-agent opportunity cost. Market value (Yahoo preseason ADP plus FantasyPros consensus) is kept apart from competitive benefit (robust rest-of-season and next-three-week category wins). A trade where the partner would lose more than 10% by market value is never shown. Each card carries one label (Market opportunity, Market-balanced, Mutually beneficial, Expensive improvement). No acceptance probability is claimed. |

Margin scores and sensitivity checks are ranking aids, not probabilities.

## Architecture and source boundaries

`src/fantasy_dashboard` is a Python analytic core and localhost HTTP server, grouped into `sources`, `yahoo`, `model`, `advisor`, `trades` and `web` subpackages; the page is static HTML, CSS and vanilla JavaScript. Module responsibilities are listed in [CURRENT_STATE.md](docs/CURRENT_STATE.md#modules). Data sources are the original workbook and league settings, NBA and fantasy schedules, reviewed mappings (all in `data/inputs/`), and dated private Yahoo captures (`data/reports/`).

Keep original inputs, private captures, and historical snapshot formats intact. No new player projection, injury status, transaction outcome, or NBA game date may be invented. A displayed FA or W label is a dated observation; it does not prove claim success or transaction timing. Yahoo writes are out of scope.

## Delivery history

| Phase | Delivered |
|---|---|
| 0 | Projection import, Yahoo browser-capture audit, reviewed ID matching |
| 1 | Dated daily lineups and descriptive category strengths |
| 2 | H2H matchup schedule, actual-state contract and season forecast (its Monte Carlo engine has since been removed) |
| 3A–3C.1 | First waiver engines (single moves, weekly plans, rolling horizon, strategies); superseded by 3E and removed in October 2026 maintenance |
| 3D, 3F | Read-only Yahoo transaction evidence; injury-aware imminent games and automatic actual-state capture |
| 3E, 3E.1, 3E.2 | Waiver Advisor, recommendation audit, advisor page |
| 3G | Overview, Matchups and Waiver Advisor views |
| 4A, 4B, 4D, 4E | Trade Lab: 1-for-1, packages and balancing, market-driven ranking, decision-focused page |

## Limitations and roadmap

**Deferred:** calibrated weekly and playoff probabilities (the earlier uncalibrated Monte Carlo engine was removed; a replacement needs calibration before anything is displayed); verification of the league-specific playoff bracket; three-team trades; a calibrated market model; sourced injury/news enhancements.

**Known modeling gaps:** The saved Yahoo capture is dated and may be stale. Future ownership and waiver outcomes are conditional. Candidate IL eligibility and direct-IL placement are not generally verified. The actual-state contract carries completed category outcomes but not completed-week category stat totals. Thirty Cup/replacement NBA games lack supplied dates and are omitted; live postponements are not verified. Legal projected starts and GP/82 availability are estimates, not actual appearances. Yahoo status alone does not provide a return date; the imminent-game rule is deliberately limited to the capture day and following scoring day. The first completed-week rollover in this league has not yet been verified live. The advisor scores every first move, but multi-move combinations use a bounded search. Trade market value is a heuristic curve over public preseason signals, not a market price; trade labels and rank weights are declared heuristics.
