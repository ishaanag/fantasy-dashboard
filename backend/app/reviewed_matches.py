"""Apply explicit, audited source-row to Yahoo site-player decisions."""

from __future__ import annotations

import csv
from hashlib import sha256
from pathlib import Path

from .browser_players import BrowserPlayer
from .matching import select_unique
from .projections import Projection


REQUIRED_COLUMNS = {
    "source_sheet", "source_row", "source_player", "site_player_id",
    "decision_origin", "review_basis",
}


def apply_reviewed_matches(report: dict, records: list[Projection], players: list[BrowserPlayer],
                           decisions_path: Path) -> dict:
    """Resolve the current review queue only when every decision still matches its inputs."""
    with decisions_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
            raise ValueError("Reviewed match file is missing required columns")
        decisions = list(reader)

    selected, _ = select_unique(records)
    source_by_row = {(record.sheet, record.row): record for record in selected}
    site_by_id = {player.site_player_id: player for player in players}
    if len(site_by_id) != len(players):
        raise ValueError("Browser player list has duplicate site IDs")
    current_flags = {(item["sheet"], item["row"]): item for item in report["manual_review"]}
    if len(current_flags) != len(report["manual_review"]):
        raise ValueError("Browser matching has multiple flags for one source row")

    existing_keys = {(item["sheet"], item["row"]) for item in report["candidate_links"]}
    linked_targets = {(item["source"], item["site_player_id"]) for item in report["candidate_links"]}
    reviewed_links, resolved, seen_rows = [], [], set()
    for line, decision in enumerate(decisions, start=2):
        try:
            key = (decision["source_sheet"], int(decision["source_row"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid reviewed source row at CSV line {line}") from exc
        site_id = decision["site_player_id"]
        if key in seen_rows or key in existing_keys:
            raise ValueError(f"Duplicate or already linked reviewed source row: {key}")
        seen_rows.add(key)
        record = source_by_row.get(key)
        flag = current_flags.get(key)
        player = site_by_id.get(site_id)
        if record is None or flag is None or record.name != decision["source_player"]:
            raise ValueError(f"Reviewed source row no longer matches the current review queue: {key}")
        if player is None or not site_id.isdigit():
            raise ValueError(f"Reviewed site ID is absent from the captured player list: {site_id}")
        if not decision["decision_origin"] or not decision["review_basis"]:
            raise ValueError(f"Reviewed decision lacks provenance at CSV line {line}")
        candidate = flag.get("candidate_site_player_id")
        candidates = flag.get("candidate_site_player_ids")
        if candidate and candidate != site_id or candidates and site_id not in candidates:
            raise ValueError(f"Reviewed site ID disagrees with the flagged candidate: {key}")
        target = (record.source, site_id)
        if target in linked_targets:
            raise ValueError(f"Reviewed site ID would duplicate a source mapping: {target}")
        linked_targets.add(target)
        reviewed_links.append({
            "source": record.source, "sheet": record.sheet, "row": record.row,
            "name": record.name, "site_player_id": site_id,
            "availability_label": player.availability_label,
            "fantasy_team_id": player.fantasy_team_id,
            "site_page": player.page, "site_row": player.row,
            "site_nba_team": player.nba_team,
            "site_eligible_positions": player.eligible_positions,
            "method": "approved_manual_review",
            "decision_origin": decision["decision_origin"],
            "review_basis": decision["review_basis"],
        })
        resolved.append({
            "source": record.source, "sheet": record.sheet, "row": record.row,
            "name": record.name, "site_player_id": site_id,
            "original_flag": flag["reason"],
            "decision_origin": decision["decision_origin"],
            "review_basis": decision["review_basis"],
        })

    missing = sorted(set(current_flags) - seen_rows)
    if missing:
        raise ValueError(f"Reviewed match file does not cover current flags: {missing[:5]}")
    links = report["candidate_links"] + reviewed_links
    linked_site_ids = {item["site_player_id"] for item in links}
    result = {**report,
              "status": "reviewed_browser_site_ids",
              "candidate_links": links,
              "manual_review": [],
              "resolved_manual_review": resolved,
              "reviewed_count": len(resolved),
              "decision_file_sha256": sha256(decisions_path.read_bytes()).hexdigest(),
              "unmatched_projections": [item for item in report["unmatched_projections"]
                                        if (item["sheet"], item["row"]) not in seen_rows],
              "unmatched_rostered_browser_players": [
                  item for item in report["unmatched_rostered_browser_players"]
                  if item["site_player_id"] not in linked_site_ids],
              }
    return result
