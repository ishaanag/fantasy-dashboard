"""Phase 3E Waiver Advisor: one read-only search over streams, swaps and permanent upgrades.

The day engine applies the Phase 2 rules (legal daily slots, GP/82 expected
availability, summed shooting volumes, known locks). Every displayed plan is
replayed through `_dated_team_totals`, and HOLD is checked against the
deterministic forecast, so the cache can never silently change a result.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
import multiprocessing
import os
import warnings
from time import perf_counter
from types import SimpleNamespace

from .audit import Auditor
from .deterministic import forecast
from .phase1 import (CATEGORIES, EASTERN, SLOTS, VOLUMES, RosterPlayer, _dated_team_totals,
                     _lineup_score, assign_lineup, eligible, resolve_projection, unavailable_for_game)
from .schedule import FantasyWeek
from .simulator import _expected_with_actual, category_result
from .waiver import _evidence_reasons, _margin, _scales, _smooth, _utc, classify_plan, pool_audit

INACTIVE_STATUSES = {'O', 'NA', 'OFS', 'INJ'}
DOUBTFUL_STATUSES = {'Q', 'D', 'GTD'}
FAR = datetime.max.replace(tzinfo=timezone.utc)
LABELS = {'fg_pct': 'FG%', 'ft_pct': 'FT%', 'three_ptm': '3PTM', 'pts': 'PTS', 'reb': 'REB',
          'ast': 'AST', 'stl': 'STL', 'blk': 'BLK', 'turnovers': 'TO'}
BASE_POSITIONS = ('PG', 'SG', 'SF', 'PF', 'C')


@dataclass(frozen=True)
class AdvisorConfig:
    """Defaults are the product behaviour; the dashboard exposes only `horizon`."""
    horizon: int | None = None            # None: 2 periods, or 3 when under 3 days remain
    expand_adds: int = 40                 # best first-move adds carried into multi-move combinations
    season_shortlist: int = 20            # permanent-upgrade candidates also carried into combinations
    beam_width: int = 32
    local_seeds: int = 8                  # best distinct plans improved by single changes; 0 turns it off
    local_adds: int = 60                  # adds tried in each single change (plus the beam's expanded adds)
    local_evaluations: int = 300_000      # deterministic budget for local improvement (about a minute on 8 workers)
    search_seconds: float = 300.0         # safety stop only (keeps results reproducible under load); the evaluation budget ends the search
    workers: int = max(1, min(8, (os.cpu_count() or 2) - 1))
    acquisition_cost: float = .15         # category points; prefers fewer moves at equal value
    soft_weight: float = .5               # horizon margin score, guards against knife-edge flips
    season_gain_weight: float = .5        # later weeks are discounted; moves can be made later
    season_loss_weight: float = 1.0       # losses count fully: a dropped player is gone
    negligible_season_loss: float = .5    # margin-score loss over the rest of season treated as noise
    recommend_threshold: float = .25
    fragile_flip_discount: float = .5     # a flip that a single source or a 5% margin shift reverses counts half
    core_margin: float = .15              # protected when no available player is within 15%
    result_limit: int = 5
    max_evaluations: int = 1_000_000
    evidence_max_age_minutes: int = 15

    def __post_init__(self):
        if self.horizon is not None and self.horizon not in (1, 2, 3):
            raise ValueError('Choose a horizon of 1, 2 or 3 scoring periods')
        if min(self.expand_adds, self.beam_width, self.result_limit, self.workers,
               self.max_evaluations, self.evidence_max_age_minutes) < 1 or \
                min(self.season_shortlist, self.local_seeds, self.local_adds, self.local_evaluations,
                    self.search_seconds) < 0:
            raise ValueError('Advisor limits must be positive')
        if any(not isfinite(v) or v < 0 for v in (self.acquisition_cost, self.soft_weight,
               self.season_gain_weight, self.season_loss_weight, self.negligible_season_loss,
               self.recommend_threshold, self.core_margin, self.fragile_flip_discount)):
            raise ValueError('Advisor weights must be finite and nonnegative')


_JOB = None   # (function, items) inherited by forked workers; never pickled


def _work(bounds):
    fn, items = _JOB
    return [fn(items[k]) for k in range(*bounds)]


def _parallel(fn, items, workers, deadline=None):
    """Evaluates items in forked workers that inherit the prepared engine (copy-on-write). With a deadline,
    chunks are submitted in order and evaluation stops once it passes, returning a prefix of the results."""
    global _JOB
    if workers <= 1 or len(items) < 400 or 'fork' not in multiprocessing.get_all_start_methods():
        out = []
        for item in items:
            if deadline is not None and perf_counter() > deadline:
                break
            out.append(fn(item))
        return out
    size = max(50, len(items) // (workers * 8))
    chunks = [(k, min(k + size, len(items))) for k in range(0, len(items), size)]
    _JOB = (fn, items)
    out = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            with multiprocessing.get_context('fork').Pool(workers) as pool:
                for part in pool.imap(_work, chunks):
                    out.extend(part)
                    if deadline is not None and perf_counter() > deadline:
                        pool.terminate()
                        break
        return out
    except (OSError, RuntimeError):
        return out + [fn(items[k]) for k in range(len(out), len(items))]
    finally:
        _JOB = None


class _Engine:
    """Phase 2 daily lineup volumes, cached by (day, eligible players, locked slots).

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
            self.score[pid] = max(1e-6, _lineup_score(values))
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


def _categories(total):
    v = dict(zip(VOLUMES, total))
    cats = {k: v[k] for k in CATEGORIES if k not in ('fg_pct', 'ft_pct')}
    cats['fg_pct'] = v['fgm'] / v['fga'] if v['fga'] else None
    cats['ft_pct'] = v['ftm'] / v['fta'] if v['fta'] else None
    return {'categories': cats, 'shooting_volumes': {k: v[k] for k in ('fgm', 'fga', 'ftm', 'fta')}}


