"""Phase 4E Trade Lab presentation: briefs built from a saved live Trade Lab result (October 21, 2026, 17:15Z
capture, all partners and package types). Presentation only; every check compares against computed fields."""
from copy import deepcopy
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.dashboard import HTML, make_handler
from app.presentation import trade_briefs
from test_trades import _get

FIXTURE = Path(__file__).parent / 'fixtures' / 'trades_4d_2026-10-21T1715Z.json'


class TradeBriefTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(FIXTURE.read_text(encoding='utf-8'))
        cls.before = json.dumps(cls.raw, sort_keys=True)
        cls.out = trade_briefs(cls.raw)
        cls.cards = cls.out['cards']

    def test_input_unchanged_and_order_preserved(self):
        self.assertEqual(json.dumps(self.raw, sort_keys=True), self.before)
        self.assertEqual([(t['id'], t['score'], t['label']) for t in self.cards],
                         [(t['id'], t['score'], t['label']) for t in self.raw['cards']])
        for t, r in zip(self.cards, self.raw['cards']):
            self.assertEqual({k: v for k, v in t.items() if k != 'brief'}, r)

    def test_fixture_covers_multi_player_packages(self):
        self.assertEqual({t['package'] for t in self.cards}, {'2-2', '1-2'})

    def test_players_sent_and_received(self):
        for t in self.cards:
            b = t['brief']
            self.assertEqual([p['id'] for p in b['send']], [p['site_player_id'] for p in t['gives_all']])
            self.assertEqual([p['id'] for p in b['receive']], [p['site_player_id'] for p in t['receives_all']])
        first = self.cards[0]['brief']
        self.assertEqual([p['name'] for p in first['send']], ['Chet Holmgren', 'Jakob Poeltl'])
        self.assertEqual([p['name'] for p in first['receive']], ['Domantas Sabonis', "De'Aaron Fox"])

    def test_gain_and_horizon(self):
        weeks = self.raw['timing']['remaining_weeks']
        for t in self.cards:
            g = t['brief']['gain']
            self.assertEqual(g['robust'], t['ranking']['robust_gain'])
            self.assertEqual(g['blended'], t['effective']['me'])
            self.assertEqual(g['next_three'], t['me']['near_term_delta'])
            self.assertEqual((g['weeks'], g['first_week'], g['last_week']), (len(weeks), weeks[0], weeks[-1]))
            self.assertLessEqual(g['robust'], g['blended'])
        self.assertIn('over 18 weeks', self.cards[0]['brief']['why'])

    def test_market_surplus_and_bands(self):
        for t in self.cards:
            m = t['brief']['market']
            self.assertEqual(m['points'], t['value']['market']['partner_surplus'])
            self.assertAlmostEqual(m['pct'], -100 * t['value']['gap'])
            self.assertEqual((m['give'], m['get']), (t['value']['market']['give'], t['value']['market']['get']))
            self.assertGreaterEqual(m['pct'], -100 * .10 - 1e-9)   # the 10% partner-loss cap still holds
        self.assertEqual([t['brief']['market']['band'] for t in self.cards], ['even', 'less', 'even', 'less', 'more'])
        self.assertIn('ADP', self.cards[0]['brief']['market']['basis'])

    def test_expensive_label_sets_expensive_band(self):
        t = deepcopy(self.raw['cards'][4])
        t['display']['label'] = 'Expensive improvement'
        m = trade_briefs({**self.raw, 'cards': [t]})['cards'][0]['brief']['market']
        self.assertEqual(m['band'], 'expensive')

    def test_partner_category_change_and_tension(self):
        for t in self.cards:
            p = t['brief']['partner']
            self.assertEqual(p['change'], t['effective']['partner'])
            self.assertEqual(p['tension'], t['effective']['partner'] < 0)
        self.assertEqual([t['brief']['partner']['tension'] for t in self.cards], [False, True, True, True, True])
        self.assertIn('3.3 category wins worse', self.cards[1]['brief']['why'])   # −3.25 rounds as the page does
        self.assertIn('5.5 category wins worse', self.cards[4]['brief']['why'])   # positive surplus, still shown

    def test_source_agreement(self):
        for t in self.cards:
            s = t['brief']['sources']
            self.assertEqual([r['me'] for r in s['rows']], [x['me_adjusted'] for x in t['sources'].values()])
            self.assertEqual(s['state'], 'agree')
        t = deepcopy(self.raw['cards'][0])
        t['sources']['leaguewinner']['me_adjusted'] = -1.0
        s = trade_briefs({**self.raw, 'cards': [t]})['cards'][0]['brief']['sources']
        self.assertEqual((s['state'], s['text']), ('split', 'Only Hashtag shows a gain'))

    def test_roster_moves(self):
        self.assertEqual(self.cards[0]['brief']['roster'], [])                       # 2-for-2: no balancing
        r = self.cards[1]['brief']['roster']
        self.assertEqual(r[0], {'side': 'me', 'text': 'You drop Myles Turner'})
        self.assertEqual(r[1]['text'], "Nikhil's Nice Team fills the open spot with free agent Duncan Robinson")
        self.assertEqual(r[1]['conditional'], 'their adds unverified')

    def test_rank_reasons_follow_existing_ranking(self):
        for i, t in enumerate(self.cards[:-1]):
            self.assertTrue(t['brief']['rank_reason'].startswith(f'Ranked above #{i + 2} for '))
        self.assertIsNone(self.cards[-1]['brief']['rank_reason'])
        self.assertIn('a better result for them (+4.5 vs −3.3 wins for them)', self.cards[0]['brief']['rank_reason'])
        self.assertIn('despite a smaller gain for you (+9.0 vs +10.0 wins for you)', self.cards[0]['brief']['rank_reason'])

    def test_shared_players(self):
        self.assertEqual(self.cards[0]['brief']['shares'], [{'card': 3, 'players': ['Chet Holmgren']}])
        self.assertEqual(self.cards[1]['brief']['shares'], [{'card': 4, 'players': ['Kawhi Leonard']}])

    def test_why_text_is_computed_and_makes_no_acceptance_claim(self):
        for t in self.cards:
            why = t['brief']['why']
            top = max(t['me']['category_delta'].items(), key=lambda x: x[1])[0]
            self.assertIn({'ast': 'AST', 'stl': 'STL', 'reb': 'REB'}.get(top, top.upper()), why)
            for word in ('accept', 'likely', 'fair', 'realistic', 'willing'):
                self.assertNotIn(word, why.lower())

    def test_empty_result(self):
        self.assertEqual(trade_briefs({'cards': []})['cards'], [])


