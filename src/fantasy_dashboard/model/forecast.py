"""Deterministic regular-season H2H forecast, plus the season-state and category helpers every engine shares.

`forecast` replays legal daily lineups for all ten teams over the remaining scheduled weeks, carries completed
Yahoo weeks once, and ranks the league with Yahoo's tiebreak. The helpers validate the actual-state contract,
turn volumes into categories, and score category margins.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import exp, isfinite

from fantasy_dashboard.inputs import MATCHUPS, ROOT
from fantasy_dashboard.league import CATEGORIES, EASTERN, VOLUMES, iso_utc, parse_utc
from fantasy_dashboard.model.lineups import dated_team_totals, rank_league, resolve_projection
from fantasy_dashboard.sources.matchups import load_matchups
from fantasy_dashboard.sources.schedule import cup_window_provisional, week_for_day

COUNTING = ('reb', 'ast', 'stl', 'blk', 'turnovers')


def categories_from_volumes(volumes: dict) -> dict:
    return {**{key: volumes[key] for key in CATEGORIES if key not in ('fg_pct', 'ft_pct')},
            'fg_pct': volumes['fgm'] / volumes['fga'] if volumes['fga'] else 0.,
            'ft_pct': volumes['ftm'] / volumes['fta'] if volumes['fta'] else 0.}


def category_result(a: dict, b: dict) -> tuple[int, int, int, dict[str, str]]:
    wins = losses = ties = 0
    outcomes = {}
    for category in CATEGORIES:
        left, right = a[category], b[category]
        if left is None or right is None:
            raise ValueError('Missing actual category total')
        direction = -1 if category == 'turnovers' else 1
        comparison = (left > right) - (left < right)
        outcome = 'W' if comparison * direction > 0 else 'L' if comparison * direction < 0 else 'T'
        outcomes[category] = outcome
        wins += outcome == 'W'
        losses += outcome == 'L'
        ties += outcome == 'T'
    return wins, losses, ties, outcomes


def parse_record(value) -> list[int]:
    if isinstance(value, str):
        value = value.split('-')
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError('Actual standings records must be nonnegative W-L-T triples')
    parsed = []
    for item in value:
        if isinstance(item, bool) or not (isinstance(item, int) or
                isinstance(item, str) and item.isdecimal()):
            raise ValueError('Actual standings category results must be whole numbers')
        count = int(item)
        if count < 0:
            raise ValueError('Actual standings category results cannot be negative')
        parsed.append(count)
    return parsed


def resolve_actual_state(inputs: dict, as_of: datetime, actual_state: dict | None):
    weeks = inputs['weeks']
    current = week_for_day(weeks, as_of.astimezone(EASTERN).date())
    if current is None:
        raise ValueError('Forecast as-of date is outside the Yahoo fantasy season')
    completed = current.number - 1
    if actual_state is None:
        if completed or inputs['league']['standings']['status'] != 'preseason':
            raise ValueError('Current per-team Yahoo standings and completed weeks are required')
        records = {team: [0, 0, 0] for team in inputs['teams']}
        actual = None
        history = []
    else:
        if actual_state.get('as_of_utc') != iso_utc(as_of):
            raise ValueError('Actual standings snapshot must match forecast as-of UTC')
        if actual_state.get('completed_weeks') != completed:
            raise ValueError('Actual standings completed_weeks disagrees with Yahoo calendar')
        raw = actual_state.get('records', {})
        if set(raw) != set(inputs['teams']):
            raise ValueError('Actual standings need all ten verified Yahoo IDs')
        records = {team: parse_record(raw[team]) for team in inputs['teams']}
        if any(sum(record) != 9 * completed for record in records.values()):
            raise ValueError('Each actual record needs nine category decisions per completed week')
        history = actual_state.get('weekly_records', [])
        if len(history) != completed or any(set(week) != set(inputs['teams']) or
                any(sum(parse_record(value)) != 9 for value in week.values()) for week in history):
            raise ValueError('Completed Yahoo weeks need nine-category W-L-T records for each team')
        if any([sum(parse_record(week[team])[i] for week in history) for i in range(3)] != records[team]
               for team in inputs['teams']):
            raise ValueError('Weekly actual results do not sum to actual standings')
        actual = actual_state.get('current_week_volumes')
        if actual is not None and (set(actual) != set(inputs['teams']) or any(
                set(volumes) != set(VOLUMES) or any(not isinstance(v, (int, float)) or not isfinite(v) or v < 0 for v in volumes.values())
                or volumes['fgm'] > volumes['fga'] or volumes['ftm'] > volumes['fta'] or volumes['three_ptm'] > volumes['fgm']
                for volumes in actual.values())):
            raise ValueError('Current-week actual shooting/counting volumes need all teams and valid stats')
    if current.number > 18:
        raise ValueError('Playoff-period live forecasts require verified bracket state')
    captured = parse_utc(inputs['availability']['captured_at_utc'])
    if as_of < captured:
        raise ValueError('Forecast as-of cannot predate the Yahoo roster capture')
    if as_of > captured and as_of.astimezone(EASTERN).date().isoformat() != inputs['availability']['lineup_date']:
        raise ValueError('Forecast needs a fresh same-day Yahoo roster/lineup snapshot')
    started = any(game.tipoff_utc <= as_of and current.start <= game.date <= current.end
                  for game in inputs['schedule'].games)
    if started and actual is None:
        raise ValueError('Intraday forecast needs captured current-week actual volumes for every team')
    # Same rule as rank_league: a lineup snapshot from another day is refused only once a game on the
    # as-of date has tipped off. Overnight, Yahoo's team page still shows the previous day's lineup;
    # that day's games are final and captured in the actual state, and nothing today is locked yet.
    local_day = as_of.astimezone(EASTERN).date()
    if started and local_day.isoformat() != inputs['availability']['lineup_date'] and any(
            game.date == local_day and game.tipoff_utc <= as_of for game in inputs['schedule'].games):
        raise ValueError('Intraday forecast needs a same-day Yahoo lineup snapshot for tipoff locks')
    return completed, records, actual, history


def lineup_plan(inputs: dict, weight: float, as_of: datetime):
    key = (weight, as_of.isoformat())
    cache = inputs.setdefault('_plan_cache', {})
    if key in cache:
        return cache[key]
    rostered = {p.site_player_id for roster in inputs['rosters'].values() for p in roster}
    projections = {site_id: resolve_projection(inputs['projections'][site_id], weight)['values']
                   for site_id in rostered}
    snapshot_day = datetime.fromisoformat(inputs['availability']['lineup_date']).date()
    starts, expected, provisional = {}, {}, {}
    for week in inputs['weeks']:
        provisional[week.number] = cup_window_provisional(week, inputs['schedule'])
        for team_id in sorted(inputs['teams'], key=int):
            roster = inputs['rosters'][team_id]
            totals, detail, _ = dated_team_totals(
                roster, [(p, {'values': projections[p.site_player_id], 'coverage': '', 'source_rows': {}, 'disagreement': {}})
                         for p in roster], inputs['schedule'], week, as_of, snapshot_day, injury_inputs=inputs)
            starts[(week.number, team_id)] = tuple(
                (d['site_player_id'], d['date'], projections[d['site_player_id']])
                for player in detail for d in player['daily_starts'])
            expected[(week.number, team_id)] = totals
    cache[key] = (starts, expected, provisional)
    if len(cache) > 6:
        cache.pop(next(iter(cache)))
    return cache[key]


def with_actual_volumes(future: dict, actual: dict | None) -> dict:
    if not actual:
        return future
    volumes = {**future['shooting_volumes'],
               **{key: future['categories'][key] for key in COUNTING + ('three_ptm', 'pts')}}
    for key in VOLUMES:
        volumes[key] += actual[key]
    categories = categories_from_volumes(volumes)
    return {'categories': categories,
            'shooting_volumes': {key: volumes[key] for key in ('fgm', 'fga', 'ftm', 'fta')}}


def category_margin(a, b, category):
    x, y = a.get(category), b.get(category)
    if x is None or y is None:
        return None
    return y-x if category == 'turnovers' else x-y


def margin_score(margins, scales):
    # Fixed per-category floors keep zero-dispersion fixtures finite. Heuristic only.
    return sum(1/(1+exp(-max(-8, min(8, margin / max(scales.get(cat, 1),
                     .001 if cat.endswith('_pct') else .1)))))
               for cat, margin in margins.items() if margin is not None)


def margin_scales(base):
    result = {}
    for cat in CATEGORIES:
        values = [p['totals'][team]['categories'][cat] for p in base['periods'].values()
                  if isinstance(p, dict) and 'totals' in p for team in p['totals']]
        values = sorted(v for v in values if v is not None and isfinite(v))
        result[cat] = max((values[3*len(values)//4] - values[len(values)//4]) if len(values) >= 4 else 0,
                          .001 if cat.endswith('_pct') else .1)
    return result


def relative_strength(totals: dict[str, dict]) -> dict[str, dict]:
    """League-relative Min-Max scores; retain projected values and ranks."""
    result = {team: {} for team in totals}
    for cat in CATEGORIES:
        values = {team: item['categories'][cat] for team, item in totals.items()}
        valid = {team: value for team, value in values.items()
                 if value is not None and isfinite(value) and value >= 0}
        if not valid:
            for team in result:
                result[team][cat] = {'score': None, 'rank': None, 'value': values[team]}
            continue
        minimum, maximum = min(valid.values()), max(valid.values())
        for team in result:
            value = values[team]
            rank = 1 + sum((other < value if cat == 'turnovers' else other > value)
                           for other in valid.values()) if team in valid else None
            score = None
            if team in valid:
                score = (50.0 if maximum == minimum else
                         100 * ((maximum - value) if cat == 'turnovers' else (value - minimum))
                         / (maximum - minimum))
            result[team][cat] = {'score': score, 'rank': rank, 'value': value}
    return result


def win_pct(record):
    return (record[0] + .5 * record[2]) / sum(record) if sum(record) else 0.0


def seed_order(records: dict[str, list[int]], history: list[dict[str, list[int]]]) -> list[str]:
    """Yahoo's tiebreak: season category win %, then the most recent week's win %, then earlier weeks in
    reverse order, then team ID."""
    return sorted(records, key=lambda team: (-win_pct(records[team]),
                  *(-win_pct(week[team]) for week in reversed(history)), int(team)))


def forecast(inputs: dict, *, matchup_path=None, hashtag_weight: float = .5,
             as_of: datetime | None = None, actual_state: dict | None = None) -> dict:
    if not isfinite(hashtag_weight) or not 0 <= hashtag_weight <= 1:
        raise ValueError('Hashtag weight must be between zero and one')
    as_of = as_of or parse_utc(inputs['availability']['captured_at_utc'])
    if as_of.tzinfo is None:
        raise ValueError('as_of must include a timezone')
    as_of = as_of.astimezone(timezone.utc)
    completed, actual_records, current_actual, actual_history = resolve_actual_state(inputs, as_of, actual_state)
    matchups = load_matchups(matchup_path or ROOT / MATCHUPS, inputs.get('matchup_team_names', inputs['teams']))
    for week, results in enumerate(actual_history, 1):
        for a, b in matchups.pairings[week]:
            left, right = parse_record(results[a]), parse_record(results[b])
            if left != [right[1], right[0], right[2]]:
                raise ValueError(f'Actual Week {week} results are not reciprocal for {a} and {b}')
    _, expected, provisional = lineup_plan(inputs, hashtag_weight, as_of)
    records = {team: value.copy() for team, value in actual_records.items()}
    history = list(actual_history)
    weekly = []
    for week, results in enumerate(actual_history, 1):
        for a, b in matchups.pairings[week]:
            weekly.append({'week': week, 'teams': [a, b],
                           'team_names': [inputs['teams'][a], inputs['teams'][b]],
                           'totals': None, 'outcomes': None, 'score': parse_record(results[a]),
                           'status': 'actual', 'cup_gap_provisional': False})
    for week in range(completed + 1, 19):
        totals = {team: with_actual_volumes(expected[(week, team)],
                  current_actual[team] if week == completed + 1 and current_actual else None)
                  for team in inputs['teams']}
        week_records = {team: [0, 0, 0] for team in inputs['teams']}
        for a, b in matchups.pairings[week]:
            wins, losses, ties, outcomes = category_result(totals[a]['categories'], totals[b]['categories'])
            week_records[a], week_records[b] = [wins, losses, ties], [losses, wins, ties]
            weekly.append({'week': week, 'teams': [a, b],
                           'team_names': [inputs['teams'][a], inputs['teams'][b]],
                           'totals': {a: totals[a], b: totals[b]}, 'outcomes': outcomes,
                           'score': [wins, losses, ties], 'status': 'projected',
                           'cup_gap_provisional': provisional[week]})
        for team in records:
            records[team] = [records[team][i] + week_records[team][i] for i in range(3)]
        history.append(week_records)
    if any(sum(record) != 162 for record in records.values()) or sum(r[0] for r in records.values()) != sum(r[1] for r in records.values()):
        raise ValueError('Season category decisions are incomplete or nonreciprocal')
    ranked = seed_order(records, [{team: parse_record(value) for team, value in week.items()} for week in history])
    teams = [{'team_id': team, 'team_name': inputs['teams'][team], 'projected_seed': seed,
              'projected_record': records[team], 'projected_win_pct': win_pct(records[team]),
              'actual_record': actual_records[team]} for seed, team in enumerate(ranked, 1)]
    # Completed weeks have verified category outcomes, but the actual stat volumes
    # needed for a full-season strength heatmap are not part of the state contract.
    periods = {}
    for week in range(completed + 1, 19):
        period = {team: with_actual_volumes(expected[(week, team)],
                  current_actual[team] if week == completed + 1 and current_actual else None)
                  for team in inputs['teams']}
        periods[str(week)] = {'totals': period, 'strengths': relative_strength(period)}
    volumes = {team: {key: 0.0 for key in VOLUMES} for team in inputs['teams']}
    for period in periods.values():
        for team, item in period['totals'].items():
            for key in VOLUMES:
                volumes[team][key] += (item['shooting_volumes'][key] if key in item['shooting_volumes']
                                        else item['categories'][key])
    season_totals = {team: {'categories': categories_from_volumes(value),
                      'shooting_volumes': {key: value[key] for key in ('fgm', 'fga', 'ftm', 'fta')}}
                     for team, value in volumes.items()}
    periods['regular'] = {'totals': season_totals, 'strengths': relative_strength(season_totals)}
    descriptive = rank_league(inputs, hashtag_weight, 'regular', as_of)
    return {'as_of_utc': iso_utc(as_of), 'hashtag_weight': hashtag_weight,
            'actual_completed_weeks': completed, 'teams': teams, 'weekly_matchups': weekly,
            'periods': periods, 'weeks': [w.as_dict() for w in inputs['weeks'][:18]],
            'quality': {'schedule': descriptive['schedule'], 'provenance': descriptive['provenance'],
                        'data_quality': descriptive['data_quality'],
                        'projection_model': descriptive['projection_model']},
            'strength_scope': 'remaining regular season' if completed else 'full regular season',
            'matchups': {'unique_pairings': 90, 'source_sha256': matchups.source_sha256}}