def _score(raw, opposing, scales):
    wins, losses, ties, outcomes = category_result(raw['categories'], opposing)
    margins = {c: _margin(raw['categories'], opposing, c) for c in CATEGORIES}
    return {'categories': raw['categories'], 'score': [wins, losses, ties], 'points': wins + .5 * ties,
            'outcomes': outcomes, 'margins': margins, 'soft': _smooth(margins, scales)}


def _days(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def _rank_word(outcome):
    return {'W': 'win', 'L': 'loss', 'T': 'tie'}[outcome]


def _horizon(engine, week_days, opposing, steps, initial_ids, actual, scales):
    """Score one transaction sequence against each scheduled horizon opponent."""
    since = {s[2]: s[4] for s in steps}
    until = {s[3]: s[4] for s in steps if s[3] is not None}
    ids = initial_ids | set(since)
    weeks, starts = [], 0
    for wi, days in enumerate(week_days):
        total, started = engine.period(days, ids, since, until)
        raw = _categories(total)
        if wi == 0 and actual:
            raw = _expected_with_actual(raw, actual)
        weeks.append(_score(raw, opposing[wi], scales))
        starts += len(started)
    return weeks, starts


def advise(inputs: dict, *, team_id=None, weight: float = .5, as_of=None, actual_state=None,
           evidence=None, matchup_path=None, config: AdvisorConfig = AdvisorConfig(), now=None,
           base=None) -> dict:
    """Rank distinct waiver strategies (streams, swaps, upgrades) against HOLD. Read-only.

    `base` may be the dashboard's cached deterministic forecast for the same weight and as-of.
    """
    started = perf_counter()
    team_id = team_id or inputs['league']['my_team_id']
    if team_id != inputs['league']['my_team_id']:
        raise ValueError('Use the authenticated Yahoo team')
    as_of = as_of or _utc(inputs['availability']['captured_at_utc'])
    now = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None or now.tzinfo is None:
        raise ValueError('As-of and current times must include a timezone')
    as_of, now = as_of.astimezone(timezone.utc), now.astimezone(timezone.utc)
    if not (base and base.get('hashtag_weight') == weight and
            base.get('as_of_utc') == as_of.isoformat().replace('+00:00', 'Z')):
        base = forecast(inputs, matchup_path=matchup_path, hashtag_weight=weight, as_of=as_of,
                        actual_state=actual_state)
    first = base['actual_completed_weeks'] + 1
    if first > 18:
        raise ValueError('The Waiver Advisor covers the regular season only')
    today = as_of.astimezone(EASTERN).date()
    current = inputs['weeks'][first-1]
    if today > current.end:
        raise ValueError('As-of date is after the current Yahoo scoring period; refresh completed results')
    horizon = config.horizon or (3 if (current.end - max(today, current.start)).days < 2 else 2)
    horizon = min(horizon, 19 - first)
    weeks = inputs['weeks'][first-1:min(first-1+horizon, 18)]
    horizon = len(weeks)
    week_days = [list(_days(max(today, w.start), w.end)) for w in weeks]
    matchups = {m['week']: m for m in base['weekly_matchups'] if team_id in m['teams']}
    opponent = {w.number: next(t for t in matchups[w.number]['teams'] if t != team_id)
                for w in inputs['weeks'][first-1:18]}
    opposing = [matchups[w.number]['totals'][opponent[w.number]]['categories'] for w in weeks]
    later = [w for w in inputs['weeks'][first-1+horizon:18] if matchups[w.number].get('status') != 'actual']
    later_days = [list(_days(w.start, w.end)) for w in later]
    later_opposing = [matchups[w.number]['totals'][opponent[w.number]]['categories'] for w in later]
    scales = _scales(base)
    current_volumes = actual_state.get('current_week_volumes', {}) if actual_state else {}
    actual = current_volumes.get(team_id)
    snapshot_day = date.fromisoformat(inputs['availability']['lineup_date'])
    schedule = inputs['schedule']

    # Evidence: gaps make plans provisional; they never stop hypothetical analysis.
    reasons = _evidence_reasons(inputs, evidence, team_id, first, as_of)
    if evidence and 'captured_at_utc' in evidence:
        try:
            age = now - _utc(evidence['captured_at_utc'])
            if age < timedelta() or age > timedelta(minutes=config.evidence_max_age_minutes):
                reasons.append('transaction_evidence_not_fresh_for_action')
        except (TypeError, ValueError):
            reasons.append('transaction_evidence_time_invalid')
    used = evidence.get('adds_used') if evidence else None
    if isinstance(used, int) and 0 <= used <= current.max_adds:
        first_budget = current.max_adds - used
    else:
        first_budget = current.max_adds
        reasons.append('acquisition_count_unverified')
    budgets = [first_budget, *(w.max_adds for w in weeks[1:])]
    statuses = (evidence.get('status_by_site_id') if evidence else None) or inputs.get('injury_statuses', {})
    cant_cut = set(evidence.get('cannot_cut_site_ids', ())) if evidence else set()
    locked_ids = set(evidence.get('locked_site_ids', ())) if evidence else set()
    claims = set(evidence.get('active_claim_site_ids', ())) if evidence else set()
    reasons = sorted(set(reasons))

    roster = inputs['rosters'][team_id]
    players = {p.site_player_id: p for p in roster}
    initial_ids = frozenset(players)
    projections = {pid: resolve_projection(inputs['projections'][pid], weight) for pid in players}

    # Candidate funnel over the complete captured Yahoo pool.
    audit = pool_audit(inputs, weight)
    by_id = {p.site_player_id: p for p in inputs['site_players']}
    horizon_days = [d for days in week_days for d in days]
    funnel, excluded, pool = Counter(yahoo_players=len(audit['rows'])), Counter(), []
    for row in audit['rows']:
        pid = row['site_player_id']
        if row['owner_team_id'] or row['availability'] not in ('FA', 'W') or pid in players:
            continue
        funnel['unowned_free_agents_or_waivers'] += 1
        player = by_id[pid]
        if not row['projectable']:
            excluded[next((i for i in ('ambiguous_mapping', 'missing_projection', 'gp_zero',
                                       'selected_source_missing_or_invalid') if i in row['issues']),
                          'not_projectable')] += 1
        elif player.nba_team not in schedule.team_counts:
            excluded['nba_schedule_missing'] += 1
        elif statuses.get(pid) in INACTIVE_STATUSES:
            excluded['yahoo_status_out_or_inactive'] += 1
        elif pid in claims:
            excluded['active_claim'] += 1
        elif not any((player.nba_team, d) in schedule.team_dates and
                     schedule.team_dates[(player.nba_team, d)].tipoff_utc > as_of for d in horizon_days):
            excluded['no_games_in_horizon'] += 1
        else:
            pool.append(pid)
            players[pid] = RosterPlayer(team_id, pid, player.name, 'BN', player.eligible_positions, player.nba_team)
            projections[pid] = resolve_projection(inputs['projections'][pid], weight)
    opponent_rosters = [frozenset(p.site_player_id for p in inputs['rosters'][opponent[w.number]]) for w in weeks]
    for w in weeks:
        for p in inputs['rosters'][opponent[w.number]]:
            players[p.site_player_id] = p
            projections[p.site_player_id] = resolve_projection(inputs['projections'][p.site_player_id], weight)
    engine = _Engine(schedule, players, projections, as_of, snapshot_day, inputs)

    def opponent_totals(eng):
        out = []
        for wi, w in enumerate(weeks):
            raw = _categories(eng.period(week_days[wi], opponent_rosters[wi])[0])
            if wi == 0 and current_volumes.get(opponent[w.number]):
                raw = _expected_with_actual(raw, current_volumes[opponent[w.number]])
            out.append(raw['categories'])
        return out
    for wi, totals in enumerate(opponent_totals(engine)):
        if any(abs((totals[c] or 0) - (opposing[wi][c] or 0)) > 1e-6*max(1, abs(opposing[wi][c] or 0)) for c in CATEGORIES):
            raise AssertionError('Advisor opponent totals diverged from the deterministic forecast')

    season_cache = {}

    def season(final_ids):
        key = frozenset(final_ids)
        if key not in season_cache:
            season_cache[key] = [_score(_categories(engine.period(days, key)[0]), opp, scales)
                                 for days, opp in zip(later_days, later_opposing)]
        return season_cache[key]

    def horizon_eval(steps):
        return _horizon(engine, week_days, opposing, steps, initial_ids, actual, scales)

    hold_h, hold_starts = horizon_eval(())
    hold_s = season(initial_ids)
    for result, week in [*zip(hold_h, weeks), *zip(hold_s, later)]:
        expected = matchups[week.number]['totals'][team_id]['categories']
        if any((result['categories'][c] is None) != (expected[c] is None) or
               (expected[c] is not None and abs(result['categories'][c]-expected[c]) > 1e-6*max(1, abs(expected[c])))
               for c in CATEGORIES):
            raise AssertionError(f'Advisor HOLD diverged from the deterministic forecast in Week {week.number}')
    hold_points, hold_soft = sum(w['points'] for w in hold_h), sum(w['soft'] for w in hold_h)
    hold_season_points, hold_season_soft = sum(w['points'] for w in hold_s), sum(w['soft'] for w in hold_s)

    def assess(steps, h, starts, final_ids):
        s = season(final_ids)
        gain = sum(w['points'] for w in h) - hold_points
        soft = sum(w['soft'] for w in h) - hold_soft
        season_points = sum(w['points'] for w in s) - hold_season_points
        season_soft = sum(w['soft'] for w in s) - hold_season_soft
        season_term = (config.season_gain_weight * season_soft if season_soft >= 0 else
                       config.season_loss_weight * min(0.0, season_soft + config.negligible_season_loss))
        return {'value': gain + config.soft_weight*soft + season_term - config.acquisition_cost*len(steps),
                'gain': gain, 'soft': soft, 'season_points': season_points, 'season_soft': season_soft,
                'season_term': season_term, 'starts': starts - hold_starts}

    # Roster roles: Yahoo restrictions, then a protected core measured against replacement level.
    season_days = list(_days(today, inputs['weeks'][:18][-1].end))

    def season_value(pid):
        games = sum(1 for d in season_days if engine.tipoff(pid, d))
        return max(0.0, _lineup_score(projections[pid]['values'])) * games, games
    pool_value = {pid: season_value(pid)[0] for pid in pool}
    best_by_pos = {pos: max((pool_value[p] for p in pool if pos in players[p].eligible_positions), default=0.0)
                   for pos in BASE_POSITIONS}
    active_count = sum(p.assigned_slot != 'IL' for p in roster)
    droppable, roster_rows = set(), []
    for p in roster:
        value, games = season_value(p.site_player_id)
        replacement = max((best_by_pos[pos] for pos in p.eligible_positions if pos in best_by_pos), default=0.0)
        if p.site_player_id in cant_cut:
            role, why = 'protected', "On Yahoo's Can't Cut list"
        elif p.assigned_slot == 'IL' and active_count >= 14:
            role, why = 'protected', 'On IL; dropping him would not open an active roster spot'
        elif value > (1 + config.core_margin) * replacement:
            role, why = 'core', (f'No available player is within {round(config.core_margin*100)}% '
                                 'of his rest-of-season value')
        else:
            role, why = 'replaceable', 'An available player offers comparable rest-of-season value'
            droppable.add(p.site_player_id)
        roster_rows.append({'site_player_id': p.site_player_id, 'name': p.name, 'slot': p.assigned_slot,
                            'role': role, 'reason': why, 'yahoo_status': statuses.get(p.site_player_id, ''),
                            'season_value': round(value, 1), 'best_available_value': round(replacement, 1),
                            'remaining_games': games})
    roster_rows.sort(key=lambda r: -r['season_value'])

    # Matchup screen: each candidate added to the current roster, scored against real opponents.
    screen = {}
    for pid in pool:
        stamp = as_of + (timedelta(days=1) if by_id[pid].availability_label == 'W' else timedelta())
        h, starts = horizon_eval(((0, week_days[0][0], pid, None, stamp, True),))
        gain = sum(w['points'] for w in h) - hold_points
        soft = sum(w['soft'] for w in h) - hold_soft
        screen[pid] = (gain + config.soft_weight*soft, gain, starts - hold_starts)
    ranked = sorted(pool, key=lambda p: (-screen[p][0], p))
    weakest = min((season_value(d)[0] for d in droppable), default=float('inf'))
    upgrades = sorted((p for p in pool if pool_value[p] > weakest),
                      key=lambda p: (-pool_value[p], p))[:config.season_shortlist]
    adds = ranked   # every eligible player is tried as a first move; combinations use the best

    def effective(day, pid):
        label = by_id[pid].availability_label
        timing = evidence.get('timing_by_site_id', {}).get(pid) if evidence else None
        if day == today and timing and not reasons:
            try:
                stamp = _utc(timing['effective_at_utc'])
                if stamp >= as_of and stamp.astimezone(EASTERN).date() == day:
                    return stamp, False
            except (KeyError, TypeError, ValueError):
                pass
        # Model boundary, never a claimed Yahoo transaction timestamp.
        stamp = as_of if day <= today else datetime.combine(day, time.min, EASTERN).astimezone(timezone.utc)
        return stamp + (timedelta(days=1) if label == 'W' else timedelta()), True

    def action_days(pid):
        found = {(0, week_days[0][0])}
        for wi, days in enumerate(week_days):
            for d in days:
                if engine.tipoff(pid, d):
                    found.add((wi, d))
                    if by_id[pid].availability_label == 'W' and d - timedelta(days=1) >= days[0]:
                        found.add((wi, d - timedelta(days=1)))
        return sorted(found)
    action = {pid: action_days(pid) for pid in adds}

    def drop_ok(drop, wi, day, stamp):
        p = players[drop]
        if wi == 0 and day == today and drop in locked_ids:
            return False
        if day == today and p.assigned_slot not in {'BN', 'IL'}:
            game = schedule.team_dates.get((p.nba_team, day))
            if game and game.tipoff_utc <= stamp:
                return False
        return True

    def canonical(steps):
        return tuple((s[1].toordinal(), s[2], s[3] or '') for s in steps)

    # Single-source checks reuse the engine; no extra forecasts are needed.
    alternates = {}
    opponent_ids = frozenset().union(*opponent_rosters)
    for name, w in (('blended', .5), ('hashtag', 1.0), ('leaguewinner', 0.0)):
        if abs(w - weight) < 1e-9:
            continue
        alt_proj = {}
        for pid in initial_ids | opponent_ids | set(adds):
            try:
                alt_proj[pid] = resolve_projection(inputs['projections'][pid], w)
            except ValueError:
                pass
        if not (initial_ids | opponent_ids) <= set(alt_proj):
            alternates[name] = None
            continue
        alt_engine = _Engine(schedule, {p: players[p] for p in alt_proj}, alt_proj, as_of, snapshot_day, inputs)
        alt_opp = opponent_totals(alt_engine)
        alt_hold, _ = _horizon(alt_engine, week_days, alt_opp, (), initial_ids, actual, scales)
        alternates[name] = (alt_engine, alt_opp, alt_hold, set(alt_proj))


    def fragile_count(steps, h):
        """Category flips to a win that one projection source alone or a 5% margin shift reverses (as in describe)."""
        checks = []
        for payload in alternates.values():
            if payload is not None and {x[2] for x in steps} <= payload[3]:
                checks.append(_horizon(payload[0], week_days, payload[1], steps, initial_ids, actual, scales)[0])
        count = 0
        for wi in range(len(weeks)):
            for c in CATEGORIES:
                if hold_h[wi]['outcomes'][c] != 'W' and h[wi]['outcomes'][c] == 'W':
                    margin = h[wi]['margins'][c]
                    if margin is None or margin <= .05 * scales[c] or any(x[wi]['outcomes'][c] != 'W' for x in checks):
                        count += 1
        return count

    action_set = {pid: set(days) for pid, days in action.items()}

    def evaluate(item):
        key, new, final = item
        h, starts = horizon_eval(new)
        a = assess(new, h, starts, final)
        a['fragile'] = fragile_count(new, h)
        a['rank'] = a['value'] - config.fragile_flip_discount * a['fragile']   # the score the final list uses
        return key, (new, final, a, None)   # weekly detail recomputed when needed

    def evaluate_many(items, deadline=None):
        """Plans are independent: evaluated in forked workers that inherit the engine and caches."""
        return _parallel(evaluate, items, config.workers, deadline)

    def droppable_now(current):
        return sorted(p for p in current if p in droppable or p not in initial_ids)

    def build(steps):
        """Replays a chronological plan under the beam's rules; its final roster, or None if a step is illegal."""
        current, counts, taken, last, last_stamp = initial_ids, [0] * horizon, set(), None, None
        for wi, day, add, drop, stamp, assumed in steps:
            if add in taken or add in current or counts[wi] >= budgets[wi] or (wi, day) not in action_set.get(add, ()):
                return None
            if (last and (day, add) <= last) or (last_stamp and stamp < last_stamp) or \
                    stamp.astimezone(EASTERN).date() > weeks[wi].end:
                return None
            if not any(engine.tipoff(add, d) and engine.tipoff(add, d) >= stamp for d in horizon_days if d >= day):
                return None
            if drop not in current or drop not in droppable_now(current) or not drop_ok(drop, wi, day, stamp):
                return None
            current = (current - {drop}) | {add}
            counts[wi] += 1
            taken.add(add)
            last, last_stamp = (day, add), stamp
        return current

    # Stage 1. Beam search over chronological add/drop sequences. Each state is scored by horizon category
    # points, horizon margin score, and the rest-of-season effect of the roster it leaves behind (dropped
    # players are never reacquired). Every first move is scored; each level is evaluated in parallel.
    hold_a = assess((), hold_h, hold_starts, initial_ids)
    complete = {(): ((), initial_ids, {**hold_a, 'fragile': 0, 'rank': hold_a['value']}, hold_h)}
    beam = [((), initial_ids)]
    evaluations, truncated, depth_reached = 0, False, 0
    expand = []
    for depth in range(sum(budgets)):
        proposals = {}
        for steps, current in beam:
            counts = [sum(s[0] == wi for s in steps) for wi in range(horizon)]
            taken = {s[2] for s in steps}
            last = (steps[-1][1], steps[-1][2]) if steps else None
            last_stamp = steps[-1][4] if steps else None
            drops = droppable_now(current)
            for add in (adds if depth == 0 else expand):
                if add in taken or add in current:
                    continue
                for wi, day in action[add]:
                    if counts[wi] >= budgets[wi] or (last and (day, add) <= last):
                        continue
                    stamp, assumed = effective(day, add)
                    if stamp.astimezone(EASTERN).date() > weeks[wi].end or (last_stamp and stamp < last_stamp):
                        continue
                    if not any(engine.tipoff(add, d) and engine.tipoff(add, d) >= stamp
                               for d in horizon_days if d >= day):
                        continue
                    for drop in drops:
                        if not drop_ok(drop, wi, day, stamp):
                            continue
                        new = steps + ((wi, day, add, drop, stamp, assumed),)
                        key = canonical(new)
                        if key not in proposals and key not in complete:
                            proposals[key] = (key, new, (current - {drop}) | {add})
        items = list(proposals.values())
        if evaluations + len(items) > config.max_evaluations:
            items, truncated = items[:config.max_evaluations - evaluations], True
        if not items:
            break
        children = dict(evaluate_many(items))
        evaluations += len(items)
        complete.update(children)
        depth_reached = depth + 1
        if depth == 0:
            best_first = {}
            for child in children.values():
                first_add = child[0][0][2]
                best_first[first_add] = max(best_first.get(first_add, float('-inf')), child[2]['rank'])
            expand = sorted(best_first, key=lambda a: (-best_first[a], a))[:config.expand_adds]
            expand += [p for p in upgrades if p not in expand]
        beam, seen = [], Counter()
        for key, child in sorted(children.items(), key=lambda kv: (-kv[1][2]['rank'], kv[0])):
            lead = frozenset(s[2] for s in child[0])
            if seen[lead] >= 2:   # keep the beam from filling with drop variants of one add set
                continue
            seen[lead] += 1
            beam.append((child[0], child[1]))
            if len(beam) >= config.beam_width:
                break
        if truncated:
            break

    # Stage 2. Local improvement: the best distinct plans repeatedly try every single change (another add or
    # day for one move, another drop, removing a move, inserting a move) and keep the best improvement,
    # until none improves or the time budget ends. Catches plans the beam's width cut off.
    local_rounds, local_evaluations, local_improvements = 0, 0, 0
    local_adds = list(dict.fromkeys([*ranked[:config.local_adds], *expand, *upgrades]))

    def neighbours(steps):
        variants = []
        for i, (wi, day, add, drop, stamp, assumed) in enumerate(steps):
            variants.append(steps[:i] + steps[i + 1:])
            before = build(steps[:i])
            if before is None:
                continue
            for d in droppable_now(before):
                if d != drop:
                    variants.append(steps[:i] + ((wi, day, add, d, stamp, assumed),) + steps[i + 1:])
            for a in local_adds:
                for w2, d2 in action[a]:
                    variants.append(steps[:i] + ((w2, d2, a, drop, *effective(d2, a)),) + steps[i + 1:])
        if len(steps) < sum(budgets):
            for a in local_adds:
                for w2, d2 in action[a]:
                    pos = sum(1 for x in steps if (x[1], x[2]) < (d2, a))
                    before = build(steps[:pos])
                    if before is None:
                        continue
                    for d in droppable_now(before):
                        variants.append(steps[:pos] + ((w2, d2, a, d, *effective(d2, a)),) + steps[pos:])
        out, seen = [], set()
        for v in variants:
            v = tuple(sorted(v, key=lambda x: (x[1], x[2])))
            key = canonical(v)
            if key in seen or key in complete:
                continue
            seen.add(key)
            final = build(v)
            if final is not None:
                out.append((key, v, final))
        return out
    if config.local_seeds and not truncated:
        deadline = started + config.search_seconds
        explored, active = set(), []

        def next_seeds():
            """The best distinct plans not yet used as a starting point (one per add set)."""
            out, leads = [], set()
            for key, rec in sorted(complete.items(), key=lambda kv: (-kv[1][2]['rank'], kv[0])):
                lead = frozenset(x[2] for x in rec[0])
                if key and key not in explored and lead not in leads:
                    leads.add(lead)
                    out.append(rec[0])
                if len(out) >= config.local_seeds:
                    break
            return out
        while perf_counter() < deadline and local_evaluations < config.local_evaluations:
            if not active:        # converged: restart from the next-best unexplored plans
                active = next_seeds()
                if not active:
                    break
            explored.update(canonical(x) for x in active)
            owner, items = {}, []
            for i, steps in enumerate(active):
                for item in neighbours(steps):
                    if item[0] not in owner:
                        owner[item[0]] = i
                        items.append(item)
            items = items[:config.local_evaluations - local_evaluations]   # deterministic budget, in order
            results = evaluate_many(items, deadline)
            complete.update(results)
            local_evaluations += len(results)
            local_rounds += 1
            best = {}
            for key, rec in results:
                i = owner[key]
                if rec[2]['rank'] > complete[canonical(active[i])][2]['rank'] + 1e-9 and \
                        (i not in best or rec[2]['rank'] > best[i][2]['rank']):
                    best[i] = rec
            local_improvements += len(best)
            if len(results) < len(items):
                break   # time budget reached mid-round
            active = [best[i][0] for i in sorted(best)]

    # One entry per transaction set (best timing), then one per add set (best drop).
    def preference(key, rec):
        steps = rec[0]
        return (-rec[2]['rank'], sum(1 for s in steps if s[0] > 0 or s[1] > today), len(steps), key)
    best_tx = {}
    for key, rec in sorted(complete.items(), key=lambda kv: preference(*kv)):
        best_tx.setdefault(frozenset((s[2], s[3]) for s in rec[0]), (key, rec))
    groups = []
    for key, rec in sorted(best_tx.values(), key=lambda kv: preference(*kv)):
        add_set = frozenset(s[2] for s in rec[0])
        twin = next((g for g in groups if g['adds'] == add_set), None)
        if twin:
            twin['alternatives'].append(rec)
        elif not any(g['adds'] and g['adds'] < add_set and g['rec'][2]['rank'] >= rec[2]['rank'] for g in groups):
            groups.append({'adds': add_set, 'rec': rec, 'alternatives': []})
    hold_group = next(g for g in groups if not g['adds'])
    def replay(steps, h):
        since = {s[2]: s[4] for s in steps}
        until = {s[3]: s[4] for s in steps if s[3] is not None}
        squad = [players[p] for p in sorted(initial_ids | set(since))]
        projected = [(p, projections[p.site_player_id]) for p in squad]
        for wi, w in enumerate(weeks):
            raw, _, _ = _dated_team_totals(squad, projected, schedule,
                                           FantasyWeek(w.number, week_days[wi][0], w.end, w.phase),
                                           as_of, snapshot_day, available_from_by_id=since,
                                           available_until_by_id=until, injury_inputs=inputs)
            if wi == 0 and actual:
                raw = _expected_with_actual(raw, actual)
            for c in CATEGORIES:
                a, b = raw['categories'][c], h[wi]['categories'][c]
                if (a is None) != (b is None) or (a is not None and abs(a-b) > 1e-9*max(1, abs(a))):
                    raise AssertionError('Advisor plan replay diverged from the Phase 2 lineup engine')

    tau = config.negligible_season_loss
    order = {'L': 0, 'T': 1, 'W': 2}

    def describe(rec, alternatives=()):
        steps, final, a, h = rec
        if h is None:
            h, _ = horizon_eval(steps)
        dropped_later = {s[3] for s in steps}
        moves = []
        for wi, day, add, drop, stamp, assumed in steps:
            label = by_id[add].availability_label
            arrives = stamp.astimezone(EASTERN).date()
            when = ('Now' if day <= today and label == 'FA' else
                    f'Claim {day:%a %b} {day.day}; arrives {arrives:%a} if the claim succeeds' if label == 'W'
                    else f'{day:%a %b} {day.day}, if still available')
            moves.append({'week': weeks[wi].number, 'date': day.isoformat(), 'when': when,
                          'add_site_player_id': add, 'add_name': players[add].name,
                          'add_team': players[add].nba_team, 'add_positions': list(players[add].eligible_positions),
                          'add_status': statuses.get(add, ''), 'availability': label,
                          'drop_site_player_id': drop, 'drop_name': players[drop].name,
                          'temporary': add in dropped_later,
                          'effective_at_utc': None if assumed else stamp.isoformat().replace('+00:00', 'Z'),
                          'modeled_effective_date': arrives.isoformat(), 'timing_assumed': assumed,
                          'future_availability_conditional': wi > 0 or day > today or label == 'W'})
        checks = {}
        for name, payload in alternates.items():
            if payload is None or not {s[2] for s in steps} <= payload[3]:
                checks[name] = None
                continue
            alt_h, _ = _horizon(payload[0], week_days, payload[1], steps, initial_ids, actual, scales)
            checks[name] = (sum(w['points'] for w in alt_h) - sum(w['points'] for w in payload[2]), alt_h)
        week_rows, fragile = [], []
        for wi, w in enumerate(weeks):
            before, after = hold_h[wi], h[wi]
            won = [c for c in CATEGORIES if before['outcomes'][c] != 'W' and after['outcomes'][c] == 'W']
            lost = [c for c in CATEGORIES if before['outcomes'][c] == 'W' and after['outcomes'][c] != 'W']
            for c in won:
                margin = after['margins'][c]
                reversed_in = [n for n, v in checks.items() if v and v[1][wi]['outcomes'][c] != 'W']
                if margin is None or margin <= .05 * scales[c] or reversed_in:
                    fragile.append({'week': w.number, 'category': LABELS[c],
                                    'reason': (f"not a win with {' or '.join(reversed_in)} projections only"
                                               if reversed_in else 'winning margin under 5% of a typical weekly spread')})
            count = sum(s[0] == wi for s in steps)
            week_rows.append({'week': w.number, 'opponent_name': inputs['teams'][opponent[w.number]],
                              'start_date': week_days[wi][0].isoformat(), 'end_date': w.end.isoformat(),
                              'before': before['score'], 'after': after['score'],
                              'categories_won': [LABELS[c] for c in won], 'categories_lost': [LABELS[c] for c in lost],
                              'acquisitions_used_after': ((used if wi == 0 and isinstance(used, int) else 0) + count),
                              'acquisition_limit': w.max_adds,
                              'categories': {c: {'label': LABELS[c], 'before': before['outcomes'][c],
                                                 'after': after['outcomes'][c], 'margin_before': before['margins'][c],
                                                 'margin_after': after['margins'][c]} for c in CATEGORIES}})
        improved, weakened = [], []
        for c in CATEGORIES:
            shift = sum(order[h[wi]['outcomes'][c]] - order[hold_h[wi]['outcomes'][c]] for wi in range(horizon))
            spread = sum((h[wi]['margins'][c] - hold_h[wi]['margins'][c]) / scales[c] for wi in range(horizon)
                         if h[wi]['margins'][c] is not None and hold_h[wi]['margins'][c] is not None)
            if shift > 0 or (shift == 0 and spread > .25):
                improved.append(LABELS[c])
            elif shift < 0 or spread < -.25:
                weakened.append(LABELS[c])
        word = 'gain' if a['season_soft'] > tau else 'cost' if a['season_soft'] < -tau else 'negligible'
        temporary = any(m['temporary'] for m in moves)
        kind = ('HOLD' if not steps else 'Streaming' if temporary else
                'Permanent upgrade' if word == 'gain' else 'Matchup swap')
        why = []
        for row in week_rows:
            if row['categories_won']:
                why.append(f"Turns {', '.join(row['categories_won'])} into wins against {row['opponent_name']} in Week {row['week']}")
            if row['categories_lost']:
                why.append(f"Gives up {', '.join(row['categories_lost'])} in Week {row['week']}")
        if steps and a['starts']:
            why.append(f"{'Adds' if a['starts'] > 0 else 'Loses'} {abs(a['starts'])} usable start{'s' if abs(a['starts']) != 1 else ''} over the horizon")
        if steps and not any(r['categories_won'] for r in week_rows) and a['soft'] > 0:
            why.append('Widens close category margins without changing a projected result')
        if steps:
            detail = f"margin score {a['season_soft']:+.2f}; deterministic count {a['season_points']:+.1f} category wins"
            why.append({'gain': f'Improves the rest-of-season roster ({detail})',
                        'negligible': f'Rest-of-season roster value is essentially unchanged ({detail})',
                        'cost': f'Weakens the rest-of-season roster ({detail})'}[word])
        else:
            why.append('Current roster; every strategy is measured against this baseline')
        missing = [] if not steps else list(reasons)
        if any(s[0] > 0 or s[1] > today for s in steps):
            missing += ['future_availability_conditional', 'candidate_status_and_ownership_must_be_refreshed']
        if any(s[5] for s in steps):
            missing.append('effective_timing_unverified')
        if any(by_id[s[2]].availability_label == 'W' for s in steps):
            missing.append('waiver_claim_success_unverified')
        if any(statuses.get(s[2]) in DOUBTFUL_STATUSES for s in steps):
            missing.append('candidate_status_questionable')
        missing = sorted(set(missing))
        cautions = [f"{f['category']} in Week {f['week']} is fragile: {f['reason']}" for f in fragile]
        cautions += [f"{m['add_name']} is listed {m['add_status']} on Yahoo" for m in moves
                     if m['add_status'] in DOUBTFUL_STATUSES]
        eligibility = classify_plan({'steps': moves, 'missing_verification': missing})
        return {'kind': kind, 'value': round(a['value'], 3), 'moves': moves, 'acquisitions': len(steps),
                'category_points_gained': a['gain'], 'margin_score_gained': round(a['soft'], 3),
                'usable_starts_gained': a['starts'], 'categories_improved': improved,
                'categories_weakened': weakened, 'weeks': week_rows,
                'season': {'assessment': word, 'category_points_delta': a['season_points'],
                           'margin_score_delta': round(a['season_soft'], 3), 'weeks': len(later),
                           'value_adjustment': round(a['season_term'], 3)},
                'final_roster_changes': {'added': sorted(players[p].name for p in final - initial_ids),
                                         'dropped': sorted(players[p].name for p in initial_ids - final)},
                'why': why, 'cautions': cautions,
                'source_check': {n: None if v is None else v[0] for n, v in checks.items()},
                'fragile_flips': fragile,
                'alternative_drops': [{'drops': [players[s[3]].name for s in alt[0]], 'value': round(alt[2]['value'], 3)}
                                      for alt in alternatives[:2]],
                'eligibility_class': eligibility,
                'state': 'HOLD' if not steps else 'Provisional' if missing else 'Actionable',
                'missing_verification': missing}

    def why_not(a, d):
        out = []
        if a['gain'] <= 0:
            out.append(f'No projected category flips in the next {horizon} scoring period{"s" if horizon > 1 else ""}')
        elif d['fragile_flips']:
            out.append('Its category flips are fragile (' + ', '.join(
                f"{f['category']} Week {f['week']}" for f in d['fragile_flips']) + ')')
        if a['season_soft'] < -tau:
            out.append(f"Weakens the rest-of-season roster (margin score {a['season_soft']:+.2f}; deterministic count {a['season_points']:+.1f} category wins)")
        if d['rank_value'] < config.recommend_threshold:
            out.append(f"Net value {d['rank_value']:+.2f} is below the {config.recommend_threshold:g} needed to beat HOLD "
                       f"after {config.acquisition_cost:g} per acquisition")
        return out

    # Rank ideas by value after discounting fragile flips, then show one plan per idea
    # (same dropped players and same projected flips); the rest become similar options.
    described = []
    ideas = [g for g in groups if g['adds']]
    above = [g for g in ideas if g['rec'][2]['value'] >= config.recommend_threshold]
    for g in (above + [g for g in ideas if g not in above][:10])[:400]:
        d = describe(g['rec'], g['alternatives'])
        d['rank_value'] = round(d['value'] - config.fragile_flip_discount * len(d['fragile_flips']), 3)
        described.append((g, d))
    described.sort(key=lambda gd: -gd[1]['rank_value'])

    def pick(items, limit):
        out = []
        for g, d in items:
            sig = (tuple(d['final_roster_changes']['dropped']),
                   tuple((w['week'], c) for w in d['weeks'] for c in w['categories_won']))
            tx = frozenset((m['add_site_player_id'], m['drop_site_player_id']) for m in d['moves'])
            twin = next((x for x in out if x[2] == sig or tx < x[3] or tx > x[3]), None)
            if twin:
                if len(twin[1]['similar_options']) < 3:
                    twin[1]['similar_options'].append({'moves': ', '.join(
                        f"+{m['add_name']} / −{m['drop_name']}" for m in d['moves']), 'rank_value': d['rank_value']})
            elif len(out) < limit:
                d['similar_options'] = []
                out.append((g, d, sig, tx))
        return out
    good = pick([gd for gd in described if gd[1]['rank_value'] >= config.recommend_threshold], config.result_limit)
    rest = [] if good else pick(described, 3)
    for g, *_ in [*good, *rest, (hold_group,)]:
        replay(g['rec'][0], g['rec'][3] if g['rec'][3] is not None else horizon_eval(g['rec'][0])[0])
    strategies = [d for _, d, *_ in good]
    # Phase 3E.1: audit only the shown strategies, reusing this engine, HOLD and caches.
    audit_started = perf_counter()
    if good:
        later_rosters = [frozenset(p.site_player_id for p in inputs['rosters'][opponent[w.number]]) for w in later]
        all_players = dict(players)
        for w in later:
            for p in inputs['rosters'][opponent[w.number]]:
                all_players.setdefault(p.site_player_id, p)
        auditor = Auditor(SimpleNamespace(
            labels=LABELS, inputs=inputs, weight=weight, schedule=schedule, as_of=as_of, snapshot_day=snapshot_day,
            engine=engine, players=players, all_players=all_players, projections=projections,
            initial_ids=initial_ids, droppable=droppable, budgets=budgets, drop_ok=drop_ok,
            weeks=weeks, week_days=week_days, later_days=later_days, later_numbers=[w.number for w in later],
            opponent=opponent, opponent_rosters=opponent_rosters, later_opponent_rosters=later_rosters,
            all_opponent_ids=frozenset().union(*opponent_rosters, *later_rosters),
            candidate_ids=frozenset(s[2] for g, *_ in good for s in g['rec'][0]),
            actual=actual, current_volumes=current_volumes, scales=scales, hold_h=hold_h, hold_s=hold_s,
            horizon_eval=horizon_eval, season=season, assess=assess, tau=tau, replay=replay,
            season_value=season_value, pool=pool, pool_value=pool_value))
        if set(auditor.c.later_numbers) & {w.number for w in weeks}:
            raise AssertionError('Horizon and rest-of-season periods overlap')
        for g, d, *_ in good:
            d['audit'] = auditor.audit(g['rec'], d)
    audit_seconds = round(perf_counter() - audit_started, 2)
    hold = {**describe(hold_group['rec']), 'rank_value': 0.0, 'similar_options': []}
    closest = []
    for g, d, *_ in rest:
        d['why_not'] = why_not(g['rec'][2], d)
        closest.append(d)
    if strategies:
        top = strategies[0]
        moves = ', '.join(f"+{m['add_name']} / −{m['drop_name']}" for m in top['moves'])
        benefit = (f"{top['category_points_gained']:+g} projected category wins over the next {horizon} scoring "
                   f"period{'s' if horizon > 1 else ''}" if top['category_points_gained'] > 0 else
                   f"no short-term change, margin score {top['season']['margin_score_delta']:+.2f} over the rest of the season")
        headline = (f"{len(strategies)} worthwhile strateg{'y' if len(strategies) == 1 else 'ies'}. "
                    f"Best: {top['kind'].lower()}, {moves} ({benefit}).")
    else:
        headline = ('HOLD is the best choice. No available move adds at least '
                    f'{config.recommend_threshold:g} net projected category points without costing rest-of-season value.')
    games_in = {pid: sum(1 for d in horizon_days if engine.tipoff(pid, d)) for pid in ranked[:10]}
    return {
        'team_id': team_id, 'team_name': inputs['teams'][team_id], 'as_of_utc': base['as_of_utc'],
        'capture_at_utc': audit['snapshot_at_utc'], 'hashtag_weight': weight,
        'current_week': first, 'horizon': horizon, 'remaining_adds': first_budget,
        'horizon_weeks': [{'week': w.number, 'start_date': week_days[wi][0].isoformat(), 'end_date': w.end.isoformat(),
                           'days_remaining': len(week_days[wi]), 'opponent_name': inputs['teams'][opponent[w.number]],
                           'projected': hold_h[wi]['score'], 'acquisitions_available': budgets[wi]}
                          for wi, w in enumerate(weeks)],
        'verdict': 'act' if strategies else 'hold', 'headline': headline,
        'strategies': strategies, 'hold': hold, 'closest_alternatives': closest,
        'evidence_reasons': reasons,
        'diagnostics': {
            'pool': {**dict(funnel), 'excluded': dict(sorted(excluded.items())), 'eligible_with_games': len(pool),
                     'single_moves_searched': len(pool), 'carried_into_combinations': len(expand),
                     'upgrade_candidates_carried': len(upgrades),
                     'top_screen': [{'site_player_id': p, 'name': players[p].name, 'nba_team': players[p].nba_team,
                                     'positions': list(players[p].eligible_positions), 'status': statuses.get(p, ''),
                                     'horizon_games': games_in[p], 'screen_value': round(screen[p][0], 3),
                                     'category_points_if_added': screen[p][1], 'starts_if_added': screen[p][2]}
                                    for p in ranked[:10]],
                     'upgrade_candidates': [{'name': players[p].name, 'season_value': round(pool_value[p], 1)}
                                            for p in upgrades]},
            'roster': roster_rows,
            'search': {'evaluations': evaluations, 'depth_reached': depth_reached, 'beam_width': config.beam_width,
                       'truncated': truncated, 'transaction_sets': len(best_tx), 'distinct_add_sets': len(groups) - 1,
                       'ideas_ranked': len(described),
                       'season_rosters_evaluated': len(season_cache), 'day_cache_entries': len(engine.cache),
                       'day_cache_hits': engine.hits, 'audit_seconds': audit_seconds, 'elapsed_seconds': round(perf_counter() - started, 2),
                       'local_search': {'rounds': local_rounds, 'evaluations': local_evaluations,
                                        'improvements': local_improvements, 'seeds': config.local_seeds,
                                        'budget': config.local_evaluations,
                                        'stopped_by_clock': perf_counter() > started + config.search_seconds,
                                        'candidate_adds': len(local_adds), 'workers': config.workers},
                       'heuristic': 'Every first move is scored; multi-move plans use a parallel beam, then local improvement of the best plans until no single change helps or the time budget ends.'}},
        'method': {'objective': ('horizon category points + soft_weight × horizon margin score + rest-of-season '
                                 'term − acquisition_cost per move'),
                   'rank_value': 'value − fragile_flip_discount per fragile flip; recommended at recommend_threshold or above',
                   'season_term': ('gains × season_gain_weight; losses beyond negligible_season_loss × '
                                   'season_loss_weight; smaller losses count as zero'),
                   'margin_score': 'Sum of logistic category margins scaled by league spread. A ranking aid, not a probability.',
                   **asdict(config)},
        'assumptions': ['Future free-agent availability, ownership and waiver claims are conditional.',
                        'A dropped player is never assumed to be available again.',
                        'Unverified effective times are model boundaries, not Yahoo transaction timestamps.',
                        'Players listed O or NA on Yahoo are excluded from adds; synchronized out/inactive roster statuses suppress only imminent games, with later GP projections retained.']}
