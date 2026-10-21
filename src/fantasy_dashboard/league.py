"""League rules and shared vocabulary: lineup slots, the nine categories, Yahoo statuses and time handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SLOTS = ("PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util")
BASE_POSITIONS = ("PG", "SG", "SF", "PF", "C")
CATEGORIES = ("fg_pct", "ft_pct", "three_ptm", "pts", "reb", "ast", "stl", "blk", "turnovers")
VOLUMES = ("fgm", "fga", "ftm", "fta", "three_ptm", "pts", "reb", "ast", "stl", "blk", "turnovers")
LABELS = {"fg_pct": "FG%", "ft_pct": "FT%", "three_ptm": "3PTM", "pts": "PTS", "reb": "REB",
          "ast": "AST", "stl": "STL", "blk": "BLK", "turnovers": "TO"}
INACTIVE_STATUSES = frozenset({"O", "NA", "OFS", "INJ"})   # removed from imminent games only
DOUBTFUL_STATUSES = frozenset({"Q", "D", "GTD"})           # keep their GP/82 appearance estimate
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


def parse_utc(value: str) -> datetime:
    """ISO timestamp (a trailing Z is accepted) converted to UTC; naive times are refused."""
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Timestamp needs an offset")
    return stamp.astimezone(timezone.utc)


def iso_utc(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
