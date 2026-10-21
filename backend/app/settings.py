"""Read the supplied league snapshot and compare fields actually exposed by Yahoo."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any


def read_local_settings(path: Path) -> dict[str, str]:
    settings = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        key = key.strip().rstrip(":")
        if key and key != "Setting":
            settings[key] = value.strip()
    return settings


def reconcile(local: dict[str, str], metadata: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    """Only compare fields whose Yahoo meaning is unambiguous."""
    playoff_text = local.get("Playoffs", "")
    playoff_count = re.search(r"\b(\d+) teams\b", playoff_text)
    playoff_start = re.search(r"Week (\d+)", playoff_text)
    trade_end = local.get("Trade End Date", "")
    try:
        trade_end = datetime.strptime(trade_end, "%B %d, %Y").date().isoformat()
    except ValueError:
        pass
    checks = [
        ("League ID#", metadata.get("league_id"), local.get("League ID#")),
        ("League Name", metadata.get("name"), local.get("League Name")),
        ("Max Teams", live.get("max_teams") or metadata.get("num_teams"), local.get("Max Teams")),
        ("Max Acquisitions per Week", live.get("max_weekly_adds"), local.get("Max Acquisitions per Week")),
        ("Waiver Time", live.get("waiver_time"), local.get("Waiver Time", "").split(" ")[0]),
        ("Trade Reject Time", live.get("trade_reject_time"), local.get("Trade Reject Time", "").split(" ")[0]),
        ("Trade End Date", live.get("trade_end_date"), trade_end),
        ("Playoff Teams", live.get("num_playoff_teams"), playoff_count.group(1) if playoff_count else None),
        ("Playoff Start Week", live.get("playoff_start_week"), playoff_start.group(1) if playoff_start else None),
        ("Playoff Reseeding", live.get("uses_playoff_reseeding"), "0" if local.get("Playoff Reseeding") == "No" else "1"),
        ("Lock Eliminated Teams", live.get("uses_lock_eliminated_teams"), "1" if local.get("Lock Eliminated Teams") == "Yes" else "0"),
    ]
    results = []
    for field, yahoo, snapshot in checks:
        if yahoo is None:
            status = "unavailable"
        else:
            status = "match" if str(yahoo).strip().lower() == str(snapshot).strip().lower() else "difference"
        results.append({"field": field, "local": snapshot, "yahoo": yahoo, "status": status})
    positions = live.get("roster_positions")
    if positions:
        parsed = []
        for item in positions:
            item = item.get("roster_position", item)
            parsed.extend([item.get("position")] * int(item.get("count", 1)))
        local_positions = [s.strip() for s in local.get("Roster Positions", "").split(",")]
        results.append({"field": "Roster Positions", "local": local_positions, "yahoo": parsed, "status": "match" if sorted(parsed) == sorted(local_positions) else "difference"})
    categories = live.get("stat_categories", {}).get("stats", [])
    if categories:
        names = [item.get("stat", item).get("display_name") for item in categories if item.get("stat", item).get("enabled", "1") in ("1", 1, True)]
        results.append({"field": "Players Stat Categories", "local": local.get("Players Stat Categories"), "yahoo": names, "status": "manual_review", "reason": "Yahoo stat IDs and display names require NBA-specific category verification"})
    results.append({"field": "Scoring Type", "local": local.get("Scoring Type"), "yahoo": live.get("scoring_type") or metadata.get("scoring_type"), "status": "manual_review", "reason": "Yahoo scoring code alone does not prove Categories versus One Win"})
    return results
