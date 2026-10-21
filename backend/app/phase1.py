"""Deterministic Phase 1 projection, lineup, and descriptive team strengths."""

from __future__ import annotations

import os

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from hashlib import sha256
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from math import isfinite, sqrt
from pathlib import Path
import csv
import json

from .blending import blend
from .browser_players import _detail, load_player_capture, match_browser_players
from .projections import Projection, import_workbook
from .matching import select_unique
from .reviewed_matches import apply_reviewed_matches
from .settings import read_local_settings
from .yahoo_browser import load_browser_capture
from .schedule import FantasyWeek, cup_window_provisional, load_fantasy_weeks, load_schedule, week_for_day


ROOT = Path(__file__).resolve().parents[2]
SLOTS = ("PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util")
CATEGORIES = ("fg_pct", "ft_pct", "three_ptm", "pts", "reb", "ast", "stl", "blk", "turnovers")
VOLUMES = ("fgm", "fga", "ftm", "fta", "three_ptm", "pts", "reb", "ast", "stl", "blk", "turnovers")
MODEL_VERSION = "dated-daily-lineups-1"
EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RosterPlayer:
    team_id: str
    site_player_id: str
    name: str
    assigned_slot: str
    eligible_positions: tuple[str, ...]
    nba_team: str | None = None


def eligible(slot: str, positions: tuple[str, ...]) -> bool:
    p = set(positions)
    return (slot == "Util" and bool(p) or slot == "G" and bool(p & {"PG", "SG"})
            or slot == "F" and bool(p & {"SF", "PF"}) or slot in p)


def assign_lineup(players: list[RosterPlayer], scores: dict[str, float], *, require_full: bool = True,
                  blocked_slots: tuple[str, ...] = ()) -> dict[str, str]:
    """Maximum-weight legal assignment; each site ID and slot occurs at most once."""
    if len({p.site_player_id for p in players}) != len(players):
        raise ValueError("Duplicate site player ID in lineup candidates")
    candidates = sorted((p for p in players if p.assigned_slot != "IL"), key=lambda p: p.site_player_id)
    if any(p.site_player_id not in scores or not isfinite(scores[p.site_player_id]) for p in candidates):
        raise ValueError("Every lineup candidate needs a finite score")

    blocked = Counter(blocked_slots)
    if any(slot not in SLOTS or count > SLOTS.count(slot) for slot, count in blocked.items()):
        raise ValueError("Invalid locked lineup slots")
    blocked_mask = 0
    for slot, count in blocked.items():
        for index in [i for i, name in enumerate(SLOTS) if name == slot][:count]:
            blocked_mask |= 1 << index
    states: dict[int, tuple[float, tuple[tuple[int, int], ...]]] = {blocked_mask: (0.0, ())}
    full = (1 << len(SLOTS)) - 1
    for i, player in enumerate(candidates):
        next_states = states.copy()  # bench is always an option
        for mask, (score, assignment) in states.items():
            for j, slot in enumerate(SLOTS):
                bit = 1 << j
                if mask & bit or not eligible(slot, player.eligible_positions):
                    continue
                new_mask = mask | bit
                option = (score + scores[player.site_player_id], assignment + ((i, j),))
                previous = next_states.get(new_mask)
                if previous is None or option[0] > previous[0] + 1e-12 or (abs(option[0] - previous[0]) <= 1e-12 and option[1] < previous[1]):
                    next_states[new_mask] = option
        states = next_states
    if require_full:
        if full not in states:
            raise ValueError("Roster cannot fill all ten legal active slots")
        chosen = states[full][1]
    else:
        chosen = min(states.items(), key=lambda item: (-item[1][0], -item[0].bit_count(), item[1][1]))[1][1]
    return {candidates[i].site_player_id: SLOTS[j] for i, j in chosen}


