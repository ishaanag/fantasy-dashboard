# Yahoo data and refresh

All Yahoo access is read-only. Captures are written to Git-ignored `data/reports/`; the dedicated signed-in Chrome profile lives in Git-ignored `data/cache/yahoo-browser-profile`. Never share either.

## Refresh

`fantasy-yahoo-refresh` (or **Refresh Yahoo** on the page) runs one signed-in session in the dedicated profile (Playwright, `pip install -e '.[browser]'`). It captures starting rosters, all-player pages with status codes, my team page, the Can't Cut List, the "Available, IL Eligible" list and, after a period's first tipoff, daily team pages for current-week volumes, then re-captures rosters. Everything is written in one atomic save with one `captured_at_utc`; nothing is written if the roster changes or a page fails.

A successful refresh from the page atomically replaces the capture, reloads inputs, injury statuses and actual state, clears forecast and trade caches, and reloads the standings and the open views. The previous in-memory data stays in place if capture fails. The refresh also recaptures the market signals when stale (below).

## Transaction snapshot

`yahoo.transactions.load_transaction_snapshot()` rejects the snapshot (no mixing) when its timestamp or player/roster fingerprints differ from the player capture; when any required section is missing (roster, adds, IL, waiver priority, Can't Cut, statuses, IL-eligible list); when team-page slots or IL use differ from the roster capture; when the add limit differs from the period's verified limit (4); or when listed players are not available. Team IDs bind captures; names are mutable (Team 8 is now "Ball Don't Lie"), and the H2H CSV is matched by the names it was verified against.

Verified live on October 20: adds used/limit, IL used/capacity, waiver priority, Can't Cut membership (27), status codes (O, Q, D, P, NA), IL-eligible free agents, final-game markers and per-player FGM/A, FTM/A and counting stats. Free-agent timing is "now" (FA label plus the verified Daily—Today setting). No waiver players or pending claims existed, so the waiver clear-time and claim-row formats are unverified; a W add stays provisional. Lock markers were not observable pre-tipoff, so schedule tipoffs are applied as locks for all roster players.

`resolve_evidence()` prefers a verified snapshot; otherwise it falls back to the newest bound `yahoo-transaction-evidence-user-*.json` (dated user observations, always provisional); otherwise none.

## In-season accuracy

- **Injury status.** A synchronized player-status section is loaded with the roster/player fingerprints even when an unrelated transaction field is incomplete. Statuses O, NA, OFS and INJ remove a rostered player from legal lineup candidates only for games on the capture day or next Yahoo scoring day, within 36 hours of capture (`model.lineups.unavailable_for_game`). Q, D and GTD keep their GP/82 appearance estimate. Later games keep the source GP projection; no return date is inferred. The same rule feeds the forecast, the advisor search and replay, and the Trade Lab.
- **Completed weeks.** After a period closes, refresh reads the standings table and one final matchup page per completed week. Five unique, reciprocal pairs per week must match the verified H2H schedule; each team needs nine category decisions; summed weekly records must equal all ten cumulative records. Completed-week category stat volumes remain unavailable; their W-L-T is carried once.
- **Current week.** A daily-page pass sums started-slot final-game FGM/A, FTM/A and counting volumes. In-progress games, missing pages/headers/dates, mismatched records and source URLs reject the automatic state with specific reasons; a current, complete `--actual-state` file remains the manual fallback.
- **Overnight captures.** After midnight Eastern, Yahoo's team page still shows the previous day's lineup, so a capture can carry yesterday's `lineup_date`. The forecast accepts it until a game on the as-of date tips off.

The Yahoo matchup-card and standings structures were inspected against a completed historical league page. This league has not completed a scoring week yet, so its first live rollover still needs a read-only verification; if its markup differs, the parser rejects it rather than inventing results.

## Market signals

`yahoo/market_capture.py` captures the Trade Lab's market inputs read-only:

- **Yahoo ADP.** The public Draft Analysis page (standard scoring) into `yahoo-draft-analysis.json` (270 players, captured October 21, 2026). Recaptured by the refresh when older than seven days. Preseason ADP is fixed once the season starts.
- **FantasyPros ECR.** Expert consensus rankings (Roto/categories) with a plain GET into `fantasypros-ecr.json`. Recaptured when three days old. On October 21 the ROS page served the final draft consensus (6 experts, updated October 20), recorded as `ranking_type: Draft`, `fallback_for: ROS`.

Run alone with `.venv/bin/fantasy-market-capture` (`--ecr-only` for FantasyPros only). The pinned test archive holds copies of both captures.
