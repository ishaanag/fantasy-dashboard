# Yahoo Fantasy Basketball 2026–27: Matchup Calendar and Daily Lineup Locks

**Implementation handoff for ChatGPT Cowork / fantasy-dashboard**  
**League:** Big Baller League (Yahoo league ID `92103`)  
**Authority:** User-supplied Yahoo matchup dates and attached `league-settings(2).md` are authoritative for this league. Yahoo Help and Yahoo Fantasy support verify platform mechanics. Do **not** substitute Yahoo public-league/default playoff dates.

## 1. Required league configuration

| Setting | Value | Dashboard implication |
|---|---|---|
| Scoring | Head-to-Head — Categories (9-cat) | Matchup stats accumulate over each *fantasy matchup period*. |
| Roster transaction deadline | **Daily – Today** | Successful roster transactions take effect today; lineup locks are per player/game. |
| Max acquisitions | **4 per fantasy week** | Applies to each Yahoo matchup period, including 14-day Weeks 7 and 17; not 4 per calendar Monday–Sunday. |
| Waivers | Standard; 1 day; continual rolling list | Waiver claims are not immediate. Only completed adds are eligible for a same-day start. |
| Lock benched players | **No** | Benched players may be dropped after their game begins; this does *not* make their fantasy stats count or allow late starts. |
| Active positions | PG, SG, G, SF, PF, F, C, C, Util, Util | **10** active lineup slots per day, subject to Yahoo position eligibility. |
| Inactive positions | BN × 4; IL × 3 | BN/IL stats do not count for that day's active lineup. |
| Playoffs | 6 teams; **Weeks 19–21** | Week 19 first playoff matchup; Week 21 final matchup. |
| Playoff tiebreak / reseeding | Higher seed wins; no reseeding | Preserve the fixed bracket and Yahoo tiebreak settings. |
| Lock eliminated teams | Yes | Do not recommend further additions for eliminated teams as executable actions. |
| Scoring categories | FG%, FT%, 3PTM, PTS, REB, AST, STL, BLK, TO | Daily starters feed matchup totals; percentage stats must use FGM/FGA and FTM/FTA, not averaged percentages. |

## 2. Authoritative fantasy-week calendar (inclusive dates)

All dates are `YYYY-MM-DD`; start and end are **inclusive**. Each row is one Yahoo matchup/scoring period, *not necessarily seven days*.

| Week | Start | End | Days | Phase / notes |
|---:|---|---|---:|---|
| 1 | 2026-10-20 | 2026-10-25 | 6 | Regular season; Tuesday opener |
| 2 | 2026-10-26 | 2026-11-01 | 7 | Regular season |
| 3 | 2026-11-02 | 2026-11-08 | 7 | Regular season |
| 4 | 2026-11-09 | 2026-11-15 | 7 | Regular season |
| 5 | 2026-11-16 | 2026-11-22 | 7 | Regular season |
| 6 | 2026-11-23 | 2026-11-29 | 7 | Regular season |
| 7 | 2026-11-30 | 2026-12-13 | **14** | Regular season; **NBA Cup extended matchup** |
| 8 | 2026-12-14 | 2026-12-20 | 7 | Regular season |
| 9 | 2026-12-21 | 2026-12-27 | 7 | Regular season |
| 10 | 2026-12-28 | 2027-01-03 | 7 | Regular season |
| 11 | 2027-01-04 | 2027-01-10 | 7 | Regular season |
| 12 | 2027-01-11 | 2027-01-17 | 7 | Regular season |
| 13 | 2027-01-18 | 2027-01-24 | 7 | Regular season |
| 14 | 2027-01-25 | 2027-01-31 | 7 | Regular season |
| 15 | 2027-02-01 | 2027-02-07 | 7 | Regular season |
| 16 | 2027-02-08 | 2027-02-14 | 7 | Regular season |
| 17 | 2027-02-15 | 2027-02-28 | **14** | Regular season; **All-Star extended matchup** |
| 18 | 2027-03-01 | 2027-03-07 | 7 | Final regular-season matchup |
| 19 | 2027-03-08 | 2027-03-14 | 7 | **Playoffs — Round 1** |
| 20 | 2027-03-15 | 2027-03-21 | 7 | **Playoffs — Round 2** |
| 21 | 2027-03-22 | 2027-03-28 | 7 | **Playoffs — championship** |

**Fantasy season end:** 2027-03-28. NBA games after that date must **not** count toward this league's fantasy season. The 2026–27 NBA regular season and Yahoo's public-league fantasy playoffs may continue past this league's end; they are not a reason to extend this calendar.