class TradeLabPageTests(TestCase):
    def test_route_adds_briefs_without_touching_cache(self):
        raw = json.loads(FIXTURE.read_text(encoding='utf-8'))
        before = json.dumps(raw, sort_keys=True)
        inputs = {'league': {'my_team_id': '4'}, 'availability': {'captured_at_utc': '2026-10-20T14:11:47Z'}}
        with patch('app.dashboard.trade_lab', return_value=raw), patch('app.dashboard.forecast', return_value={}):
            codes, body = _get(make_handler(inputs), '/api/trades?hashtag_weight=0.5')
        self.assertEqual(codes, [200])
        self.assertEqual(len(body['cards']), 5)
        self.assertTrue(all('brief' in t for t in body['cards']))
        self.assertEqual(json.dumps(raw, sort_keys=True), before)
        self.assertFalse(body['verification']['executable'])

    def test_page_markers(self):
        for marker in ('function tradeWhy', "'Why this trade?'", "'Detailed numbers'", 'id="tradeFilters"',
                       'id="tradeScope"', 'Results from', 'Earlier results stay dimmed', "'Shortlist, measured against no trade'",
                       'Market value alone does not show whether they would accept', "'Roster moves'", 'function xside'):
            self.assertIn(marker, HTML)
        self.assertNotIn("'Numbers')", HTML)
