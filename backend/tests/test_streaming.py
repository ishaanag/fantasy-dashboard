import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture
"""Focused Phase 3B search and replay checks on a tiny dated fixture."""
from datetime import date, datetime, time, timedelta, timezone
from itertools import product
from io import BytesIO
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.phase1 import CATEGORIES, VOLUMES, EASTERN, RosterPlayer, _dated_team_totals, _lineup_score
from app.schedule import FantasyWeek
from app.simulator import category_result
from app.streaming import SearchConfig, StrategyPolicy, optimize_week, optimize_horizon
from app.dashboard import HTML, make_handler
from app.waiver import _margin, _smooth


UTC = timezone.utc
START = datetime(2026, 10, 20, 12, tzinfo=UTC)
D1, D2 = date(2026, 10, 20), date(2026, 10, 21)


def projection(fgm, fga, ftm, fta, pts, ast=0):
    values = {key: 0.0 for key in VOLUMES}
    values.update(fgm=fgm, fga=fga, ftm=ftm, fta=fta, pts=pts, ast=ast, gp=82.0)
    return {'values': values, 'coverage': {}, 'source_rows': {}, 'disagreement': {}}


class FixtureSchedule:
    def __init__(self):
        self.team_dates = {
            ('ANCHOR', D1): SimpleNamespace(tipoff_utc=datetime(2026, 10, 20, 21, tzinfo=UTC), tipoff_et=None),
            ('ANCHOR', D2): SimpleNamespace(tipoff_utc=datetime(2026, 10, 21, 21, tzinfo=UTC), tipoff_et=None),
            ('OLD', D1): SimpleNamespace(tipoff_utc=datetime(2026, 10, 20, 22, tzinfo=UTC), tipoff_et=None),
            ('A', D1): SimpleNamespace(tipoff_utc=datetime(2026, 10, 20, 23, tzinfo=UTC), tipoff_et=None),
            ('B', D2): SimpleNamespace(tipoff_utc=datetime(2026, 10, 21, 23, tzinfo=UTC), tipoff_et=None)}
        self.team_counts = {'ANCHOR': 2, 'OLD': 1, 'A': 1, 'B': 1}

    def games_for(self, team, start, end, *, as_of=None):
        return [g for (name, day), g in self.team_dates.items()
                if name == team and start <= day <= end and (as_of is None or g.tipoff_utc > as_of)]