## 3. Verified Yahoo Daily – Today behavior

These are separate controls: **(a) transactions/ownership**, **(b) lineup position changes**, and **(c) scoring eligibility**.

1. **Lineup lock is per player's scheduled NBA game start**, not a blanket lock at the day's first NBA tipoff. Until a player's own scheduled game starts, they can be moved between eligible starting and bench slots, provided the swap does not require moving a locked player. After their scheduled start, their active/bench assignment for that game is fixed. **Use scheduled tipoff, not the actual delayed tipoff, for the rule.**
2. **Daily – Today transactions:** Yahoo documents *no advance daily transaction deadline* for this mode; a successful add/drop updates the roster immediately. However, a newly added player can contribute **only if** acquired and put in a valid active slot **before that player's scheduled tipoff**. An add cannot collect stats retroactively.
3. **Pending waiver claims:** "1 day" waiver time does not mean every player can be added immediately. An unprocessed claim does not confer ownership or lineup eligibility. Actual Yahoo claim-processing timestamps and status take precedence over estimates.
4. **Started active player:** Once the scheduled tipoff passes, that day's started active player is locked into the starting position and cannot normally be directly dropped until the next day's unlock. Avoid promising same-day roster churn by dropping an already-started active player.
5. **Started benched player (`Lock Benched Players = No`):** Yahoo Fantasy support states a benched player can be dropped even after their NBA game has started when this setting is off. That does **not** allow activating them after tipoff; their completed/started game cannot be counted for the manager. Platform-specific constraints (e.g., mobile versus web, IL moves) may differ; present proposed transactions as advice until validated against Yahoo.
6. **Scoring:** Only players active *at the time of their game* earn scoring stats for the team; BN and IL do not. Changing tomorrow's lineup does not remove stats legally earned in a previous game. NBA All-Star Game statistics do not count toward Yahoo league scoring.
7. **Two-week matchup periods:** Weeks **7** and **17** are single Yahoo fantasy weeks, with one opponent and cumulative stats for the whole 14-day period. The **4-add cap does not reset on the intervening Monday**. Treat any midperiod reset as a bug unless Yahoo's live league state demonstrates a commissioner rule change.

### Same-day illustration

At 7:40 p.m. Eastern, Player A's scheduled game began at 7:00 p.m., while Player B's is scheduled for 10:00 p.m.:

- Player A's active/bench decision is already locked for that day's game.
- Player B can still be set active before 10:00 p.m. if owned, eligible for an open/unlocked starting slot, and not blocked by an already-locked player.
- A newly acquired *free agent* B may be usable today if the add completed before 10:00 p.m. and Yahoo permits the lineup placement. This is **not** true for a pending waiver claim.
- A previously benched A may be droppable under this league's `Lock Benched Players = No` rule, but A's past stats do not become active.

## 4. Dashboard implementation contract

**Canonical data model:** Keep an explicit 21-row `fantasyWeeks` table (or equivalent JSON/config) with `week`, `startDate`, `endDate`, `phase`, `isExtended`, and `maxAdds: 4`. Do not calculate `week = floor(daysSinceOct20 / 7) + 1`.

