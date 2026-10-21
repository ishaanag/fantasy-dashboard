"""Deterministic projection matching; site IDs require browser verification."""

from __future__ import annotations

from collections.abc import Iterable
from difflib import get_close_matches
import re
import unicodedata

from fantasy_dashboard.sources.projections import Projection

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
TEAM_ALIASES = {"SA": "SAS", "GS": "GSW", "NY": "NYK", "NO": "NOP", "UTAH": "UTA", "PHO": "PHX", "BRK": "BKN"}


def normalize_name(value: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    parts = re.findall(r"[a-z0-9]+", ascii_name)
    while parts and parts[-1] in SUFFIXES:
        parts.pop()
    return " ".join(parts)


def normalize_team(value: str | None) -> str | None:
    if not value:
        return None
    code = value.strip().upper()
    return TEAM_ALIASES.get(code, code)


def _positions(value: str | None) -> set[str]:
    return set(re.findall(r"PG|SG|SF|PF|C", value.upper())) if value else set()


def select_unique(records: Iterable[Projection]) -> tuple[list[Projection], list[dict]]:
    """Keep the best auditable row: fewest errors, then lowest rank, then row."""
    groups: dict[tuple[str, str], list[Projection]] = {}
    selected: list[Projection] = []
    for record in records:
        if not record.name:
            continue
        groups.setdefault((record.source, normalize_name(record.name)), []).append(record)
    duplicates = []
    for (source, name), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda r: (sum(i.severity == "error" for i in r.issues), r.rank if r.rank is not None else 10**9, r.row))
        selected.append(ordered[0])
        if len(group) > 1:
            duplicates.append({"source": source, "normalized_name": name, "rows": [r.row for r in group], "selected_row": ordered[0].row, "policy": "fewest_errors_then_lowest_rank_then_row", "conflicting_values": any(r.raw != ordered[0].raw for r in ordered[1:])})
    return sorted(selected, key=lambda r: (r.source, r.rank or 10**9, r.row)), duplicates


def match_sources(records: Iterable[Projection]) -> dict:
    """Cross-source names are provisional candidates, not verified site IDs."""
    unique, duplicates = select_unique(records)
    by_source = {source: {normalize_name(r.name): r for r in unique if r.source == source and r.name} for source in ("LeagueWinner", "Hashtag")}
    lw, hashtag = by_source["LeagueWinner"], by_source["Hashtag"]
    shared = sorted(lw.keys() & hashtag.keys())
    review = []
    for name in shared:
        left, right = lw[name], hashtag[name]
        if normalize_team(left.team) and normalize_team(right.team) and normalize_team(left.team) != normalize_team(right.team):
            review.append({"normalized_name": name, "reason": "team_conflict", "LeagueWinner": {"row": left.row, "name": left.name, "team": left.team}, "Hashtag": {"row": right.row, "name": right.name, "team": right.team}})
        if _positions(left.position) and _positions(right.position) and not _positions(left.position) & _positions(right.position):
            review.append({"normalized_name": name, "reason": "position_conflict", "LeagueWinner": {"row": left.row, "position": left.position}, "Hashtag": {"row": right.row, "position": right.position}})
    for dup in duplicates:
        review.append({"normalized_name": dup["normalized_name"], "reason": "duplicate_source_rows", "source": dup["source"], "rows": dup["rows"]})
    lw_only = [{"row": lw[n].row, "name": lw[n].name, "rank": lw[n].rank, "suggestions": get_close_matches(n, hashtag.keys(), n=3, cutoff=0.65)} for n in sorted(lw.keys() - hashtag.keys())]
    hashtag_only = [{"row": hashtag[n].row, "name": hashtag[n].name, "rank": hashtag[n].rank, "suggestions": get_close_matches(n, lw.keys(), n=3, cutoff=0.65)} for n in sorted(hashtag.keys() - lw.keys())]
    for source, rows in (("LeagueWinner", lw_only), ("Hashtag", hashtag_only)):
        review.extend({"source": source, "row": item["row"], "name": item["name"], "reason": "possible_alias", "suggestions": item["suggestions"]} for item in rows if item["suggestions"])
    return {
        "status": "provisional_source_names",
        "shared_unique_names": len(shared),
        "leaguewinner_only": lw_only,
        "hashtag_only": hashtag_only,
        "duplicates": duplicates,
        "review_queue": review,
        "shared_pairs": [{"normalized_name": n, "leaguewinner_row": lw[n].row, "hashtag_row": hashtag[n].row} for n in shared],
    }