def resolve_projection(sources: dict[str, Projection], hashtag_weight: float) -> dict:
    if not isfinite(hashtag_weight) or not 0 <= hashtag_weight <= 1:
        raise ValueError("Hashtag weight must be between 0 and 1")
    selected_source = "Hashtag" if hashtag_weight == 1 else "LeagueWinner" if hashtag_weight == 0 else None
    if selected_source and selected_source not in sources:
        raise ValueError(f"Selected source {selected_source} is missing for this player")
    result = blend(sources.get("Hashtag"), sources.get("LeagueWinner"), hashtag_weight)
    required = (*VOLUMES, "gp")
    missing = [name for name in required if result.values[name] is None]
    if missing:
        raise ValueError(f"Projection has missing required inputs: {missing}")
    values = result.values
    if values["gp"] < 0 or values["gp"] > 82:
        raise ValueError("Projected games must be between 0 and 82")
    for prefix in ("fg", "ft"):
        if values[prefix + "m"] < 0 or values[prefix + "a"] < values[prefix + "m"]:
            raise ValueError("Invalid projected shooting volume")
    if any(values[key] < 0 for key in VOLUMES):
        raise ValueError("Negative per-game projection")
    return {"values": values, "sources": result.sources, "coverage": result.coverage,
            "disagreement": result.disagreement,
            "source_rows": {key: {"sheet": row.sheet, "row": row.row, "name": row.name,
                                   "reported_fg_pct": row.fg_pct_reported, "reported_ft_pct": row.ft_pct_reported,
                                   "issues": [asdict(issue) for issue in row.issues]}
                            for key, row in sorted(sources.items())}}


