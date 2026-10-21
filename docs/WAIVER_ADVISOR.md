# Waiver Advisor

`GET /api/advisor?hashtag_weight=…&horizon=auto|1|2|3` calls `advise()` (`src/fantasy_dashboard/advisor/search.py`) with the dashboard's cached forecast, then `web.presentation.present()` adds plain-language `display` labels. Defaults live in `AdvisorConfig`; the page exposes only the horizon. Suggestions stay provisional until Yahoo ownership, budget, claims and timing are verified together.

## Search

- **Horizon.** The current period plus the next (three periods when fewer than three days remain).
- **Pool.** Every unowned FA/W player, minus missing or ambiguous projections, GP 0, Yahoo status O/NA, active claims and players with no horizon game.
- **Roster roles.** Protected (Can't Cut; an IL player whose drop opens no active spot), core (rest-of-season lineup value more than 15% above the best available player at any of his positions), replaceable.
- **Stage 1.** Every first move is scored: every eligible add, action day (now, each game day, the day before a waiver game) and legal drop. A beam of 32 states then builds chronological multi-move plans over the 40 best adds plus 20 permanent upgrades, within each period's add budget. A dropped player is never re-added; a pending waiver blocks earlier follow-ups; started players cannot be dropped after tipoff.
- **Stage 2, local improvement.** The 8 best distinct plans try every single change (another add or day for one move, another drop, removing or inserting a move, over 60+ candidate adds) and keep the best improvement; converged plans are replaced by the next-best unexplored ones until a fixed budget of 300,000 evaluations is spent. The budget is deterministic, so repeated runs match; a 300 s clock is only a safety stop.
- **Parallelism.** Plans are evaluated in forked workers that inherit the engine copy-on-write (`parallel.py`), so results are identical to a single-process run.

On the October 21 live capture the earlier default found a best plan worth 1.39 in 14 s and the widest earlier beam 1.95 in 102 s; the current default finds 2.04 in 84 s, and a five-minute run with twice the width and 1.6 million evaluations finds the same top two plans.

## Objective and ranking

- **Value.** Horizon category points + 0.5 × horizon margin score + season term − 0.15 per add. The season term compares the final roster's rest-of-season margin score with HOLD: gains count at 0.5, losses beyond 0.5 count in full, smaller losses count as zero. The margin score is a logistic of each category margin over the league's interquartile spread (`deterministic._smooth`); it is a ranking aid, not a probability. The deterministic rest-of-season flip count is shown beside it.
- **Fragility.** Each flip is rechecked with Hashtag-only and LeagueWinner-only projections (opponents recomputed by the same engine) and a 5% margin shift. Each fragile flip subtracts 0.5 to form `rank_value`, the score the search and the final list both use; plans need `rank_value` ≥ 0.25.
- **Distinct output.** Best timing per transaction set, best drop per add set, then one card per idea (same dropped players and flips, or a subset/superset), with the rest listed as similar options. Up to five plus HOLD; if none qualify, the three closest alternatives with `why_not`.
- **Integrity.** The day engine picks starters by greedy lineup score with an augmenting-path check (exact for this slot structure) and falls back to `assign_lineup` on ties or locks. HOLD and opponent totals are asserted against the forecast, and every shown plan is replayed through `dated_team_totals`. Evidence gaps make plans provisional; they never block analysis (missing evidence assumes a full budget, marked `acquisition_count_unverified`).

## Recommendation audit

Each shown strategy (never beam candidates) carries `audit` from `advisor.audit.Auditor`, rendered as the collapsed **Detailed audit** section of its card. It explains ranking; it never changes it.

- **Counterfactuals.** Every sub-plan of the recommended sequence is evaluated (all subsets up to five steps; above that, prefixes, singles, leave-one-outs and the net plan). Order and timing are kept. When an omitted step's player would have been dropped later, that later step drops the player he replaced instead (`rewire`). Each sub-plan is checked with the advisor's own legality rules; illegal ones are reported, not scored. Each scored sub-plan is replayed through `dated_team_totals`.
- **Per transaction.** Chronological gain, leave-one-out contribution, the move made alone, and a verdict (`adds value`, `tradeoff`, `hurts`, `margins only`, `costs margin`, `no measurable value`, `required by a later move`). `interaction` is the full plan minus the sum of moves made alone; ≥0.5 category wins either way is flagged as non-additive.
- **Simpler plans.** Shorter legal sub-plans are `preferable` (short-term and rest-of-season wins both no worse) or `comparable` (within 0.5/1.0); the net-permanent plan is always shown for reference.
- **Permanent changes.** The net plan pairs each final addition with the original player it replaces. Each pair shows per-game stats with attempts, GP, scheduled and expected games, lineup starts, lineup value, the swap alone, pool scarcity, and flags (asset downgrade, GP-dependent edge, short-term-only help, a loss small enough for the 0.5 noise allowance). Trade value is stated as not quantified.
- **Sources.** The plan, net plan, simpler plans and leave-one-outs are re-scored with Hashtag-only and LeagueWinner-only (and Blended when another weight is selected), each rebuilding HOLD and opponent totals. Flags: a flip or rest-of-season gain that disappears under one source, horizon margins under 5% of a typical weekly spread, and player-level disagreement (GP ≥15 apart, per-game lineup value ≥25% apart, a counting stat ≥30% apart).
- **Integrity.** The audit asserts that its full-plan numbers equal the card's, that its source deltas equal `source_check`, and that horizon and rest-of-season periods do not overlap.

Two behaviours can make a plan look better than it is, and the audit surfaces both rather than changing ranking: the deterministic rest-of-season flip count swings on knife-edge categories (it can disagree in sign with the margin score), and the ranking ignores rest-of-season margin losses under 0.5.

## Page

- **Overview.** A headline, the horizon matchups (projected record, days and adds left), the capture time and data gaps in plain words, then a comparison table of each strategy and HOLD. Each card shows a goal title from the flipped categories, four tiles (horizon category wins, rest of season, acquisitions, permanent changes), a dated add/drop timeline, a conditional-timing note and every critical risk. Later cards say why they may be preferable.
- **Why this plan?** Per-category results with edge changes, the projection-source table, each move's contribution, permanent changes, simpler alternatives, close variations and Yahoo verification gaps.
- **Detailed audit.** The full audit plus ranking inputs and raw verification fields. Search diagnostics and the horizon setting sit under **Advanced settings and search diagnostics**.

Rest-of-season label: from the full-plan rest-of-season deltas under the selected weight, Hashtag-only and LeagueWinner-only — Stronger (all positive), Weaker (all negative), About the same (all zero), Mixed (signs differ) or Leans stronger/weaker (one direction plus zero). Risk rows come from fragile flips, source gaps, rest-of-season disagreement or cost, a preferable simpler plan and the audit's replacement flags. Yahoo status (Ready now, Provisional, Conditional) is shown apart from projection agreement.

## Tools

`scripts/advisor_snapshot.py [out.json]` runs the advisor on the saved live capture exactly as the dashboard does and prints a summary.
