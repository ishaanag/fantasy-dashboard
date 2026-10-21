"""Validate a read-only capture of Yahoo's signed-in league pages."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from difflib import get_close_matches
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from fantasy_dashboard.sources.matching import normalize_name, normalize_team, select_unique
from fantasy_dashboard.sources.projections import Projection

SLOTS = {"PG", "SG", "G", "SF", "PF", "F", "C", "Util", "BN", "IL"}
POSITIONS = {"PG", "SG", "SF", "PF", "C"}


@dataclass(frozen=True)
class BrowserRosterPlayer:
    fantasy_team_id: str
    assigned_slot: str
    site_player_id: str
    name: str
    nba_team: str
    eligible_positions: tuple[str, ...]


def checked_url(value: str, league_id: str, suffix: str) -> None:
    url = urlparse(value)
    if url.scheme != "https" or url.hostname != "basketball.fantasysports.yahoo.com":
        raise ValueError("Capture source must be the Yahoo Fantasy Basketball site")
    if url.path != f"/nba/{league_id}/{suffix}":
        raise ValueError(f"Capture source path does not match league {league_id}")


def fnv32(value: str) -> str:
    result = 2166136261
    for char in value:
        result = ((result ^ ord(char)) * 16777619) & 0xFFFFFFFF
    return f"{result:x}"


def _clean(value: str) -> str:
    return " ".join(value.split())


def capture_file(directory: Path, name: str) -> Path:
    if Path(name).name != name:
        raise ValueError("Capture file must be in the capture directory")
    return directory / name


def load_browser_capture(path: Path, local_settings: dict[str, str]) -> tuple[dict, list[BrowserRosterPlayer]]:
    """Read captured visible rows and return a report plus typed roster rows."""
    capture = json.loads(path.read_text(encoding="utf-8"))
    league_id = str(capture["league_id"])
    if league_id != local_settings["League ID#"]:
        raise ValueError("Browser capture league ID does not match local settings")
    roster_info = capture["rosters"]
    checked_url(roster_info["source_url"], league_id, "startingrosters")
    teams = {str(team["team_id"]): team["name"] for team in capture["teams"]}
    if len(teams) != len(capture["teams"]) or len(teams) != 10:
        raise ValueError("Capture must contain ten unique fantasy teams")
    if str(capture["my_team_id"]) not in teams:
        raise ValueError("My team is missing from captured teams")
    roster_text = capture_file(path.parent, roster_info["file"]).read_text(encoding="utf-8").rstrip("\n")
    if fnv32(roster_text) != roster_info["fnv32"]:
        raise ValueError("Roster capture fingerprint differs from browser observation")
    players = []
    for index, row in enumerate(csv.reader(roster_text.splitlines(), delimiter="\t"), start=1):
        if len(row) != 5:
            raise ValueError(f"Roster row {index} has {len(row)} fields, expected 5")
        team_id, slot, site_id, name, detail = row
        if team_id not in teams or slot not in SLOTS or not site_id.isdigit() or not name:
            raise ValueError(f"Roster row {index} has invalid identity or slot")
        match = re.fullmatch(r"([A-Z]{2,3}) - (PG|SG|SF|PF|C)(?:,(?:PG|SG|SF|PF|C))*", detail)
        if not match:
            raise ValueError(f"Roster row {index} has invalid NBA team/eligibility")
        nba_team, positions = detail.split(" - ", 1)
        eligibility = tuple(positions.split(","))
        if any(position not in POSITIONS for position in eligibility):
            raise ValueError(f"Roster row {index} has invalid eligibility")
        players.append(BrowserRosterPlayer(team_id, slot, site_id, name, nba_team, eligibility))
    site_ids = [player.site_player_id for player in players]
    if len(set(site_ids)) != len(site_ids):
        raise ValueError("Roster capture has duplicate site player IDs")
    counts = {team_id: sum(p.fantasy_team_id == team_id for p in players) for team_id in teams}
    if any(count == 0 or count > 17 for count in counts.values()):
        raise ValueError("A captured team has an implausible roster size")

    settings_capture = json.loads(capture_file(path.parent, capture["settings"]["file"]).read_text(encoding="utf-8"))
    checked_url(settings_capture["source_url"], league_id, "settings")
    live_settings: dict[str, str] = {}
    for key, value in settings_capture["rows"]:
        label = _clean(key).rstrip(":")
        if label in live_settings:
            raise ValueError(f"Duplicate browser setting: {label}")
        live_settings[label] = _clean(value)
    setting_comparison = [
        {"field": key, "local": value, "browser": live_settings.get(key), "status":
         "unavailable" if key not in live_settings else
         "match" if _clean(value) == live_settings[key] else "difference"}
        for key, value in local_settings.items()
    ]
    extra_settings = sorted(set(live_settings) - set(local_settings))
    report = {
        "schema_version": 1,
        "status": "verified_browser_snapshot",
        "captured_at_utc": capture["captured_at_utc"],
        "league_id": league_id,
        "league_name": capture["league_name"],
        "my_team_id": str(capture["my_team_id"]),
        "lineup_date": roster_info["lineup_date"],
        "roster_source_url": roster_info["source_url"],
        "settings_source_url": settings_capture["source_url"],
        "standings": capture["standings"],
        "team_count": len(teams),
        "rostered_player_count": len(players),
        "unique_site_player_ids": len(set(site_ids)),
        "roster_counts": [{"team_id": team_id, "team_name": teams[team_id], "players": count}
                          for team_id, count in sorted(counts.items(), key=lambda pair: int(pair[0]))],
        "settings_comparison": setting_comparison,
        "settings_summary": {
            "matched": sum(item["status"] == "match" for item in setting_comparison),
            "different": sum(item["status"] == "difference" for item in setting_comparison),
            "unavailable": sum(item["status"] == "unavailable" for item in setting_comparison),
            "extra_browser_fields": extra_settings,
        },
        "identity_scope": "Yahoo site player IDs from visible player links",
        "availability_scope": "Not verified by starting-rosters page",
    }
    return report, players


def match_browser_rosters(records: list[Projection], players: list[BrowserRosterPlayer]) -> dict:
    """Provisional name/team matching to browser site player IDs."""
    unique, _ = select_unique(records)
    by_name: dict[str, list[BrowserRosterPlayer]] = {}
    for player in players:
        by_name.setdefault(normalize_name(player.name), []).append(player)
    candidates, manual_review, unmatched = [], [], []
    for record in unique:
        normalized = normalize_name(record.name or "")
        found = by_name.get(normalized, [])
        entry = {"source": record.source, "row": record.row, "name": record.name}
        if len(found) > 1:
            manual_review.append({**entry, "reason": "name_collision",
                                  "candidate_site_player_ids": [p.site_player_id for p in found]})
        elif not found:
            suggestions = get_close_matches(normalized, by_name.keys(), n=3, cutoff=0.65)
            unmatched.append({**entry, "suggestions": suggestions})
            if suggestions:
                manual_review.append({**entry, "reason": "possible_alias", "suggestions": suggestions})
        else:
            player = found[0]
            source_positions = set(re.findall(r"PG|SG|SF|PF|C", (record.position or "").upper()))
            if normalize_team(record.team) and normalize_team(record.team) != normalize_team(player.nba_team):
                manual_review.append({**entry, "reason": "team_conflict",
                                      "site_player_id": player.site_player_id,
                                      "source_team": record.team, "browser_team": player.nba_team})
            elif source_positions and not source_positions & set(player.eligible_positions):
                manual_review.append({**entry, "reason": "position_conflict",
                                      "site_player_id": player.site_player_id,
                                      "source_position": record.position,
                                      "browser_positions": player.eligible_positions})
            else:
                candidates.append({**entry, "site_player_id": player.site_player_id,
                                   "fantasy_team_id": player.fantasy_team_id,
                                   "method": "unique_normalized_name"})
    candidate_ids = {item["site_player_id"] for item in candidates}
    return {
        "status": "provisional_browser_site_ids",
        "candidate_links": candidates,
        "manual_review": manual_review,
        "unmatched_projections": unmatched,
        "unmatched_rostered_browser_players": [
            {"site_player_id": p.site_player_id, "name": p.name, "fantasy_team_id": p.fantasy_team_id}
            for p in players if p.site_player_id not in candidate_ids
        ],
    }
