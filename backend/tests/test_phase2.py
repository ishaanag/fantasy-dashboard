from __future__ import annotations
import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import random
import unittest

from app.matchups import load_matchups
from app.phase1 import ROOT, load_inputs
from app.simulator import (_actual_state, _plan, _rank, _semifinal_pairs, _series_winner, _stream_seed,
                           category_result, draw_player, simulate)


class MatchupImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs()
        cls.path = ROOT / 'H2H-Matchups.csv'

    def test_real_schedule_reconciles_to_verified_ids(self):
        m = load_matchups(self.path, self.inputs['teams'])
        self.assertEqual(m.encoding, 'cp1252')
        self.assertEqual(m.aliases, {'??????????????': '6'})
        self.assertEqual(len(m.pairings), 18)
        self.assertEqual(sum(map(len, m.pairings.values())), 90)
        self.assertEqual(m.opponent(1, '1'), '2')
        self.assertEqual(m.opponent(1, '6'), '4')
        self.assertEqual(m.pairings[1], m.pairings[10])

    def _mutate(self, mutate):
        with self.path.open(newline='', encoding='cp1252') as handle:
            rows = list(csv.reader(handle))
        mutate(rows)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'bad.csv'
            with path.open('w', newline='', encoding='cp1252') as handle:
                csv.writer(handle).writerows(rows)
            return load_matchups(path, self.inputs['teams'])

    def test_column_order_does_not_define_team_ids(self):
        original = load_matchups(self.path, self.inputs['teams'])
        reordered = self._mutate(lambda rows: [row.__setitem__(slice(1, None), list(reversed(row[1:]))) for row in rows])
        self.assertEqual(reordered.pairings, original.pairings)

    def test_nonreciprocal_week_fails(self):
        with self.assertRaisesRegex(ValueError, 'Nonreciprocal'):
            self._mutate(lambda rows: rows[1].__setitem__(1, 'Hookah Doncic'))

    def test_ambiguous_name_fails(self):
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self._mutate(lambda rows: rows[0].__setitem__(6, '?????????'))

    def test_pair_frequency_fails(self):
        def mutate(rows):
            rows[10][1:] = rows[2][1:]
        with self.assertRaisesRegex(ValueError, 'exactly twice'):
            self._mutate(mutate)


