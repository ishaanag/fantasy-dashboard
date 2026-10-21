"""Schedule-aware baseline H2H Monte Carlo; source observations stay separate from draws."""
from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from urllib.parse import urlparse
from datetime import datetime, timezone
from math import isfinite, sqrt
import random

from .matchups import load_matchups
from .phase1 import (CATEGORIES, EASTERN, ROOT, VOLUMES, _dated_team_totals,
                     rank_league, resolve_projection)
from .schedule import cup_window_provisional, week_for_day

MODEL_VERSION = 'h2h-baseline-1'
COUNTING = ('reb', 'ast', 'stl', 'blk', 'turnovers')


def _ci(mean: float, se: float) -> list[float]:
    return [max(0., mean - 1.96 * se), min(1., mean + 1.96 * se)]


def _poisson(rng: random.Random, mean: float) -> int:
    if mean <= 0:
        return 0
    # Exact for small rates, normal approximation in the high-volume tail.
    if mean >= 30:
        return max(0, round(rng.gauss(mean, sqrt(mean))))
    from math import exp
    threshold, product, count = exp(-mean), 1.0, 0
    while product > threshold:
        product *= rng.random()
        count += 1
    return count - 1


def _binomial(rng: random.Random, trials: int, probability: float) -> int:
    return sum(rng.random() < probability for _ in range(trials))


def draw_player(rng: random.Random, values: dict, *, availability_probability: float | None = None) -> dict[str, int]:
    """One played game. Shared minutes/pace draws induce cross-stat dependence."""
    if rng.random() >= (values['gp'] / 82 if availability_probability is None else availability_probability):
        return {key: 0 for key in VOLUMES}
    minutes = rng.gammavariate(20, 1 / 20)
    pace = rng.gammavariate(35, 1 / 35)
    offense = rng.gammavariate(18, 1 / 18)
    common = minutes * pace
    fga = _poisson(rng, values['fga'] * common * offense)
    fta = _poisson(rng, values['fta'] * common * offense)
    # A mild per-game propensity fluctuation makes shooting overdispersed.
    def shooting(makes: float, attempts: float, n: int) -> int:
        if not attempts or not n:
            return 0
        p = min(1., max(0., makes / attempts))
        if p in (0., 1.):
            return int(n * p)
        propensity = rng.betavariate(p * 80, (1 - p) * 80)
        return _binomial(rng, n, propensity)
    fgm, ftm = shooting(values['fgm'], values['fga'], fga), shooting(values['ftm'], values['fta'], fta)
    three_share = min(1., values['three_ptm'] / values['fgm']) if values['fgm'] else 0.
    three = _binomial(rng, fgm, three_share)
    result = {'fgm': fgm, 'fga': fga, 'ftm': ftm, 'fta': fta,
              'three_ptm': three, 'pts': 2 * fgm + three + ftm}
    for key in COUNTING:
        # Shared factor plus category-specific gamma variation.
        rate = values[key] * common * rng.gammavariate(12, 1 / 12)
        result[key] = _poisson(rng, rate)
    return result


def _categories(volumes: dict) -> dict:
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


def _zero() -> dict[str, float]:
    return {key: 0. for key in VOLUMES}


