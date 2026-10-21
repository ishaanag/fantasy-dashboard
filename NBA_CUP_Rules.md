# Handoff: 2026–27 NBA Cup schedule handling

## Goal
Make the Yahoo Fantasy Basketball dashboard accurate while the NBA Cup determines the **final two regular-season games per NBA team**. Implement within the existing schedule ingestion, game-count, and projection logic; do not redesign unrelated features.

## Source data
- `nba_2026_27_team_schedule.csv`: **2,400 team-game rows**, 80 dated games per team. Fields include `team_abbr`, `game_date`, `tipoff_at_et`, `tipoff_at_utc`, `opponent_abbr`, `home_away`, `source_game_number`.
- `nba_2026_27_games.csv`: **1,200 dated NBA games**, one row per game.
- Original NBA schedule PDF is **as of August 13, 2026**, subject to change.

## NBA Cup rules that affect the dashboard
- Each team plays **82** regular-season games: **80 currently dated + 2 TBD** (30 additional games league-wide).
- Cup **group-play games are already included** in the 80 dated games. Do **not** add them again.
- Cup **quarterfinals (Dec 4–5)** and **semifinals (Dec 8–9, as applicable)** count as regular-season games. Teams that do not advance, or are eliminated, receive replacement regular-season games during the Cup scheduling window.
- The **Dec 11 championship does NOT count** as a regular-season game or toward regular-season player statistics. Never count it for fantasy volume.
- Opponents, dates, and tipoffs for outstanding games must come from an authoritative updated schedule; never fabricate them.

## Implementation requirements
1. Keep the 1,200 known games as confirmed dated fixtures; track `unscheduled_regular_season_games = 82 - confirmed_regular_season_games` **per team**, initially **2**.
2. **Dated/weekly fantasy calculations** (matchup game counts, streaming, back-to-backs, daily availability, remaining games in a date window): include only confirmed fixtures. Clearly label Cup-window projections as **provisional/understated** while dates are unresolved. Respect the league's actual fantasy-week boundaries.
3. **Season-total views:** show both `confirmed_scheduled_games` and `regular_season_total_target = 82`, plus the unscheduled gap; do not pretend the undated games have known dates.
4. Support **idempotent ingestion** of newly announced NBA Cup/replacement fixtures, matching games by authoritative stable ID when available (otherwise verified matchup/date/time), avoiding duplicates. Recompute impacted team dates, game counts, projections, and matchup totals when the schedule changes.
5. Exclude Cup championship fixtures from the **regular-season schedule and all regular-season fantasy projections**, even if a live feed includes them.
6. Preserve timezone-aware tipoff timestamps (Eastern/UTC) and neutral-site games; don't rely on PDF `source_game_number` as a Yahoo or NBA API ID.

## Acceptance checks
- Initial state: **1,200 unique games / 2,400 team-game rows / 80 confirmed per team / 2 TBD per team / 30 undated games league-wide**.
- Dated December windows do not show invented games, and the dashboard warns about provisional Cup-week totals.
- After official additions: **1,230 unique regular-season games, 82 per team, zero TBD**; no double counting of group play, knockout games, replacement games, or championship.
- Re-imports and reschedules update existing fixtures without duplicates; all affected team and fantasy-week totals refresh.

**Reference:** [Official 2026 NBA Cup format/rules](https://www.nba.com/news/nba-cup-101) and [FAQ](https://www.nba.com/news/nba-cup-frequently-asked-questions).