class SimulationTests(unittest.TestCase):
    def test_category_math_and_playoff_seed_tie(self):
        a = dict(zip(('fg_pct','ft_pct','three_ptm','pts','reb','ast','stl','blk','turnovers'),
                     (.5,.8,10,100,40,20,5,3,10)))
        b = dict(zip(a, (.4,.8,11,90,40,18,6,2,12)))
        w,l,t,outcomes = category_result(a,b)
        self.assertEqual((w,l,t), (5,2,2))
        self.assertEqual(outcomes['turnovers'], 'W')
        self.assertEqual(outcomes['ft_pct'], 'T')
        self.assertEqual(_series_winner('a','b',{'a':a,'b':a},{'a':2,'b':5}), 'a')
        self.assertEqual(_semifinal_pairs(['1','2','3','4','5','6'],'6','4'),
                         (('1','4'),('2','6'))) 

    def test_player_draws_keep_shooting_consistent_and_nonnegative(self):
        values = {'gp':82,'fgm':8.,'fga':16.,'ftm':4.,'fta':5.,'three_ptm':3.,
                  'pts':23.,'reb':7.,'ast':5.,'stl':1.,'blk':1.,'turnovers':3.}
        rng=random.Random(42)
        for _ in range(100):
            draw=draw_player(rng,values)
            self.assertTrue(all(value >= 0 for value in draw.values()))
            self.assertLessEqual(draw['fgm'],draw['fga'])
            self.assertLessEqual(draw['ftm'],draw['fta'])
            self.assertLessEqual(draw['three_ptm'],draw['fgm'])
            self.assertEqual(draw['pts'],2*draw['fgm']+draw['three_ptm']+draw['ftm'])
        values.update(fgm=0.,fga=0.,ftm=0.,fta=0.,three_ptm=0.)
        self.assertEqual(draw_player(rng,values)['fga'],0)
        empty={cat:0 for cat in ('fg_pct','ft_pct','three_ptm','pts','reb','ast','stl','blk','turnovers')}
        self.assertEqual(category_result(empty,empty)[:3],(0,0,9))

    def test_unverified_bracket_is_gated_and_verified_fixture_runs(self):
        inputs=load_inputs()
        fixture={'source_url':'https://basketball.fantasysports.yahoo.com/nba/92103/playoffs',
                 'captured_at_utc':'2027-03-08T12:00:00Z', 'league_id':'92103',
                 'first_round':[[3,6],[4,5]],
                 'semifinals':[[1,'winner_4_5'],[2,'winner_3_6']],
                 'reseeding':False,'tie_break':'higher_seed'}
        result=simulate(inputs,draws=3,seed=11,bracket=fixture)
        self.assertEqual(result['playoff_bracket']['status'],'conditional_supplied_evidence')
        self.assertEqual(sum(t['p_finals'] for t in result['teams']),2)
        self.assertEqual(sum(t['p_champion'] for t in result['teams']),1)
        bad={**fixture,'reseeding':True}
        with self.assertRaisesRegex(ValueError,'differs'):
            simulate(inputs,draws=1,bracket=bad)

    def test_plan_cache_and_common_random_keys(self):
        inputs=load_inputs()
        as_of=datetime.fromisoformat(inputs['availability']['captured_at_utc'].replace('Z','+00:00'))
        first=_plan(inputs,.5,as_of)
        self.assertIs(first,_plan(inputs,.5,as_of))
        self.assertEqual(_stream_seed(42,1,7,'1','123','2026-12-07'),
                         _stream_seed(42,1,7,'1','123','2026-12-07'))
        self.assertNotEqual(_stream_seed(42,1,7,'1','123','2026-12-07'),
                            _stream_seed(42,1,7,'1','123','2026-12-08'))

    def test_completed_week_is_carried_and_not_resimulated(self):
        inputs=load_inputs()
        inputs['availability']={**inputs['availability'],
            'captured_at_utc':'2026-10-26T14:00:00Z','lineup_date':'2026-10-26'}
        records={team:[0,0,9] for team in inputs['teams']}
        state={'as_of_utc':'2026-10-26T14:00:00Z','completed_weeks':1,
               'records':records,'weekly_records':[records]}
        result=simulate(inputs,draws=1,seed=3,actual_state=state)
        self.assertEqual(result['actual_completed_weeks'],1)
        self.assertEqual(len(result['weekly_matchups']),85)
        self.assertTrue(all(match['week']>1 for match in result['weekly_matchups']))
        self.assertTrue(all(sum(team['expected_final_record'].values())==162
                            for team in result['teams']))

    def test_convergence_diagnostic_uses_half_runs(self):
        result=simulate(load_inputs(),draws=100,seed=7)
        self.assertEqual(result['convergence']['half_draws'],50)
        self.assertGreater(result['convergence']['metrics_checked'],800)
        self.assertGreaterEqual(result['convergence']['p95_absolute_difference'],0)

    def test_yahoo_recent_week_tiebreak(self):
        records={'a':[10,8,0],'b':[10,8,0]}
        history=[{'a':[5,4,0],'b':[6,3,0]}, {'a':[5,4,0],'b':[4,5,0]}]
        self.assertEqual(_rank(records,history,random.Random(2))[0], 'a')

    def test_preseason_asof_rejects_uncaptured_started_games(self):
        inputs=load_inputs()
        with self.assertRaisesRegex(ValueError,'Intraday'):
            _actual_state(inputs,datetime(2026,10,21,2,tzinfo=timezone.utc),None)
        with self.assertRaisesRegex(ValueError,'fresh same-day'):
            _actual_state(inputs,datetime(2026,10,21,14,tzinfo=timezone.utc),None)

    def test_source_weight_changes_weekly_inputs(self):
        inputs=load_inputs()
        hashtag=simulate(inputs,draws=1,seed=2,hashtag_weight=1)
        leaguewinner=simulate(inputs,draws=1,seed=2,hashtag_weight=0)
        self.assertNotEqual(hashtag['weekly_matchups'][0]['expected_totals'],
                            leaguewinner['weekly_matchups'][0]['expected_totals'])
        self.assertEqual(hashtag['hashtag_weight'],1)
        self.assertEqual(leaguewinner['hashtag_weight'],0)

    def test_reproducible_end_to_end_forecast(self):
        inputs=load_inputs()
        first=simulate(inputs,draws=3,seed=31)
        second=simulate(inputs,draws=3,seed=31)
        self.assertEqual(first,second)
        self.assertEqual(len(first['weekly_matchups']),90)
        self.assertEqual(len(first['teams']),10)
        self.assertIn('source_disagreement_by_team_stat', first)
        self.assertIn('workbook_sha256', first['provenance'])
        self.assertEqual(first['playoff_bracket']['status'], 'gated_unverified')
        self.assertTrue(all(t['p_finals'] is None and t['p_champion'] is None for t in first['teams']))
        self.assertEqual(sum(t['p_playoffs'] for t in first['teams']),6)
        self.assertEqual(first['comparison_policy'].split()[0], 'Common')
        for match in first['weekly_matchups']:
            self.assertAlmostEqual(sum(match['score_probabilities'].values()),1)
            self.assertEqual(len(match['score_probabilities']),55)
            self.assertEqual(len(match['expected_wins_ci95']),2)
            for probabilities in match['category_probabilities'].values():
                self.assertAlmostEqual(sum(probabilities.values()),1)
            self.assertEqual(len(match['expected_totals']),2)
        self.assertTrue(first['weekly_matchups'][30]['cup_gap_provisional'])


if __name__ == '__main__':
    unittest.main()
