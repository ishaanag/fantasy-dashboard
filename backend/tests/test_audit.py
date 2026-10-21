import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture
"""Phase 3E.1 recommendation audit: counterfactual logic on a hand-checkable fixture, then the saved snapshot."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import TestCase

from app.advisor import AdvisorConfig, LABELS, advise
from app.audit import Auditor, final_roster, rewire
from app.phase1 import CATEGORIES, ROOT, VOLUMES, load_inputs, resolve_projection
from app.waiver import _utc

UTC = timezone.utc
D0, D1 = date(2026, 10, 21), date(2026, 10, 28)
T0, T1 = datetime(2026, 10, 21, 12, tzinfo=UTC), datetime(2026, 10, 28, 12, tzinfo=UTC)
X_A = (0, D0, 'X', 'A', T0, False)      # Week 1: X for A
Y_X = (1, D1, 'Y', 'X', T1, False)      # Week 2: Y for X (X is a stream)
Z_B = (1, D1, 'Z', 'B', T1, False)      # Week 2: Z for B
U_B = (1, D1, 'U', 'B', T1, False)      # Week 2: U for B, which does nothing


def _week(wins):
    out = {c: 'W' if c in wins else 'L' for c in CATEGORIES}
    w = len(wins)
    return {'outcomes': out, 'score': [w, 9 - w, 0], 'points': w, 'soft': 0.0,
            'margins': {c: 1.0 if c in wins else -1.0 for c in CATEGORIES}}


def _rosters(steps):
    roster, by_week = {'A', 'B', 'C'}, []
    for wi in range(2):
        for s in steps:
            if s[0] == wi:
                roster = (roster - {s[3]}) | {s[2]}
        by_week.append(set(roster))
    return by_week


def toy(locked=()):
    """Week 1 PTS needs X. Week 2 PTS needs X; REB needs Y and Z together (an interaction).
    Rest of season: AST needs Z; STL needs Y and Z. C is protected."""
    def horizon_eval(steps):
        r = _rosters(steps)
        return [_week({'pts'} if 'X' in r[0] else set()),
                _week({c for c, ok in (('pts', 'X' in r[1]), ('reb', {'Y', 'Z'} <= r[1])) if ok})], 0

    def season(final):
        return [_week({c for c, ok in (('ast', 'Z' in final), ('stl', {'Y', 'Z'} <= final)) if ok})]
    hold_h, hold_s = horizon_eval(())[0], season(frozenset('ABC'))

    def assess(steps, h, starts, final):
        gain = sum(w['points'] for w in h) - sum(w['points'] for w in hold_h)
        sp = sum(w['points'] for w in season(final)) - sum(w['points'] for w in hold_s)
        return {'value': gain + .5 * sp - .15 * len(steps), 'gain': gain, 'soft': 0.0, 'season_points': sp,
                'season_soft': 0.0, 'season_term': .5 * sp, 'starts': 0}

    class Engine:
        def period(self, days, ids, since=None, until=None):
            return None, [(d, p) for d in days for p in sorted(ids) if p not in (until or {})]
    names = 'ABCXYZU'
    values = {**{k: 1.0 for k in VOLUMES}, 'fga': 2.0, 'fta': 2.0, 'gp': 70.0}
    return Auditor(SimpleNamespace(
        labels=LABELS, inputs={'projections': {}}, weight=.5, schedule=None, as_of=T0, snapshot_day=D0,
        engine=Engine(), players={p: SimpleNamespace(name=p, eligible_positions=('C',)) for p in names},
        all_players={}, projections={p: {'values': values} for p in names},
        initial_ids=frozenset('ABC'), droppable={'A', 'B'}, budgets=[1, 2],
        drop_ok=lambda drop, wi, day, stamp: (drop, day) not in locked,
        weeks=[SimpleNamespace(number=1), SimpleNamespace(number=2)], week_days=[[D0], [D1]],
        later_days=[[date(2026, 11, 4)]], later_numbers=[3], opponent={}, opponent_rosters=[],
        later_opponent_rosters=[], all_opponent_ids=frozenset(), candidate_ids=frozenset('XYZU'),
        actual=None, current_volumes={}, scales={c: 1.0 for c in CATEGORIES}, hold_h=hold_h, hold_s=hold_s,
        horizon_eval=horizon_eval, season=season, assess=assess, tau=.5, replay=lambda steps, h: None,
        season_value=lambda p: (10.0, 5), pool=list('XYZU'), pool_value={p: 10.0 for p in 'XYZU'}))


def card(auditor, steps):
    ev = auditor.evaluate(steps)
    return {'category_points_gained': ev['horizon_points'], 'usable_starts_gained': 0,
            'season': {'category_points_delta': ev['season_points'], 'margin_score_delta': ev['season_margin']},
            'fragile_flips': [], 'eligibility_class': 'provisional', 'missing_verification': [],
            'weeks': [{'week': w, 'before': [0, 9, 0], 'after': [0, 9, 0]} for w in (1, 2)]}


class RewireTests(TestCase):
    def test_omitted_stream_passes_its_drop_to_the_later_move(self):
        steps = (X_A, Y_X, Z_B)
        kept, notes = rewire(steps, {1, 2})
        self.assertEqual([(s[2], s[3]) for s in kept], [('Y', 'A'), ('Z', 'B')])
        self.assertEqual(notes, [('X', 'A')])
        self.assertEqual(rewire(steps, {0, 1, 2})[0], steps)

    def test_permanent_versus_temporary(self):
        final = final_roster(frozenset('ABC'), (X_A, Y_X, Z_B))
        self.assertEqual(final, frozenset('CYZ'))       # X was a temporary stream
        self.assertNotIn('X', final)


class ToyAuditTests(TestCase):
    def test_marginal_contributions_interactions_and_horizon_split(self):
        auditor = toy()
        steps = (X_A, Y_X, Z_B)
        a = auditor.audit((steps,), card(auditor, steps))
        t1, t2, t3 = a['transactions']
        self.assertTrue(t1['temporary'])
        self.assertEqual((t1['leave_one_out']['horizon_points'], t1['leave_one_out']['season_points']), (1, 0))
        self.assertIn('never joins', t1['leave_one_out_note'])
        self.assertEqual((t1['chronological']['horizon_points'], t1['chronological']['season_points']), (2, 0))
        self.assertEqual((t2['leave_one_out']['horizon_points'], t2['leave_one_out']['season_points']), (0, 1))
        self.assertEqual((t2['independent']['horizon_points'], t2['independent']['season_points']), (0, 0))
        # Short-horizon and post-horizon effects stay separate: Z alone is a rest-of-season move only.
        self.assertEqual((t3['independent']['horizon_points'], t3['independent']['season_points']), (0, 1))
        self.assertEqual({t['verdict'] for t in a['transactions']}, {'adds value'})
        self.assertEqual((a['interaction']['horizon_points'], a['interaction']['season_points']), (0, 1))
        self.assertTrue(a['interaction']['nonlinear'])
        self.assertEqual([x['player'] for x in a['temporary']], ['X'])
        self.assertEqual([(r['outgoing'], r['incoming']) for r in a['replacements']], [('A', 'Y'), ('B', 'Z')])
        tiers = {x['label']: x['tier'] for x in a['simpler_alternatives']}
        self.assertEqual(tiers['+X / −A then +Z / −B'], 'comparable')
        self.assertEqual(tiers['+Y / −A then +Z / −B'], 'reference')       # net permanent plan
        self.assertIsNone(a['summary']['preferable_simpler_plan'])
        self.assertEqual(a['scope']['post_horizon_weeks'], [3])

    def test_superior_shorter_plan_is_flagged(self):
        auditor = toy()
        steps = (X_A, U_B)
        a = auditor.audit((steps,), card(auditor, steps))
        self.assertEqual(a['transactions'][1]['verdict'], 'no measurable value')
        self.assertEqual(a['summary']['preferable_simpler_plan'], '+X / −A')
        self.assertTrue(any('Simpler plan is preferable' in w for w in a['warnings']))

    def test_omission_that_breaks_legality_is_not_evaluated(self):
        auditor = toy(locked={('A', D1)})
        steps = (X_A, Y_X)
        a = auditor.audit((steps,), card(auditor, steps))
        first = a['transactions'][0]
        self.assertIsNone(first['leave_one_out'])
        self.assertEqual(first['verdict'], 'required by a later move')
        self.assertIn('locked', first['leave_one_out_note'])
        self.assertFalse(any(m['add'] == 'Y' and m['drop'] == 'A'
                             for x in a['simpler_alternatives'] for m in x['moves']))
        self.assertIn('not on the roster', auditor.check((Y_X,)))


class SavedSnapshotAuditTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.as_of = _utc(cls.inputs['availability']['captured_at_utc'])
        cls.result = advise(cls.inputs, as_of=cls.as_of, now=cls.as_of,   # audit behaviour; small search budget
                            config=AdvisorConfig(beam_width=8, expand_adds=12, season_shortlist=6, local_evaluations=20_000))
        cls.ids = {p.name: p.site_player_id for p in cls.inputs['site_players']}

    def test_every_strategy_is_audited_consistently(self):
        r = self.result
        self.assertTrue(r['strategies'])
        self.assertLess(r['diagnostics']['search']['audit_seconds'], 10)
        for s in r['strategies']:
            a = s['audit']
            self.assertEqual(a['summary']['short_term']['category_wins'], s['category_points_gained'])
            self.assertEqual(a['summary']['rest_of_season']['category_wins'], s['season']['category_points_delta'])
            self.assertEqual(len(a['transactions']), len(s['moves']))
            first = a['sensitivity'][0]['sources']
            self.assertEqual(first['selected']['horizon_points'], s['category_points_gained'])
            for key in ('hashtag', 'leaguewinner'):           # matches the advisor's own fragility check
                self.assertFalse(first[key].get('unavailable'))
                self.assertEqual(first[key]['horizon_points'], s['source_check'][key])
            horizon = a['scope']['horizon_weeks']
            self.assertEqual(a['scope']['post_horizon_weeks'], list(range(horizon[-1] + 1, 19)))
            added = set(s['final_roster_changes']['added'])
            self.assertEqual({x['incoming'] for x in a['replacements']}, added)
            self.assertEqual({x['player'] for x in a['temporary']},
                             {m['add_name'] for m in s['moves']} - added)

    def test_replacement_shooting_is_attempt_weighted(self):
        for s in self.result['strategies']:
            for rep in s['audit']['replacements']:
                for side in ('incoming', 'outgoing'):
                    v = resolve_projection(self.inputs['projections'][self.ids[rep[side]]], .5)['values']
                    self.assertAlmostEqual(rep['per_game'][side]['fg_pct'], round(v['fgm'] / v['fga'], 3))
                    self.assertAlmostEqual(rep['per_game'][side]['ft_pct'], round(v['ftm'] / v['fta'], 3))