def load_inputs(root: Path = ROOT, reports: Path | None = None) -> dict:
    """Revalidate saved captures and reviewed source links on each launch."""
    # FANTASY_REPORTS_DIR lets the regression suite pin an archived capture; the app uses the live folder.
    reports = Path(reports or os.environ.get("FANTASY_REPORTS_DIR") or root / "data" / "reports")
    workbook = root / "player-projections-2026-27.xlsx"
    batch = import_workbook(workbook)
    _, duplicate_rows = select_unique(batch.records)
    if duplicate_rows:
        raise ValueError(f"Duplicate projection source rows require review: {len(duplicate_rows)} groups")
    local = read_local_settings(root / "league-settings.md")
    league_path = reports / "yahoo-browser-capture.json"
    player_path = reports / "yahoo-browser-players-capture.json"
    league, _ = load_browser_capture(league_path, local)
    availability, site_players = load_player_capture(player_path)
    if availability["status"] != "verified" or league["league_id"] != availability["league_id"]:
        raise ValueError("Yahoo capture is not reconciled to the expected league")
    league_manifest = json.loads(league_path.read_text(encoding="utf-8"))
    player_manifest = json.loads(player_path.read_text(encoding="utf-8"))
    league_teams = {str(t["team_id"]): t["name"] for t in league_manifest["teams"]}
    player_teams = {str(t["team_id"]): t["name"] for t in player_manifest["teams"]}
    if set(league_teams) != set(player_teams) or len(player_teams) != 10:
        raise ValueError("Ten team identities must agree across Yahoo captures")
    # Team IDs are the binding; names are mutable and come from the fresh roster/player capture.
    availability["renamed_teams"] = {k: [league_teams[k], v] for k, v in player_teams.items() if league_teams[k] != v}
    if league["settings_summary"]["different"] or league["settings_summary"]["unavailable"]:
        raise ValueError("Yahoo league rules are not fully verified")
    decisions = root / "data" / "aliases" / "player_site_matches.csv"
    matches = apply_reviewed_matches(match_browser_players(batch.records, site_players), batch.records, site_players, decisions)
    if matches["manual_review"] or matches["duplicate_mappings"] or matches["unmatched_rostered_browser_players"]:
        raise ValueError("Unresolved or duplicated projection identities")
    by_row = {(p.sheet, p.row): p for p in batch.records}
    projections: dict[str, dict[str, Projection]] = defaultdict(dict)
    for link in matches["candidate_links"]:
        row = by_row[(link["sheet"], link["row"])]
        target = projections[link["site_player_id"]]
        if row.source in target:
            raise ValueError(f"Duplicate {row.source} projection for site ID {link['site_player_id']}")
        target[row.source] = row
    roster_file = reports / player_manifest["rosters"]["file"]
    rosters: dict[str, list[RosterPlayer]] = defaultdict(list)
    with roster_file.open(newline="", encoding="utf-8") as handle:
        for team_id, slot, site_id, name, detail in csv.reader(handle, delimiter="\t"):
            nba_team, positions = _detail(detail)
            rosters[team_id].append(RosterPlayer(team_id, site_id, name, slot, positions, nba_team))
    if set(rosters) != set(player_teams):
        raise ValueError("Roster team IDs do not match league teams")
    for team_id, roster in rosters.items():
        if len(roster) > 17 or Counter(p.assigned_slot for p in roster)["BN"] > 4 or Counter(p.assigned_slot for p in roster)["IL"] > 3:
            raise ValueError(f"Roster capacity exceeded for team {team_id}")
        for p in roster:
            if p.site_player_id not in projections:
                raise ValueError(f"Unmatched rostered Yahoo player {p.site_player_id}")
    schedule = load_schedule(root / "nba_2026_27_team_schedule.csv")
    weeks = load_fantasy_weeks(root / "YAHOO_FANTASY_CALENDAR_AND_LINEUP_LOCKS_2026-27.md")
    if any(p.nba_team not in schedule.team_counts for roster in rosters.values() for p in roster):
        raise ValueError("Rostered Yahoo NBA team is missing from the dated schedule")
    availability["player_fingerprint"] = player_manifest["players"]["fnv32"]
    availability["roster_fingerprint"] = player_manifest["rosters"]["fnv32"]
    inputs = {"league": league, "availability": availability, "site_players": site_players,
            "pool_manual_review": matches["manual_review"], "teams": player_teams,
            "matchup_team_names": league_teams,
            "schedule": schedule, "weeks": weeks,
            "calendar_sha256": sha256((root / "YAHOO_FANTASY_CALENDAR_AND_LINEUP_LOCKS_2026-27.md").read_bytes()).hexdigest(),
            "cup_rules_sha256": sha256((root / "NBA_CUP_Rules.md").read_bytes()).hexdigest(),
            "rosters": rosters, "projections": projections, "workbook_sha256": batch.file_sha256,
            "decision_file_sha256": matches["decision_file_sha256"],
            "matching": {"linked_source_rows": len(matches["candidate_links"]),
                         "reviewed_count": matches["reviewed_count"],
                         "unmatched_rostered": len(matches["unmatched_rostered_browser_players"]),
                         "duplicate_mappings": len(matches["duplicate_mappings"]),
                         "unmatched_projection_rows": len(matches["unmatched_projections"])}}
    # A synchronized Yahoo status capture is observation of the next game, not a
    # return-date forecast. Keep the projection GP unchanged for later games.
    from .yahoo_transactions import SNAPSHOT_NAME, load_injury_statuses
    statuses, captured = load_injury_statuses(reports / SNAPSHOT_NAME, inputs)
    if captured:
        inputs["injury_statuses"] = statuses
        inputs["injury_status_captured_at_utc"] = captured
    from .adp_capture import load_adp, load_ecr   # market signals for trade value; optional
    inputs["adp"] = load_adp(reports)
    inputs["ecr"] = load_ecr(reports)
    return inputs


def unavailable_for_game(inputs: dict | None, player_id: str, tipoff: datetime, as_of: datetime) -> bool:
    """Confirmed Yahoo out/inactive status applies only to the imminent game."""
    if not inputs or inputs.get("injury_statuses", {}).get(player_id) not in {"O", "NA", "OFS", "INJ"}:
        return False
    stamp = inputs.get("injury_status_captured_at_utc")
    if stamp != inputs.get("availability", {}).get("captured_at_utc"):
        return False
    captured = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (captured <= as_of <= captured + timedelta(hours=36) and
            as_of < tipoff <= captured + timedelta(hours=36) and
            tipoff.astimezone(EASTERN).date() <= captured.astimezone(EASTERN).date() + timedelta(days=1))


