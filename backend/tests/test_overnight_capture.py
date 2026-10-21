import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture
"""Overnight refresh: Yahoo still shows the previous day's lineup after midnight Eastern."""
from datetime import date, datetime, timedelta, timezone
from unittest import TestCase

from app.phase1 import EASTERN, ROOT, VOLUMES, load_inputs
from app.simulator import _actual_state

UTC = timezone.utc


def captured(inputs, as_of):
    """Inputs as if the roster/lineup capture happened at `as_of`, with the lineup page still dated Oct 20."""
    stamp = as_of.isoformat().replace('+00:00', 'Z')
    out = {k: v for k, v in inputs.items() if k != '_phase2_plan_cache'}
    out['availability'] = {**inputs['availability'], 'captured_at_utc': stamp, 'lineup_date': '2026-10-20'}
    actual = {'as_of_utc': stamp, 'completed_weeks': 0, 'records': {t: [0, 0, 0] for t in inputs['teams']},
              'weekly_records': [], 'current_week_volumes': {t: {v: 0 for v in VOLUMES} for t in inputs['teams']}}
    return out, actual


class OvernightCaptureTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        games = cls.inputs['schedule'].games
        cls.first_oct21 = min(g.tipoff_utc for g in games if g.date == date(2026, 10, 21))

    def test_previous_day_lineup_accepted_before_today_tips_off(self):
        as_of = datetime(2026, 10, 21, 5, 56, tzinfo=UTC)          # 1:56 a.m. Eastern; Oct 20 games final
        self.assertLess(as_of, self.first_oct21)
        inputs, actual = captured(self.inputs, as_of)
        completed, _, volumes, _ = _actual_state(inputs, as_of, actual)
        self.assertEqual((completed, set(volumes)), (0, set(self.inputs['teams'])))

    def test_previous_day_lineup_refused_once_today_tips_off(self):
        as_of = self.first_oct21 + timedelta(minutes=5)
        self.assertEqual(as_of.astimezone(EASTERN).date(), date(2026, 10, 21))
        inputs, actual = captured(self.inputs, as_of)
        with self.assertRaisesRegex(ValueError, 'same-day Yahoo lineup snapshot'):
            _actual_state(inputs, as_of, actual)
