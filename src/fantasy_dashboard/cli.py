"""Reproducible projection import and Yahoo browser-capture audit; writes QA reports under data/reports/."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path

from fantasy_dashboard.inputs import LEAGUE_SETTINGS, REPORTS, REVIEWED_MATCHES, ROOT, WORKBOOK
from fantasy_dashboard.sources.matching import match_sources, select_unique
from fantasy_dashboard.sources.projections import import_workbook
from fantasy_dashboard.sources.reviewed_matches import apply_reviewed_matches
from fantasy_dashboard.sources.settings import read_local_settings
from fantasy_dashboard.yahoo.player_capture import load_player_capture, match_browser_players
from fantasy_dashboard.yahoo.roster_capture import load_browser_capture, match_browser_rosters


def _write(path: Path, data: object) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)


def run(root: Path, output: Path, browser_capture: Path | None = None,
        browser_players_capture: Path | None = None,
        matching_decisions: Path | None = None) -> dict:
    batch = import_workbook(root / WORKBOOK)
    settings = read_local_settings(root / LEAGUE_SETTINGS)
    source_matches = match_sources(batch.records)
    selected, duplicates = select_unique(batch.records)
    issue_counts = Counter((issue.sheet, issue.code, issue.severity) for issue in batch.issues)
    quality = {
        "schema_version": 1,
        "workbook_sha256": batch.file_sha256,
        "sheet_row_counts": batch.row_counts,
        "raw_record_count": len(batch.records),
        "selected_unique_count": len(selected),
        "duplicate_groups": duplicates,
        "issue_counts": [{"sheet": sheet, "code": code, "severity": severity, "count": count} for (sheet, code, severity), count in sorted(issue_counts.items())],
        "issues": [asdict(issue) for issue in batch.issues],
        "local_settings": settings,
        "source_overlap": {key: source_matches[key] for key in ("shared_unique_names",)},
    }
    reports = {
        "projections.json": {"schema_version": 1, "workbook_sha256": batch.file_sha256,
                             "records": [record.as_dict() for record in batch.records]},
        "data-quality.json": quality,
    }
    matching = {"schema_version": 1, "workbook_sha256": batch.file_sha256,
                "source_matching": source_matches}
    if browser_capture is not None:
        browser_report, browser_players = load_browser_capture(browser_capture, settings)
        reports["yahoo-browser-feasibility.json"] = browser_report
        matching["browser_matching"] = match_browser_rosters(batch.records, browser_players)
    if browser_players_capture is not None:
        availability, site_players = load_player_capture(browser_players_capture)
        if availability["league_id"] != settings["League ID#"]:
            raise ValueError("Player capture league ID does not match local settings")
        reports["yahoo-browser-availability.json"] = availability
        matching["browser_player_matching"] = (
            match_browser_players(batch.records, site_players) if availability["status"] == "verified" else
            {"status": "blocked_reconciliation_conflict", "candidate_links": [],
             "manual_review": availability["roster_reconciliation"]["conflicts"]}
        )
        decision_file = matching_decisions or root / REVIEWED_MATCHES
        if matching_decisions is not None and not decision_file.is_file():
            raise FileNotFoundError(decision_file)
        if decision_file.is_file() and availability["status"] == "verified":
            matching["browser_player_matching"] = apply_reviewed_matches(
                matching["browser_player_matching"], batch.records, site_players, decision_file)
    elif matching_decisions is not None:
        raise ValueError("Reviewed matches require --browser-players-capture")
    reports["player-matching.json"] = matching
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    for filename, contents in reports.items():
        _write(output / filename, contents)
    return {"rows": batch.row_counts, "issues": len(batch.issues), "duplicate_groups": len(duplicates), "shared_names": source_matches["shared_unique_names"], "browser_snapshot": browser_report["status"] if browser_capture is not None else "not_requested", "browser_players": availability["status"] if browser_players_capture is not None else "not_requested", "reviewed_matches": matching.get("browser_player_matching", {}).get("reviewed_count", 0)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Projection import and Yahoo capture audit")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--browser-capture", type=Path, help="Validate a saved read-only Yahoo browser capture")
    parser.add_argument("--browser-players-capture", type=Path, help="Validate a complete All Players capture against its fresh roster")
    parser.add_argument("--matching-decisions", type=Path, help="Apply an approved source-row to site-ID decision CSV (default: data/inputs/player_site_matches.csv when present)")
    args = parser.parse_args()
    root = args.root.resolve()
    output = (args.output or root / REPORTS).resolve()
    print(json.dumps(run(root, output, args.browser_capture, args.browser_players_capture, args.matching_decisions), sort_keys=True))


if __name__ == "__main__":
    main()