**Date attribution:** Each NBA game must have an authoritative *fantasy scoring date* (`YYYY-MM-DD`, preferably Yahoo's own game date). Find its week with `startDate <= fantasyDate <= endDate`. Store scheduled tipoff as an offset-aware/UTC timestamp for lock comparisons. Display time in the user's selected timezone; avoid parsing a local tipoff string as UTC or grouping a UTC calendar date into the wrong fantasy week. Yahoo's displayed times can depend on account timezone settings.

**Lineup eligibility rule (pseudocode):**

```text
weekForGame(game) = only fantasyWeeks row containing game.fantasyDate
playerLockedForGame(player, now) = now >= player.game.scheduledTipoffUtc
canCountStats(player, game) =
  game is in fantasy season
  AND player was owned before their scheduled tipoff
  AND player occupied an eligible ACTIVE slot at their scheduled tipoff
canRecommendSameDayStart(player, now, lineup) =
  player is currently owned
  AND player has a scheduled game today
  AND now < player.game.scheduledTipoffUtc
  AND a legal active slot is open or can be freed using only unlocked players
```

Treat the pseudocode as a forecast/optimizer model, **not** a replacement for Yahoo's authoritative transaction/lineup acceptance state. Handle late schedule changes, postponed games, waived players and Yahoo status updates by refreshing the source and marking uncertain availability instead of silently assuming locks or stats.

**Optimizer requirements:**

- Evaluate all daily starter possibilities within each *entire* Yahoo matchup period, respecting 10 active slots, eligible positions, individual game locks, and the 4-add limit.
- Streaming/player value should account for real opportunities to start: a scheduled game on an overcrowded day may be less valuable than a game on a low-volume day.
- Use the remaining fantasy days of the current period and complete future periods, not a fixed next-seven-days horizon.
- Accumulate 9-cat stats over each matchup; calculate `FG% = sum(FGM)/sum(FGA)` and `FT% = sum(FTM)/sum(FTA)` with zero-denominator handling; turnovers are lower-is-better.
- Season scheduling, NBA Cup knockout-game generation, and postponed-game handling belong to the NBA schedule module; never invent tipoff times or extra NBA Cup games here.

## 5. Must-pass acceptance tests

| Test | Expected outcome |
|---|---|
| 2026-10-20 / 2026-10-25 | Week 1 |
| 2026-10-26 | Week 2 |
| 2026-12-06 **and** 2026-12-07 | Both Week 7; no Monday acquisition reset |
| 2026-12-13 / 2026-12-14 | Week 7 / Week 8 |
| 2027-02-21 **and** 2027-02-22 | Both Week 17; no Monday acquisition reset |
| 2027-02-28 / 2027-03-01 | Week 17 / Week 18 |
| 2027-03-07 / 2027-03-08 | Regular season Week 18 / playoff Week 19 |
| 2027-03-21 / 2027-03-22 | Playoff Week 20 / championship Week 21 |
| 2027-03-28 / 2027-03-29 | Week 21 / **outside league fantasy season** |
| A starts 7 p.m.; B starts 10 p.m.; now 8 p.m. | A locked; B still adjustable if move is legal |
| Add B at 9:30 p.m., start B before 10 p.m. | Eligible to accrue B's stats today, if acquisition and lineup transaction succeed |
| Add B after 10 p.m. | Cannot count B's game today |
| A starts on BN; lock-benched setting is No | May drop A after tipoff, but cannot retroactively activate/count A's game |
| Already-started active A | Cannot directly drop/move A to free today's active slot |
| Week 7/17 with 4 completed adds | No fifth acquisition until next matchup period, despite intermediate Monday |
| All-Star Game | No scoring stats |

Also assert all 21 date ranges are ordered, contiguous, nonoverlapping, and have the supplied durations. Do not modify any user's source league settings or override live Yahoo data silently.

## 6. Sources and confidence

**League-specific (authoritative):** User-provided fantasy-week table in chat; uploaded `league-settings(2).md` (2026–27 Big Baller League settings, league `92103`). These override public default league settings.

**Yahoo official docs (platform behavior):**

- Transaction and lineup deadlines: https://help.yahoo.com/kb/sports-rewards-program/complete-draft-leagues-deadline-sln6775.html
- Roster and lineup management: https://ca.help.yahoo.com/kb/fantasy-football/roster-lineup-management-yahoo-fantasy-sln22673.html
- Stats counted only for active players at game time; no retroactive credit: https://help.yahoo.com/kb/homepage/standard-leagues-sln6868.html
- All-Star Game scoring and unchanged weekly acquisitions cap: https://help.yahoo.com/kb/fantasy-basketball/all-star-week-scoring-fantasy-basketball-sln14221.html
- Transaction limits apply through playoffs: https://help.yahoo.com/kb/fantasy-basketball/set-adjust-transaction-limits-tknproductsportsfantasyname-sln6981.html
- Yahoo displayed timezone settings: https://help.yahoo.com/kb/SLN29111.html

**Yahoo Fantasy support clarification (moderator comments, weaker than formal Help):**

- Lock-benched = No allows dropping a started benched player; started active players remain locked: https://www.reddit.com/r/YahooFantasy/comments/17ehnoh/ and https://www.reddit.com/r/YahooFantasy/comments/13onn6x/
- Yahoo discussion of two-week NBA Cup matchup (4 adds for an entire 14-day matchup): https://www.reddit.com/r/fantasybball/comments/1h4o3zb/

**Unverified / must not invent:** exact overnight waiver-processing clock time; behavior of unusual commissioner overrides; lineup effects of specific postponed/rescheduled games; Yahoo's handling of ambiguous cross-midnight basketball dates. Use live Yahoo state to settle these when applicable.
