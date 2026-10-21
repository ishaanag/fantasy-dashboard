import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture
"""Phase 4D: market value kept apart from competitive value, asymmetric labels, ranking and Pareto pruning."""
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase

from app.dashboard import HTML
from app.deterministic import forecast
from app.phase1 import ROOT, load_inputs
from app.trades import (MARKET_FLOOR, TradeConfig, _classify, _market_cost, _ordinal, _pareto, _robust, _score,
                        ecr_matches, market_label, market_values, trade_lab)
from app.waiver import _utc

CFG = TradeConfig()


def fake(me=4.0, them=0.0, give=50.0, get=50.0, my_cut=0.0, partner_cut=0.0, near=0.0, sources=None,
         narrow=0.0, partner='3', gives=('A',), gets=('X',), tid='t'):
    """A replayed package reduced to the fields the 4D rules read."""
    gap = (get - (give - partner_cut)) / max(abs(get), abs(give - partner_cut), MARKET_FLOOR)
    flips = [{'delta': me - narrow, 'narrow': False}] + ([{'delta': narrow, 'narrow': True}] if narrow else [])
    return {'id': tid, 'me': {'adjusted_delta': me, 'flips': flips, 'near_term_delta': near},
            'partner': {'adjusted_delta': them, 'team_id': partner}, 'sources': sources,
            'gives_all': [{'site_player_id': g} for g in gives], 'receives_all': [{'site_player_id': g} for g in gets],
            'value': {'gap': gap, 'model_gap': 0.0,
                      'market': {'give': give, 'get': get, 'my_cut': my_cut, 'partner_cut': partner_cut,
                                 'partner_surplus': give - get - partner_cut, 'my_cost': give + my_cut - get}}}


def both(me, them=1.0):
    return {k: {'me_points': me, 'me_adjusted': me, 'partner_points': them} for k in ('hashtag', 'leaguewinner')}


class AsymmetricRulesTests(TestCase):
    def test_overpay_in_market_with_meaningful_gain_ranks_and_is_an_opportunity(self):
        t = fake(me=5.0, them=0.2, give=60, get=35, sources=both(5.0, .2))      # partner gains 25 market points
        self.assertLess(t['value']['gap'], -CFG.fair_band)
        self.assertEqual(_classify(t, CFG)[0], 'recommended')
        self.assertEqual(market_label(t, CFG), 'Market opportunity')
        even = fake(me=2.0, them=0.2, sources=both(2.0, .2))
        self.assertGreater(_score(t, CFG), _score(even, CFG))                     # overpaying is not penalized away

    def test_overpay_without_meaningful_improvement_is_excluded(self):
        self.assertEqual(_classify(fake(me=1.0, give=60, get=30), CFG)[0], 'excluded')        # immaterial
        t = fake(me=2.0, give=70, get=20, sources=both(2.0))                                  # 50 points for 2 wins
        self.assertEqual(_classify(t, CFG), ('excluded', 'large market concession for a small, fragile or source-dependent gain'))
        big = fake(me=6.0, give=70, get=20, sources=both(6.0))                                # same cost, 6 wins
        self.assertEqual(_classify(big, CFG)[0], 'recommended')
        self.assertEqual(market_label(big, CFG), 'Market opportunity')
        dear = fake(me=3.5, give=70, get=20, sources=both(3.5))                               # 14 points a win
        self.assertEqual(_classify(dear, CFG)[0], 'recommended')
        self.assertEqual(market_label(dear, CFG), 'Expensive improvement')

    def test_partner_market_surplus_without_category_gain_still_qualifies(self):
        t = fake(me=4.0, them=-1.5, give=55, get=35, sources=both(4.0, -1.5))
        self.assertEqual(_classify(t, CFG)[0], 'recommended')                    # partner gain is not required
        self.assertEqual(market_label(t, CFG), 'Market opportunity')
        mutual = fake(me=4.0, them=2.0, give=55, get=35, sources=both(4.0, 2.0))
        self.assertEqual(market_label(mutual, CFG), 'Mutually beneficial')
        self.assertGreater(_score(mutual, CFG), _score(t, CFG))                  # supporting evidence only

    def test_fair_trade_with_almost_no_benefit_is_excluded(self):
        self.assertEqual(_classify(fake(me=.5, them=3.0, sources=both(.5, 3.0)), CFG)[0], 'excluded')

    def test_trade_favouring_me_by_market_is_never_shown(self):
        t = fake(me=18.0, give=40, get=52, sources=both(18.0))           # partner loses 23%: no manager would consider it
        self.assertEqual(_classify(t, CFG), ('excluded', 'partner would lose market value; not a realistic proposal'))
        small = fake(me=6.0, give=50, get=53, sources=both(6.0))        # partner loses under 10%
        self.assertEqual(_classify(small, CFG)[0], 'recommended')
        self.assertLess(_score(small, CFG), _score(fake(me=6.0, sources=both(6.0)), CFG))   # any partner loss costs rank

    def test_source_disagreement_lowers_robust_gain(self):
        split = {'hashtag': {'me_points': 6, 'me_adjusted': 6, 'partner_points': 0},
                 'leaguewinner': {'me_points': 1, 'me_adjusted': 1, 'partner_points': 0}}
        t = fake(me=5.0, sources=split)
        self.assertEqual(_robust(t, CFG), 1.0)
        self.assertEqual(_score(t, CFG) < _score(fake(me=5.0, sources=both(5.0)), CFG), True)
        self.assertEqual(t['ranking']['robust_gain'], 1.0)
        fails = {k: dict(v, me_points=-1, me_adjusted=-1) for k, v in split.items()}
        self.assertEqual(_classify(fake(me=5.0, sources=fails), CFG)[0], 'excluded')

    def test_forced_cut_counts_in_market_cost(self):
        t = fake(me=4.0, give=40, get=40, my_cut=12)                    # 1-for-2: I cut a player worth 12
        self.assertEqual(_market_cost(t), 12)
        p = fake(me=4.0, give=40, get=40, partner_cut=12)               # 2-for-1: they cut a player worth 12
        self.assertEqual(p['value']['market']['partner_surplus'], -12)
        self.assertGreater(p['value']['gap'], 0)

    def test_similar_gains_prefer_less_market_value_and_pareto_drops_variants(self):
        cheap = fake(me=4.0, give=50, get=45, gets=('X',), tid='cheap')
        dear = fake(me=4.0, give=70, get=45, gets=('Y',), tid='dear')                  # shares A, costs more
        other = fake(me=4.0, give=70, get=45, gives=('B',), gets=('Z',), tid='other')  # different players
        for t in (cheap, dear, other):
            t['effective'] = {'me': 4.0, 'partner': 0.0}
        self.assertGreater(_score(cheap, CFG), _score(dear, CFG))
        kept, dropped = _pareto([cheap, dear, other], CFG)
        self.assertEqual([t['id'] for t in kept], ['cheap', 'other'])
        self.assertEqual(dear['dominated_by'], 'cheap')
        better = fake(me=5.0, give=70, get=45, gets=('Y',), tid='better')              # more gain: not dominated
        self.assertEqual(len(_pareto([cheap, better], CFG)[0]), 2)


class MarketValueTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.as_of = _utc(cls.inputs['availability']['captured_at_utc'])
        cls.ctx = SimpleNamespace(players={p.site_player_id: p for t in cls.inputs['teams'] for p in cls.inputs['rosters'][t]},
                                  site={p.site_player_id: p for p in cls.inputs['site_players']}, as_of=cls.as_of)

    def test_normalization_matching_and_coverage(self):
        self.assertEqual(_ordinal({'a': 1.4, 'b': 3.0, 'c': 3.0, 'd': 7}), {'a': 1, 'b': 2.5, 'c': 2.5, 'd': 4})
        matched, unmatched = ecr_matches(self.inputs)
        names = {self.ctx.site[p].name for p in matched}
        self.assertIn('N. Alexander-Walker', names)                    # Yahoo's abbreviated name
        self.assertLessEqual(len(unmatched), 10)
        m = market_values(self.inputs, self.ctx, self.as_of)
        active = [p for p, x in self.ctx.players.items() if x.assigned_slot != 'IL']
        self.assertTrue(all(m[p]['basis'] == 'market_consensus' for p in active))
        for p in active:
            sig = [s['value'] for s in m[p]['signals'].values()]
            self.assertAlmostEqual(m[p]['value'], sum(sig) / len(sig))
        jokic = next(p for p in active if self.ctx.players[p].name == 'Nikola Jokić')
        self.assertEqual(m[jokic]['signals']['adp']['position'], 1)
        self.assertEqual(m[jokic]['signals']['ecr']['position'], 1)

    def test_depth_discount_stops_two_good_players_for_the_best(self):
        m = market_values(self.inputs, self.ctx, self.as_of)
        pid = {p.name: p.site_player_id for p in self.inputs['site_players']}

        def side(*names):
            vals = sorted((m[pid[n]]['value'] for n in names), reverse=True)
            return vals[0] + CFG.depth_discount * sum(vals[1:])
        get, give = side('Nikola Jokić', 'Yaxel Lendeborg'), side('Kawhi Leonard', 'Chet Holmgren')
        added = (m[pid['Nikola Jokić']]['value'] + m[pid['Yaxel Lendeborg']]['value']
                 - m[pid['Kawhi Leonard']]['value'] - m[pid['Chet Holmgren']]['value'])
        self.assertLess(abs(added), 5)                                     # plain sums called this even
        self.assertGreater((get - give) / get, CFG.viable_market_limit)    # with the discount the partner loses too much

    def test_stale_or_missing_consensus_falls_back_to_adp(self):
        stale = market_values(self.inputs, self.ctx, self.as_of + timedelta(days=30))
        self.assertTrue(all(v['basis'] in ('yahoo_preseason_adp',) for v in stale.values()))
        none = market_values({**self.inputs, 'ecr': None}, self.ctx, self.as_of)
        self.assertTrue(all('ecr' not in v['signals'] for v in none.values()))
        neither = market_values({**self.inputs, 'ecr': None, 'adp': None}, self.ctx, self.as_of)
        self.assertTrue(all(v['basis'] == 'projection_source_rank' for v in neither.values()))


