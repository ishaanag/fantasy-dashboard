"""Dashboard shell: navigation, redesigned views and the additive /api/deterministic identity fields."""
import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from app.dashboard import HTML, make_handler


def _get(handler, path):
    request = object.__new__(handler)
    request.path = path
    request.wfile, codes = BytesIO(), []
    request.send_response = codes.append
    request.send_header = lambda *args: None
    request.end_headers = lambda: None
    request.do_GET()
    return codes, json.loads(request.wfile.getvalue() or b'null')


class ShellTests(TestCase):
    def test_three_views_and_settings_drawer(self):
        for marker in ('href="#overview"', 'href="#matchups"', 'href="#waivers"', 'id="view-overview"',
                       'id="view-matchups"', 'id="view-waivers"', 'id="settings"', 'id="sourceSeg"',
                       'id="source"', 'id="weight"', 'id="health"', 'id="method"'):
            self.assertIn(marker, HTML)

    def test_redesigned_regions_present(self):
        for marker in ('id="kpis"', 'id="path"', 'id="standings"', 'id="heatmap"', 'id="profile"',
                       'id="period"', 'id="week"', 'id="weekGames"', 'id="matchupCard"'):
            self.assertIn(marker, HTML)
        self.assertNotIn('id="matchupTable"', HTML)   # old statistical table retired
        self.assertNotIn('id="quality"', HTML)        # diagnostics moved into the drawer

    def test_projection_language_is_not_probabilistic(self):
        self.assertIn('Not a probability', HTML)
        self.assertIn('not win probabilities', HTML)
        self.assertIn('they are not probabilities', HTML)

    def test_advisor_shell_preserved_and_lazy(self):
        for marker in ('id="advisorResults"', 'id="advisorHorizon"', 'id="yahooRefresh"', 'Refresh Yahoo',
                       'function renderAdvisor', 'function strategyCard', "view==='waivers'&&advisorStale"):
            self.assertTrue(marker in HTML, marker)   # advisor loads only when its tab opens


class DeterministicRouteTests(TestCase):
    def test_identity_fields_are_additive_and_cache_untouched(self):
        cached = {'teams': [], 'hashtag_weight': .5}
        handler = make_handler({'league': {'my_team_id': '4', 'league_name': 'Big Baller League'}})
        with patch('app.dashboard.forecast', return_value=cached) as mocked:
            codes, body = _get(handler, '/api/deterministic?hashtag_weight=0.5')
            _get(handler, '/api/deterministic?hashtag_weight=0.5')
        self.assertEqual(codes, [200])
        self.assertEqual(mocked.call_count, 1)          # cached forecast reused
        self.assertEqual(body['my_team_id'], '4')
        self.assertEqual(body['league_name'], 'Big Baller League')
        self.assertEqual(body['teams'], [])
        self.assertEqual(cached, {'teams': [], 'hashtag_weight': .5})   # engine output not mutated
