"""Validated dated NBA fixtures and this league's Yahoo fantasy-week calendar."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import re

CUP_FINAL_DATE = date(2026, 12, 11)
CUP_UNRESOLVED_START = date(2026, 12, 4)
CUP_UNRESOLVED_END = date(2026, 12, 13)
REGULAR_SEASON_TARGET = 82


@dataclass(frozen=True)
class FantasyWeek:
    number: int
    start: date
    end: date
    phase: str
    max_adds: int = 4

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_dict(self) -> dict:
        return {"week": self.number, "start_date": self.start.isoformat(),
                "end_date": self.end.isoformat(), "days": self.days,
                "phase": self.phase, "is_extended": self.days == 14,
                "max_adds": self.max_adds}


@dataclass(frozen=True)
class Game:
    date: date  # ET/Yahoo scoring date supplied in the source CSV
    tipoff_utc: datetime
    teams: tuple[str, str]
    home_away: tuple[str, str]
    source_game_number: str  # original PDF lineage only; never treated as a stable API ID
    tipoff_et: datetime | None = None  # original offset-aware Eastern timestamp


@dataclass(frozen=True)
class Schedule:
    games: tuple[Game, ...]
    team_dates: dict[tuple[str, date], Game]
    team_counts: dict[str, int]
    unscheduled_by_team: dict[str, int]
    file_sha256: str
    excluded_cup_championship_rows: int

    def games_for(self, team: str, start: date, end: date, *, as_of: datetime | None = None) -> list[Game]:
        return [self.team_dates[(team, day)] for day in _days(start, end)
                if (team, day) in self.team_dates and (as_of is None or self.team_dates[(team, day)].tipoff_utc > as_of)]

    def metadata(self) -> dict:
        return {"status": "dated_fixtures_with_cup_gap" if any(self.unscheduled_by_team.values()) else "dated_fixtures_complete",
                "confirmed_unique_games": len(self.games),
                "confirmed_team_game_rows": sum(self.team_counts.values()),
                "confirmed_scheduled_games_per_team": self.team_counts,
                "regular_season_total_target_per_team": REGULAR_SEASON_TARGET,
                "unscheduled_regular_season_games_per_team": self.unscheduled_by_team,
                "unscheduled_games_league_wide": sum(self.unscheduled_by_team.values()) // 2,
                "excluded_cup_championship_rows": self.excluded_cup_championship_rows,
                "source_sha256": self.file_sha256,
                "source_status": "User-supplied dated schedule; original PDF as of 2026-08-13. Postponement status is unavailable in this CSV."}


def _days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def load_fantasy_weeks(path: Path) -> tuple[FantasyWeek, ...]:
    weeks = []
    pattern = re.compile(r"^\|\s*(\d+)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*\**(\d+)\**\s*\|\s*(.*?)\s*\|")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        number, start, end, stated_days, note = match.groups()
        phase = "playoffs" if "Playoffs" in note else "regular_season"
        week = FantasyWeek(int(number), date.fromisoformat(start), date.fromisoformat(end), phase)
        if week.days != int(stated_days):
            raise ValueError(f"Yahoo week {number} has inconsistent duration")
        weeks.append(week)
    if len(weeks) != 21 or [w.number for w in weeks] != list(range(1, 22)):
        raise ValueError("Yahoo calendar must have exactly 21 numbered weeks")
    if weeks[0].start != date(2026, 10, 20) or weeks[-1].end != date(2027, 3, 28):
        raise ValueError("Yahoo fantasy season boundaries differ from supplied handoff")
    if any(a.end + timedelta(days=1) != b.start for a, b in zip(weeks, weeks[1:])):
        raise ValueError("Yahoo fantasy weeks must be contiguous and nonoverlapping")
    if [w.number for w in weeks if w.days == 14] != [7, 17]:
        raise ValueError("The two extended Yahoo matchup periods differ from supplied handoff")
    if [w.number for w in weeks if w.phase == "playoffs"] != [19, 20, 21]:
        raise ValueError("Yahoo playoff periods differ from supplied handoff")
    return tuple(weeks)


def week_for_day(weeks: tuple[FantasyWeek, ...], day: date) -> FantasyWeek | None:
    return next((week for week in weeks if week.start <= day <= week.end), None)


def load_schedule(path: Path) -> Schedule:
    """Load a complete current CSV snapshot; repeated loads are idempotent."""
    raw = path.read_bytes()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    required = {"team_abbr", "game_date", "tipoff_at_et", "tipoff_at_utc", "opponent_abbr", "home_away", "source_game_number"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("NBA team schedule is missing required columns")
    grouped: dict[tuple[date, tuple[str, str], datetime], list[dict]] = defaultdict(list)
    excluded = 0
    team_names: set[str] = set()
    for row in rows:
        day = date.fromisoformat(row["game_date"])
        kind = (row.get("game_type") or "").casefold()
        status = (row.get("status") or "").casefold()
        if day == CUP_FINAL_DATE or "cup championship" in kind or "cup final" in kind:
            excluded += 1
            continue
        if status in {"cancelled", "canceled", "postponed"}:
            continue  # date is not confirmed until the replacement fixture is supplied
        if status and status not in {"scheduled", "confirmed"}:
            raise ValueError(f"Unknown NBA game status: {status}")
        team, opponent = row["team_abbr"], row["opponent_abbr"]
        if not team or not opponent or team == opponent or row["home_away"] not in {"home", "away", "neutral"}:
            raise ValueError("Invalid NBA matchup identity or venue")
        et = datetime.fromisoformat(row["tipoff_at_et"])
        utc = datetime.fromisoformat(row["tipoff_at_utc"].replace("Z", "+00:00"))
        if et.tzinfo is None or utc.tzinfo is None or et.date() != day or et.astimezone(timezone.utc) != utc:
            raise ValueError("NBA schedule has inconsistent scoring date or timezone-aware tipoff")
        utc = utc.astimezone(timezone.utc)
        grouped[(day, tuple(sorted((team, opponent))), utc)].append(row)
        team_names.add(team)
    if len(team_names) != 30:
        raise ValueError("NBA schedule must include all 30 NBA teams")
    games = []
    team_dates: dict[tuple[str, date], Game] = {}
    for (day, teams, utc), pair in grouped.items():
        if len(pair) != 2 or {r["team_abbr"] for r in pair} != set(teams) or any(r["opponent_abbr"] != next(t for t in teams if t != r["team_abbr"]) for r in pair):
            raise ValueError(f"NBA fixture is missing its reciprocal team row: {teams} {day}")
        ordered = sorted(pair, key=lambda r: r["team_abbr"])
        venues = tuple(r["home_away"] for r in ordered)
        if venues not in {("home", "away"), ("away", "home"), ("neutral", "neutral")}:
            raise ValueError("NBA fixture has inconsistent home/away status")
        if ordered[0]["source_game_number"] != ordered[1]["source_game_number"]:
            raise ValueError("Reciprocal NBA rows disagree on original source lineage")
        game = Game(day, utc, teams, venues, ordered[0]["source_game_number"],
                    datetime.fromisoformat(ordered[0]["tipoff_at_et"]))
        for team in teams:
            key = team, day
            if key in team_dates:
                raise ValueError(f"Multiple fixtures for {team} on {day}; review schedule update")
            team_dates[key] = game
        games.append(game)
    counts = Counter(team for team, _ in team_dates)
    if any(counts[team] > REGULAR_SEASON_TARGET for team in team_names):
        raise ValueError("An NBA team exceeds 82 confirmed regular-season games")
    sorted_games = tuple(sorted(games, key=lambda g: (g.date, g.tipoff_utc, g.teams)))
    return Schedule(sorted_games, team_dates, dict(sorted(counts.items())),
                    {team: REGULAR_SEASON_TARGET - counts[team] for team in sorted(team_names)},
                    sha256(raw).hexdigest(), excluded)


def cup_window_provisional(week: FantasyWeek, schedule: Schedule) -> bool:
    return (week.start <= CUP_UNRESOLVED_END and week.end >= CUP_UNRESOLVED_START
            and any(schedule.unscheduled_by_team.values()))