def _plan(inputs: dict, weight: float, as_of: datetime):
    key = (weight, as_of.isoformat())
    cache = inputs.setdefault('_phase2_plan_cache', {})
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
            totals, detail, _ = _dated_team_totals(
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


def _expected_with_actual(future: dict, actual: dict | None) -> dict:
    if not actual:
        return future
    volumes = {**future['shooting_volumes'],
               **{key: future['categories'][key] for key in COUNTING + ('three_ptm', 'pts')}}
    for key in VOLUMES:
        volumes[key] += actual[key]
    categories = _categories(volumes)
    return {'categories': categories,
            'shooting_volumes': {key: volumes[key] for key in ('fgm', 'fga', 'ftm', 'fta')}}


def _stream_seed(*parts) -> int:
    encoded = '|'.join(map(str, parts)).encode('utf-8')
    return int.from_bytes(sha256(encoded).digest()[:8], 'big')


def _sample_week(seed: int, draw_index: int, starts: dict, week: int, teams: dict,
                 actual: dict | None = None):
    result = {}
    for team_id in teams:
        totals = _zero()
        weekly_health = {}
        for player_id, day, values in starts[(week, team_id)]:
            if player_id not in weekly_health:
                baseline = values['gp'] / 82
                if baseline in (0., 1.):
                    weekly_health[player_id] = baseline
                else:
                    shock = random.Random(_stream_seed(seed, draw_index, week, team_id, player_id, 'health')).gauss(0, 1)
                    weekly_health[player_id] = min(1., max(0., baseline + .25 * sqrt(baseline * (1 - baseline)) * shock))
            rng = random.Random(_stream_seed(seed, draw_index, week, team_id, player_id, day))
            for key, value in draw_player(rng, values, availability_probability=weekly_health[player_id]).items():
                totals[key] += value
        if actual:
            for key in VOLUMES:
                totals[key] += actual.get(team_id, {}).get(key, 0)
        result[team_id] = _categories(totals)
    return result


def _record(value) -> list[int]:
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


def _actual_state(inputs: dict, as_of: datetime, actual_state: dict | None):
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
        if actual_state.get('as_of_utc') != as_of.isoformat().replace('+00:00', 'Z'):
            raise ValueError('Actual standings snapshot must match forecast as-of UTC')
        if actual_state.get('completed_weeks') != completed:
            raise ValueError('Actual standings completed_weeks disagrees with Yahoo calendar')
        raw = actual_state.get('records', {})
        if set(raw) != set(inputs['teams']):
            raise ValueError('Actual standings need all ten verified Yahoo IDs')
        records = {team: _record(raw[team]) for team in inputs['teams']}
        if any(sum(record) != 9 * completed for record in records.values()):
            raise ValueError('Each actual record needs nine category decisions per completed week')
        history = actual_state.get('weekly_records', [])
        if len(history) != completed or any(set(week) != set(inputs['teams']) or
                any(sum(_record(value)) != 9 for value in week.values()) for week in history):
            raise ValueError('Completed Yahoo weeks need nine-category W-L-T records for each team')
        if any([sum(_record(week[team])[i] for week in history) for i in range(3)] != records[team]
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
    captured = datetime.fromisoformat(inputs['availability']['captured_at_utc'].replace('Z', '+00:00'))
    if as_of < captured:
        raise ValueError('Forecast as-of cannot predate the Yahoo roster capture')
    if as_of > captured and as_of.astimezone(EASTERN).date().isoformat() != inputs['availability']['lineup_date']:
        raise ValueError('Forecast needs a fresh same-day Yahoo roster/lineup snapshot')
    started = any(game.tipoff_utc <= as_of and current.start <= game.date <= current.end
                  for game in inputs['schedule'].games)
    if started and actual is None:
        raise ValueError('Intraday forecast needs captured current-week actual volumes for every team')
    # Same rule as Phase 1: a lineup snapshot from another day is refused only once a game on the
    # as-of date has tipped off. Overnight, Yahoo's team page still shows the previous day's lineup;
    # that day's games are final and captured in the actual state, and nothing today is locked yet.
    local_day = as_of.astimezone(EASTERN).date()
    if started and local_day.isoformat() != inputs['availability']['lineup_date'] and any(
            game.date == local_day and game.tipoff_utc <= as_of for game in inputs['schedule'].games):
        raise ValueError('Intraday forecast needs a same-day Yahoo lineup snapshot for tipoff locks')
    return completed, records, actual, history


def _rank(records: dict[str, list[int]], history: list[dict], rng: random.Random) -> list[str]:
    # Yahoo Basketball compares the most recent week's win percentage, then
    # previous weeks in reverse order, when cumulative percentage is tied.
    jitter = {team: rng.random() for team in records}
    def pct(record):
        return (record[0] + .5 * record[2]) / max(1, sum(record))
    return sorted(records, key=lambda team: (
        -pct(records[team]), *(-pct(_record(week[team])) for week in reversed(history)), jitter[team]))


def _series_winner(a: str, b: str, results: dict, seeds: dict[str, int]) -> str:
    wins, losses, _, _ = category_result(results[a], results[b])
    return a if wins > losses else b if losses > wins else min((a, b), key=lambda team: seeds[team])


def _validate_bracket(bracket: dict | None, league_id: str) -> dict | None:
    if bracket is None:
        return None
    required = {'source_url', 'captured_at_utc', 'league_id', 'first_round',
                'semifinals', 'reseeding', 'tie_break'}
    if set(bracket) != required:
        raise ValueError('Bracket verification needs exact source, capture time, league ID and rules')
    parsed = urlparse(bracket['source_url'])
    if (parsed.scheme != 'https' or parsed.hostname != 'basketball.fantasysports.yahoo.com'
            or str(league_id) not in parsed.path.split('/') or str(bracket['league_id']) != str(league_id)):
        raise ValueError('Bracket source must identify this Yahoo fantasy basketball league')
    try:
        observed = datetime.fromisoformat(bracket['captured_at_utc'].replace('Z', '+00:00'))
    except (ValueError, AttributeError) as exc:
        raise ValueError('Bracket capture timestamp is invalid') from exc
    if observed.tzinfo is None:
        raise ValueError('Bracket capture timestamp needs a timezone')
    if (bracket['first_round'] != [[3, 6], [4, 5]] or
            bracket['semifinals'] != [[1, 'winner_4_5'], [2, 'winner_3_6']] or
            bracket['reseeding'] is not False or bracket['tie_break'] != 'higher_seed'):
        raise ValueError('Supplied Yahoo bracket differs from the supported fixed six-team bracket')
    return bracket


def _semifinal_pairs(ranked: list[str], winner36: str, winner45: str) -> tuple[tuple[str, str], ...]:
    """Fixed bracket: seed 1 receives the 4/5 winner; seed 2 the 3/6 winner."""
    return ((ranked[0], winner45), (ranked[1], winner36))


def simulate(inputs: dict, *, matchup_path=None, hashtag_weight: float = .5,
             as_of: datetime | None = None, draws: int = 500, seed: int = 20261020,
             actual_state: dict | None = None, bracket: dict | None = None) -> dict:
    if not 0 <= hashtag_weight <= 1 or not isfinite(hashtag_weight):
        raise ValueError('Hashtag weight must be between zero and one')
    if not 1 <= draws <= 20000:
        raise ValueError('Draw count must be from 1 to 20000')
    as_of = as_of or datetime.fromisoformat(inputs['availability']['captured_at_utc'].replace('Z', '+00:00'))
    if as_of.tzinfo is None:
        raise ValueError('as_of must include a timezone')
    as_of = as_of.astimezone(timezone.utc)
    completed, records, current_actual, actual_history = _actual_state(inputs, as_of, actual_state)
    verified_bracket = _validate_bracket(bracket, inputs['league']['league_id'])
    matchups = load_matchups(matchup_path or ROOT / 'H2H-Matchups.csv', inputs.get('matchup_team_names', inputs['teams']))
    if actual_history:
        for week, results in enumerate(actual_history, 1):
            for a, b in matchups.pairings[week]:
                left, right = _record(results[a]), _record(results[b])
                if left != [right[1], right[0], right[2]]:
                    raise ValueError(f'Actual Week {week} results are not reciprocal for {a} and {b}')
    starts, expected, provisional = _plan(inputs, hashtag_weight, as_of)
    rng = random.Random(seed)  # used only to randomize unresolved standings ties
    tally = defaultdict(lambda: {'category': {cat: Counter() for cat in CATEGORIES},
                                'score': Counter(), 'majority': 0, 'wins_sum': 0., 'wins_sq': 0.})
    team_tally = {team: {'finish': Counter(), 'seed': Counter(), 'playoffs': 0, 'finals': 0,
                         'champion': 0, 'wins': 0., 'losses': 0., 'ties': 0., 'pct_sq': 0.} for team in inputs['teams']}
    checkpoint = None
    half_draws = draws // 2
    for draw_index in range(draws):
        standings = {team: record.copy() for team, record in records.items()}
        history = list(actual_history)
        for week in range(completed + 1, 19):
            results = _sample_week(seed, draw_index, starts, week, inputs['teams'],
                                   current_actual if week == completed + 1 else None)
            weekly_records = {team: [0, 0, 0] for team in inputs['teams']}
            for a, b in matchups.pairings[week]:
                w, l, t, outcomes = category_result(results[a], results[b])
                standings[a][0] += w; standings[a][1] += l; standings[a][2] += t
                standings[b][0] += l; standings[b][1] += w; standings[b][2] += t
                weekly_records[a] = [w, l, t]
                weekly_records[b] = [l, w, t]
                record = tally[(week, a, b)]
                for cat, outcome in outcomes.items():
                    record['category'][cat][outcome] += 1
                record['score'][(w, l, t)] += 1
                record['majority'] += w > l
                record['wins_sum'] += w
                record['wins_sq'] += w * w
            history.append(weekly_records)
        ranked = _rank(standings, history, rng)
        seeds = {team: position for position, team in enumerate(ranked, 1)}
        for team in ranked:
            item = team_tally[team]
            item['finish'][seeds[team]] += 1
            item['wins'] += standings[team][0]
            item['losses'] += standings[team][1]
            item['ties'] += standings[team][2]
            pct = (standings[team][0] + .5 * standings[team][2]) / max(1, sum(standings[team]))
            item['pct_sq'] += pct * pct
            if seeds[team] <= 6:
                item['seed'][seeds[team]] += 1
                item['playoffs'] += 1
        if verified_bracket is not None:
            results = _sample_week(seed, draw_index, starts, 19, inputs['teams'])
            winner36 = _series_winner(ranked[2], ranked[5], results, seeds)
            winner45 = _series_winner(ranked[3], ranked[4], results, seeds)
            results = _sample_week(seed, draw_index, starts, 20, inputs['teams'])
            (semi1, semi2) = _semifinal_pairs(ranked, winner36, winner45)
            finalist1 = _series_winner(*semi1, results, seeds)
            finalist2 = _series_winner(*semi2, results, seeds)
            team_tally[finalist1]['finals'] += 1
            team_tally[finalist2]['finals'] += 1
            results = _sample_week(seed, draw_index, starts, 21, inputs['teams'])
            champion = _series_winner(finalist1, finalist2, results, seeds)
            team_tally[champion]['champion'] += 1
        if draws >= 100 and draw_index + 1 == half_draws:
            checkpoint = {
                'playoffs': {team: item['playoffs'] for team, item in team_tally.items()},
                'category_wins': {key: {cat: item['category'][cat]['W'] for cat in CATEGORIES}
                                  for key, item in tally.items()}}
    weekly = []
    for (week, a, b), item in sorted(tally.items()):
        expected_wins = item['wins_sum'] / draws
        variance = max(0., item['wins_sq'] / draws - expected_wins ** 2)
        category_probs = {cat: {outcome: item['category'][cat][outcome] / draws
                                for outcome in ('W', 'L', 'T')} for cat in CATEGORIES}
        category_se = {cat: {outcome: sqrt(p * (1 - p) / draws) for outcome, p in probabilities.items()}
                       for cat, probabilities in category_probs.items()}
        weekly.append({'week': week, 'perspective_team_id': a, 'teams': [a, b], 'team_names': [inputs['teams'][a], inputs['teams'][b]],
                       'expected_totals': {team: _expected_with_actual(expected[(week, team)],
                           current_actual[team] if week == completed + 1 and current_actual else None)
                           for team in (a, b)},
                       'category_probabilities': category_probs,
                       'category_mc_se': category_se,
                       'category_ci95': {cat: {outcome: _ci(p, category_se[cat][outcome])
                                               for outcome, p in probabilities.items()}
                                         for cat, probabilities in category_probs.items()},
                       'score_probabilities': {f'{w}-{l}-{9-w-l}': item['score'][(w, l, 9-w-l)] / draws
                                               for w in range(10) for l in range(10-w)},
                       'expected_category_wins': expected_wins, 'expected_category_win_pct':
                       (expected_wins + .5 * sum(item['category'][c]['T'] for c in CATEGORIES) / draws) / 9,
                       'expected_wins_mc_se': sqrt(variance / draws),
                       'expected_wins_ci95': [max(0., expected_wins - 1.96 * sqrt(variance / draws)),
                                              min(9., expected_wins + 1.96 * sqrt(variance / draws))],
                       'majority_win_probability': item['majority'] / draws,
                       'majority_win_mc_se': sqrt((item['majority'] / draws) * (1 - item['majority'] / draws) / draws),
                       'majority_win_ci95': _ci(item['majority'] / draws,
                           sqrt((item['majority'] / draws) * (1 - item['majority'] / draws) / draws)),
                       'cup_gap_provisional': provisional[week]})
    teams = []
    for team, item in team_tally.items():
        teams.append({'team_id': team, 'team_name': inputs['teams'][team],
                      'actual_record': records[team], 'expected_final_record':
                      {key: item[key] / draws for key in ('wins', 'losses', 'ties')},
                      'expected_category_win_pct': (item['wins'] + .5 * item['ties']) /
                      max(1., item['wins'] + item['losses'] + item['ties']),
                      'expected_category_win_pct_mc_se': sqrt(max(0., item['pct_sq'] / draws -
                          ((item['wins'] + .5 * item['ties']) / max(1., item['wins'] + item['losses'] + item['ties'])) ** 2) / draws),
                      'expected_final_seed': sum(position * n for position, n in item['finish'].items()) / draws,
                      'finish_distribution': {str(i): item['finish'][i] / draws for i in range(1, 11)},
                      'seed_probabilities': {str(i): item['seed'][i] / draws for i in range(1, 7)},
                      'p_playoffs': item['playoffs'] / draws,
                      'p_playoffs_mc_se': sqrt((item['playoffs'] / draws) *
                                                (1 - item['playoffs'] / draws) / draws),
                      'p_playoffs_ci95': _ci(item['playoffs'] / draws,
                          sqrt((item['playoffs'] / draws) * (1 - item['playoffs'] / draws) / draws)),
                      **{f'p_{key}': item[key] / draws if verified_bracket else None
                         for key in ('finals', 'champion')},
                      **{f'p_{key}_mc_se': sqrt((item[key] / draws) * (1 - item[key] / draws) / draws)
                         if verified_bracket else None for key in ('finals', 'champion')},
                      **{f'p_{key}_ci95': _ci(item[key] / draws,
                          sqrt((item[key] / draws) * (1 - item[key] / draws) / draws))
                         if verified_bracket else None for key in ('finals', 'champion')}})
    teams.sort(key=lambda item: (-item['expected_category_win_pct'], item['expected_final_seed']))
    convergence = None
    if checkpoint is not None:
        deltas = []
        for team, item in team_tally.items():
            before = checkpoint['playoffs'][team] / half_draws
            after = (item['playoffs'] - checkpoint['playoffs'][team]) / (draws - half_draws)
            deltas.append(abs(before - after))
        for key, item in tally.items():
            for cat in CATEGORIES:
                before = checkpoint['category_wins'][key][cat] / half_draws
                after = (item['category'][cat]['W'] - checkpoint['category_wins'][key][cat]) / (draws - half_draws)
                deltas.append(abs(before - after))
        ordered = sorted(deltas)
        convergence = {'method': 'first-half versus second-half probabilities using the same fixed-seed run',
                       'half_draws': half_draws, 'metrics_checked': len(deltas),
                       'median_absolute_difference': ordered[len(ordered) // 2],
                       'p95_absolute_difference': ordered[int(.95 * (len(ordered) - 1))],
                       'max_absolute_difference': ordered[-1],
                       'interpretation': 'Diagnostic only; MC intervals exclude projection, injury and schedule uncertainty.'}
    disagreement = {}
    for team_id, roster in inputs['rosters'].items():
        by_stat = defaultdict(list)
        for player in roster:
            for stat, value in resolve_projection(inputs['projections'][player.site_player_id], .5)['disagreement'].items():
                if value is not None:
                    by_stat[stat].append(value)
        disagreement[team_id] = {stat: sum(values) / len(values) for stat, values in by_stat.items()}
    return {'model_version': MODEL_VERSION, 'as_of_utc': as_of.isoformat().replace('+00:00', 'Z'),
            'draws': draws, 'seed': seed, 'hashtag_weight': hashtag_weight,
            'comparison_policy': 'Common random numbers keyed by draw, week, Yahoo team ID, player ID and fantasy date; reuse seed for source comparisons.',
            'convergence': convergence,
            'playoff_bracket': {'status': 'conditional_supplied_evidence' if verified_bracket else 'gated_unverified',
                                'source_url': verified_bracket['source_url'] if verified_bracket else None,
                                'captured_at_utc': verified_bracket['captured_at_utc'] if verified_bracket else None,
                                'verification_limit': 'Supplied file shape and league URL validated; Yahoo page content requires independent review.' if verified_bracket else 'No league-specific Yahoo bracket capture supplied.'},
            'provenance': {'yahoo_roster_as_of_utc': inputs['availability']['captured_at_utc'],
                           'yahoo_settings_as_of_utc': inputs['league']['captured_at_utc'],
                           'workbook_sha256': inputs['workbook_sha256'],
                           'reviewed_matches_sha256': inputs['decision_file_sha256'],
                           'yahoo_calendar_sha256': inputs['calendar_sha256'],
                           'nba_cup_rules_sha256': inputs['cup_rules_sha256']},
            'source_disagreement_by_team_stat': disagreement,
            'actual_completed_weeks': completed, 'teams': teams, 'weekly_matchups': weekly,
            'schedule': inputs['schedule'].metadata(), 'matchups': {'source_sha256': matchups.source_sha256,
            'encoding': matchups.encoding, 'name_aliases': matchups.aliases, 'regular_weeks': 18,
            'unique_pairings': 90},
            'assumptions': {'uncertainty': 'Uncalibrated baseline: GP/82 availability with a shared player-week health shock; gamma minutes, pace, offense and category variation; conditional binomial shooting. MC intervals cover finite-draw error only.',
                            'cup': 'Undated Cup/replacement games omitted. No games invented; championship excluded.',
                            'live_status': 'NBA postponement status unavailable in supplied dated CSV.',
                            'standings_tiebreak': 'Yahoo Basketball cumulative category win percentage, then most recent weekly percentage backward; unresolved exact ties randomized.',
                            'playoff_bracket': 'Six-team bracket requires a captured Yahoo bracket with 3–6, 4–5, fixed semifinals and top-two byes.',
                            'playoff_probabilities': 'Finals and championship outputs are unavailable until bracket evidence is supplied and validated.' if not verified_bracket else 'Conditional on the supplied bracket evidence; page contents require independent review.',
                            'shooting': 'Made field goals and free throws conditional on attempts; 3PTM conditional on FGM; simulated points derive from makes.',
                            'lineups': 'Phase 1 maximum-weight legal daily lineup and captured same-day tipoff locks reused.'},
            'descriptive_ranking': [{'team_id': t['team_id'], 'power_rank': t['power_rank'], 'power_score': t['power_score']}
                                    for t in rank_league(inputs, hashtag_weight, 'regular', as_of)['teams']]}