class StreamingTests(TestCase):
    def setUp(self):
        self.week = FantasyWeek(1, D1, D2, 'regular_season')
        self.old = RosterPlayer('4', '0', 'Old', 'BN', ('PG',), 'OLD')
        self.anchor = RosterPlayer('4', '9', 'Anchor', 'BN', ('C',), 'ANCHOR')
        self.a = SimpleNamespace(site_player_id='1', name='A', eligible_positions=('PG',),
                                 nba_team='A', availability_label='FA')
        self.b = SimpleNamespace(site_player_id='2', name='B', eligible_positions=('SG',),
                                 nba_team='B', availability_label='FA')
        self.projections = {'0': projection(2, 5, 1, 2, 5),
                            '1': projection(8, 10, 1, 2, 20),
                            '2': projection(1, 2, 9, 10, 10, 10),
                            '9': projection(0, 1, 0, 1, 1)}
        self.schedule = FixtureSchedule()
        self.inputs = {'league': {'my_team_id': '4', 'league_id': '1'},
                       'availability': {'captured_at_utc': START.isoformat(),
                                        'lineup_date': D1.isoformat(), 'player_fingerprint': 'fixture'},
                       'teams': {'4': 'Me', '5': 'Opponent'}, 'rosters': {'4': [self.old, self.anchor]},
                       'projections': self.projections, 'schedule': self.schedule, 'weeks': [self.week]}
        self.evidence = {'captured_at_utc': START.isoformat(), 'player_fingerprint': 'fixture',
                         'team_id': '4', 'week': 1, 'adds_used': 2, 'cannot_cut_site_ids': ['9'],
                         'locked_site_ids': [], 'timing_by_site_id': {
                             '1': {'kind': 'free_agent', 'effective_at_utc': START.isoformat()},
                             '2': {'kind': 'free_agent', 'effective_at_utc': START.isoformat()}},
                         'empty_slot_allowed': False, 'direct_il_site_ids': [],
                         'il_eligible_site_ids': [], 'active_claim_site_ids': []}
        base, _, _ = _dated_team_totals([self.old, self.anchor],
                                        [(self.old, self.projections['0']), (self.anchor, self.projections['9'])],
                                        self.schedule, self.week, START, D1)
        opponent = {'fg_pct': .5, 'ft_pct': .6, 'three_ptm': 0, 'pts': 15, 'reb': 0,
                    'ast': 3, 'stl': 0, 'blk': 0, 'turnovers': 0}
        score = list(category_result(base['categories'], opponent)[:3])
        self.forecast = {'as_of_utc': START.isoformat(), 'actual_completed_weeks': 0,
                         'weekly_matchups': [{'week': 1, 'teams': ['4', '5'], 'score': score,
                           'totals': {'4': base, '5': {'categories': opponent}}}],
                         'periods': {'1': {'totals': {'4': base, '5': {'categories': opponent}}}}}

    def run_plan(self, duplicate_candidates=False, candidates=None, as_of_override=None, now_override=None,
                 config=None, **changes):
        evidence = {**self.evidence, **changes}
        adds = candidates or ([self.a, self.a, self.b] if duplicate_candidates else [self.a, self.b])
        audit = {'snapshot_at_utc': START.isoformat()}
        with patch('app.streaming.forecast', return_value=self.forecast), \
             patch('app.streaming._shortlist', return_value=(adds, [self.old],
                   self.projections, audit)), \
             patch('app.streaming.pool_audit', return_value=audit), \
             patch('app.streaming.resolve_projection', side_effect=lambda p, w: p):
            return optimize_week(self.inputs, evidence=evidence, as_of=as_of_override,
                now=now_override or as_of_override or START,
                config=config or SearchConfig(beam_width=100, candidate_limit=2, drop_limit=1, result_limit=20))

    def run_strategy(self, *, policy=None, candidates=None, config=None):
        audit = {'snapshot_at_utc': START.isoformat()}
        with patch('app.streaming.forecast', return_value=self.forecast), \
             patch('app.streaming._shortlist', return_value=(candidates or [self.a, self.b],
                   [self.old], self.projections, audit)), \
             patch('app.streaming.resolve_projection', side_effect=lambda p, w: p):
            return optimize_horizon(self.inputs, horizon=1, as_of=START, now=START,
                evidence=self.evidence, strategy_policy=policy or StrategyPolicy(
                    protected_site_ids=('9',), streaming_slot_site_ids=('0',)),
                config=config or SearchConfig(beam_width=100, candidate_limit=2,
                    drop_limit=1, result_limit=20))

    def test_strategy_collapses_date_variants_and_preserves_material_outcomes(self):
        result = self.run_strategy()
        sequences = [tuple((s['add_site_player_id'], s['drop_site_player_id'])
                           for s in p['steps']) for p in result['plans']]
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertGreater(result['plans'][0]['acquisitions'], 0)
        exhaustive = self.run_plan(config=SearchConfig(beam_width=100, candidate_limit=2,
            drop_limit=1, result_limit=100, acquisition_cost=0, retention_penalty=0))
        self.assertEqual(result['plans'][0]['short_horizon_category_win_delta'],
            max(p['delta_category_points'] for p in exhaustive['plans']))
        self.assertTrue(any(p['steps'] and p['timing_alternatives'] for p in result['plans']))
        self.assertTrue(all(s['drop_site_player_id'] != '9'
            for p in result['plans'] for s in p['steps']))
        self.assertTrue(all(p['post_horizon_category_win_delta'] >= 0 for p in result['plans']))
        self.assertTrue(all(p['category_flip_robustness']['robust_flip_count'] >= 0
            for p in result['plans']))

    def test_strategy_hold_and_manual_core_override(self):
        hold = self.run_strategy(policy=StrategyPolicy(protected_site_ids=('0', '9'),
            streaming_slot_site_ids=()))
        self.assertEqual([p['acquisitions'] for p in hold['plans']], [0])
        permanent = self.run_strategy(policy=StrategyPolicy(mode='permanent',
            protected_site_ids=('9',), streaming_slot_site_ids=()))
        self.assertTrue(any(p['steps'] for p in permanent['plans']))
        self.assertTrue(all(s['drop_site_player_id'] != '9'
            for p in permanent['plans'] for s in p['steps']))
        self.assertIn('id="advisorResults"', HTML)  # 3E: one Waiver Advisor panel; legacy routes stay
        handler = make_handler(self.inputs, transaction_evidence=self.evidence)
        request = object.__new__(handler)
        request.path = '/api/strategies?mode=permanent&horizon=1&future_win_loss_tolerance=0.5&protected_site_ids=9'
        request.wfile = BytesIO()
        codes = []
        request.send_response = codes.append
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        with patch('app.dashboard.optimize_horizon', return_value={'plans': []}) as mocked:
            request.do_GET()
        self.assertEqual(codes, [200])
        self.assertEqual(mocked.call_args.kwargs['strategy_policy'].mode, 'permanent')
        self.assertEqual(mocked.call_args.kwargs['strategy_policy'].protected_site_ids, ('9',))

    def test_budget_roster_conservation_and_conditional_timing(self):
        result = self.run_plan()
        self.assertEqual(result['remaining_adds'], 2)
        self.assertTrue(any(p['acquisitions'] == 0 for p in result['plans']))
        self.assertTrue(any(p['acquisitions'] == 1 for p in result['plans']))
        self.assertTrue(any(p['acquisitions'] == 2 for p in result['plans']))
        for plan in result['plans']:
            self.assertLessEqual(plan['acquisitions_used_after'], 4)
            self.assertEqual(len(plan['final_roster']), 2)
            current = {'0', '9'}
            for step in plan['steps']:
                self.assertIn(step['drop_site_player_id'], current)
                self.assertNotIn(step['add_site_player_id'], current)
                current.remove(step['drop_site_player_id'])
                current.add(step['add_site_player_id'])
            self.assertEqual(current, {p['site_player_id'] for p in plan['final_roster']})
            if any(s['date'] == D2.isoformat() for s in plan['steps']):
                self.assertEqual(plan['state'], 'Provisional')
                self.assertIn('future_availability_conditional', plan['missing_verification'])
        exhausted = self.run_plan(adds_used=4)
        self.assertEqual([p['acquisitions'] for p in exhausted['plans']], [0])

    def test_weighted_shooting_locks_and_exhaustive_category_optimum(self):
        result = self.run_plan()
        self.assertGreater(self.run_plan(duplicate_candidates=True)['search']['duplicate_states_pruned'], 0)
        opponent = self.forecast['weekly_matchups'][0]['totals']['5']['categories']
        players = {'0': self.old, '9': self.anchor, '1': RosterPlayer('4','1','A','BN',('PG',),'A'),
                   '2': RosterPlayer('4','2','B','BN',('SG',),'B')}
        # Independent exhaustive replay of every 0, 1 and 2 action sequence.
        sequences = [()]
        for day in (D1, D2):
            for add in ('1', '2'):
                sequences.append(((day, add, '0'),))
                for later in (day, D2):
                    for other in ({'1','2'}-{add}):
                        sequences.append(((day, add, '0'), (later, other, add)))
        best = -1.0
        for sequence in sequences:
            union = [players[p] for p in {'0', '9'} | {step[1] for step in sequence}]
            since = {a: START if day == D1 else datetime(2026,10,21,4,tzinfo=UTC)
                     for day,a,_ in sequence}
            until = {d: since[a] for _,a,d in sequence}
            raw, _, _ = _dated_team_totals(union, [(p,self.projections[p.site_player_id]) for p in union],
                self.schedule, self.week, START, D1, available_from_by_id=since,
                available_until_by_id=until)
            score = category_result(raw['categories'], opponent)
            best = max(best, score[0]+.5*score[2])
        top = result['plans'][0]
        self.assertEqual(top['after'][0]+.5*top['after'][2], best)
        # A's 8/10, B's 1/2, and Anchor's 0/1 twice combine by attempts.
        combined = next(p for p in result['plans'] if len(p['steps']) == 2
                        and [s['add_site_player_id'] for s in p['steps']] == ['1','2']
                        and [s['date'] for s in p['steps']] == [D1.isoformat(), D2.isoformat()])
        self.assertAlmostEqual(combined['categories']['fg_pct']['after'], 9/14)
        self.assertAlmostEqual(combined['categories']['ft_pct']['after'], 10/14)
        self.assertEqual([d['usable_starts'] for d in combined['daily_starts']], [2,2])
        self.assertEqual(combined['daily_starts'][0]['started_site_ids'], ['1', '9'])
        self.assertEqual(combined['daily_starts'][1]['started_site_ids'], ['2', '9'])
        self.assertEqual(result['plans'], self.run_plan()['plans'])

    def test_missing_evidence_and_protected_drop_never_actionable(self):
        verified = self.run_plan()
        self.assertTrue(any(p['state'] == 'Actionable' and p['acquisitions'] == 1
                            for p in verified['plans']))
        stale = self.run_plan(now_override=START+timedelta(hours=1))
        self.assertTrue(all(p['state'] != 'Actionable' for p in stale['plans'] if p['acquisitions']))
        self.assertIn('transaction_evidence_not_fresh_for_action', stale['evidence_reasons'])
        partial = self.run_plan(status='user_supplied_partial', observed_date=D1.isoformat())
        self.assertTrue(all(p['state'] != 'Actionable' for p in partial['plans'] if p['acquisitions']))
        protected = self.run_plan(cannot_cut_site_ids=['0'])
        self.assertTrue(all(p['acquisitions'] == 0 for p in protected['plans']))

    def test_four_add_ceiling_and_started_player_lock(self):
        extras = [SimpleNamespace(site_player_id=str(i), name=str(i), eligible_positions=('SG',),
                    nba_team='B', availability_label='FA') for i in (3,4,5)]
        for p in extras:
            self.projections[p.site_player_id] = self.projections['2']
        many = self.run_plan(candidates=[self.a, self.b, *extras], adds_used=0)
        self.assertEqual(max(p['acquisitions'] for p in many['plans']), 4)
        self.assertTrue(all(p['acquisitions_used_after'] <= 4 for p in many['plans']))
        self.old = RosterPlayer('4', '0', 'Old', 'PG', ('PG',), 'OLD')
        self.inputs['rosters']['4'][0] = self.old
        late = datetime(2026, 10, 20, 22, 30, tzinfo=UTC)
        locked = self.run_plan(as_of_override=late)
        self.assertTrue(all(s['date'] != D1.isoformat()
                            for p in locked['plans'] for s in p['steps']))

    def test_waiver_arrival_does_not_enable_early_followup(self):
        waiver = SimpleNamespace(site_player_id='1', name='A', eligible_positions=('PG',),
                                 nba_team='A', availability_label='W')
        evidence = {**self.evidence, 'timing_by_site_id': {}}
        result = self.run_plan(candidates=[waiver, self.b], **evidence)
        for plan in result['plans']:
            self.assertTrue(all(s['effective_at_utc'] is None for s in plan['steps']))
            self.assertEqual([s['modeled_effective_date'] for s in plan['steps']],
                             sorted(s['modeled_effective_date'] for s in plan['steps']))
            if plan['steps'] and plan['steps'][0]['add_site_player_id'] == '1':
                self.assertEqual(len(plan['steps']), 1)
            if any(s['add_site_player_id'] == '1' for s in plan['steps']):
                self.assertEqual(plan['state'], 'Provisional')
                self.assertIn('waiver_claim_success_unverified', plan['missing_verification'])

    def test_dashboard_weekly_route_preserves_single_move_view(self):
        self.assertNotIn('id="singleMoves"', HTML)  # 3E retired the panel, not the route
        handler = make_handler(self.inputs, transaction_evidence=self.evidence)
        request = object.__new__(handler)
        request.path = '/api/streaming?beam_width=7&candidate_limit=3'
        request.wfile = BytesIO()
        codes = []
        request.send_response = codes.append
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        with patch('app.dashboard.optimize_week', return_value={'plans': []}) as mocked:
            request.do_GET()
        self.assertEqual(codes, [200])
        self.assertEqual(json.loads(request.wfile.getvalue()), {'plans': []})
        self.assertEqual(mocked.call_args.kwargs['config'].beam_width, 7)

    def test_rolling_horizon_exact_small_fixture_and_budget_reset(self):
        # Every candidate has a game on each day, so exhaustive action dates
        # and the planner's useful-date screen cover the same legal choices.
        days = [D1 + timedelta(days=i) for i in range(6)]
        for team in ('ANCHOR', 'OLD', 'A', 'B'):
            for day in days:
                self.schedule.team_dates[(team, day)] = SimpleNamespace(
                    tipoff_utc=datetime.combine(day, time(21), UTC), tipoff_et=None)
        self.schedule.team_counts = {name: 6 for name in ('ANCHOR', 'OLD', 'A', 'B')}
        self.schedule.unscheduled_by_team = {}
        opponent = {'fg_pct': .5, 'ft_pct': .6, 'three_ptm': 0, 'pts': 15, 'reb': 0,
                    'ast': 3, 'stl': 0, 'blk': 0, 'turnovers': 0}
        self.evidence['adds_used'] = 3
        players = {'0': self.old, '9': self.anchor,
                   '1': RosterPlayer('4','1','A','BN',('PG',),'A'),
                   '2': RosterPlayer('4','2','B','BN',('SG',),'B')}
        for horizon in (2, 3):
            weeks = [FantasyWeek(i+1, days[2*i], days[2*i+1], 'regular_season',
                                 4 if i == 0 else 1) for i in range(horizon)]
            self.inputs['weeks'] = weeks
            matchups = []
            for week in weeks:
                raw, _, _ = _dated_team_totals([self.old, self.anchor],
                    [(self.old,self.projections['0']), (self.anchor,self.projections['9'])],
                    self.schedule, week, START, D1)
                matchups.append({'week': week.number, 'teams': ['4','5'],
                    'score': list(category_result(raw['categories'], opponent)[:3]),
                    'totals': {'4': raw, '5': {'categories': opponent}}})
            base = {'as_of_utc': START.isoformat(), 'actual_completed_weeks': 0,
                    'weekly_matchups': matchups,
                    'periods': {str(m['week']): {'totals': m['totals']} for m in matchups}}
            config = SearchConfig(beam_width=1000, candidate_limit=2, drop_limit=1,
                                  result_limit=1000, max_generated=100000)
            audit = {'snapshot_at_utc': START.isoformat()}
            with patch('app.streaming.forecast', return_value=base), \
                 patch('app.streaming._shortlist', return_value=([self.a,self.b], [self.old],
                       self.projections, audit)), \
                 patch('app.streaming.resolve_projection', side_effect=lambda p,w:p):
                result = optimize_horizon(self.inputs, horizon=horizon, as_of=START,
                    now=START, evidence=self.evidence, config=config)
                repeat = optimize_horizon(self.inputs, horizon=horizon, as_of=START,
                    now=START, evidence=self.evidence, config=config)
            self.assertEqual(result['plans'], repeat['plans'])
            self.assertFalse(result['search']['truncated'])
            # Independently enumerate HOLD and every legal 1- or 2-add path.
            sequences = [()]
            for first_day, first_add in product(days[:2*horizon], ('1','2')):
                wi = days.index(first_day)//2
                for second_day in days[days.index(first_day):2*horizon]:
                    wj = days.index(second_day)//2
                    if wj == wi:
                        continue  # one acquisition in each modeled week
                    second_add = '2' if first_add == '1' else '1'
                    sequences.append(((first_day, first_add, '0'),
                                      (second_day, second_add, first_add)))
                sequences.append(((first_day, first_add, '0'),))
            best = float('-inf')
            for sequence in sequences:
                since = {a: START if day == D1 else datetime.combine(day, time.min, EASTERN).astimezone(UTC)
                         for day,a,_ in sequence}
                until = {d: since[a] for _,a,d in sequence}
                roster = [players[p] for p in sorted({'0','9'} | set(since))]
                points = 0.0
                for week in weeks:
                    raw, _, _ = _dated_team_totals(roster,
                        [(p,self.projections[p.site_player_id]) for p in roster],
                        self.schedule, week, START, D1, available_from_by_id=since,
                        available_until_by_id=until)
                    score = category_result(raw['categories'], opponent)
                    points += score[0]+.5*score[2]
                incoming = [_lineup_score(self.projections[a]['values']) for _,a,_ in sequence]
                scale = max(_lineup_score(self.projections[p]['values']) for p in ('0','9'))
                retention = max(0, _lineup_score(self.projections['0']['values'])-min(incoming, default=0))/scale if sequence else 0
                best = max(best, points-config.acquisition_cost*len(sequence)-config.retention_penalty*retention)
            self.assertAlmostEqual(result['plans'][0]['net_category_points'], best)
            for plan in result['plans']:
                roster_ids = {'0','9'}
                for step in plan['steps']:
                    self.assertIn(step['drop_site_player_id'], roster_ids)
                    roster_ids.remove(step['drop_site_player_id'])
                    roster_ids.add(step['add_site_player_id'])
                self.assertEqual(roster_ids, {p['site_player_id'] for p in plan['final_roster']})
                self.assertEqual([w['acquisitions_used_after'] for w in plan['weeks']],
                    [3+plan['weeks'][0]['acquisitions'], *[w['acquisitions'] for w in plan['weeks'][1:]]])
                self.assertTrue(all(w['acquisitions_used_after'] <= w['acquisition_limit'] for w in plan['weeks']))

    def test_hold_when_cost_exceeds_gain_and_rolling_route(self):
        weekly = self.run_plan(config=SearchConfig(beam_width=100, candidate_limit=2,
            drop_limit=1, result_limit=20, acquisition_cost=100))
        self.assertEqual(weekly['plans'][0]['acquisitions'], 0)
        self.assertIn('id="advisorHorizon"', HTML)
        handler = make_handler(self.inputs, transaction_evidence=self.evidence)
        request = object.__new__(handler)
        request.path = '/api/rolling?horizon=3'
        request.wfile = BytesIO()
        codes = []
        request.send_response = codes.append
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        with patch('app.dashboard.optimize_horizon', return_value={'plans': []}) as mocked:
            request.do_GET()
        self.assertEqual(codes, [200])
        self.assertEqual(mocked.call_args.kwargs['horizon'], 3)

    def test_week_seven_extended_boundary_and_cup_gap(self):
        start = date(2026, 12, 4)
        as_of = datetime(2026, 12, 4, 12, tzinfo=UTC)
        self.schedule.team_dates = {}
        for day in (start, date(2026, 12, 18)):
            for team in ('ANCHOR', 'OLD'):
                self.schedule.team_dates[(team, day)] = SimpleNamespace(
                    tipoff_utc=datetime.combine(day, time(21), UTC), tipoff_et=None)
        self.schedule.unscheduled_by_team = {'ANCHOR': 2}
        self.inputs['weeks'] = [FantasyWeek(i, D1, D2, 'unused') for i in range(1,7)] + [
            FantasyWeek(7, start, date(2026,12,17), 'regular_season'),
            FantasyWeek(8, date(2026,12,18), date(2026,12,24), 'regular_season')]
        opponent = self.forecast['weekly_matchups'][0]['totals']['5']['categories']
        matchups = []
        for week in self.inputs['weeks'][6:]:
            raw, _, _ = _dated_team_totals([self.old, self.anchor],
                [(self.old,self.projections['0']), (self.anchor,self.projections['9'])],
                self.schedule, week, as_of, D1)
            matchups.append({'week': week.number, 'teams': ['4','5'],
                'score': list(category_result(raw['categories'], opponent)[:3]),
                'totals': {'4': raw, '5': {'categories': opponent}}})
        base = {'as_of_utc': as_of.isoformat(), 'actual_completed_weeks': 6,
                'weekly_matchups': matchups,
                'periods': {str(m['week']): {'totals': m['totals']} for m in matchups}}
        evidence = {**self.evidence, 'week': 7, 'adds_used': 4}
        with patch('app.streaming.forecast', return_value=base), \
             patch('app.streaming._shortlist', return_value=([], [], self.projections,
                   {'snapshot_at_utc': START.isoformat()})), \
             patch('app.streaming.resolve_projection', side_effect=lambda p,w:p):
            result = optimize_horizon(self.inputs, horizon=2, as_of=as_of,
                now=as_of, evidence=evidence)
        weeks = result['plans'][0]['weeks']
        self.assertTrue(weeks[0]['is_extended'])
        self.assertTrue(weeks[0]['cup_gap_provisional'])
        self.assertFalse(weeks[1]['is_extended'])
        self.assertFalse(weeks[1]['cup_gap_provisional'])
        self.assertIn('cup_schedule_gap', result['plans'][0]['missing_verification'])

    def test_terminal_roster_loss_rejects_short_stream(self):
        days = [D1+timedelta(days=i) for i in range(6)]
        self.schedule.team_dates = {}
        for team, selected in {'ANCHOR': (days[0],days[2],days[4]),
                               'OLD': (days[0],days[4]), 'A': (days[0],)}.items():
            for day in selected:
                self.schedule.team_dates[(team,day)] = SimpleNamespace(
                    tipoff_utc=datetime.combine(day,time(21),UTC), tipoff_et=None)
        self.schedule.team_counts = {'ANCHOR':3,'OLD':2,'A':1}
        self.projections['1'] = projection(2,5,1,2,20)
        weeks = [FantasyWeek(i+1,days[2*i],days[2*i+1],'regular_season',
                             4 if i == 0 else 0) for i in range(3)]
        self.inputs['weeks'] = weeks
        matchups = []
        for week in weeks:
            raw, _, _ = _dated_team_totals([self.old,self.anchor],
                [(self.old,self.projections['0']),(self.anchor,self.projections['9'])],
                self.schedule,week,START,D1)
            opponent = dict(raw['categories'])
            opponent['pts'] = raw['categories']['pts']+(-1 if week.number == 3 else 1)
            matchups.append({'week':week.number,'teams':['4','5'],'status':'projected',
                'score':list(category_result(raw['categories'],opponent)[:3]),
                'totals':{'4':raw,'5':{'categories':opponent}}})
        base = {'as_of_utc':START.isoformat(),'actual_completed_weeks':0,
                'weekly_matchups':matchups,
                'periods':{str(m['week']):{'totals':m['totals']} for m in matchups}}
        evidence = {**self.evidence,'adds_used':3}
        audit = {'snapshot_at_utc':START.isoformat()}
        def run(weight):
            with patch('app.streaming.forecast',return_value=base), \
                 patch('app.streaming._shortlist',return_value=([self.a],[self.old],
                       self.projections,audit)), \
                 patch('app.streaming.resolve_projection',side_effect=lambda p,w:p):
                return optimize_horizon(self.inputs,horizon=2,as_of=START,now=START,
                    evidence=evidence,config=SearchConfig(candidate_limit=1,drop_limit=1,
                    beam_width=20,terminal_value_weight=weight))
        self.assertEqual(run(0)['plans'][0]['acquisitions'],1)
        protected = run(1)
        self.assertEqual(protected['plans'][0]['acquisitions'],0)
        streamer = next(p for p in protected['plans'] if p['acquisitions'])
        self.assertLess(streamer['conditional_rest_of_season_category_delta'],0)
        with patch('app.streaming.forecast', return_value=base), \
             patch('app.streaming._shortlist', return_value=([self.a], [self.old],
                   self.projections, audit)), \
             patch('app.streaming.resolve_projection', side_effect=lambda p, w: p):
            strategy = optimize_horizon(self.inputs, horizon=2, as_of=START, now=START,
                evidence=evidence, strategy_policy=StrategyPolicy(
                    protected_site_ids=('9',), streaming_slot_site_ids=('0',)))
        self.assertEqual([p['acquisitions'] for p in strategy['plans']], [0])
        # Both rosters lose future PTS, so unchanged future W-L-T alone must
        # not allow the loss of Old's remaining scheduled value.
        from copy import deepcopy
        flat_future = deepcopy(base)
        flat_future['weekly_matchups'][2]['totals']['5']['categories']['pts'] = 1000
        flat_future['weekly_matchups'][2]['score'] = list(category_result(
            flat_future['weekly_matchups'][2]['totals']['4']['categories'],
            flat_future['weekly_matchups'][2]['totals']['5']['categories'])[:3])
        with patch('app.streaming.forecast', return_value=flat_future), \
             patch('app.streaming._shortlist', return_value=([self.a], [self.old],
                   self.projections, audit)), \
             patch('app.streaming.resolve_projection', side_effect=lambda p, w: p):
            no_future_flip = optimize_horizon(self.inputs, horizon=2, as_of=START,
                now=START, evidence=evidence, strategy_policy=StrategyPolicy(
                    protected_site_ids=('9',), streaming_slot_site_ids=('0',)))
        self.assertEqual([p['acquisitions'] for p in no_future_flip['plans']], [0])
