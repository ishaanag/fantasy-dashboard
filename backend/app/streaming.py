"""Bounded, deterministic waiver plans across Yahoo scoring periods."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from time import perf_counter

from .deterministic import forecast
from .phase1 import CATEGORIES, EASTERN, VOLUMES, RosterPlayer, _dated_team_totals, _lineup_score, resolve_projection
from .schedule import FantasyWeek, cup_window_provisional
from .simulator import _expected_with_actual, category_result
from .waiver import _evidence_reasons, _margin, _scales, _smooth, _utc, pool_audit


@dataclass(frozen=True)
class SearchConfig:
    beam_width: int = 12
    candidate_limit: int = 6
    drop_limit: int = 4
    retention_penalty: float = .15
    acquisition_cost: float = .25
    terminal_value_weight: float = 1.0
    result_limit: int = 6
    evidence_max_age_minutes: int = 15
    max_generated: int = 12000

    def __post_init__(self):
        if min(self.beam_width, self.candidate_limit, self.drop_limit, self.result_limit) < 1:
            raise ValueError('Search limits must be positive')
        if any(not isfinite(value) or value < 0 for value in (self.retention_penalty,
                    self.acquisition_cost, self.terminal_value_weight)):
            raise ValueError('Search costs must be finite and nonnegative')
        if self.evidence_max_age_minutes < 1:
            raise ValueError('Evidence freshness window must be positive')
        if self.max_generated < 1:
            raise ValueError('Generated-state budget must be positive')


@dataclass(frozen=True)
class StrategyPolicy:
    mode: str = 'streaming'
    protected_site_ids: tuple[str, ...] | None = None
    streaming_slot_site_ids: tuple[str, ...] | None = None
    future_win_loss_tolerance: float = 0.0
    future_margin_deterioration_tolerance: float = 0.5
    retained_value_loss_tolerance: float = 0.0

    def __post_init__(self):
        if self.mode not in {'streaming', 'permanent'}:
            raise ValueError('Choose streaming or permanent mode')
        if any(not isfinite(v) or v < 0 for v in (self.future_win_loss_tolerance,
                self.future_margin_deterioration_tolerance, self.retained_value_loss_tolerance)):
            raise ValueError('Strategy tolerances must be finite and nonnegative')


def suggest_roster_policy(inputs, team_id, weight, as_of, *, protected_site_ids=None,
                          streaming_slot_site_ids=None, slot_count=2):
    """Suggest a small replaceable tail from remaining games and position scarcity."""
    roster = inputs['rosters'][team_id]
    ids = {p.site_player_id for p in roster}
    for chosen in (protected_site_ids, streaming_slot_site_ids):
        if chosen is not None and not set(chosen) <= ids:
            raise ValueError('Roster control includes a player outside the authenticated roster')
    replacement = {pos: 0.0 for pos in ('PG', 'SG', 'SF', 'PF', 'C')}
    for candidate in inputs.get('site_players', ()):
        if candidate.fantasy_team_id or candidate.availability_label not in {'FA', 'W'}:
            continue
        sources = inputs['projections'].get(candidate.site_player_id)
        if not sources:
            continue
        try:
            score = max(0.0, _lineup_score(resolve_projection(sources, weight)['values']))
        except ValueError:
            continue
        for pos in set(candidate.eligible_positions) & replacement.keys():
            replacement[pos] = max(replacement[pos], score)
    end = inputs['weeks'][-1].end
    values = {}
    for p in roster:
        projection = resolve_projection(inputs['projections'][p.site_player_id], weight)
        games = len(inputs['schedule'].games_for(p.nba_team, as_of.astimezone(EASTERN).date(), end, as_of=as_of))
        positions = set(p.eligible_positions) & {'PG', 'SG', 'SF', 'PF', 'C'}
        score = max(0, _lineup_score(projection['values']))
        scarcity = 1 + .12 * max((max(0, score-replacement[pos]) / max(1, score)
                                 for pos in positions), default=0)
        values[p.site_player_id] = score * games * scarcity
    eligible = [p.site_player_id for p in roster if p.assigned_slot != 'IL']
    ordered = sorted(eligible, key=lambda site_id: (values[site_id], site_id))
    suggested_slots = tuple(ordered[:slot_count])
    slots = tuple(streaming_slot_site_ids) if streaming_slot_site_ids is not None else suggested_slots
    if protected_site_ids is None:
        replaceable_count = min(max(0, len(eligible)-1), slot_count)
        unprotected = set(ordered[:replaceable_count])
        suggested_core = {p.site_player_id for p in roster
            if p.site_player_id not in unprotected and p.assigned_slot != 'IL'}
        starters = [p.site_player_id for p in roster if p.assigned_slot not in {'BN', 'IL'}]
        if starters and all(site_id in suggested_core for site_id in starters):
            suggested_core.remove(min(starters, key=lambda site_id: (values[site_id], site_id)))
        protected = tuple(sorted(suggested_core))
    else:
        protected = tuple(protected_site_ids)
    if set(protected) & set(slots):
        raise ValueError('Protected core and streaming slots must be disjoint')
    return {'protected_site_ids': protected, 'streaming_slot_site_ids': slots,
            'suggested_streaming_slot_site_ids': suggested_slots,
            'replacement_scores_by_position': replacement,
            'retained_player_values': values,
            'roster': [{'site_player_id': p.site_player_id, 'name': p.name,
                        'retained_value': round(values[p.site_player_id], 3),
                        'assigned_slot': p.assigned_slot} for p in roster]}


def _days(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def _categories(v):
    result = {key: v[key] for key in CATEGORIES if key not in {'fg_pct', 'ft_pct'}}
    result['fg_pct'] = v['fgm'] / v['fga'] if v['fga'] else None
    result['ft_pct'] = v['ftm'] / v['fta'] if v['fta'] else None
    return result


def _shortlist(inputs, team_id, weight, as_of, week, config, evidence, actual_state, matchup_path):
    """Screen projectable schedule specialists; the search checks each actual transition."""
    audit = pool_audit(inputs, weight)
    roster = inputs['rosters'][team_id]
    protected = set(evidence.get('cannot_cut_site_ids', ())) if evidence else set()
    locked = set(evidence.get('locked_site_ids', ())) if evidence else set()
    projections = {p.site_player_id: resolve_projection(inputs['projections'][p.site_player_id], weight) for p in roster}
    drops = sorted((p for p in roster if p.site_player_id not in protected | locked and p.assigned_slot != 'IL'),
                   key=lambda p: (_lineup_score(projections[p.site_player_id]['values']), p.site_player_id))[:config.drop_limit]
    if not drops:
        return [], [], projections, audit
    by_id = {p.site_player_id: p for p in inputs['site_players']}
    candidates = []
    for row in audit['rows']:
        if not row['projectable'] or row['availability'] not in {'FA', 'W'} or row['owner_team_id']:
            continue
        p = by_id[row['site_player_id']]
        if p.nba_team not in inputs['schedule'].team_counts:
            continue
        games = len(inputs['schedule'].games_for(p.nba_team, as_of.astimezone(EASTERN).date(), week.end, as_of=as_of))
        if not games:
            continue
        projection = resolve_projection(inputs['projections'][p.site_player_id], weight)
        projections[p.site_player_id] = projection
        candidates.append((p, games, projection['values']))
    # Reserve specialist slots before filling with the general lineup proxy.
    selected = {}
    keys = ('three_ptm', 'pts', 'reb', 'ast', 'stl', 'blk')
    for key in keys:
        top = sorted(candidates, key=lambda row: (-row[1]*row[2][key], row[0].site_player_id))
        for p, _, _ in top[:max(1, config.candidate_limit // (len(keys)+1))]:
            if len(selected) < config.candidate_limit:
                selected[p.site_player_id] = p
    for p, games, v in sorted(candidates, key=lambda row: (-row[1]*_lineup_score(row[2]), row[0].site_player_id)):
        if len(selected) >= config.candidate_limit:
            break
        selected[p.site_player_id] = p
    # The old Phase 3A call recalculated every remaining fantasy week for every
    # pair, then the search checked the same transitions again. Its legal
    # constraints are applied below at each chronological action.
    adds = [selected[p] for p in sorted(selected)]
    return adds, drops, projections, audit


def optimize_week(inputs, *, team_id=None, weight=.5, as_of=None, actual_state=None,
                  evidence=None, matchup_path=None, config=SearchConfig(), now=None):
    """Plan at most the remaining four weekly adds; future availability stays conditional."""
    started = perf_counter()
    team_id = team_id or inputs['league']['my_team_id']
    if team_id != inputs['league']['my_team_id']:
        raise ValueError('Use the authenticated Yahoo team')
    as_of = as_of or _utc(inputs['availability']['captured_at_utc'])
    if as_of.tzinfo is None:
        raise ValueError('As-of time must include a timezone')
    as_of = as_of.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Current time must include a timezone')
    now = now.astimezone(timezone.utc)
    base = forecast(inputs, matchup_path=matchup_path, hashtag_weight=weight, as_of=as_of, actual_state=actual_state)
    week_number = base['actual_completed_weeks'] + 1
    if week_number > 18:
        raise ValueError('Weekly planner covers the regular season only')
    week = inputs['weeks'][week_number-1]
    today = as_of.astimezone(EASTERN).date()
    if not week.start <= today <= week.end:
        raise ValueError('As-of date is outside the current Yahoo scoring period')
    matchup = next(m for m in base['weekly_matchups'] if m['week'] == week_number and team_id in m['teams'])
    opponent = next(t for t in matchup['teams'] if t != team_id)
    opposing = matchup['totals'][opponent]['categories']
    baseline = matchup['totals'][team_id]
    before = matchup['score'] if matchup['teams'][0] == team_id else [matchup['score'][1], matchup['score'][0], matchup['score'][2]]
    scales = _scales(base)
    reasons = _evidence_reasons(inputs, evidence, team_id, week_number, as_of)
    if evidence and 'captured_at_utc' in evidence:
        try:
            age = now - _utc(evidence['captured_at_utc'])
            if age < timedelta() or age > timedelta(minutes=config.evidence_max_age_minutes):
                reasons.append('transaction_evidence_not_fresh_for_action')
        except (TypeError, ValueError):
            reasons.append('transaction_evidence_time_invalid')
    remaining = max(0, week.max_adds - evidence['adds_used']) if evidence and isinstance(evidence.get('adds_used'), int) else 0
    initial = {p.site_player_id: p for p in inputs['rosters'][team_id]}
    if remaining:
        adds, drops, projections, audit = _shortlist(inputs, team_id, weight, as_of, week, config,
                                                    evidence, actual_state, matchup_path)
    else:
        adds, drops, projections, audit = [], [], {}, pool_audit(inputs, weight)
    for p in initial.values():
        projections.setdefault(p.site_player_id, resolve_projection(inputs['projections'][p.site_player_id], weight))
    players = {**initial, **{p.site_player_id: RosterPlayer(team_id, p.site_player_id, p.name, 'BN',
                      p.eligible_positions, p.nba_team) for p in adds}}
    days = list(_days(today, week.end))
    initial_ids = frozenset(initial)
    protected = set(evidence.get('cannot_cut_site_ids', ())) if evidence else set()
    locked = set(evidence.get('locked_site_ids', ())) if evidence and not reasons else set()
    claims = set(evidence.get('active_claim_site_ids', ())) if evidence and not reasons else set()
    allowed_initial_drops = {p.site_player_id for p in drops}
    snapshot_day = date.fromisoformat(inputs['availability']['lineup_date'])
    day_cache = {}
    cache_hits = 0

    def effective(day, player):
        timing = evidence.get('timing_by_site_id', {}).get(player.site_player_id) if evidence else None
        if timing and not reasons and day == today:
            try:
                stamp = _utc(timing['effective_at_utc'])
                if stamp >= as_of and stamp.astimezone(EASTERN).date() == day:
                    return stamp, False
            except (KeyError, TypeError, ValueError):
                pass
        # Model boundary, never a claimed Yahoo transaction timestamp.
        stamp = as_of if day == today else datetime.combine(day, time.min, EASTERN).astimezone(timezone.utc)
        return stamp + (timedelta(days=1) if player.availability_label == 'W' else timedelta()), True

    def evaluate(steps):
        nonlocal cache_hits
        since = {add: stamp for _, add, _, stamp, _ in steps}
        until = {drop: stamp for _, _, drop, stamp, _ in steps}
        roster = [players[p] for p in sorted(initial_ids | set(since))]
        projected = [(p, projections[p.site_player_id]) for p in roster]
        volumes = {key: 0.0 for key in VOLUMES}
        daily = []
        for day in days:
            # Only players who can contribute on this date affect the day result.
            # This lets future transaction branches reuse unchanged prior days.
            key = (day, tuple(p.site_player_id for p in roster
                if (p.nba_team, day) in inputs['schedule'].team_dates
                and inputs['schedule'].team_dates[(p.nba_team, day)].tipoff_utc > as_of
                and inputs['schedule'].team_dates[(p.nba_team, day)].tipoff_utc >= since.get(p.site_player_id, as_of)
                and inputs['schedule'].team_dates[(p.nba_team, day)].tipoff_utc < until.get(p.site_player_id, datetime.max.replace(tzinfo=timezone.utc))))
            if key in day_cache:
                raw, day_row = day_cache[key]
                cache_hits += 1
            else:
                mini = FantasyWeek(week.number, day, day, week.phase)
                raw, _, rows = _dated_team_totals(roster, projected, inputs['schedule'], mini, as_of,
                    snapshot_day, available_from_by_id=since, available_until_by_id=until, injury_inputs=inputs)
                day_row = rows[0]
                day_cache[key] = raw, day_row
            for name in VOLUMES:
                volumes[name] += raw['shooting_volumes'][name] if name in raw['shooting_volumes'] else raw['categories'][name]
            daily.append({'date': day.isoformat(), 'usable_starts': len(day_row['modeled_starts']),
                          'started_site_ids': [s['site_player_id'] for s in day_row['modeled_starts']]})
        raw = {'categories': _categories(volumes), 'shooting_volumes': {k: volumes[k] for k in ('fgm','fga','ftm','fta')}}
        if actual_state:
            raw = _expected_with_actual(raw, actual_state.get('current_week_volumes', {}).get(team_id))
        margins = {cat: _margin(raw['categories'], opposing, cat) for cat in CATEGORIES}
        score = list(category_result(raw['categories'], opposing)[:3])
        return raw, margins, score, daily

    def rank(steps, result):
        score = result[2]
        lost = {d for _, _, d, _, _ in steps if d in initial_ids}
        incoming = [_lineup_score(projections[a]['values']) for _, a, _, _, _ in steps]
        scale = max((_lineup_score(projections[p]['values']) for p in initial_ids), default=1) or 1
        retention_loss = sum(max(0, _lineup_score(projections[d]['values'])-min(incoming, default=0))/scale for d in lost)
        points = score[0]+.5*score[2]
        cost = config.acquisition_cost*len(steps)+config.retention_penalty*retention_loss
        return (points-cost, points, _smooth(result[1], scales), -len(steps)), retention_loss

    def key(steps):
        return tuple(sorted((a, d, stamp.isoformat()) for _, a, d, stamp, _ in steps))

    hold = evaluate(())
    complete = {(): (hold, initial_ids)}
    beam = [((), initial_ids, 0)]
    generated = duplicates = 0
    for _ in range(min(remaining, 4)):
        expanded = {}
        for steps, roster_ids, first_day in beam:
            used = {s[1] for s in steps}
            for index in range(first_day, len(days)):
                day = days[index]
                for player in adds:
                    add_id = player.site_player_id
                    if add_id in used or add_id in roster_ids or add_id in claims:
                        continue
                    stamp, assumed = effective(day, player)
                    if stamp.astimezone(EASTERN).date() > week.end:
                        continue
                    if steps and stamp < steps[-1][3]:
                        continue  # no subsequent roster action before a pending add is effective
                    for drop_id in sorted(roster_ids):
                        if drop_id in initial_ids and drop_id not in allowed_initial_drops:
                            continue
                        if drop_id in protected | locked:
                            continue
                        drop = players[drop_id]
                        if drop.assigned_slot == 'IL' and sum(players[p].assigned_slot != 'IL' for p in roster_ids) >= 14:
                            continue
                        if day == today and drop.assigned_slot not in {'BN','IL'}:
                            game = inputs['schedule'].team_dates.get((drop.nba_team, day))
                            if game and game.tipoff_utc <= stamp:
                                continue
                        new_steps = steps + ((day, add_id, drop_id, stamp, assumed),)
                        state_key = key(new_steps)
                        generated += 1
                        if state_key in expanded:
                            duplicates += 1
                            continue
                        new_roster = frozenset((roster_ids - {drop_id}) | {add_id})
                        expanded[state_key] = (new_steps, new_roster, index, evaluate(new_steps))
        if not expanded:
            break
        ordered = sorted(expanded.values(), key=lambda x: (tuple(-v for v in rank(x[0], x[3])[0]), key(x[0])))
        beam = [(s, r, i) for s, r, i, _ in ordered[:config.beam_width]]
        for s, r, _, result in ordered[:max(config.beam_width, config.result_limit)]:
            complete[s] = result, r

    old_margins = {cat: _margin(baseline['categories'], opposing, cat) for cat in CATEGORIES}
    old_outcomes = category_result(baseline['categories'], opposing)[3]
    plans = []
    for steps, (result, roster_ids) in complete.items():
        raw, margins, score, daily = result
        order, retention_loss = rank(steps, result)
        new_outcomes = category_result(raw['categories'], opposing)[3]
        missing = list(reasons) if steps else []
        if steps:
            if any(day > today for day, *_ in steps):
                missing.append('future_availability_conditional')
            if any(next(p for p in adds if p.site_player_id == add).availability_label == 'W'
                   for _, add, _, _, _ in steps):
                missing.append('waiver_claim_success_unverified')
            if any(assumed for *_, assumed in steps):
                missing.append('effective_timing_unverified')
            if reasons or any(day > today for day, *_ in steps):
                missing.append('candidate_status_and_ownership_must_be_refreshed')
        missing = sorted(set(missing))
        actions = [{'date': day.isoformat(), 'add_site_player_id': add, 'add_name': players[add].name,
                    'drop_site_player_id': drop, 'drop_name': players[drop].name,
                    'effective_at_utc': None if assumed else stamp.isoformat().replace('+00:00','Z'),
                    'modeled_effective_date': stamp.astimezone(EASTERN).date().isoformat(),
                    'timing_assumed': assumed,
                    'future_availability_conditional': day > today or
                        next(p for p in adds if p.site_player_id == add).availability_label == 'W'}
                   for day, add, drop, stamp, assumed in steps]
        plans.append({'state': 'HOLD' if not steps else 'Provisional' if missing else 'Actionable',
                      'missing_verification': missing, 'steps': actions, 'acquisitions': len(steps),
                      'acquisitions_used_after': evidence['adds_used']+len(steps) if evidence else None,
                      'remaining_adds': remaining-len(steps), 'before': before, 'after': score,
                      'delta_category_points': score[0]+.5*score[2]-before[0]-.5*before[2],
                      'category_wins_gained': [cat for cat in CATEGORIES if old_outcomes[cat] != 'W' and new_outcomes[cat] == 'W'],
                      'category_wins_lost': [cat for cat in CATEGORIES if old_outcomes[cat] == 'W' and new_outcomes[cat] != 'W'],
                      'smooth_margin_score': _smooth(margins, scales),
                      'retention_penalty': config.retention_penalty*retention_loss,
                      'acquisition_cost': config.acquisition_cost*len(steps),
                      'net_category_points': order[0],
                      'ranking_explanation': 'Projected category points minus acquisition and retained-player costs; category margins break ties only.',
                      'long_term_warning': 'Valuable roster player dropped; future weeks are not optimized.' if retention_loss > .01 else None,
                      'categories': {cat: {'before': baseline['categories'][cat], 'after': raw['categories'][cat],
                                   'directed_margin_before': old_margins[cat], 'directed_margin_after': margins[cat],
                                   'result_before': old_outcomes[cat], 'result_after': new_outcomes[cat]}
                                   for cat in CATEGORIES},
                      'daily_starts': daily,
                      'final_roster': [{'site_player_id': p, 'name': players[p].name,
                                        'assigned_slot': players[p].assigned_slot} for p in sorted(roster_ids)],
                      '_order': order, '_steps': steps})
    plans.sort(key=lambda p: (tuple(-x for x in p['_order']),
                 tuple((s['date'], s['add_site_player_id'], s['drop_site_player_id']) for s in p['steps'])))
    chosen = plans[:config.result_limit]
    for count in range(5):
        option = next((p for p in plans if p['acquisitions'] == count), None)
        if option and option not in chosen:
            chosen.append(option)
    cache_entries, prior_hits = len(day_cache), cache_hits
    for p in chosen:
        # Replay selected plans with empty day cache through Phase 2's lineup
        # engine, including the original HOLD state.
        day_cache.clear()
        replay = evaluate(p['_steps'])
        if replay[2] != p['after'] or any(
                (replay[0]['categories'][cat] is None) != (p['categories'][cat]['after'] is None) or
                (replay[0]['categories'][cat] is not None and
                 abs(replay[0]['categories'][cat] - p['categories'][cat]['after']) > 1e-9)
                for cat in CATEGORIES):
            raise AssertionError('Streaming plan replay diverged')
        del p['_steps']
        del p['_order']
    return {'team_id': team_id, 'team_name': inputs['teams'][team_id], 'opponent_team_id': opponent,
            'opponent_name': inputs['teams'][opponent], 'week': week_number, 'week_end': week.end.isoformat(),
            'as_of_utc': base['as_of_utc'], 'capture_at_utc': audit['snapshot_at_utc'],
            'remaining_adds': remaining, 'evidence_reasons': reasons,
            'assumptions': ['Future free-agent availability is conditional.',
                           'Unverified effective boundaries are hypothetical modeling times, not Yahoo transaction timestamps.',
                           'Waiver arrivals use a hypothetical 24-hour delay; claim success and processing time are unverified.'],
            'search': {'beam_width': config.beam_width, 'candidate_limit': config.candidate_limit,
                       'drop_limit': config.drop_limit, 'shortlisted_adds': len(adds), 'shortlisted_drops': len(drops),
                       'generated': generated, 'duplicate_states_pruned': duplicates,
                       'day_cache_entries': cache_entries, 'day_cache_hits': prior_hits,
                       'replayed_plans': len(chosen),
                       'elapsed_seconds': round(perf_counter()-started, 3),
                       'heuristic': 'Beam search; optimality is not guaranteed outside exhaustive fixtures.'},
            'plans': chosen}


def optimize_horizon(inputs, *, horizon=2, team_id=None, weight=.5, as_of=None,
                     actual_state=None, evidence=None, matchup_path=None,
                     config=SearchConfig(), now=None, strategy_policy: StrategyPolicy | None = None):
    """Search one evolving roster across 1–3 verified Yahoo scoring periods.

    The complete sequence is evaluated against each scheduled opponent. Category
    points pay for acquisitions and permanent losses; smooth margins only guide
    equal-net-value branches. Future ownership and processing remain conditional.
    """
    if horizon not in (1, 2, 3):
        raise ValueError('Choose a horizon of 1, 2 or 3 weeks')
    started = perf_counter()
    team_id = team_id or inputs['league']['my_team_id']
    if team_id != inputs['league']['my_team_id']:
        raise ValueError('Use the authenticated Yahoo team')
    as_of = as_of or _utc(inputs['availability']['captured_at_utc'])
    now = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None or now.tzinfo is None:
        raise ValueError('As-of and current times must include a timezone')
    as_of = as_of.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    base = forecast(inputs, matchup_path=matchup_path, hashtag_weight=weight,
                    as_of=as_of, actual_state=actual_state)
    first = base['actual_completed_weeks'] + 1
    if first + horizon - 1 > 18:
        raise ValueError('Horizon extends beyond the regular season')
    weeks = inputs['weeks'][first-1:first-1+horizon]
    today = as_of.astimezone(EASTERN).date()
    if not weeks[0].start <= today <= weeks[0].end or any(
            a.end + timedelta(days=1) != b.start or b.number != a.number + 1
            for a, b in zip(weeks, weeks[1:])):
        raise ValueError('Yahoo week boundaries are not verified and contiguous')
    matchups = [next(m for m in base['weekly_matchups']
                     if m['week'] == week.number and team_id in m['teams']) for week in weeks]
    opponents = [next(t for t in m['teams'] if t != team_id) for m in matchups]
    opposing = [m['totals'][opp]['categories'] for m, opp in zip(matchups, opponents)]
    baselines = [m['totals'][team_id] for m in matchups]
    before = [m['score'] if m['teams'][0] == team_id else
              [m['score'][1], m['score'][0], m['score'][2]] for m in matchups]
    scales = _scales(base)
    reasons = _evidence_reasons(inputs, evidence, team_id, first, as_of)
    if evidence and 'captured_at_utc' in evidence:
        try:
            age = now - _utc(evidence['captured_at_utc'])
            if age < timedelta() or age > timedelta(minutes=config.evidence_max_age_minutes):
                reasons.append('transaction_evidence_not_fresh_for_action')
        except (TypeError, ValueError):
            reasons.append('transaction_evidence_time_invalid')
    first_budget = max(0, weeks[0].max_adds-evidence['adds_used']) if evidence and isinstance(evidence.get('adds_used'), int) else 0
    budgets = (first_budget, *(w.max_adds for w in weeks[1:]))
    initial = {p.site_player_id: p for p in inputs['rosters'][team_id]}
    initial_ids = frozenset(initial)
    controls = (suggest_roster_policy(inputs, team_id, weight, as_of,
        protected_site_ids=strategy_policy.protected_site_ids,
        streaming_slot_site_ids=strategy_policy.streaming_slot_site_ids)
        if strategy_policy else None)
    # Screening covers the complete horizon, including schedule specialists.
    span = FantasyWeek(first, today, weeks[-1].end, 'rolling_horizon')
    adds, drops, projections, audit = _shortlist(inputs, team_id, weight, as_of, span,
                                                config, evidence, actual_state, matchup_path)
    for p in initial.values():
        projections.setdefault(p.site_player_id,
                               resolve_projection(inputs['projections'][p.site_player_id], weight))
    players = {**initial, **{p.site_player_id: RosterPlayer(team_id, p.site_player_id,
               p.name, 'BN', p.eligible_positions, p.nba_team) for p in adds}}
    protected = set(evidence.get('cannot_cut_site_ids', ())) if evidence else set()
    if controls:
        protected.update(controls['protected_site_ids'])
    locked = set(evidence.get('locked_site_ids', ())) if evidence else set()
    claims = set(evidence.get('active_claim_site_ids', ())) if evidence else set()
    allowed_initial_drops = {p.site_player_id for p in drops}
    if controls and strategy_policy.mode == 'streaming':
        allowed_initial_drops &= set(controls['streaming_slot_site_ids'])
    snapshot_day = date.fromisoformat(inputs['availability']['lineup_date'])
    week_days = [list(_days(max(today, w.start), w.end)) for w in weeks]
    # A move without an eligible future game cannot improve a matchup. For
    # waivers, include the day before a game to model a 24-hour arrival.
    action_days = []
    for wi, days in enumerate(week_days):
        useful = {days[0]}
        for p in adds:
            for day in days:
                if (p.nba_team, day) in inputs['schedule'].team_dates:
                    useful.add(day)
                    if p.availability_label == 'W' and day-timedelta(days=1) in days:
                        useful.add(day-timedelta(days=1))
        action_days.extend((wi, day) for day in sorted(useful))
    day_cache = {}
    cache_hits = 0

    def effective(day, player):
        timing = evidence.get('timing_by_site_id', {}).get(player.site_player_id) if evidence else None
        if day == today and timing and not reasons:
            try:
                stamp = _utc(timing['effective_at_utc'])
                if stamp >= as_of and stamp.astimezone(EASTERN).date() == day:
                    return stamp, False
            except (KeyError, TypeError, ValueError):
                pass
        stamp = as_of if day == today else datetime.combine(day, time.min, EASTERN).astimezone(timezone.utc)
        return stamp + (timedelta(days=1) if player.availability_label == 'W' else timedelta()), True

    def evaluate(steps):
        nonlocal cache_hits
        since = {add: stamp for _, _, add, _, stamp, _ in steps}
        until = {drop: stamp for _, _, _, drop, stamp, _ in steps}
        roster = [players[p] for p in sorted(initial_ids | set(since))]
        projected = [(p, projections[p.site_player_id]) for p in roster]
        out = []
        for wi, week in enumerate(weeks):
            volumes = {key: 0.0 for key in VOLUMES}
            daily = []
            for day in week_days[wi]:
                active = tuple(p.site_player_id for p in roster if
                    (p.nba_team, day) in inputs['schedule'].team_dates and
                    inputs['schedule'].team_dates[(p.nba_team, day)].tipoff_utc > as_of and
                    inputs['schedule'].team_dates[(p.nba_team, day)].tipoff_utc >= since.get(p.site_player_id, as_of) and
                    inputs['schedule'].team_dates[(p.nba_team, day)].tipoff_utc < until.get(p.site_player_id, datetime.max.replace(tzinfo=timezone.utc)))
                key = (day, active)
                if key in day_cache:
                    raw, row = day_cache[key]
                    cache_hits += 1
                else:
                    mini = FantasyWeek(week.number, day, day, week.phase)
                    raw, _, rows = _dated_team_totals(roster, projected, inputs['schedule'], mini,
                        as_of, snapshot_day, available_from_by_id=since, available_until_by_id=until, injury_inputs=inputs)
                    row = rows[0]
                    day_cache[key] = raw, row
                for name in VOLUMES:
                    volumes[name] += raw['shooting_volumes'][name] if name in raw['shooting_volumes'] else raw['categories'][name]
                daily.append({'date': day.isoformat(), 'usable_starts': len(row['modeled_starts']),
                              'started_site_ids': [s['site_player_id'] for s in row['modeled_starts']]})
            raw = {'categories': _categories(volumes),
                   'shooting_volumes': {k: volumes[k] for k in ('fgm','fga','ftm','fta')}}
            if wi == 0 and actual_state:
                raw = _expected_with_actual(raw, actual_state.get('current_week_volumes', {}).get(team_id))
            margins = {cat: _margin(raw['categories'], opposing[wi], cat) for cat in CATEGORIES}
            score = list(category_result(raw['categories'], opposing[wi])[:3])
            out.append((raw, margins, score, daily))
        return out

    def rank(steps, results):
        points = sum(r[2][0]+.5*r[2][2] for r in results)
        if strategy_policy:
            smooth = sum(_smooth(r[1], scales) for r in results)
            return (points, smooth, -len(steps), smooth), 0.0
        lost = {d for _, _, _, d, _, _ in steps if d in initial_ids}
        incoming = [_lineup_score(projections[a]['values']) for _, _, a, _, _, _ in steps]
        scale = max((_lineup_score(projections[p]['values']) for p in initial_ids), default=1) or 1
        retention = sum(max(0, _lineup_score(projections[d]['values'])-min(incoming, default=0))/scale for d in lost)
        cost = config.acquisition_cost*len(steps)+config.retention_penalty*retention
        smooth = sum(_smooth(r[1], scales) for r in results)
        return (points-cost, points, smooth, -len(steps)), retention

    hold = evaluate(())
    complete = {(): (hold, initial_ids)}
    beam = [((), initial_ids, 0)]
    generated = duplicates = 0
    truncated = False
    for _ in range(sum(budgets)):
        expanded = {}
        for steps, roster_ids, first_index in beam:
            used = {s[2] for s in steps}
            counts = [sum(s[0] == wi for s in steps) for wi in range(horizon)]
            for index in range(first_index, len(action_days)):
                wi, day = action_days[index]
                if counts[wi] >= budgets[wi]:
                    continue
                for player in adds:
                    add_id = player.site_player_id
                    if add_id in used or add_id in roster_ids or add_id in claims:
                        continue
                    stamp, assumed = effective(day, player)
                    # A boundary-crossing pending waiver needs provider evidence
                    # for which period is charged; omit it from this model.
                    if stamp.astimezone(EASTERN).date() > weeks[wi].end or (steps and stamp < steps[-1][4]):
                        continue
                    if not any((player.nba_team, future) in inputs['schedule'].team_dates and
                               inputs['schedule'].team_dates[(player.nba_team, future)].tipoff_utc >= stamp
                               for future in _days(day, weeks[-1].end)):
                        continue
                    for drop_id in sorted(roster_ids):
                        if drop_id in initial_ids and drop_id not in allowed_initial_drops:
                            continue
                        if drop_id in protected or (wi == 0 and drop_id in locked):
                            continue
                        drop = players[drop_id]
                        if drop.assigned_slot == 'IL' and sum(players[p].assigned_slot != 'IL' for p in roster_ids) >= 14:
                            continue
                        if day == today and drop.assigned_slot not in {'BN','IL'}:
                            game = inputs['schedule'].team_dates.get((drop.nba_team, day))
                            if game and game.tipoff_utc <= stamp:
                                continue
                        new_steps = steps + ((wi, day, add_id, drop_id, stamp, assumed),)
                        state_key = tuple((s[0], s[2], s[3], s[4]) for s in new_steps)
                        generated += 1
                        if state_key in expanded:
                            duplicates += 1
                            continue
                        new_roster = frozenset((roster_ids-{drop_id}) | {add_id})
                        expanded[state_key] = (new_steps, new_roster, index, evaluate(new_steps))
                        if generated >= config.max_generated:
                            truncated = True
                            break
                    if truncated:
                        break
                if truncated:
                    break
            if truncated:
                break
        if not expanded:
            break
        ordered = sorted(expanded.values(), key=lambda x: (tuple(-v for v in rank(x[0], x[3])[0]),
                        tuple((s[0], s[1], s[2], s[3]) for s in x[0])))
        beam = [(s, r, i) for s, r, i, _ in ordered[:config.beam_width]]
        for s, r, _, result in ordered[:max(config.beam_width, config.result_limit)]:
            complete[s] = result, r
        if truncated:
            break

    future_weeks = inputs['weeks'][first-1+horizon:18]
    future_cup_gap = any(cup_window_provisional(week, inputs['schedule']) for week in future_weeks)
    plans = []
    for steps, (results, roster_ids) in complete.items():
        order, retention = rank(steps, results)
        records = []
        roster_end = set(initial_ids)
        for wi, week in enumerate(weeks):
            for action in steps:
                if action[0] == wi:
                    roster_end.remove(action[3])
                    roster_end.add(action[2])
            raw, margins, score, daily = results[wi]
            old_margins = {cat: _margin(baselines[wi]['categories'], opposing[wi], cat) for cat in CATEGORIES}
            previous = category_result(baselines[wi]['categories'], opposing[wi])[3]
            after_outcomes = category_result(raw['categories'], opposing[wi])[3]
            records.append({'week': week.number, 'start_date': week.start.isoformat(),
                'end_date': week.end.isoformat(), 'is_extended': week.days == 14,
                'cup_gap_provisional': cup_window_provisional(week, inputs['schedule']),
                'opponent_team_id': opponents[wi], 'opponent_name': inputs['teams'][opponents[wi]],
                'before': before[wi], 'after': score,
                'acquisitions': sum(s[0] == wi for s in steps),
                'acquisitions_used_after': (evidence['adds_used'] if wi == 0 and evidence else 0)+sum(s[0] == wi for s in steps),
                'acquisition_limit': week.max_adds,
                'roster_at_end': [{'site_player_id': p, 'name': players[p].name}
                                  for p in sorted(roster_end)],
                'category_wins_gained': [cat for cat in CATEGORIES if previous[cat] != 'W' and after_outcomes[cat] == 'W'],
                'category_wins_lost': [cat for cat in CATEGORIES if previous[cat] == 'W' and after_outcomes[cat] != 'W'],
                'categories': {cat: {'directed_margin_before': old_margins[cat],
                    'directed_margin_after': margins[cat], 'result_before': previous[cat],
                    'result_after': after_outcomes[cat]} for cat in CATEGORIES},
                'daily_starts': daily})
        missing = [] if not steps else list(reasons)
        if any(s[0] > 0 or s[1] > today for s in steps):
            missing.extend(['future_availability_conditional', 'candidate_status_and_ownership_must_be_refreshed'])
        if any(s[5] for s in steps):
            missing.append('effective_timing_unverified')
        if any(next(p for p in adds if p.site_player_id == s[2]).availability_label == 'W' for s in steps):
            missing.append('waiver_claim_success_unverified')
        if any(w['cup_gap_provisional'] for w in records):
            missing.append('cup_schedule_gap')
        if future_cup_gap:
            missing.append('cup_schedule_gap_in_terminal_value')
        actions = [{'week': weeks[wi].number, 'date': day.isoformat(),
                    'add_site_player_id': add, 'add_name': players[add].name,
                    'drop_site_player_id': drop, 'drop_name': players[drop].name,
                    'modeled_effective_date': stamp.astimezone(EASTERN).date().isoformat(),
                    'effective_at_utc': None if assumed else stamp.isoformat().replace('+00:00','Z'),
                    'timing_assumed': assumed, 'future_availability_conditional': wi > 0 or day > today or
                    next(p for p in adds if p.site_player_id == add).availability_label == 'W'}
                   for wi, day, add, drop, stamp, assumed in steps]
        cumulative_before = [sum(w['before'][i] for w in records) for i in range(3)]
        cumulative_after = [sum(w['after'][i] for w in records) for i in range(3)]
        plans.append({'state': 'HOLD' if not steps else 'Provisional' if missing else 'Actionable',
                      'steps': actions, 'weeks': records,
                      'cumulative_before': cumulative_before, 'cumulative_after': cumulative_after,
                      'delta_category_points': cumulative_after[0]+.5*cumulative_after[2]-cumulative_before[0]-.5*cumulative_before[2],
                      'acquisitions': len(steps), 'acquisition_cost': config.acquisition_cost*len(steps),
                      'retention_penalty': config.retention_penalty*retention,
                      'net_category_points': order[0],
                      'smooth_margin_score': order[2],
                      'ranking_explanation': 'Sum of projected category points across scheduled opponents, minus acquisition and retained-player costs; margins break net ties only.',
                      'long_term_warning': 'A dropped roster player has higher projected per-game lineup value than an incoming player.' if retention > .01 else None,
                      'missing_verification': sorted(set(missing)),
                      'final_roster': [{'site_player_id': p, 'name': players[p].name,
                         'assigned_slot': players[p].assigned_slot} for p in sorted(roster_ids)],
                      '_order': order, '_steps': steps})
    plans.sort(key=lambda p: (tuple(-x for x in p['_order']),
                tuple((s['week'], s['date'], s['add_site_player_id']) for s in p['steps'])))
    hold_plan = next(p for p in plans if not p['steps'])
    if strategy_policy:
        # Collapse date permutations before the expensive rest-of-season check.
        # Materially different outcomes remain visible as timing alternatives.
        grouped = {}
        for p in plans:
            sequence = tuple((s['add_site_player_id'], s['drop_site_player_id']) for s in p['steps'])
            if sequence not in grouped:
                p['timing_alternatives'] = []
                grouped[sequence] = p
            else:
                primary = grouped[sequence]
                signature = tuple(tuple(w['categories'][cat]['result_after'] for cat in CATEGORIES)
                                  for w in p['weeks'])
                prior = {tuple(tuple(w['categories'][cat]['result_after'] for cat in CATEGORIES)
                               for w in primary['weeks'])}
                prior.update(tuple(tuple(w['categories'][cat]['result_after'] for cat in CATEGORIES)
                                   for w in alt['weeks']) for alt in primary['timing_alternatives'])
                if signature not in prior:
                    primary['timing_alternatives'].append({'steps': p['steps'], 'weeks': p['weeks'],
                        'delta_category_points': p['delta_category_points']})
        plans = list(grouped.values())
        plans.sort(key=lambda p: (tuple(-x for x in p['_order']),
            tuple((s['week'], s['date'], s['add_site_player_id']) for s in p['steps'])))
    # The final roster is assumed to remain in place after the selected
    # horizon. Recalculate its conditional rest-of-season category results
    # against the actual schedule, only for a bounded set of finalists.
    finalists = plans[:max(18, config.result_limit*3)]
    if hold_plan not in finalists:
        finalists.append(hold_plan)
    future_matchups = {m['week']: m for m in base['weekly_matchups'] if team_id in m['teams']}
    terminal_by_roster = {}
    terminal_evaluations = 0
    asset_pruned = 0
    def roster_value(site_ids):
        value = 0.0
        for site_id in site_ids:
            player = players[site_id]
            games = len(inputs['schedule'].games_for(player.nba_team,
                weeks[-1].end + timedelta(days=1), inputs['weeks'][-1].end, as_of=as_of))
            positions = set(player.eligible_positions) & {'PG', 'SG', 'SF', 'PF', 'C'}
            score = max(0, _lineup_score(projections[site_id]['values']))
            replacement = controls['replacement_scores_by_position'] if controls else {}
            scarcity = 1 + .12 * max((max(0, score-replacement.get(pos, 0)) / max(1, score)
                                     for pos in positions), default=0)
            value += score * games * scarcity
        return value
    original_value = roster_value(initial_ids) if controls else 0.0
    for p in finalists:
        final_ids = tuple(row['site_player_id'] for row in p['final_roster'])
        retained = roster_value(final_ids) if strategy_policy else None
        if strategy_policy and p['steps'] and retained < original_value - strategy_policy.retained_value_loss_tolerance-1e-9:
            p['qualifies'] = False
            asset_pruned += 1
            continue
        if final_ids not in terminal_by_roster:
            projected = [(players[site_id], projections[site_id]) for site_id in final_ids]
            delta = 0.0
            deterioration = 0.0
            future_margin_gain = 0.0
            for week in future_weeks:
                matchup = future_matchups.get(week.number)
                if not matchup or matchup.get('status') == 'actual':
                    continue
                opponent = next(t for t in matchup['teams'] if t != team_id)
                raw, _, _ = _dated_team_totals([row[0] for row in projected], projected,
                    inputs['schedule'], week, as_of, snapshot_day, injury_inputs=inputs)
                score = category_result(raw['categories'], matchup['totals'][opponent]['categories'])
                old = (matchup['score'] if matchup['teams'][0] == team_id else
                       [matchup['score'][1], matchup['score'][0], matchup['score'][2]])
                delta += score[0]+.5*score[2]-old[0]-.5*old[2]
                if strategy_policy:
                    for cat in CATEGORIES:
                        new_margin = _margin(raw['categories'], matchup['totals'][opponent]['categories'], cat)
                        old_margin = _margin(matchup['totals'][team_id]['categories'],
                                             matchup['totals'][opponent]['categories'], cat)
                        if new_margin is not None and old_margin is not None:
                            change = (new_margin-old_margin) / scales[cat]
                            future_margin_gain += change
                            deterioration += max(0.0, -change)
            terminal_by_roster[final_ids] = (delta, deterioration, future_margin_gain)
            terminal_evaluations += 1
        delta, deterioration, future_margin_gain = terminal_by_roster[final_ids]
        p['conditional_rest_of_season_category_delta'] = delta
        if strategy_policy:
            p['final_roster_retained_value'] = round(retained, 3)
            p['retained_value_delta'] = round(retained-original_value, 3)
            p['future_margin_deterioration'] = round(deterioration, 3)
            p['future_normalized_margin_delta'] = round(future_margin_gain, 3)
            p['normalized_category_margin_improvement'] = round(sum(
                _smooth({cat: w['categories'][cat]['directed_margin_after'] for cat in CATEGORIES}, scales)
                - _smooth({cat: w['categories'][cat]['directed_margin_before'] for cat in CATEGORIES}, scales)
                for w in p['weeks']), 3)
            p['short_horizon_category_win_delta'] = p['delta_category_points']
            p['post_horizon_category_win_delta'] = delta
            p['qualifies'] = (not p['steps'] or (p['delta_category_points'] > 0 and
                delta >= -strategy_policy.future_win_loss_tolerance-1e-9 and
                deterioration <= strategy_policy.future_margin_deterioration_tolerance+1e-9 and
                retained >= original_value-strategy_policy.retained_value_loss_tolerance-1e-9))
            p['ranking_explanation'] = ('Qualifies when short-horizon category wins improve and '
                'future category wins, margins, and retained roster value stay within the selected limits.')
            p['acquisition_cost'] = 0.0
            p['retention_penalty'] = 0.0
            p['net_category_points'] = p['delta_category_points']
            p['_order'] = (p['delta_category_points'], -len(p['steps']),
                p['normalized_category_margin_improvement'])
            continue
        # Treat later weeks as a preservation cost, not speculative credit for
        # unmodeled future transactions or ownership changes.
        p['terminal_value_adjustment'] = config.terminal_value_weight*min(0.0, delta)
        p['net_category_points'] += p['terminal_value_adjustment']
        p['ranking_explanation'] = ('Horizon category points minus acquisition, retained-player, and conditional '
            'rest-of-season loss costs; category margins break ties only.')
        if delta < 0:
            p['long_term_warning'] = (p['long_term_warning'] or 'Final roster loses projected categories after the selected horizon.')
        p['_order'] = (p['net_category_points'], *p['_order'][1:])
    if strategy_policy:
        finalists = [p for p in finalists if p['qualifies']]
        alternate = {}
        for model, source_weight in ((('blended', .5), ('hashtag', 1.0), ('leaguewinner', 0.0))
                                     if any(p['steps'] for p in finalists) else ()):
            if weight == source_weight:
                alternate[model] = (base, projections)
                continue
            try:
                alternate[model] = (forecast(inputs, matchup_path=matchup_path,
                    hashtag_weight=source_weight, as_of=as_of, actual_state=actual_state),
                    {site_id: resolve_projection(inputs['projections'][site_id], source_weight)
                     for site_id in players})
            except (ValueError, KeyError):
                alternate[model] = None
        for p in finalists:
            if not p['steps']:
                p['category_flip_robustness'] = {'blended_flips': [], 'hashtag_only_flips': [],
                    'leaguewinner_only_flips': [], 'fragile_flips': [], 'robust_flip_count': 0,
                    'method': 'HOLD baseline'}
                continue
            selected_flips = [(w['week'], cat) for w in p['weeks'] for cat in CATEGORIES
                if w['categories'][cat]['result_before'] != 'W' and
                   w['categories'][cat]['result_after'] == 'W']
            source_flips = {}
            for model, payload in alternate.items():
                if payload is None:
                    source_flips[model] = None
                    continue
                alternative_base, alternative_projections = payload
                matchups_by_week = {m['week']: m for m in alternative_base['weekly_matchups']
                    if team_id in m['teams']}
                since = {s[2]: s[4] for s in p['_steps']}
                until = {s[3]: s[4] for s in p['_steps']}
                roster = [players[site_id] for site_id in sorted(initial_ids | set(since))]
                projected = [(row, alternative_projections[row.site_player_id]) for row in roster]
                found = []
                for week in weeks:
                    matchup = matchups_by_week[week.number]
                    opponent = next(t for t in matchup['teams'] if t != team_id)
                    raw, _, _ = _dated_team_totals(roster, projected, inputs['schedule'],
                        FantasyWeek(week.number, max(today, week.start), week.end, week.phase),
                        as_of, snapshot_day, available_from_by_id=since, available_until_by_id=until, injury_inputs=inputs)
                    if week.number == first and actual_state:
                        raw = _expected_with_actual(raw, actual_state.get('current_week_volumes', {}).get(team_id))
                    before_outcomes = category_result(matchup['totals'][team_id]['categories'],
                        matchup['totals'][opponent]['categories'])[3]
                    after_outcomes = category_result(raw['categories'], matchup['totals'][opponent]['categories'])[3]
                    found.extend((week.number, cat) for cat in CATEGORIES
                        if before_outcomes[cat] != 'W' and after_outcomes[cat] == 'W')
                source_flips[model] = found
            fragile = []
            for week_number, cat in selected_flips:
                week_record = next(w for w in p['weeks'] if w['week'] == week_number)
                margin = week_record['categories'][cat]['directed_margin_after']
                alternate_loss = any(flips is not None and (week_number, cat) not in flips
                    for flips in source_flips.values())
                perturbation_loss = margin is None or margin <= .05 * scales[cat]
                if alternate_loss or perturbation_loss:
                    fragile.append({'week': week_number, 'category': cat,
                        'reason': 'alternate_projection' if alternate_loss else 'five_percent_margin_perturbation'})
            p['category_flip_robustness'] = {'blended_flips': source_flips['blended'],
                'hashtag_only_flips': source_flips['hashtag'],
                'leaguewinner_only_flips': source_flips['leaguewinner'],
                'fragile_flips': fragile, 'robust_flip_count': len(selected_flips)-len(fragile),
                'method': 'Deterministic source comparison and 5% margin-scale perturbation; not a probability.'}
            p['_order'] = (p['delta_category_points'],
                p['category_flip_robustness']['robust_flip_count'], -p['acquisitions'],
                p['normalized_category_margin_improvement'])
        finalists.sort(key=lambda p: (tuple(-x for x in p['_order']),
            tuple((s['week'], s['date'], s['add_site_player_id']) for s in p['steps'])))
    else:
        finalists.sort(key=lambda p: (tuple(-x for x in p['_order']),
            tuple((s['week'], s['date'], s['add_site_player_id']) for s in p['steps'])))
    chosen = finalists[:config.result_limit]
    if hold_plan not in chosen:
        chosen.append(hold_plan)
    cache_entries, prior_hits = len(day_cache), cache_hits
    for p in chosen:
        day_cache.clear()
        replay = evaluate(p['_steps'])
        if any(replay[wi][2] != p['weeks'][wi]['after'] or any(
                (replay[wi][1][cat] is None) !=
                    (p['weeks'][wi]['categories'][cat]['directed_margin_after'] is None) or
                (replay[wi][1][cat] is not None and abs(replay[wi][1][cat]-
                    p['weeks'][wi]['categories'][cat]['directed_margin_after']) > 1e-9)
                for cat in CATEGORIES) for wi in range(horizon)):
            raise AssertionError('Rolling plan replay diverged')
        del p['_steps'], p['_order']
    output = {'team_id': team_id, 'team_name': inputs['teams'][team_id],
            'horizon': horizon, 'first_week': first, 'as_of_utc': base['as_of_utc'],
            'capture_at_utc': audit['snapshot_at_utc'], 'evidence_reasons': sorted(set(reasons)),
            'assumptions': ['Future ownership and free-agent availability are conditional.',
                'Future waiver claim success and processing time are unverified.',
                'Only verified Yahoo period boundaries reset the weekly acquisition budget.',
                'Category-margin scores guide search and are not probabilities.'],
            'search': {'beam_width': config.beam_width, 'candidate_limit': config.candidate_limit,
                'drop_limit': config.drop_limit, 'shortlisted_adds': len(adds),
                'shortlisted_drops': len(drops), 'generated': generated,
                'duplicate_states_pruned': duplicates, 'day_cache_entries': cache_entries,
                'day_cache_hits': prior_hits, 'replayed_plans': len(chosen),
                'truncated': truncated, 'elapsed_seconds': round(perf_counter()-started, 3),
                'terminal_finalists': len(finalists), 'terminal_rosters_evaluated': terminal_evaluations,
                'asset_pruned_before_terminal': asset_pruned,
                'heuristic': 'Bounded beam search; optimality is not guaranteed outside exhaustive fixtures.'},
            'plans': chosen}
    if strategy_policy:
        output['mode'] = strategy_policy.mode
        output['roster_controls'] = controls
        output['yahoo_cannot_cut_site_ids'] = sorted(evidence.get('cannot_cut_site_ids', ())) if evidence else []
        output['policy'] = {'future_win_loss_tolerance': strategy_policy.future_win_loss_tolerance,
            'future_margin_deterioration_tolerance': strategy_policy.future_margin_deterioration_tolerance,
            'retained_value_loss_tolerance': strategy_policy.retained_value_loss_tolerance}
    return output
