import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture
"""Phase 4A Trade Lab: two-team replay, asset value, guards, screening recall, safeguards and route."""
from collections import Counter
from datetime import datetime, time, timedelta, timezone
import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from app.dashboard import HTML, make_handler
from app.deterministic import forecast
from app.phase1 import (CATEGORIES, EASTERN, ROOT, SLOTS, RosterPlayer, _dated_team_totals, assign_lineup,
                        eligible, load_inputs, resolve_projection)
from app.trades import (TradeConfig, _classify, _Context, asset_values, evaluate, fairness_label, trade_lab,
                        value_gap)
from app.waiver import _utc

UTC = timezone.utc


class SavedSnapshotTradeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.as_of = _utc(cls.inputs['availability']['captured_at_utc'])
        cls.base = forecast(cls.inputs, hashtag_weight=.5, as_of=cls.as_of)
        cls.result = trade_lab(cls.inputs, as_of=cls.as_of, now=cls.as_of, base=cls.base,
                               config=TradeConfig(package_types=('1-1',)))   # 4A covers 1-for-1 trades
        cls.eff = date_ = cls.as_of.astimezone(EASTERN).date() + timedelta(days=3)
        cls.ctx = _Context(cls.inputs, cls.base, .5, cls.as_of, None, date_)
        cls.me = cls.inputs['league']['my_team_id']
        cls.card = next(c for c in cls.result['cards'] if c.get('package', '1-1') == '1-1')   # 4A tests replay a 1-for-1

    def test_hold_reproduces_forecast(self):
        mine = next(t for t in self.base['teams'] if t['team_id'] == self.me)
        self.assertEqual(self.result['hold']['record'], mine['projected_record'])
        self.assertEqual(self.result['hold']['seed'], mine['projected_seed'])
        self.assertEqual(self.result['timing']['earliest_effective_day'], self.eff.isoformat())
        self.assertEqual(self.result['timing']['trade_deadline'], '2027-03-04')

    def test_both_teams_match_independent_phase2_totals(self):
        c, inputs = self.card, self.inputs
        partner, give, get = c['partner']['team_id'], c['gives']['site_player_id'], c['receives']['site_player_id']
        eff_time = datetime.combine(self.eff, time(0), EASTERN).astimezone(UTC)
        for team, out_id, in_id in ((self.me, give, get), (partner, get, give)):
            src = next(p for p in inputs['rosters'][partner if team == self.me else self.me] if p.site_player_id == in_id)
            roster = [*inputs['rosters'][team], RosterPlayer(team, in_id, src.name, 'BN', src.eligible_positions, src.nba_team)]
            proj = [(p, resolve_projection(inputs['projections'][p.site_player_id], .5)) for p in roster]
            raws = self.ctx.team_weeks(team, out_id, in_id)['raw']
            for wi, week in enumerate(self.ctx.weeks[:4]):   # includes the effective-day split in Week 1
                totals, _, _ = _dated_team_totals(roster, proj, inputs['schedule'], week, self.as_of,
                                                  self.ctx.engine.snapshot_day, {in_id: eff_time}, {out_id: eff_time},
                                                  injury_inputs=inputs)
                for cat in CATEGORIES:
                    self.assertAlmostEqual(raws[wi]['categories'][cat], totals['categories'][cat], places=6)

    def test_lineups_legal_and_shooting_from_volumes(self):
        c = self.card
        eng = self.ctx.engine
        roster = (frozenset(p.site_player_id for p in self.inputs['rosters'][self.me]) - {c['gives']['site_player_id']}) \
            | {c['receives']['site_player_id']}
        for day in self.ctx.week_days[2]:
            _, starters = eng.day(day, roster, {}, {})
            self.assertLessEqual(len(starters), 10)
            placed = assign_lineup([eng.players[p] for p in starters], {p: eng.score[p] for p in starters},
                                   require_full=False)
            self.assertEqual(set(placed), set(starters))
            used = Counter(placed.values())
            self.assertTrue(all(used[slot] <= SLOTS.count(slot) for slot in used))
            self.assertTrue(all(eligible(slot, eng.players[p].eligible_positions) for p, slot in placed.items()))
        for raw in self.ctx.team_weeks(self.me, c['gives']['site_player_id'], c['receives']['site_player_id'])['raw']:
            v = raw['shooting_volumes']
            self.assertAlmostEqual(raw['categories']['fg_pct'], v['fgm'] / v['fga'])
            self.assertAlmostEqual(raw['categories']['ft_pct'], v['ftm'] / v['fta'])

    def test_standings_reciprocal_and_head_to_head_recomputed(self):
        c = self.card
        partner, give, get = c['partner']['team_id'], c['gives']['site_player_id'], c['receives']['site_player_id']
        changed = {self.me: self.ctx.team_weeks(self.me, give, get)['raw'],
                   partner: self.ctx.team_weeks(partner, get, give)['raw']}
        season = self.ctx.season(changed)
        self.assertTrue(all(sum(r) == 162 for r in season['records'].values()))
        self.assertEqual(sum(r[0] for r in season['records'].values()), sum(r[1] for r in season['records'].values()))
        mine, theirs = self.ctx.rows(self.me, changed), self.ctx.rows(partner, changed)
        for a in mine:
            if a['opponent'] == partner:
                b = next(r for r in theirs if r['week'] == a['week'])
                self.assertEqual(a['score'], [b['score'][1], b['score'][0], b['score'][2]])
        self.assertEqual(season['records'][self.me], c['me']['record_after'])
        self.assertEqual(season['records'][partner], c['partner']['record_after'])

    def test_asset_values_and_fairness(self):
        values = asset_values(self.ctx, {})
        by_name = {self.ctx.players[p].name: v for p, v in values.items()}
        self.assertGreater(by_name['Nikola Jokić']['value'], by_name['Myles Turner']['value'])
        for v in values.values():
            self.assertAlmostEqual(v['value'], (v['per_team_game'] - v['replacement']) * v['team_games'])
        self.assertAlmostEqual(value_gap(300, 200), -value_gap(200, 300))
        cfg = TradeConfig()
        self.assertEqual(fairness_label(value_gap(210, 200), 210, 200, cfg), 'Fair')
        self.assertEqual(fairness_label(value_gap(100, 200), 100, 200, cfg), 'Overpay')
        self.assertEqual(fairness_label(value_gap(10, -5), 10, -5, cfg), 'Both near replacement level')

    def test_guards_against_immaterial_lopsided_and_source_failure(self):
        cfg = TradeConfig()   # Phase 4D rules; the full scenario set is in test_trades_4d.py

        def t(me, them, gap, narrow=0.0, sources=None):
            return {'me': {'adjusted_delta': me, 'flips': [{'delta': narrow, 'narrow': True}] if narrow else []},
                    'partner': {'adjusted_delta': them}, 'sources': sources,
                    'value': {'gap': gap, 'market': {'give': 50.0, 'get': 50.0 * (1 + gap), 'my_cut': 0.0}}}
        both = {'hashtag': {'me_points': 3, 'partner_points': 1, 'me_adjusted': 3},
                'leaguewinner': {'me_points': 2, 'partner_points': 1, 'me_adjusted': 2}}
        self.assertEqual(_classify(t(1.0, 2, 0), cfg)[0], 'excluded')                        # immaterial
        self.assertEqual(_classify(t(3.0, 0, .5), cfg)[0], 'excluded')                       # lopsided by market value
        fails = {k: {'me_points': 0, 'partner_points': 1, 'me_adjusted': 0} for k in both}
        self.assertEqual(_classify(t(3.0, 2, 0, sources=fails), cfg)[0], 'excluded')
        self.assertEqual(_classify(t(3.0, -2, -.3), cfg)[0], 'recommended')                  # partner loss is a risk, not a block
        self.assertEqual(_classify(t(3.0, 2, .25, sources=both), cfg)[0], 'excluded')        # partner loses market value

    def test_cards_distinct_conditional_and_never_executable(self):
        r, cfg = self.result, TradeConfig()
        cards = [c for c in r['cards'] if not c.get('alternative')]
        self.assertTrue(cards)
        pairs = [(c['gives']['site_player_id'], c['receives']['site_player_id']) for c in cards]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual(len({g for _, g in pairs}), len(pairs))
        self.assertLessEqual(max(sum(1 for g, _ in pairs if g == x) for x, _ in pairs), cfg.per_player_limit)
        self.assertEqual(len({e['id'] for e in r['all_evaluated']}), len(r['all_evaluated']))
        for c in cards:
            self.assertEqual(c['tier'], 'recommended')
            self.assertGreaterEqual(c['me']['adjusted_delta'], cfg.material_gain)
            self.assertNotEqual(c['gives']['slot'], 'IL')
            self.assertNotEqual(c['receives']['slot'], 'IL')
            self.assertEqual(set(c['sources']), {'hashtag', 'leaguewinner'})
            self.assertLessEqual(c['value']['gap'], cfg.fair_band)
            if c['label'] == 'Mutually beneficial':
                self.assertGreaterEqual(c['effective']['partner'], cfg.partner_credible_gain)
        v = r['verification']
        self.assertFalse(v['executable'])
        self.assertEqual(v['eligibility_class'], 'conditional_future')
        self.assertEqual(v['basis'], 'provisional_offline')      # no transaction evidence supplied
        self.assertIn('commissioner_review', v['always_unverified'])

    def test_protection_partner_selection_and_screen_recall(self):
        holm = next(p.site_player_id for p in self.inputs['rosters'][self.me] if p.name == 'Chet Holmgren')
        kw = dict(as_of=self.as_of, now=self.as_of, base=self.base)
        full = trade_lab(self.inputs, config=TradeConfig(partner_ids=('3',), protected_ids=frozenset({holm}),
                                                          shortlist=None, finalists=0), **kw)
        screened = trade_lab(self.inputs, config=TradeConfig(partner_ids=('3',), protected_ids=frozenset({holm}),
                                                              shortlist=40, finalists=0), **kw)
        for run in (full, screened):
            self.assertTrue(all(e['partner'] == '3' and e['gives'] != 'Chet Holmgren' for e in run['all_evaluated']))
            self.assertEqual(run['cards'], [])                  # unchecked candidates are never shown
        self.assertTrue(full['diagnostics']['exhaustive'])
        exact = {e['id']: e for e in full['all_evaluated']}
        for e in screened['all_evaluated']:                     # same replay numbers either way
            self.assertEqual((e['me'], e['partner_adjusted']), (exact[e['id']]['me'], exact[e['id']]['partner_adjusted']))
        cfg = TradeConfig()   # leading exact candidates that could still qualify (viable by market and model or screen)
        best = [e['id'] for e in sorted(full['all_evaluated'], key=lambda e: -e['score'])
                if e['me'] >= 1.5 and e['gap'] <= cfg.viable_market_limit and (
                    e['model_gap'] >= -cfg.viable_model_floor or e['screen_me'] >= cfg.floor_screen_gate)][:5]
        kept = {e['id'] for e in screened['all_evaluated']}
        self.assertGreaterEqual(sum(i in kept for i in best), 4)  # screen recall on the leading exact candidates

    def test_deadline_and_as_of_safeguards(self):
        with self.assertRaisesRegex(ValueError, 'deadline'):
            trade_lab(self.inputs, as_of=datetime(2027, 3, 5, 17, tzinfo=UTC), now=self.as_of)
        with self.assertRaises(ValueError):
            trade_lab(self.inputs, as_of=self.as_of - timedelta(days=1), now=self.as_of)
        with self.assertRaisesRegex(ValueError, 'Protected'):
            trade_lab(self.inputs, as_of=self.as_of, now=self.as_of, base=self.base,
                      config=TradeConfig(protected_ids=frozenset({'not-mine'})))


