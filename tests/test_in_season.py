"""Phase 3F availability and verified actual-state contracts."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from fantasy_dashboard.advisor.search import AdvisorConfig, advise
from fantasy_dashboard.inputs import MATCHUPS, ROOT, load_inputs
from fantasy_dashboard.league import VOLUMES, RosterPlayer
from fantasy_dashboard.model.forecast import forecast, with_actual_volumes
from fantasy_dashboard.model.lineups import dated_team_totals, unavailable_for_game
from fantasy_dashboard.sources.matchups import load_matchups
from fantasy_dashboard.sources.schedule import FantasyWeek, Game
from fantasy_dashboard.web.server import load_captured_actual
from fantasy_dashboard.yahoo.transactions import build_actual_state

import pinned_capture  # noqa: F401  (pins the archived capture)

UTC = timezone.utc


class Phase3FTests(unittest.TestCase):
    def test_imminent_out_opens_a_legal_start_without_changing_future_gp(self):
        captured = datetime(2026, 10, 20, 14, tzinfo=UTC)
        day = date(2026, 10, 21)
        later = date(2026, 10, 24)
        game1 = Game(day, datetime(2026, 10, 21, 23, tzinfo=UTC), ('BOS', 'NYK'), ('BOS', 'NYK'), '1')
        game2 = Game(later, datetime(2026, 10, 25, 0, tzinfo=UTC), ('BOS', 'NYK'), ('BOS', 'NYK'), '2')
        dates = {('BOS', day): game1, ('BOS', later): game2}
        schedule = SimpleNamespace(team_dates=dates, games_for=lambda team, start, end, as_of=None:
                                   [g for (t, d), g in dates.items() if t == team and start <= d <= end and g.tipoff_utc > as_of])
        injured = RosterPlayer('1', '101', 'Out starter', 'PG', ('PG',), 'BOS')
        substitute = RosterPlayer('1', '102', 'Questionable substitute', 'BN', ('PG',), 'BOS')
        values = {**dict.fromkeys(VOLUMES, 1.0), 'gp': 82.0}
        values['pts'] = 20.0
        projected = [(injured, {'values': values, 'coverage': '', 'source_rows': {}, 'disagreement': {}}),
                     (substitute, {'values': values, 'coverage': '', 'source_rows': {}, 'disagreement': {}})]
        inputs = {'availability': {'captured_at_utc': captured.isoformat().replace('+00:00', 'Z')},
                  'injury_status_captured_at_utc': captured.isoformat().replace('+00:00', 'Z'),
                  'injury_statuses': {'101': 'O', '102': 'Q'}}
        totals, detail, days = dated_team_totals([injured, substitute], projected, schedule,
            FantasyWeek(1, day, day, 'regular'), captured, injury_inputs=inputs)
        self.assertEqual([s['site_player_id'] for s in days[0]['modeled_starts']], ['102'])
        self.assertEqual(totals['categories']['pts'], 20)
        self.assertEqual(next(d for d in detail if d['site_player_id'] == '101')['expected_games'], 0)
        later_totals, _, later_days = dated_team_totals([injured, substitute], projected, schedule,
            FantasyWeek(2, later, later, 'regular'), captured, injury_inputs=inputs)
        self.assertEqual(len(later_days[0]['modeled_starts']), 2)
        self.assertEqual(later_totals['categories']['pts'], 40)
        self.assertFalse(unavailable_for_game({**inputs, 'injury_status_captured_at_utc': '2026-10-19T14:00:00Z'},
                                              '101', game1.tipoff_utc, captured))

    def test_completed_results_reconcile_and_roll_over_once(self):
        inputs = load_inputs(ROOT)
        pairings = load_matchups(ROOT / MATCHUPS, inputs['matchup_team_names']).pairings
        matches = [{'teams': list(pair), 'scores': ['5', '4']} for pair in pairings[1]]
        raw = {'errors': [], 'completed_pages': [{'week': 1, 'source': '/nba/92103?lhst=matchups&matchup_week=1&module=matchups',
               'final': True, 'week_header': True, 'matches': matches}],
               'standings_source': '/nba/92103?lhst=stand&module=standings',
               'standings_records': {a: [5, 4, 0] for a, _ in pairings[1]} |
                                    {b: [4, 5, 0] for _, b in pairings[1]}}
        captured = '2026-10-26T14:00:00Z'
        state = build_actual_state(raw, captured_at_utc=captured, team_ids=list(inputs['teams']),
                                   dates=[], completed_weeks=1, pairings=pairings, league_id='92103')
        self.assertEqual(state['status'], 'captured', state['problems'])
        inputs['availability'] = {**inputs['availability'], 'captured_at_utc': captured, 'lineup_date': '2026-10-26'}
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'yahoo-actual-state.json'
            path.write_text(json.dumps(state))
            self.assertEqual(load_captured_actual(inputs, Path(folder))[1], 'captured')
            path.write_text(json.dumps({**state, 'as_of_utc': '2026-10-25T14:00:00Z'}))
            self.assertEqual(load_captured_actual(inputs, Path(folder))[1], 'stale_not_bound_to_capture')
        result = forecast(inputs, actual_state=state)
        self.assertEqual(sum(m['status'] == 'actual' for m in result['weekly_matchups']), 5)
        for team in result['teams']:
            self.assertEqual(team['actual_record'], state['records'][team['team_id']])
            self.assertEqual(sum(team['projected_record']), 162)
        raw['standings_records'][next(iter(raw['standings_records']))] = [4, 5, 0]
        rejected = build_actual_state(raw, captured_at_utc=captured, team_ids=list(inputs['teams']),
                                      dates=[], completed_weeks=1, pairings=pairings, league_id='92103')
        self.assertIn('completed_results_do_not_reconcile_to_standings', rejected['problems'])
        raw['completed_pages'][0]['matches'][0]['teams'].reverse()
        rejected = build_actual_state(raw, captured_at_utc=captured, team_ids=list(inputs['teams']),
                                      dates=[], completed_weeks=1, pairings=pairings, league_id='92103')
        self.assertEqual(rejected['status'], 'rejected')

    def test_daily_actuals_are_started_final_only_and_added_once(self):
        day = '2026-10-21'
        row = ['PG', '101', 'W, 101-99', ['5/10', '.500', '2/3', '.667', '1', '13', '5', '4', '2', '1', '1']]
        page = {'team': '1', 'date': day, 'page_date': 'Wed, Oct 21', 'header_ok': True,
                'rows': [row, ['BN', '102', 'W, 101-99', row[3]], ['SG', '103', '7:00 pm', row[3]]]}
        state = build_actual_state({'pages': [page]}, captured_at_utc='2026-10-22T00:00:00Z',
                                   team_ids=['1'], dates=[day], completed_weeks=0)
        self.assertEqual(state['status'], 'captured', state['problems'])
        self.assertEqual(state['current_week_volumes']['1']['pts'], 13)
        future = {'shooting_volumes': {'fgm': 2, 'fga': 4, 'ftm': 1, 'fta': 2},
                  'categories': {'three_ptm': 1, 'pts': 5, 'reb': 2, 'ast': 1, 'stl': 0, 'blk': 0, 'turnovers': 1}}
        combined = with_actual_volumes(future, state['current_week_volumes']['1'])
        self.assertEqual(combined['categories']['pts'], 18)
        self.assertAlmostEqual(combined['categories']['fg_pct'], 7/14)
        page['rows'][2][2] = '3rd Qtr'
        self.assertEqual(build_actual_state({'pages': [page]}, captured_at_utc='2026-10-22T00:00:00Z',
            team_ids=['1'], dates=[day], completed_weeks=0)['status'], 'rejected')

    def test_advisor_hold_tracks_injury_adjusted_forecast(self):
        inputs = load_inputs(ROOT)
        captured = inputs['availability']['captured_at_utc']
        as_of = datetime.fromisoformat(captured.replace('Z', '+00:00'))
        roster = inputs['rosters'][inputs['league']['my_team_id']]
        game = next(g for g in inputs['schedule'].games if as_of < g.tipoff_utc <= as_of + timedelta(hours=36)
                    and any(p.nba_team in g.teams and p.assigned_slot != 'IL' for p in roster))
        player = next(p for p in roster if p.nba_team in game.teams and p.assigned_slot != 'IL')
        inputs['injury_statuses'] = {player.site_player_id: 'O'}
        inputs['injury_status_captured_at_utc'] = captured
        base = forecast(inputs, as_of=as_of)
        result = advise(inputs, as_of=as_of, now=as_of, base=base, config=AdvisorConfig(horizon=1, beam_width=8, expand_adds=12, season_shortlist=6,
                                                                              local_evaluations=20_000))
        self.assertEqual(result['hold']['weeks'][0]['before'], next(m['score'] if m['teams'][0] == inputs['league']['my_team_id']
                           else [m['score'][1], m['score'][0], m['score'][2]] for m in base['weekly_matchups']
                           if m['week'] == 1 and inputs['league']['my_team_id'] in m['teams']))


if __name__ == '__main__':
    unittest.main()