def _lineup_score(values: dict[str, float]) -> float:
    """Fixed, declared per-game proxy used only to choose ten active players."""
    per_game = (values["pts"] / 20 + values["reb"] / 8 + values["ast"] / 6 +
                values["three_ptm"] / 3 + values["stl"] / 1.5 + values["blk"] / 1.5 -
                values["turnovers"] / 4 + (values["fgm"] - .47 * values["fga"]) / 5 +
                (values["ftm"] - .78 * values["fta"]) / 3)
    return per_game * values["gp"] / 82


def _strengths(teams: list[dict]) -> None:
    for category in CATEGORIES:
        observed = [team["totals"]["categories"][category] for team in teams]
        if any(value is None for value in observed):
            raise ValueError(f"Category {category} has no attempts for a team; ranking is unavailable")
        oriented = [-value if category == "turnovers" else value for value in observed]
        mean = sum(oriented) / len(oriented)
        variance = sum((value - mean) ** 2 for value in oriented) / len(oriented)
        sd = sqrt(variance)
        for index, team in enumerate(teams):
            value = oriented[index]
            rank = 1 + sum(other > value + 1e-12 for other in oriented)
            percentile = (sum(other < value - 1e-12 for other in oriented) + .5 * sum(abs(other - value) <= 1e-12 for other in oriented)) / len(oriented)
            team["strengths"][category] = {"rank": rank, "percentile": percentile,
                                            "z_score": (value - mean) / sd if sd else 0.0}
    for team in teams:
        team["power_score"] = sum(team["strengths"][category]["percentile"] for category in CATEGORIES) / len(CATEGORIES)
    teams.sort(key=lambda team: (-team["power_score"], int(team["team_id"])))
    for index, team in enumerate(teams, 1):
        team["power_rank"] = index


def _dated_team_totals(roster: list[RosterPlayer], projected: list[tuple[RosterPlayer, dict]],
                       schedule, week, as_of: datetime,
                       lineup_snapshot_date=None, available_from_by_id=None, available_until_by_id=None, injury_inputs=None) -> tuple[dict, list[dict], list[dict]]:
    """Expected stats from each confirmed future team-game and legal daily starts."""
    by_id = {p.site_player_id: projection for p, projection in projected}
    available_from_by_id = available_from_by_id or {}
    available_until_by_id = available_until_by_id or {}
    totals = {key: 0.0 for key in VOLUMES}
    starts_by_id: dict[str, list[dict]] = defaultdict(list)
    days = []
    day = week.start
    local_as_of_day = as_of.astimezone(EASTERN).date()
    from datetime import timedelta
    while day <= week.end:
        candidates = [p for p in roster if p.assigned_slot != "IL" and
                      (p.nba_team, day) in schedule.team_dates and
                      schedule.team_dates[(p.nba_team, day)].tipoff_utc > as_of and
                      schedule.team_dates[(p.nba_team, day)].tipoff_utc >= available_from_by_id.get(p.site_player_id, as_of) and
                      schedule.team_dates[(p.nba_team, day)].tipoff_utc < available_until_by_id.get(p.site_player_id, datetime.max.replace(tzinfo=timezone.utc)) and
                      not unavailable_for_game(injury_inputs, p.site_player_id, schedule.team_dates[(p.nba_team, day)].tipoff_utc, as_of)]
        scores = {p.site_player_id: max(1e-6, _lineup_score(by_id[p.site_player_id]["values"])) for p in candidates}
        locked = ()
        if day == local_as_of_day and day == lineup_snapshot_date:
            locked = tuple(p.assigned_slot for p in roster
                           if p.assigned_slot not in {"BN", "IL"} and
                           (p.nba_team, day) in schedule.team_dates and
                           schedule.team_dates[(p.nba_team, day)].tipoff_utc <= as_of)
        chosen = assign_lineup(candidates, scores, require_full=False, blocked_slots=locked)
        assignments = []
        for p in sorted(candidates, key=lambda player: player.site_player_id):
            slot = chosen.get(p.site_player_id)
            if slot is None:
                continue
            game = schedule.team_dates[(p.nba_team, day)]
            availability = by_id[p.site_player_id]["values"]["gp"] / 82
            for key in VOLUMES:
                totals[key] += availability * by_id[p.site_player_id]["values"][key]
            entry = {"site_player_id": p.site_player_id, "slot": slot,
                     "tipoff_utc": game.tipoff_utc.isoformat().replace("+00:00", "Z"),
                     "tipoff_et": game.tipoff_et.isoformat() if game.tipoff_et else None,
                     "expected_appearance": availability}
            assignments.append(entry)
            starts_by_id[p.site_player_id].append({"date": day.isoformat(), **entry})
        days.append({"date": day.isoformat(), "scheduled_eligible_players": len(candidates),
                     "captured_locked_slots": locked, "modeled_starts": assignments})
        day += timedelta(days=1)
    categories = {key: totals[key] for key in CATEGORIES if key not in {"fg_pct", "ft_pct"}}
    categories["fg_pct"] = totals["fgm"] / totals["fga"] if totals["fga"] else None
    categories["ft_pct"] = totals["ftm"] / totals["fta"] if totals["fta"] else None
    detail = []
    for p, projection in projected:
        starts = starts_by_id[p.site_player_id]
        scheduled = schedule.games_for(p.nba_team, week.start, week.end, as_of=as_of)
        detail.append({"site_player_id": p.site_player_id, "name": p.name,
                       "nba_team": p.nba_team, "captured_slot": p.assigned_slot,
                       "eligible_positions": p.eligible_positions,
                       "confirmed_scheduled_games": len(scheduled),
                       "modeled_start_opportunities": len(starts),
                       "expected_games": sum(item["expected_appearance"] for item in starts),
                       "daily_starts": starts, "source_coverage": projection["coverage"],
                       "source_rows": projection["source_rows"],
                       "disagreement": projection["disagreement"]})
    return {"categories": categories,
            "shooting_volumes": {key: totals[key] for key in ("fgm", "fga", "ftm", "fta")}}, detail, days


