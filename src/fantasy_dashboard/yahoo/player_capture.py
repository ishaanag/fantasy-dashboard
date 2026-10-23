"""Validate a complete Yahoo site player-list capture and provisional row links."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from difflib import get_close_matches
import json
from pathlib import Path
import re

from fantasy_dashboard.sources.matching import normalize_name, normalize_team, select_unique
from fantasy_dashboard.sources.projections import Projection
from fantasy_dashboard.yahoo.roster_capture import (
    POSITIONS,
    SLOTS,
    BrowserRosterPlayer,
    capture_file,
    checked_url,
    fnv32,
)


# Yahoo shows a waiver player as "W" or, once the clear date is set, "W (Oct 24)".
WAIVER_LABEL = re.compile(r"W(?: \([A-Z][a-z]{2} \d{1,2}\))?")


def is_waiver_label(label: str) -> bool:
    return WAIVER_LABEL.fullmatch(label) is not None

@dataclass(frozen=True)
class BrowserPlayer:
    page: int
    row: int
    site_player_id: str
    name: str
    nba_team: str
    eligible_positions: tuple[str, ...]
    availability_label: str
    fantasy_team_id: str | None


def parse_player_detail(value: str) -> tuple[str, tuple[str, ...]]:
    match = re.fullmatch(r"([A-Z]{2,3}) - (PG|SG|SF|PF|C)(?:,(?:PG|SG|SF|PF|C))*", value)
    if not match:
        raise ValueError(f"Invalid player team/eligibility: {value!r}")
    team, positions = value.split(" - ", 1)
    result = tuple(positions.split(","))
    if any(position not in POSITIONS for position in result):
        raise ValueError("Invalid player eligibility")
    return team, result


def load_player_capture(path: Path) -> tuple[dict, list[BrowserPlayer]]:
    """Require all pages, unique IDs, and ownership agreement with a fresh roster."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    league_id = str(manifest["league_id"])
    checked_url(manifest["players_source_url"], league_id, "players")
    checked_url(manifest["roster_source_url"], league_id, "startingrosters")
    if manifest["player_filter"] != "ALL":
        raise ValueError("Player capture must use Yahoo's All Players filter")
    teams = {str(t["team_id"]): t["name"] for t in manifest["teams"]}
    if len(teams) != len(manifest["teams"]) or len(teams) != 10 or len(set(teams.values())) != 10:
        raise ValueError("Player capture requires ten unique teams and names")
    roster_text = capture_file(path.parent, manifest["rosters"]["file"]).read_text(encoding="utf-8").rstrip("\n")
    if fnv32(roster_text) != manifest["rosters"]["fnv32"]:
        raise ValueError("Fresh roster fingerprint differs from capture")
    roster_rows = list(csv.reader(roster_text.splitlines(), delimiter="\t"))
    if any(len(row) != 5 for row in roster_rows):
        raise ValueError("Fresh roster row shape differs")
    fresh = {row[2]: row for row in roster_rows}
    if len(fresh) != len(roster_rows):
        raise ValueError("Fresh roster has duplicate site player IDs")
    rosters = []
    for row in roster_rows:
        team_id, slot, site_id, name, detail = row
        if team_id not in teams or slot not in SLOTS or not site_id.isdigit() or not name:
            raise ValueError("Fresh roster has invalid identity or slot")
        nba_team, eligibility = parse_player_detail(detail)
        rosters.append(BrowserRosterPlayer(team_id, slot, site_id, name, nba_team, eligibility))
    counts = Counter(p.fantasy_team_id for p in rosters)
    if any(counts[team_id] == 0 or counts[team_id] > 17 for team_id in teams):
        raise ValueError("Fresh roster has an implausible team size")

    raw_text = capture_file(path.parent, manifest["players"]["file"]).read_text(encoding="utf-8").rstrip("\n")
    if fnv32(raw_text) != manifest["players"]["fnv32"]:
        raise ValueError("Player-list fingerprint differs from capture")
    players: list[BrowserPlayer] = []
    page_sizes: Counter[int] = Counter()
    seen: set[str] = set()
    for line, row in enumerate(csv.reader(raw_text.splitlines(), delimiter="\t"), 1):
        if len(row) != 6:
            raise ValueError(f"Player row {line} has {len(row)} fields, expected 6")
        page, position, site_id, name, detail, label = row
        if not page.isdigit() or not position.isdigit() or not site_id.isdigit() or not name or not label:
            raise ValueError(f"Player row {line} has invalid identity or lineage")
        page_number, row_number = int(page), int(position)
        if page_number < 1 or row_number != page_sizes[page_number] + 1 or row_number > 25:
            raise ValueError(f"Player row {line} breaks page order")
        if site_id in seen:
            raise ValueError(f"Duplicate site player ID: {site_id}")
        seen.add(site_id)
        page_sizes[page_number] += 1
        nba_team, eligibility = parse_player_detail(detail)
        if is_waiver_label(label):
            label = "W"   # the clear date is kept separately as waiver evidence
        owners = [team_id for team_id, team_name in teams.items() if team_name == label]
        if label not in {"FA", "W"} and not owners:
            raise ValueError(f"Unknown availability label at player row {line}: {label}")
        players.append(BrowserPlayer(page_number, row_number, site_id, name, nba_team, eligibility,
                                     label, owners[0] if owners else None))
    pages = len(page_sizes)
    if pages < 1 or sorted(page_sizes) != list(range(1, pages + 1)) or any(page_sizes[i] != 25 for i in range(1, pages)):
        raise ValueError("Player-list pagination has a missing or short intermediate page")
    if manifest["players"]["page_count"] != pages or manifest["players"]["row_count"] != len(players) or not manifest["players"].get("last_page_no_next"):
        raise ValueError("Player-list pagination is incomplete")
    roster_by_id = {p.site_player_id: p for p in rosters}
    conflicts = []
    for player in players:
        roster = roster_by_id.get(player.site_player_id)
        if roster and (player.fantasy_team_id != roster.fantasy_team_id or player.name != roster.name or player.nba_team != roster.nba_team or player.eligible_positions != roster.eligible_positions):
            conflicts.append({"site_player_id": player.site_player_id, "reason": "roster_player_mismatch",
                              "list_label": player.availability_label, "roster_team_id": roster.fantasy_team_id})
        elif not roster and player.fantasy_team_id:
            conflicts.append({"site_player_id": player.site_player_id, "reason": "owned_but_missing_from_roster",
                              "list_label": player.availability_label, "list_team_id": player.fantasy_team_id})
    for site_id in sorted(roster_by_id.keys() - seen):
        conflicts.append({"site_player_id": site_id, "reason": "rostered_but_missing_from_list"})
    label_counts = dict(sorted(Counter(p.availability_label for p in players).items()))
    report = {
        "schema_version": 1, "status": "verified" if not conflicts else "reconciliation_conflict",
        "captured_at_utc": manifest["captured_at_utc"], "league_id": league_id,
        "lineup_date": manifest["lineup_date"], "player_filter": "ALL",
        "player_source_url": manifest["players_source_url"],
        "roster_source_url": manifest["roster_source_url"],
        "page_count": pages, "page_sizes": [page_sizes[i] for i in range(1, pages + 1)],
        "player_count": len(players), "unique_site_player_ids": len(seen),
        "rostered_count": len(rosters), "free_agent_count": label_counts.get("FA", 0),
        "waiver_count": label_counts.get("W", 0), "availability_labels": label_counts,
        "roster_reconciliation": {"conflict_count": len(conflicts), "conflicts": conflicts},
        "identity_scope": "Yahoo site player IDs from visible player links",
        "availability_scope": "Displayed labels at capture time; transaction eligibility and waiver timing unverified",
    }
    return report, players