def _get(handler, path):
    request = object.__new__(handler)
    request.path = path
    request.wfile, codes = BytesIO(), []
    request.send_response = codes.append
    request.send_header = lambda *args: None
    request.end_headers = lambda: None
    request.do_GET()
    return codes, json.loads(request.wfile.getvalue() or b'null')


class TradeRouteTests(TestCase):
    def test_route_caches_and_recomputes_verification(self):
        inputs = {'league': {'my_team_id': '4'}, 'availability': {'captured_at_utc': '2026-10-20T14:11:47Z'}}
        handler = make_handler(inputs)
        with patch('app.dashboard.trade_lab', return_value={'cards': []}) as lab, \
                patch('app.dashboard.forecast', return_value={}):
            codes, body = _get(handler, '/api/trades?hashtag_weight=0.5&partners=3,1&protect=99')
            _get(handler, '/api/trades?hashtag_weight=0.5&partners=1,3&protect=99')
        self.assertEqual(codes, [200])
        self.assertEqual(lab.call_count, 1)
        config = lab.call_args.kwargs['config']
        self.assertEqual(config.partner_ids, ('1', '3'))
        self.assertEqual(config.protected_ids, frozenset({'99'}))
        self.assertFalse(body['verification']['executable'])

    def test_page_has_lazy_trade_lab(self):
        for marker in ('href="#trades"', 'id="view-trades"', 'id="tradeResults"', 'id="tradePartner"',
                       'id="tradeProtect"', 'function tradeCard', 'function tradeHold', 'function tradeCompare',
                       "view==='trades'&&tradesStale", 'not acceptance or win probabilities'):
            self.assertIn(marker, HTML)
