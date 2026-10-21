from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fantasy_dashboard.inputs import MATCHUPS, ROOT, load_inputs
from fantasy_dashboard.league import parse_utc
from fantasy_dashboard.model.forecast import category_result, lineup_plan, resolve_actual_state, seed_order
from fantasy_dashboard.sources.matchups import load_matchups

import pinned_capture  # noqa: F401  (pins the archived capture)


class MatchupImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs()
        cls.path = ROOT / MATCHUPS

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


class SeasonStateTests(unittest.TestCase):
    def test_category_math(self):
        a = dict(zip(('fg_pct','ft_pct','three_ptm','pts','reb','ast','stl','blk','turnovers'),
                     (.5,.8,10,100,40,20,5,3,10)))
        b = dict(zip(a, (.4,.8,11,90,40,18,6,2,12)))
        w,l,t,outcomes = category_result(a,b)
        self.assertEqual((w,l,t), (5,2,2))
        self.assertEqual(outcomes['turnovers'], 'W')
        self.assertEqual(outcomes['ft_pct'], 'T')
        empty={cat:0 for cat in ('fg_pct','ft_pct','three_ptm','pts','reb','ast','stl','blk','turnovers')}
        self.assertEqual(category_result(empty,empty)[:3],(0,0,9))

    def test_plan_cache(self):
        inputs=load_inputs()
        as_of=parse_utc(inputs['availability']['captured_at_utc'])
        first=lineup_plan(inputs,.5,as_of)
        self.assertIs(first,lineup_plan(inputs,.5,as_of))

    def test_yahoo_recent_week_tiebreak(self):
        records={'1':[10,8,0],'2':[10,8,0]}
        history=[{'1':[5,4,0],'2':[6,3,0]}, {'1':[5,4,0],'2':[4,5,0]}]
        self.assertEqual(seed_order(records,history), ['1','2'])
        self.assertEqual(seed_order(records,history[:1]), ['2','1'])
        self.assertEqual(seed_order(records,[]), ['1','2'])   # then team ID

    def test_preseason_asof_rejects_uncaptured_started_games(self):
        inputs=load_inputs()
        with self.assertRaisesRegex(ValueError,'Intraday'):
            resolve_actual_state(inputs,datetime(2026,10,21,2,tzinfo=timezone.utc),None)
        with self.assertRaisesRegex(ValueError,'fresh same-day'):
            resolve_actual_state(inputs,datetime(2026,10,21,14,tzinfo=timezone.utc),None)


if __name__ == '__main__':
    unittest.main()
