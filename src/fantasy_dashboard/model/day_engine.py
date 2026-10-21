"""Cached daily lineup engine shared by the Waiver Advisor, its audit and the Trade Lab.

Applies the forecast's lineup rules (legal daily slots, GP/82 expected availability, summed
shooting volumes, known locks) to arbitrary rosters and scores a period against an opponent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fantasy_dashboard.league import CATEGORIES, EASTERN, SLOTS, VOLUMES, eligible
from fantasy_dashboard.model.forecast import category_margin, category_result, margin_score, with_actual_volumes
from fantasy_dashboard.model.lineups import assign_lineup, lineup_score, unavailable_for_game

FAR = datetime.max.replace(tzinfo=timezone.utc)


class DayEngine:
    """Daily lineup volumes, cached by (day, eligible players, locked slots).

    The starters are a maximum-weight independent set of a transversal matroid
    (players that can fill distinct legal slots), so greedy by lineup score with
    an augmenting-path check is exact. Equal scores or captured locks fall back
    to `assign_lineup` so its tie rules apply unchanged.
    """

    def __init__(self, schedule, players, projections, as_of, snapshot_day, injury_inputs=None):
        self.schedule, self.players, self.projections = schedule, players, projections
        self.as_of, self.snapshot_day, self.injury_inputs = as_of, snapshot_day, injury_inputs
        self.today = as_of.astimezone(EASTERN).date()
        self.vec, self.score, self.slots = {}, {}, {}
        for pid in players:
            values = projections[pid]['values']
            self.vec[pid] = tuple(values['gp'] / 82 * values[k] for k in VOLUMES)
            self.score[pid] = max(1e-6, lineup_score(values))
            self.slots[pid] = tuple(j for j, slot in enumerate(SLOTS)
                                    if eligible(slot, players[pid].eligible_positions))
        self.cache = {}
        self.computed = self.hits = 0

    def tipoff(self, pid, day):
        game = self.schedule.team_dates.get((self.players[pid].nba_team, day))
        return game.tipoff_utc if game and game.tipoff_utc > self.as_of else None

    def day(self, day, roster_ids, since, until):
        candidates = []
        for pid in roster_ids:
            if self.players[pid].assigned_slot == 'IL':
                continue
            tip = self.tipoff(pid, day)
            if tip is None or tip < since.get(pid, self.as_of) or tip >= until.get(pid, FAR) or \
                    unavailable_for_game(self.injury_inputs, pid, tip, self.as_of):
                continue
            candidates.append(pid)
        locked = ()
        if day == self.today and day == self.snapshot_day:
            locked = tuple(sorted(
                self.players[pid].assigned_slot for pid in roster_ids
                if self.players[pid].assigned_slot not in {'BN', 'IL'}
                and (self.players[pid].nba_team, day) in self.schedule.team_dates
                and self.schedule.team_dates[(self.players[pid].nba_team, day)].tipoff_utc <= self.as_of))
        key = (day, tuple(sorted(candidates)), locked)
        found = self.cache.get(key)
        if found is not None:
            self.hits += 1
            return found
        starters = self._starters(key[1], locked)
        result = (tuple(sum(self.vec[p][i] for p in starters) for i in range(len(VOLUMES))), starters)
        self.cache[key] = result
        self.computed += 1
        return result

    def _starters(self, candidates, locked):
        scores = [self.score[p] for p in candidates]
        if locked or len(set(scores)) != len(scores):
            chosen = assign_lineup([self.players[p] for p in candidates],
                                   {p: self.score[p] for p in candidates},
                                   require_full=False, blocked_slots=locked)
            return tuple(sorted(chosen))
        owner = [None] * len(SLOTS)

        def augment(pid, seen):
            for j in self.slots[pid]:
                if j in seen:
                    continue
                seen.add(j)
                if owner[j] is None or augment(owner[j], seen):
                    owner[j] = pid
                    return True
            return False
        return tuple(sorted(p for p in sorted(candidates, key=lambda p: -self.score[p])
                            if augment(p, set())))

    def period(self, days, roster_ids, since=None, until=None):
        since, until = since or {}, until or {}
        total = [0.0] * len(VOLUMES)
        starts = []
        for day in days:
            volumes, starters = self.day(day, roster_ids, since, until)
            for i, value in enumerate(volumes):
                total[i] += value
            starts.extend((day, p) for p in starters)
        return total, starts


def period_totals(total):
    v = dict(zip(VOLUMES, total))
    cats = {k: v[k] for k in CATEGORIES if k not in ('fg_pct', 'ft_pct')}
    cats['fg_pct'] = v['fgm'] / v['fga'] if v['fga'] else None
    cats['ft_pct'] = v['ftm'] / v['fta'] if v['fta'] else None
    return {'categories': cats, 'shooting_volumes': {k: v[k] for k in ('fgm', 'fga', 'ftm', 'fta')}}


def score_against(raw, opposing, scales):
    wins, losses, ties, outcomes = category_result(raw['categories'], opposing)
    margins = {c: category_margin(raw['categories'], opposing, c) for c in CATEGORIES}
    return {'categories': raw['categories'], 'score': [wins, losses, ties], 'points': wins + .5 * ties,
            'outcomes': outcomes, 'margins': margins, 'soft': margin_score(margins, scales)}


def date_range(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def horizon_scores(engine, week_days, opposing, steps, initial_ids, actual, scales):
    """Score one transaction sequence against each scheduled horizon opponent."""
    since = {s[2]: s[4] for s in steps}
    until = {s[3]: s[4] for s in steps if s[3] is not None}
    ids = initial_ids | set(since)
    weeks, starts = [], 0
    for wi, days in enumerate(week_days):
        total, started = engine.period(days, ids, since, until)
        raw = period_totals(total)
        if wi == 0 and actual:
            raw = with_actual_volumes(raw, actual)
        weeks.append(score_against(raw, opposing[wi], scales))
        starts += len(started)
    return weeks, starts
