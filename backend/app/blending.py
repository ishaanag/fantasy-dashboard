"""Per-game source blending and attempt-weighted shooting calculations."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable

from .projections import Projection


STATS = ("gp", "mpg", "fgm", "fga", "ftm", "fta", "three_ptm", "pts", "reb", "ast", "stl", "blk", "turnovers")


@dataclass(frozen=True)
class BlendedProjection:
    values: dict[str, float | None]
    fg_pct: float | None
    ft_pct: float | None
    sources: tuple[str, ...]
    coverage: str
    disagreement: dict[str, float | None]


def shooting_pct(makes: float, attempts: float) -> float | None:
    if not all(isfinite(x) for x in (makes, attempts)) or makes < 0 or attempts < makes:
        raise ValueError("Invalid shooting volume")
    return makes / attempts if attempts else None


def attempt_weighted_pct(volumes: Iterable[tuple[float, float]]) -> float | None:
    totals = list(volumes)
    for made, attempted in totals:
        shooting_pct(made, attempted)
    return shooting_pct(sum(m for m, _ in totals), sum(a for _, a in totals))


def blend(hashtag: Projection | None, leaguewinner: Projection | None, hashtag_weight: float = 0.5) -> BlendedProjection:
    if not 0 <= hashtag_weight <= 1 or not isfinite(hashtag_weight):
        raise ValueError("Hashtag weight must be between 0 and 1")
    if hashtag is None and leaguewinner is None:
        raise ValueError("At least one source is required")
    if hashtag and hashtag.source != "Hashtag" or leaguewinner and leaguewinner.source != "LeagueWinner":
        raise ValueError("Source mismatch")
    sources = tuple(r.source for r in (hashtag, leaguewinner) if r is not None)
    if hashtag and leaguewinner:
        weights = ((hashtag, hashtag_weight), (leaguewinner, 1 - hashtag_weight))
    else:
        weights = ((hashtag or leaguewinner, 1.0),)
    values: dict[str, float | None] = {}
    disagreement: dict[str, float | None] = {}
    for stat in STATS:
        available = [(getattr(record, stat), weight) for record, weight in weights if record is not None and getattr(record, stat) is not None and weight > 0]
        # Missing a positive-weight input is never imputed from another source.
        if len(available) != sum(weight > 0 for _, weight in weights):
            values[stat] = None
        else:
            values[stat] = sum(value * weight for value, weight in available)
        if hashtag and leaguewinner:
            a, b = getattr(hashtag, stat), getattr(leaguewinner, stat)
            disagreement[stat] = abs(a - b) / max(abs(a), abs(b), 1.0) if a is not None and b is not None else None
    for prefix in ("fg", "ft"):
        made, attempted = values[prefix + "m"], values[prefix + "a"]
        if made is not None and attempted is not None:
            try:
                pct = shooting_pct(made, attempted)
            except ValueError:
                pct = None
        else:
            pct = None
        values[prefix + "_pct"] = pct
    return BlendedProjection(values, values["fg_pct"], values["ft_pct"], sources, "two_sources" if len(sources) == 2 else "one_source", disagreement)
