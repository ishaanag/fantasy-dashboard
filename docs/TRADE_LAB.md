# Trade Lab

`GET /api/trades?hashtag_weight=…&partners=id,id&protect=id,id&types=1-1,2-1,1-2,2-2` calls `trade_lab()` (`src/fantasy_dashboard/trades/`) with the dashboard's cached forecasts for the selected weight, Hashtag-only and LeagueWinner-only. Results are cached per weight, as-of, partners, protected set and types (up to 16 combinations); `verification` is recomputed on every request so evidence age stays current. `web.presentation.trade_briefs()` adds a `brief` to each card on a copy, so the cached result is untouched. Defaults live in `TradeConfig`: my team (Yahoo ID 4), all nine partners, no protected players, all four package types, IL-slotted players left out.

The Trade Lab serves only my team. It looks for trades that give a partner players the market values highly in exchange for players who do more for my nine categories. Giving away market value is allowed and is ranked by what it buys. Nothing predicts acceptance and nothing is sent to Yahoo.

## Replay

- **Timing.** Trade end date, review type and reject time come from the verified settings capture (March 4, 2027; Commissioner; 2 days); analysis stops if they are missing or the deadline has passed. A trade is assumed to take effect `acceptance_days` (1) plus the reject window after the as-of date. Games before that day count for the original teams. The effective date, acceptance, review, pending trades, keeper value and injury returns are listed as unverified on every result.
- **Two-team replay.** `trades.replay._Context` builds one `model.day_engine.DayEngine` over all rostered players. Each team's rest-of-season weekly totals come from legal daily lineups with the old roster before the effective day and the new roster from it; current-week actual volumes are added as in the forecast. HOLD for all ten teams must reproduce `/api/deterministic` totals, records and seeds (assertion). For a trade, both teams are replayed, every matchup involving either is recomputed (including any meeting between them), and the league is re-ranked with the forecast's tiebreak (`model.forecast.seed_order`).
- **Side metrics.** Rest-of-season category wins (W + T/2), the same with narrow flips counted at half (`adjusted_delta`; narrow = margin under 3% of the opponent total or 0.4 percentage points), per-category changes, flips, next three weeks, record and seed before/after, lineup starts, and the league rank of each remaining-season category total.
- **Roster balancing.** Each team keeps at most 14 active players and 3 IL. The side taking an extra player clears a spot by dropping an active player (never an incoming player, one of my untouchables, or a player on Yahoo's league-wide Can't Cut list when captured) or by moving an O/INJ player to a free IL slot (IL eligibility unverified; he is assumed not to return). Every legal drop is replayed and the team keeps the one best for itself, rechecked once after the other side's fill. A side left with 13 active players is scored with the spot open and with each of the best current free agents (top 8 by lineup value plus the best at each base position). Waiver (W) players are never used. 2-for-2 packages need no balancing.
- **Fill opportunity cost.** A fill is used only when it beats the open spot and an acquisition is available in the effective week. The same free agent could be added without any trade, so ranking uses `effective` = adjusted change − max(0, that free agent's value as a plain waiver swap) − half of the best fill's edge over the next-best free agent.
- **Model value.** Per-game category z-scores over rostered non-IL players (shooting as makes above league average at the player's volume), summed, times GP/82, minus the best active projectable free agent at the player's scarcest position, times team games after the effective day. It is shown only as an asset-sacrifice risk (`value.model_gap`).

## Market value

`trades.market.market_values()` and `market_summary()` combine two independent public signals, captured read-only (see [YAHOO_DATA.md](YAHOO_DATA.md#market-signals)):

1. Yahoo preseason ADP, standard scoring (`data/reports/yahoo-draft-analysis.json`).
2. FantasyPros expert consensus rankings, Roto/categories (`data/reports/fantasypros-ecr.json`).

Each signal is re-ranked to an ordinal position among Yahoo's players (ties averaged), mapped to 100 × (e^−(pos−1)/40 − e^−(R−1)/40) with R the rostered active players, and the two are averaged. A side counts its best player in full and each further player at `depth_discount` (0.5): the side that gets the best player is the one managers see as winning. Undrafted players count as replacement by ADP. A drafted player FantasyPros does not list counts as missing for ECR, not zero. ECR older than 14 days drops out. With neither signal the projection-source ranks are used and `basis` says so. Matching uses the normalized name, the team for duplicates, and first initial + surname + team for names Yahoo abbreviates. Yahoo's league player list is never used: from the first tipoff its default order is season-to-date stats.

`value.market` holds `give`, `get`, `my_cut` and `partner_cut` (market value of a player either side must drop), `partner_surplus` = give − get − partner_cut, `my_cost` = give + my_cut − get, `by_signal` gaps and `signals_disagree` (signal gaps more than 25 points apart). `value.gap` is positive when the trade favours me by market value.

## Tiers, labels and ranking

- **Robust gain.** `_robust` = the lower of the blended effective gain and each source's gain alone, net of any fill opportunity cost.
- **Excluded.** Effective gain below 1.5; gain disappears under both sources; partner market loss above `viable_market_limit` (10%; such packages are never shown); or market cost above `expensive_cost_per_win` (12 market points per robust win) when the gain is under 3.0, half or more of it rests on narrow flips, or a source shows no gain. Otherwise `recommended`. Partner category loss, low usage and model-value overpay are risks, not exclusions.
- **Labels** (one per card, in this order). Expensive improvement: market cost over 10 points and over 12 per robust win. Mutually beneficial: partner gains at least 0.5, not fill-dependent, no source shows a partner loss. Market opportunity: partner gains more than 15% by market value. Market-balanced: otherwise.
- **Rank score** (`ranking` parts, in category wins). Robust gain; plausibility −3 × gap when the partner loses market value; market cost −0.02 per point surrendered; next three weeks +0.25 per win (±2 cap); sources +0.5 when both hold, −0.5 when split; partner +0.25 per win (−2 to +3 cap); complexity −0.25 per extra player and per drop, IL move or fill; a simpler 1-for-1 part within 0.5 of the package's gain (`simpler_alternative`) −1.5.
- **Guards.** An outgoing player worth at least the best incoming player who starts under 60% of his remaining team games for me marks the package low-usage.
- **Pareto pruning.** Before finalists, a package is dropped when another with the same partner and at least one shared player is no worse on my gain, market cost, plausibility and package size, within 0.25 on the next three weeks, and better on one (`dominated_by`, `dominated_variations`).

## Search

1. Generate every unique package (99,225 feasible with all nine partners; 74,529 of them 2-for-2), minus those needing two drops or with no legal drop.
2. Fairness checks run first; the lineup screen (the linear estimate extended to several players, the cheapest drop and the top free agent) runs only on viable packages, in parallel.
3. Replay a package exactly when the partner's estimated market loss (cheapest cut assumed) is at most 10% and either the model-value give-up is within 60% or, past that, the screen gives me at least 0.5 (`floor_screen_gate`). Paying a premium never removes a package. Team-week totals, drop lists and waiver alternatives are cached by roster change; near-equivalent packages are folded into the card they resemble.
4. Finalists (24: up to four partner-premium packages, four places per type, then the best remaining) get drop costs and 1-for-1 comparisons and are re-run with Hashtag-only and LeagueWinner-only using the same balancing.
5. Cards: up to five recommended; a player of mine appears in at most two cards, a received player once. Cards are never padded with below-threshold packages. `keep_finalists=True` also returns every source-checked finalist (tests and diagnostics only).

Replays run in forked workers (`parallel.py`); all replays matched a single-process run exactly. On the live October 21 capture (all partners, all types) the search replayed 16,912 packages (1,567 dominated variations removed) in 71.7 s total (replay 43.7 s, sources 15.4 s), plus about 22 s for cold forecasts. A reference run with no model-value pre-filter (64,254 replays, 262 s) kept all of the default's top 100 packages and identical cards. `scripts/trade_benchmark.py [partner_id …]` measures runtime and recall against exhaustive replays.

## Page

Fourth tab, loaded lazily. Partner, type and untouchable filters sit in a collapsed Filters disclosure whose summary shows the scope; the action is **Search**; earlier results stay dimmed during a new search; stale responses are ignored.

- **Shortlist.** Table of #, trade, partner, You, Market to them (points and %), Them, Sources, with a No trade row.
- **Cards.** A "Get …" headline, a send ⇄ receive exchange with ADP/ECR/market points, four tiles (your gain with week range and next three weeks; market to them; their category change; source agreement), a templated "why" sentence, a tension note whenever the partner projects a loss, roster-move chips, and shared-player and rank-reason lines.
- **Why this trade?** (collapsed): category table with league ranks before → after, market send/receive/cut bars, why they might consider it, roster balancing, per-source results and risks.
- **Detailed numbers** (collapsed): weekly, categories, players, lineup fit, flips, sources, balancing options, 1-for-1 parts and equivalents.

Brief fields: `send`/`receive`, `gain`, `market` (`pct` = −100 × `value.gap`; display bands "They receive more/less market value" at ±5%, "About even" between, "High market cost for the gain" for Expensive improvement), `partner`, `sources` (agree / split / fails / unchecked), `roster`, `why`, `rank_reason` and `shares`. Numbers in brief text round half away from zero to match the page. The bands and shortlist are display choices, not calibrated.

## Limitations

- Both market signals are preseason views for now: ADP is preseason, and FantasyPros has not yet published in-season ROS rankings (its ROS page serves the final draft consensus, recorded as `ranking_type: Draft`, `fallback_for: ROS`).
- The depth discount, market curve (decay 40), 10% partner-loss cap, 12-points-per-win line, 15% premium band, screen gate, fill credit, low-usage share and rank weights are declared heuristics. The screen gate was checked against a no-floor run on one capture.
- The pre-replay market check assumes the cheapest cut; Pareto pruning compares only packages with the same partner and a shared player; free-agent fills try a top-13 shortlist.
- No three-team trades. An IL-moved player's return and the later drop it forces are not modelled; an open spot is filled with one permanent free agent, not a planned stream; IL-slotted players are left out.
- Free-agent status, partner adds, trade-blocked players, pending trades and keeper value are not captured; without evidence, Can't Cut and my adds are unverified. The effective date is assumed, not observed. Other teams' later moves are not modelled.
