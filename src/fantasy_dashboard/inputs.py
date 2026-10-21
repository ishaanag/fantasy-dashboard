"""Source input locations and `load_inputs`, which revalidates every input and saved capture on launch."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from hashlib import sha256
import json
import os
from pathlib import Path

from fantasy_dashboard.league import RosterPlayer
from fantasy_dashboard.sources.matching import select_unique
from fantasy_dashboard.sources.projections import Projection, import_workbook
from fantasy_dashboard.sources.reviewed_matches import apply_reviewed_matches
from fantasy_dashboard.sources.schedule import load_fantasy_weeks, load_schedule
from fantasy_dashboard.sources.settings import read_local_settings
from fantasy_dashboard.yahoo.player_capture import load_player_capture, match_browser_players, parse_player_detail
from fantasy_dashboard.yahoo.roster_capture import load_browser_capture
from fantasy_dashboard.yahoo.transactions import SNAPSHOT_NAME, load_injury_statuses

ROOT = Path(__file__).resolve().parents[2]

# Read-only source inputs, relative to the repository root.
INPUTS = Path("data/inputs")
WORKBOOK = INPUTS / "player-projections-2026-27.xlsx"
LEAGUE_SETTINGS = INPUTS / "league-settings.md"
MATCHUPS = INPUTS / "H2H-Matchups.csv"
NBA_SCHEDULE = INPUTS / "nba_2026_27_team_schedule.csv"
FANTASY_CALENDAR = INPUTS / "YAHOO_FANTASY_CALENDAR_AND_LINEUP_LOCKS_2026-27.md"
CUP_RULES = INPUTS / "NBA_CUP_Rules.md"
REVIEWED_MATCHES = INPUTS / "player_site_matches.csv"

# Private, Git-ignored captures and the signed-in browser profile.
REPORTS = Path("data/reports")
BROWSER_PROFILE = Path("data/cache/yahoo-browser-profile")


def reports_dir(root: Path = ROOT) -> Path:
    """FANTASY_REPORTS_DIR lets the regression suite pin an archived capture; the app uses the live folder."""
    return Path(os.environ.get("FANTASY_REPORTS_DIR") or root / REPORTS)


def load_inputs(root: Path = ROOT, reports: Path | None = None) -> dict:
    """Revalidate saved captures and reviewed source links on each launch."""
    reports = Path(reports or reports_dir(root))
    batch = import_workbook(root / WORKBOOK)
    _, duplicate_rows = select_unique(batch.records)
    if duplicate_rows:
        raise ValueError(f"Duplicate projection source rows require review: {len(duplicate_rows)} groups")
    local = read_local_settings(root / LEAGUE_SETTINGS)
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
    matches = apply_reviewed_matches(match_browser_players(batch.records, site_players), batch.records, site_players,
                                     root / REVIEWED_MATCHES)
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
            nba_team, positions = parse_player_detail(detail)
            rosters[team_id].append(RosterPlayer(team_id, site_id, name, slot, positions, nba_team))
    if set(rosters) != set(player_teams):
        raise ValueError("Roster team IDs do not match league teams")
    for team_id, roster in rosters.items():
        slots = Counter(p.assigned_slot for p in roster)
        if len(roster) > 17 or slots["BN"] > 4 or slots["IL"] > 3:
            raise ValueError(f"Roster capacity exceeded for team {team_id}")
        for p in roster:
            if p.site_player_id not in projections:
                raise ValueError(f"Unmatched rostered Yahoo player {p.site_player_id}")
    schedule = load_schedule(root / NBA_SCHEDULE)
    weeks = load_fantasy_weeks(root / FANTASY_CALENDAR)
    if any(p.nba_team not in schedule.team_counts for roster in rosters.values() for p in roster):
        raise ValueError("Rostered Yahoo NBA team is missing from the dated schedule")
    availability["player_fingerprint"] = player_manifest["players"]["fnv32"]
    availability["roster_fingerprint"] = player_manifest["rosters"]["fnv32"]
    inputs = {"league": league, "availability": availability, "site_players": site_players,
              "pool_manual_review": matches["manual_review"], "teams": player_teams,
              "matchup_team_names": league_teams,
              "schedule": schedule, "weeks": weeks,
              "calendar_sha256": sha256((root / FANTASY_CALENDAR).read_bytes()).hexdigest(),
              "cup_rules_sha256": sha256((root / CUP_RULES).read_bytes()).hexdigest(),
              "rosters": rosters, "projections": projections, "workbook_sha256": batch.file_sha256,
              "decision_file_sha256": matches["decision_file_sha256"],
              "matching": {"linked_source_rows": len(matches["candidate_links"]),
                           "reviewed_count": matches["reviewed_count"],
                           "unmatched_rostered": len(matches["unmatched_rostered_browser_players"]),
                           "duplicate_mappings": len(matches["duplicate_mappings"]),
                           "unmatched_projection_rows": len(matches["unmatched_projections"])}}
    # A synchronized Yahoo status capture is observation of the next game, not a
    # return-date forecast. Keep the projection GP unchanged for later games.
    statuses, captured = load_injury_statuses(reports / SNAPSHOT_NAME, inputs)
    if captured:
        inputs["injury_statuses"] = statuses
        inputs["injury_status_captured_at_utc"] = captured
    from fantasy_dashboard.yahoo.market_capture import load_adp, load_ecr  # market signals for trade value; optional
    inputs["adp"] = load_adp(reports)
    inputs["ecr"] = load_ecr(reports)
    return inputs
