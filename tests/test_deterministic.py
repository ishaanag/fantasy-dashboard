import unittest

from fantasy_dashboard.inputs import MATCHUPS, ROOT, load_inputs
from fantasy_dashboard.league import CATEGORIES
from fantasy_dashboard.model.forecast import categories_from_volumes, category_result, forecast, relative_strength

import pinned_capture  # noqa: F401  (pins the archived capture)


class DeterministicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.result = forecast(cls.inputs, matchup_path=ROOT / MATCHUPS)

    def test_weekly_results_reconcile_to_season_and_reciprocal_league(self):
        result = self.result
        self.assertEqual(len(result['weekly_matchups']), 90)
        totals = {t['team_id']: [0, 0, 0] for t in result['teams']}
        for matchup in result['weekly_matchups']:
            self.assertEqual(sum(matchup['score']), 9)
            a, b = matchup['teams']
            for i, value in enumerate(matchup['score']):
                totals[a][i] += value
                totals[b][[1, 0, 2][i]] += value
            self.assertEqual(category_result(matchup['totals'][a]['categories'],
                                             matchup['totals'][b]['categories'])[:3], tuple(matchup['score']))
        self.assertEqual(sum(v[0] for v in totals.values()), sum(v[1] for v in totals.values()))
        for team in result['teams']:
            self.assertEqual(team['projected_record'], totals[team['team_id']])
            self.assertEqual(sum(team['projected_record']), 162)
            w, l, t = team['projected_record']
            self.assertAlmostEqual(team['projected_win_pct'], (w + .5*t)/162)

    def test_category_direction_weighted_percentages_and_normalization(self):
        a = {'fgm': 5, 'fga': 10, 'ftm': 7, 'fta': 10, 'three_ptm': 4,
             'pts': 50, 'reb': 50, 'ast': 20, 'stl': 5, 'blk': 2, 'turnovers': 10}
        b = {**a, 'fgm': 8, 'fga': 20, 'ftm': 3, 'fta': 5, 'reb': 47, 'turnovers': 12}
        cats_a, cats_b = categories_from_volumes(a), categories_from_volumes(b)
        self.assertEqual(cats_a['fg_pct'], .5)
        self.assertEqual(cats_b['fg_pct'], .4)
        self.assertEqual(cats_b['ft_pct'], .6)
        outcomes = category_result(cats_a, cats_b)[3]
        self.assertEqual(outcomes['fg_pct'], 'W')
        self.assertEqual(outcomes['turnovers'], 'W')
        scores = relative_strength({'a': {'categories': cats_a}, 'b': {'categories': cats_b}})
        self.assertEqual(scores['a']['reb']['score'], 100)
        self.assertEqual(scores['b']['reb']['score'], 0)
        self.assertEqual(scores['a']['turnovers']['score'], 100)
        self.assertEqual(scores['b']['turnovers']['score'], 0)
        self.assertEqual(scores['a']['fg_pct']['rank'], 1)
        empty = {cat: 0 for cat in CATEGORIES}
        missing = {**empty, 'reb': None}
        safe = relative_strength({'a': {'categories': empty}, 'b': {'categories': missing}})
        self.assertIsNone(safe['b']['reb']['score'])
        self.assertEqual(safe['a']['reb']['score'], 50)
        self.assertEqual(safe['a']['pts']['score'], 50)


    def test_min_max_midpoint_ties_and_missing_values(self):
        base = {cat: 5.0 for cat in CATEGORIES}
        totals = {'low': {'categories': {**base, 'reb': 10, 'turnovers': 30}},
                  'mid': {'categories': {**base, 'reb': 20, 'turnovers': 20}},
                  'high': {'categories': {**base, 'reb': 30, 'turnovers': 10}},
                  'missing': {'categories': {**base, 'reb': None, 'turnovers': None}}}
        scores = relative_strength(totals)
        for team, expected in [('low', 0), ('mid', 50), ('high', 100)]:
            self.assertEqual(scores[team]['reb']['score'], expected)
            self.assertEqual(scores[team]['turnovers']['score'], expected)
            self.assertEqual(scores[team]['reb']['value'], totals[team]['categories']['reb'])
        self.assertEqual(scores['high']['reb']['rank'], 1)
        self.assertEqual(scores['high']['turnovers']['rank'], 1)
        self.assertIsNone(scores['missing']['reb']['score'])
        self.assertIsNone(scores['missing']['reb']['rank'])
        self.assertTrue(all(scores[team]['pts']['score'] == 50 for team in totals))

    def test_completed_week_results_are_preserved_once(self):
        inputs = load_inputs(ROOT)
        inputs['availability'] = {**inputs['availability'],
                                  'captured_at_utc': '2026-10-26T14:00:00Z',
                                  'lineup_date': '2026-10-26'}
        records = {team: [0, 0, 9] for team in inputs['teams']}
        state = {'as_of_utc': '2026-10-26T14:00:00Z', 'completed_weeks': 1,
                 'records': records, 'weekly_records': [records]}
        result = forecast(inputs, matchup_path=ROOT / MATCHUPS, actual_state=state)
        self.assertEqual(len(result['weekly_matchups']), 90)
        self.assertEqual(sum(m['status'] == 'actual' for m in result['weekly_matchups']), 5)
        self.assertTrue(all(m['week'] > 1 for m in result['weekly_matchups'] if m['status'] == 'projected'))
        self.assertEqual(result['strength_scope'], 'remaining regular season')
        self.assertTrue(all(sum(team['projected_record']) == 162 for team in result['teams']))
        self.assertTrue(all(team['actual_record'] == [0, 0, 9] for team in result['teams']))
