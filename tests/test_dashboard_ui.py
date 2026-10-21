"""Dashboard shell: navigation, redesigned views and the additive /api/deterministic identity fields."""
from unittest import TestCase
from unittest.mock import patch

from fantasy_dashboard.web.server import make_handler

from support import PAGE, get_json


class ShellTests(TestCase):
    def test_three_views_and_settings_drawer(self):
        for marker in ('href="#overview"', 'href="#matchups"', 'href="#waivers"', 'id="view-overview"',
                       'id="view-matchups"', 'id="view-waivers"', 'id="settings"', 'id="sourceSeg"',
                       'id="source"', 'id="weight"', 'id="health"', 'id="method"'):
            self.assertIn(marker, PAGE)

    def test_redesigned_regions_present(self):
        for marker in ('id="kpis"', 'id="path"', 'id="standings"', 'id="heatmap"', 'id="profile"',
                       'id="period"', 'id="week"', 'id="weekGames"', 'id="matchupCard"'):
            self.assertIn(marker, PAGE)
        self.assertNotIn('id="matchupTable"', PAGE)   # old statistical table retired
        self.assertNotIn('id="quality"', PAGE)        # diagnostics moved into the drawer

    def test_projection_language_is_not_probabilistic(self):
        self.assertIn('Not a probability', PAGE)
        self.assertIn('not win probabilities', PAGE)
        self.assertIn('they are not probabilities', PAGE)

    def test_advisor_shell_preserved_and_lazy(self):
        for marker in ('id="advisorResults"', 'id="advisorHorizon"', 'id="yahooRefresh"', 'Refresh Yahoo',
                       'function renderAdvisor', 'function strategyCard', "view==='waivers'&&advisorStale"):
            self.assertTrue(marker in PAGE, marker)   # advisor loads only when its tab opens


class DeterministicRouteTests(TestCase):
    def test_identity_fields_are_additive_and_cache_untouched(self):
        cached = {'teams': [], 'hashtag_weight': .5}
        handler = make_handler({'league': {'my_team_id': '4', 'league_name': 'Big Baller League'}})
        with patch('fantasy_dashboard.web.server.forecast', return_value=cached) as mocked:
            codes, body = get_json(handler, '/api/deterministic?hashtag_weight=0.5')
            get_json(handler, '/api/deterministic?hashtag_weight=0.5')
        self.assertEqual(codes, [200])
        self.assertEqual(mocked.call_count, 1)          # cached forecast reused
        self.assertEqual(body['my_team_id'], '4')
        self.assertEqual(body['league_name'], 'Big Baller League')
        self.assertEqual(body['teams'], [])
        self.assertEqual(cached, {'teams': [], 'hashtag_weight': .5})   # engine output not mutated
