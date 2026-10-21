"""Deterministic regular-season H2H forecast from the existing legal lineup plan."""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite

from .matchups import load_matchups
from .phase1 import CATEGORIES, ROOT, VOLUMES, rank_league
from .simulator import _actual_state, _categories, _expected_with_actual, _plan, _record, category_result


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


def _win_pct(record):
    return (record[0] + .5 * record[2]) / sum(record) if sum(record) else 0.0


def forecast(inputs: dict, *, matchup_path=None, hashtag_weight: float = .5,
             as_of: datetime | None = None, actual_state: dict | None = None) -> dict:
    if not isfinite(hashtag_weight) or not 0 <= hashtag_weight <= 1:
        raise ValueError('Hashtag weight must be between zero and one')
    as_of = as_of or datetime.fromisoformat(inputs['availability']['captured_at_utc'].replace('Z', '+00:00'))
    if as_of.tzinfo is None:
        raise ValueError('as_of must include a timezone')
    as_of = as_of.astimezone(timezone.utc)
    completed, actual_records, current_actual, actual_history = _actual_state(inputs, as_of, actual_state)
    matchups = load_matchups(matchup_path or ROOT / 'H2H-Matchups.csv', inputs.get('matchup_team_names', inputs['teams']))
    for week, results in enumerate(actual_history, 1):
        for a, b in matchups.pairings[week]:
            left, right = _record(results[a]), _record(results[b])
            if left != [right[1], right[0], right[2]]:
                raise ValueError(f'Actual Week {week} results are not reciprocal for {a} and {b}')
    _, expected, provisional = _plan(inputs, hashtag_weight, as_of)
    records = {team: value.copy() for team, value in actual_records.items()}
    history = list(actual_history)
    weekly = []
    for week, results in enumerate(actual_history, 1):
        for a, b in matchups.pairings[week]:
            weekly.append({'week': week, 'teams': [a, b],
                           'team_names': [inputs['teams'][a], inputs['teams'][b]],
                           'totals': None, 'outcomes': None, 'score': _record(results[a]),
                           'status': 'actual', 'cup_gap_provisional': False})
    for week in range(completed + 1, 19):
        totals = {team: _expected_with_actual(expected[(week, team)],
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
    ranked = sorted(records, key=lambda team: (-_win_pct(records[team]),
                    *(-_win_pct(_record(week[team])) for week in reversed(history)), int(team)))
    teams = [{'team_id': team, 'team_name': inputs['teams'][team], 'projected_seed': seed,
              'projected_record': records[team], 'projected_win_pct': _win_pct(records[team]),
              'actual_record': actual_records[team]} for seed, team in enumerate(ranked, 1)]
    # Completed weeks have verified category outcomes, but the actual stat volumes
    # needed for a full-season strength heatmap are not part of the state contract.
    periods = {}
    for week in range(completed + 1, 19):
        period = {team: _expected_with_actual(expected[(week, team)],
                  current_actual[team] if week == completed + 1 and current_actual else None)
                  for team in inputs['teams']}
        periods[str(week)] = {'totals': period, 'strengths': relative_strength(period)}
    volumes = {team: {key: 0.0 for key in VOLUMES} for team in inputs['teams']}
    for period in periods.values():
        for team, item in period['totals'].items():
            for key in VOLUMES:
                volumes[team][key] += (item['shooting_volumes'][key] if key in item['shooting_volumes']
                                        else item['categories'][key])
    season_totals = {team: {'categories': _categories(value),
                      'shooting_volumes': {key: value[key] for key in ('fgm', 'fga', 'ftm', 'fta')}}
                     for team, value in volumes.items()}
    periods['regular'] = {'totals': season_totals, 'strengths': relative_strength(season_totals)}
    descriptive = rank_league(inputs, hashtag_weight, 'regular', as_of)
    return {'as_of_utc': as_of.isoformat().replace('+00:00', 'Z'), 'hashtag_weight': hashtag_weight,
            'actual_completed_weeks': completed, 'teams': teams, 'weekly_matchups': weekly,
            'periods': periods, 'weeks': [w.as_dict() for w in inputs['weeks'][:18]],
            'quality': {'schedule': descriptive['schedule'], 'provenance': descriptive['provenance'],
                        'data_quality': descriptive['data_quality'],
                        'projection_model': descriptive['projection_model']},
            'strength_scope': 'remaining regular season' if completed else 'full regular season',
            'matchups': {'unique_pairings': 90, 'source_sha256': matchups.source_sha256}}
