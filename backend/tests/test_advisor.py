import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture
"""Phase 3E Waiver Advisor: engine equivalence, saved-snapshot behaviour, exhaustive fixture."""
from datetime import date, datetime, time, timedelta, timezone
from itertools import product
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.advisor import AdvisorConfig, _Engine, _categories, _days, advise
from app.deterministic import forecast
from app.phase1 import (CATEGORIES, EASTERN, ROOT, VOLUMES, RosterPlayer, _dated_team_totals,
                        _lineup_score, load_inputs, resolve_projection)
from app.schedule import FantasyWeek
from app.simulator import category_result
from app.waiver import _margin, _scales, _smooth, _utc

UTC = timezone.utc


# Behaviour tests use a small search budget; SavedSnapshotAdvisorTests.setUpClass keeps the full default search.
FAST = dict(beam_width=8, expand_adds=12, season_shortlist=6, local_evaluations=20_000)


class SavedSnapshotAdvisorTests(TestCase):
    """Archived capture without Yahoo transaction evidence: hypothetical but still useful."""

    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.as_of = _utc(cls.inputs['availability']['captured_at_utc'])
        cls.base = forecast(cls.inputs, hashtag_weight=.5, as_of=cls.as_of)
        cls.result = advise(cls.inputs, as_of=cls.as_of, now=cls.as_of, base=cls.base)

    def test_engine_matches_phase2_lineups_with_add_and_drop_windows(self):
        team = self.inputs['league']['my_team_id']
        roster = self.inputs['rosters'][team]
        players = {p.site_player_id: p for p in roster}
        fa = next(p for p in self.inputs['site_players'] if p.availability_label == 'FA'
                  and p.site_player_id in self.inputs['projections'] and len(self.inputs['projections'][p.site_player_id]) == 2
                  and p.nba_team in self.inputs['schedule'].team_counts)
        players[fa.site_player_id] = RosterPlayer(team, fa.site_player_id, fa.name, 'BN', fa.eligible_positions, fa.nba_team)
        proj = {pid: resolve_projection(self.inputs['projections'][pid], .5) for pid in players}
        drop = min((p for p in roster if p.assigned_slot != 'IL'),
                   key=lambda p: _lineup_score(proj[p.site_player_id]['values'])).site_player_id
        snapshot = date.fromisoformat(self.inputs['availability']['lineup_date'])
        engine = _Engine(self.inputs['schedule'], players, proj, self.as_of, snapshot)
        stamp = self.as_of + timedelta(days=2)
        since, until = {fa.site_player_id: stamp}, {drop: stamp}
        for week in self.inputs['weeks'][:3]:
            raw, _, _ = _dated_team_totals(list(players.values()), [(players[p], proj[p]) for p in players],
                                           self.inputs['schedule'], week, self.as_of, snapshot, since, until)
            ours = _categories(engine.period(list(_days(week.start, week.end)), frozenset(players), since, until)[0])
            for c in CATEGORIES:
                self.assertAlmostEqual(ours['categories'][c], raw['categories'][c], places=9)

    def test_strategies_are_legal_distinct_explained_and_provisional(self):
        r = self.result
        json.dumps(r, allow_nan=False)
        self.assertEqual(r['hold']['eligibility_class'], 'hold')
        self.assertIn('transaction_evidence_missing', r['evidence_reasons'])
        self.assertLessEqual(len(r['strategies']), 5)
        roles = {row['site_player_id']: row['role'] for row in r['diagnostics']['roster']}
        seen = set()
        for s in r['strategies']:
            self.assertGreaterEqual(s['rank_value'], .25)
            tx = frozenset((m['add_site_player_id'], m['drop_site_player_id']) for m in s['moves'])
            self.assertTrue(all(tx != other and not tx < other and not tx > other for other in seen))
            seen.add(tx)
            added, dates = set(), [m['date'] for m in s['moves']]
            self.assertEqual(dates, sorted(dates))
            for m in s['moves']:
                self.assertNotIn(m['add_site_player_id'], roles)       # never re-adds a dropped player
                self.assertNotIn(m['add_site_player_id'], added)
                self.assertNotIn(m['add_status'], {'O', 'NA'})
                added.add(m['add_site_player_id'])
                if m['drop_site_player_id'] in roles:
                    self.assertEqual(roles[m['drop_site_player_id']], 'replaceable')
            self.assertTrue(all(w['acquisitions_used_after'] <= w['acquisition_limit'] for w in s['weeks']))
            self.assertTrue(s['why'])
            self.assertNotEqual(s['eligibility_class'], 'verified_current')
        if not r['strategies']:
            self.assertTrue(r['closest_alternatives'])
        pool = r['diagnostics']['pool']
        self.assertEqual(pool['yahoo_players'], len(self.inputs['site_players']))
        self.assertEqual(pool['eligible_with_games'], pool['single_moves_searched'])
        self.assertGreater(pool['single_moves_searched'], pool['carried_into_combinations'])

    def test_reproducible_and_full_search_at_least_as_good(self):
        fast = [advise(self.inputs, as_of=self.as_of, now=self.as_of, base=self.base, config=AdvisorConfig(**FAST))
                for _ in range(2)]
        key = lambda r: [(s['value'], [(m['date'], m['add_site_player_id'], m['drop_site_player_id'])
                          for m in s['moves']]) for s in r['strategies'] + r['closest_alternatives']]
        self.assertEqual(key(fast[0]), key(fast[1]))        # deterministic, parallel workers included
        best = lambda r: max([s['rank_value'] for s in r['strategies']] or [0.0])
        self.assertGreaterEqual(best(self.result), best(fast[0]) - 1e-9)   # the full search never does worse
        search = self.result['diagnostics']['search']
        self.assertGreater(search['local_search']['evaluations'], 0)
        self.assertFalse(search['local_search']['stopped_by_clock'])

    def test_hold_explains_closest_alternatives(self):
        r = advise(self.inputs, as_of=self.as_of, now=self.as_of, base=self.base,
                   config=AdvisorConfig(recommend_threshold=50, **FAST))
        self.assertEqual((r['verdict'], r['strategies']), ('hold', []))
        self.assertTrue(1 <= len(r['closest_alternatives']) <= 3)
        self.assertTrue(all(any('below the 50' in x for x in a['why_not']) for a in r['closest_alternatives']))
        self.assertTrue(r['headline'].startswith('HOLD'))

    def test_valuable_players_not_sacrificed_without_core_rule(self):
        # With the protected core switched off, only the rest-of-season penalty guards stars.
        r = advise(self.inputs, as_of=self.as_of, now=self.as_of, base=self.base,
                   config=AdvisorConfig(core_margin=100, **FAST))
        values = sorted((row['season_value'], row['site_player_id']) for row in r['diagnostics']['roster'])
        stars = {pid for _, pid in values[-4:]}
        for s in r['strategies']:
            self.assertFalse(stars & {m['drop_site_player_id'] for m in s['moves']})
            self.assertNotEqual(s['season']['assessment'], 'cost')