def match_browser_players(records: list[Projection], players: list[BrowserPlayer]) -> dict:
    """Link only unique normalized names with nonconflicting team/eligibility."""
    unique, _ = select_unique(records)
    by_name: dict[str, list[BrowserPlayer]] = defaultdict(list)
    for player in players:
        by_name[normalize_name(player.name)].append(player)
    links, review, unmatched = [], [], []
    for record in unique:
        name = normalize_name(record.name or "")
        found = by_name.get(name, [])
        entry = {"source": record.source, "sheet": record.sheet, "row": record.row, "name": record.name}
        if len(found) > 1:
            review.append({**entry, "reason": "name_collision", "candidate_site_player_ids": sorted(p.site_player_id for p in found)})
            continue
        if not found:
            suggestions = get_close_matches(name, by_name, n=3, cutoff=0.65)
            unmatched.append({**entry, "suggestions": suggestions})
            if suggestions:
                review.append({**entry, "reason": "possible_alias", "suggestions": suggestions})
            continue
        player = found[0]
        source_team = normalize_team(record.team)
        source_positions = set(re.findall(r"PG|SG|SF|PF|C", (record.position or "").upper()))
        if source_team and source_team != normalize_team(player.nba_team):
            review.append({**entry, "reason": "team_conflict", "candidate_site_player_id": player.site_player_id,
                           "source_team": record.team, "site_team": player.nba_team})
        elif source_positions and not source_positions.intersection(player.eligible_positions):
            review.append({**entry, "reason": "position_conflict", "candidate_site_player_id": player.site_player_id,
                           "source_position": record.position, "site_positions": player.eligible_positions})
        elif not source_team and not source_positions:
            review.append({**entry, "reason": "insufficient_source_identity", "candidate_site_player_id": player.site_player_id})
        else:
            links.append({**entry, "site_player_id": player.site_player_id,
                          "availability_label": player.availability_label,
                          "fantasy_team_id": player.fantasy_team_id,
                          "site_page": player.page, "site_row": player.row,
                          "site_nba_team": player.nba_team,
                          "site_eligible_positions": player.eligible_positions,
                          "method": "unique_normalized_name_checked_team_position"})
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for link in links:
        grouped[(link["source"], link["site_player_id"])].append(link)
    duplicates = [{"source": key[0], "site_player_id": key[1], "rows": [item["row"] for item in group]}
                  for key, group in sorted(grouped.items()) if len(group) > 1]
    blocked = {(item["source"], item["site_player_id"]) for item in duplicates}
    if blocked:
        review.extend({"source": item["source"], "sheet": item["sheet"], "row": item["row"],
                       "name": item["name"], "reason": "duplicate_site_mapping",
                       "candidate_site_player_id": item["site_player_id"]}
                      for item in links if (item["source"], item["site_player_id"]) in blocked)
        links = [item for item in links if (item["source"], item["site_player_id"]) not in blocked]
    linked_ids = {item["site_player_id"] for item in links}
    return {"status": "provisional_browser_site_ids", "candidate_links": links,
            "manual_review": review, "unmatched_projections": unmatched,
            "unmatched_rostered_browser_players": [
                {"site_player_id": p.site_player_id, "name": p.name, "fantasy_team_id": p.fantasy_team_id}
                for p in players if p.fantasy_team_id and p.site_player_id not in linked_ids],
            "duplicate_mappings": duplicates}