def rank_league(inputs: dict, hashtag_weight: float = .5, week_number: int | None = None,
                as_of: datetime | None = None) -> dict:
    if not isfinite(hashtag_weight) or not 0 <= hashtag_weight <= 1:
        raise ValueError("Hashtag weight must be between 0 and 1")
    captured = datetime.fromisoformat(inputs["availability"]["captured_at_utc"].replace("Z", "+00:00"))
    as_of = as_of or captured
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone aware")
    as_of = as_of.astimezone(timezone.utc)
    if week_number is None:
        week_number = "regular"
    weeks = inputs["weeks"]
    if week_number == "regular":
        week = FantasyWeek(0, weeks[0].start, weeks[17].end, "regular_season_total")
        selection = {**week.as_dict(), "week": "regular", "label": "Regular season total (Weeks 1–18)",
                     "included_weeks": list(range(1, 19)), "max_adds": None,
                     "playoff_participation": "not_applicable"}
    elif week_number == "all":
        week = FantasyWeek(0, weeks[0].start, weeks[-1].end, "all_fantasy_weeks")
        selection = {**week.as_dict(), "week": "all", "label": "All fantasy weeks (1–21)",
                     "included_weeks": list(range(1, 22)), "max_adds": None,
                     "playoff_participation": "hypothetical_for_all_ten_teams"}
    elif isinstance(week_number, int) and 1 <= week_number <= len(weeks):
        week = weeks[week_number - 1]
        selection = {**week.as_dict(), "label": f"Yahoo Week {week_number}",
                     "included_weeks": [week_number],
                     "playoff_participation": "hypothetical_for_all_ten_teams" if week_number >= 19 else "not_applicable"}
    else:
        raise ValueError("Choose regular, all, or a Yahoo week from 1 to 21")
    snapshot_date = date.fromisoformat(inputs["availability"]["lineup_date"])
    local_as_of_day = as_of.astimezone(EASTERN).date()
    if week.start <= local_as_of_day <= week.end and local_as_of_day != snapshot_date and any(
            game.date == local_as_of_day and game.tipoff_utc <= as_of for game in inputs["schedule"].games):
        raise ValueError("No same-day Yahoo lineup snapshot exists to validate locked slots; refresh Yahoo or choose an earlier as-of time")
    if local_as_of_day > week.end:
        raise ValueError("Selected period has ended before the requested as-of time; historical stats are unavailable")
    teams = []
    for team_id, name in sorted(inputs["teams"].items(), key=lambda pair: int(pair[0])):
        roster = sorted(inputs["rosters"][team_id], key=lambda player: player.site_player_id)
        projected = [(p, resolve_projection(inputs["projections"][p.site_player_id], hashtag_weight)) for p in roster]
        totals, detail, days = _dated_team_totals(roster, projected, inputs["schedule"], week, as_of, snapshot_date, injury_inputs=inputs)
        teams.append({"team_id": team_id, "team_name": name,
                      "actual_record": inputs["league"]["standings"].get("record_for_each_team"),
                      "actual_seed": None, "totals": totals, "strengths": {}, "lineup": detail,
                      "days": days,
                      "quality": {"roster_players": len(roster),
                                  "scheduled_start_opportunities": sum(d["modeled_start_opportunities"] for d in detail),
                                  "expected_games": sum(d["expected_games"] for d in detail),
                                  "captured_bench": sum(d["captured_slot"] == "BN" for d in detail),
                                  "captured_il": sum(d["captured_slot"] == "IL" for d in detail),
                                  "one_source": sum(d["source_coverage"] == "one_source" for d in detail),
                                  "missing_projection": 0,
                                  "source_warnings": sum(issue["severity"] == "warning" for d in detail for row in d["source_rows"].values() for issue in row["issues"]),
                                  "source_disagreement_mean": (sum(v for d in detail for v in d["disagreement"].values() if v is not None) /
                                                               max(1, sum(v is not None for d in detail for v in d["disagreement"].values())))}})
    if len(teams) != 10:
        raise ValueError("Exactly ten verified Yahoo teams are required")
    _strengths(teams)
    provisional = cup_window_provisional(week, inputs["schedule"])
    return {"model_version": MODEL_VERSION, "scoring": "Yahoo Head-to-Head Categories, 9-cat",
            "metric_label": "Descriptive power score: mean league category percentile; not a win probability",
            "projection_model": "Hashtag-only" if hashtag_weight == 1 else "LeagueWinner-only" if hashtag_weight == 0 else "Blended",
            "hashtag_weight": hashtag_weight, "leaguewinner_weight": 1 - hashtag_weight,
            "week": selection, "weeks": [w.as_dict() for w in inputs["weeks"]],
            "schedule": {**inputs["schedule"].metadata(),
                         "selected_week_provisional_cup": provisional,
                         "selected_week_warning": "Undated NBA Cup/replacement games are omitted; projected totals are understated."
                                                  if provisional else None,
                         "expected_games_formula": "One confirmed scheduled game × projected GP/82 when assigned an eligible daily active slot",
                         "lineup_policy": "Maximum-weight legal lineup independently each scheduled day; BN may start on another day; IL excluded",
                         "lineup_proxy": "max(0.000001, (PTS/20 + REB/8 + AST/6 + 3PTM/3 + STL/1.5 + BLK/1.5 - TO/4 + (FGM-.47×FGA)/5 + (FTM-.78×FTA)/3) × GP/82)",
                         "lock_policy": "Only games with scheduled tipoff after as_of_utc are forecast; started active players occupy their captured slots on the captured lineup date. Historical stats and later lineup edits are unavailable.",
                         "postponement_status": "Unverified in supplied CSV; refresh authoritative schedule when changed"},
            "provenance": {"yahoo_roster_as_of_utc": inputs["availability"]["captured_at_utc"],
                           "lineup_date": inputs["availability"]["lineup_date"],
                           "yahoo_settings_as_of_utc": inputs["league"]["captured_at_utc"],
                           "forecast_as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                           "nba_schedule_sha256": inputs["schedule"].file_sha256,
                           "yahoo_calendar_sha256": inputs["calendar_sha256"],
                           "nba_cup_rules_sha256": inputs["cup_rules_sha256"],
                           "workbook_sha256": inputs["workbook_sha256"],
                           "reviewed_matches_sha256": inputs["decision_file_sha256"]},
            "data_quality": inputs["matching"], "teams": teams}