class TradeLab4DTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.as_of = _utc(cls.inputs['availability']['captured_at_utc'])
        bases = {w: forecast(cls.inputs, hashtag_weight=w, as_of=cls.as_of) for w in (.5, 1.0, 0.0)}
        cls.kw = dict(as_of=cls.as_of, now=cls.as_of, base=bases[.5], source_bases={1.0: bases[1.0], 0.0: bases[0.0]})
        cls.cfg = TradeConfig(partner_ids=('3', '5'), package_types=('1-1', '2-1', '1-2'))
        cls.result = trade_lab(cls.inputs, config=cls.cfg, **cls.kw)

    def test_package_valuation_and_labels_on_real_packages(self):
        r = self.result
        m = market_values(self.inputs, SimpleNamespace(players={p.site_player_id: p for t in self.inputs['teams']
                                                                 for p in self.inputs['rosters'][t]},
                                                        site={p.site_player_id: p for p in self.inputs['site_players']}),
                          self.as_of)
        for t in r['cards']:
            mk = t['value']['market']
            for key, side in (('give', 'gives_all'), ('get', 'receives_all')):   # best in full, the rest discounted
                vals = sorted((m[p['site_player_id']]['value'] for p in t[side]), reverse=True)
                self.assertAlmostEqual(mk[key], vals[0] + CFG.depth_discount * sum(vals[1:]))
            for side, key in (('me', 'my_cut'), ('partner', 'partner_cut')):
                b = t['balancing'][side]
                self.assertAlmostEqual(mk[key], m[b['player']['site_player_id']]['value'] if b['kind'] == 'drop' else 0.0)
            self.assertAlmostEqual(mk['partner_surplus'], mk['give'] - mk['get'] - mk['partner_cut'])
            self.assertAlmostEqual(sum(t['ranking'].values()), t['score'], places=3)
            self.assertIn(t['display']['label'], ('Market opportunity', 'Market-balanced', 'Expensive improvement',
                                                  'Mutually beneficial'))
            for k in ('my_gain', 'give_up', 'they_receive', 'why_consider', 'why_benefit', 'risks'):
                self.assertTrue(t['display'][k] is not None)
        self.assertTrue(all(t['value']['gap'] <= self.cfg.viable_market_limit for t in r['cards']))
        self.assertEqual(r['market']['basis_counts'], {'market_consensus': r['market']['rostered']})

    def test_candidate_filtering_never_drops_partner_premiums(self):
        ev = self.result['all_evaluated']
        self.assertTrue(any(e['gap'] < -.5 for e in ev))          # big premiums to the partner are still replayed
        # The pre-replay check assumes each side's cheapest cut; the exact replay may cut someone else.
        self.assertTrue(all(e['gap'] <= self.cfg.viable_market_limit or
                            any(b['kind'] == 'drop' for b in e['balancing'].values()) for e in ev))
        dominated = [e for e in ev if e['dominated_by']]
        self.assertEqual(len(dominated), self.result['diagnostics']['dominated_variations'])
        shown = {t['id'] for t in self.result['cards']}
        self.assertFalse(shown & {e['id'] for e in dominated})

    def test_reproducible(self):
        again = trade_lab(self.inputs, config=self.cfg, **self.kw)
        key = lambda r: [(e['id'], round(e['score'], 9), e['tier'], e['label']) for e in r['all_evaluated']]
        self.assertEqual(key(again), key(self.result))
        self.assertEqual([t['id'] for t in again['cards']], [t['id'] for t in self.result['cards']])

    def test_page_markers(self):
        self.assertNotIn('High upside, difficult sells', HTML)
        for marker in ("'Market to them'", "'What I give up'", "'Why they would consider it'",
                       "'Why I benefit'", "'Risks'", 'Market value sources', "'ECR'"):
            self.assertIn(marker, HTML)