START = datetime(2026, 10, 20, 12, tzinfo=UTC)
D1, D2 = date(2026, 10, 20), date(2026, 10, 21)


def projection(fgm, fga, ftm, fta, pts, ast=0, reb=0):
    values = {key: 0.0 for key in VOLUMES}
    values.update(fgm=fgm, fga=fga, ftm=ftm, fta=fta, pts=pts, ast=ast, reb=reb, gp=82.0)
    return {'values': values, 'coverage': {}, 'source_rows': {}, 'disagreement': {}}


class FixtureSchedule:
    def __init__(self):
        games = {'ANCHOR': (D1, D2), 'OLD': (D1,), 'WEAK': (D2,), 'A': (D1,), 'B': (D2,), 'C': (D1, D2), 'OPP': (D1, D2)}
        self.team_dates = {(team, day): SimpleNamespace(tipoff_utc=datetime.combine(day, time(23), UTC), tipoff_et=None)
                           for team, days in games.items() for day in days}
        self.team_counts = {team: len(days) for team, days in games.items()}

    def games_for(self, team, start, end, *, as_of=None):
        return [g for (name, day), g in self.team_dates.items()
                if name == team and start <= day <= end and (as_of is None or g.tipoff_utc > as_of)]


class ExhaustiveFixtureTests(TestCase):
    """Two days, three free agents, two adds left: compare against every legal sequence."""

    def test_beam_matches_exhaustive_optimum(self):
        week = FantasyWeek(1, D1, D2, 'regular_season')
        schedule = FixtureSchedule()
        mine = [RosterPlayer('4', '0', 'Old', 'BN', ('PG',), 'OLD'),
                RosterPlayer('4', '8', 'Weak', 'BN', ('SG',), 'WEAK'),
                RosterPlayer('4', '9', 'Anchor', 'BN', ('C',), 'ANCHOR')]
        theirs = [RosterPlayer('5', '7', 'Rival', 'BN', ('PF',), 'OPP')]
        free = {'1': ('A', ('PG',)), '2': ('B', ('SG',)), '3': ('C', ('SF',))}
        projections = {'0': projection(2, 5, 1, 2, 5), '8': projection(1, 4, 0, 0, 3, 1),
                       '9': projection(0, 1, 0, 1, 1), '1': projection(8, 10, 1, 2, 20),
                       '2': projection(1, 2, 9, 10, 10, 10), '3': projection(4, 9, 2, 3, 11, 2, 6),
                       '7': projection(6, 12, 3, 4, 16, 3, 4)}
        projected = lambda squad: [(p, projections[p.site_player_id]) for p in squad]
        totals = {t: _dated_team_totals(r, projected(r), schedule, week, START, D1)[0]
                  for t, r in (('4', mine), ('5', theirs))}
        score = list(category_result(totals['4']['categories'], totals['5']['categories'])[:3])
        base = {'as_of_utc': '2026-10-20T12:00:00Z', 'hashtag_weight': .5, 'actual_completed_weeks': 0,
                'weekly_matchups': [{'week': 1, 'teams': ['4', '5'], 'score': score, 'totals': totals,
                                     'status': 'projected'}],
                'periods': {'1': {'totals': totals}}}
        site = [SimpleNamespace(site_player_id=k, name=n, eligible_positions=pos, nba_team=n,
                                availability_label='FA', fantasy_team_id=None) for k, (n, pos) in free.items()]
        inputs = {'league': {'my_team_id': '4', 'league_id': '1'},
                  'availability': {'captured_at_utc': START.isoformat(), 'lineup_date': D1.isoformat(),
                                   'player_fingerprint': 'fixture'},
                  'teams': {'4': 'Me', '5': 'Rival'}, 'rosters': {'4': mine, '5': theirs},
                  'projections': projections, 'schedule': schedule, 'weeks': [week], 'site_players': site}
        evidence = {'captured_at_utc': START.isoformat(), 'player_fingerprint': 'fixture', 'team_id': '4',
                    'week': 1, 'adds_used': 2, 'cannot_cut_site_ids': ['9'], 'locked_site_ids': [],
                    'timing_by_site_id': {k: {'kind': 'free_agent', 'effective_at_utc': START.isoformat()} for k in free},
                    'empty_slot_allowed': False, 'direct_il_site_ids': [], 'il_eligible_site_ids': [],
                    'active_claim_site_ids': []}
        audit = {'rows': [{'site_player_id': k, 'owner_team_id': None, 'availability': 'FA', 'projectable': True,
                           'issues': []} for k in free], 'snapshot_at_utc': START.isoformat()}
        config = AdvisorConfig(fragile_flip_discount=0, beam_width=50, result_limit=20)
        with patch('app.advisor.pool_audit', return_value=audit), \
             patch('app.advisor.resolve_projection', side_effect=lambda p, w: p):
            result = advise(inputs, as_of=START, now=START, evidence=evidence, base=base, config=config)

        scales = _scales(base)
        everyone = {p.site_player_id: p for p in mine}
        everyone.update({k: RosterPlayer('4', k, n, 'BN', pos, n) for k, (n, pos) in free.items()})

        def value(steps):
            since = {a: s for _, a, _, s in steps}
            until = {d: s for _, _, d, s in steps}
            squad = [everyone[p] for p in sorted({'0', '8', '9'} | set(since))]
            raw = _dated_team_totals(squad, projected(squad), schedule, week, START, D1, since, until)[0]
            w, _, t, _ = category_result(raw['categories'], totals['5']['categories'])
            margins = {c: _margin(raw['categories'], totals['5']['categories'], c) for c in CATEGORIES}
            return w + .5*t, _smooth(margins, scales)
        hold_points, hold_soft = value(())
        stamps = {D1: START, D2: datetime.combine(D2, time.min, EASTERN).astimezone(UTC)}
        best = 0.0
        moves = [(day, add) for day in (D1, D2) for add in free]
        for n in (1, 2):
            for picks in product(moves, repeat=n):
                if list(picks) != sorted(set(picks)) or len({a for _, a in picks}) != n:
                    continue
                for drops in product(['0', '8', *free], repeat=n):
                    current, steps, legal = {'0', '8'}, [], True
                    for (day, add), drop in zip(picks, drops):
                        if drop not in current:
                            legal = False
                            break
                        current = (current - {drop}) | {add}
                        steps.append((day, add, drop, stamps[day]))
                    if legal:
                        points, soft = value(tuple(steps))
                        best = max(best, points - hold_points + .5*(soft - hold_soft) - .15*n)
        shown = [s['value'] for s in result['strategies'] + result['closest_alternatives']] + [0.0]
        self.assertAlmostEqual(max(shown), round(best, 3), places=9)   # values are reported to 3 decimals
        self.assertGreater(best, .25)
        self.assertEqual(result['verdict'], 'act')
        self.assertTrue(all(m['drop_site_player_id'] != '9' for s in result['strategies'] for m in s['moves']))


class AdvisorRouteTests(TestCase):
    def test_route_uses_cached_forecast_and_horizon(self):
        from io import BytesIO
        from app.dashboard import HTML, make_handler
        handler = make_handler({'league': {'my_team_id': '4'}})
        request = object.__new__(handler)
        request.path = '/api/advisor?hashtag_weight=0.5&horizon=3'
        request.wfile, codes = BytesIO(), []
        request.send_response = codes.append
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        with patch('app.dashboard.advise', return_value={'strategies': []}) as mocked, \
             patch('app.dashboard.forecast', return_value={'cached': True}):
            request.do_GET()
        self.assertEqual(codes, [200])
        self.assertEqual(mocked.call_args.kwargs['config'].horizon, 3)
        self.assertEqual(mocked.call_args.kwargs['base'], {'cached': True})
        self.assertIn('id="advisorResults"', HTML)
        self.assertNotIn('id="weeklyPlan"', HTML)
