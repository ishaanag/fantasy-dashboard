from dataclasses import replace
from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fantasy_dashboard.inputs import REPORTS, ROOT, load_inputs
from fantasy_dashboard.league import parse_utc
from fantasy_dashboard.web.server import make_handler
from fantasy_dashboard.yahoo.evidence import classify_plan, evidence_reasons
from fantasy_dashboard.yahoo.transactions import (
    SNAPSHOT_NAME,
    build_actual_state,
    build_snapshot,
    load_transaction_snapshot,
    resolve_evidence,
)

import pinned_capture  # noqa: F401  (pins the archived capture)


class YahooSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.team = cls.inputs['league']['my_team_id']
        cls.roster = cls.inputs['rosters'][cls.team]
        cls.ids = {p.name: p.site_player_id for p in cls.roster}
        cls.fa = [p.site_player_id for p in cls.inputs['site_players'] if p.availability_label == 'FA'
                  and p.site_player_id in cls.inputs['projections'] and len(cls.inputs['projections'][p.site_player_id]) == 2]
        cls.captured = cls.inputs['availability']['captured_at_utc']
        cls.now = parse_utc(cls.captured)

    def raw(self, **changes):
        raw = {'errors': [], 'team_text': ['You have used 0 of your 4 player adds for this week',
               'You have used 1 of 3 IL positions on your roster', 'Waiver Priority: 1st', 'Tue, Oct 20'],
               'roster': [{'id': p.site_player_id, 'slot': p.assigned_slot, 'code': 'O' if p.assigned_slot == 'IL' else '',
                           'game': '', 'slot_class': 'pos-label Btn-primary'} for p in self.roster],
               'pending_section': False, 'pending_ids': [], 'cant_cut_paginated': False,
               'cant_cut': [self.ids[n] for n in ('Scottie Barnes', 'Shai Gilgeous-Alexander', 'Chet Holmgren', 'Kawhi Leonard')],
               'il_eligible': {'ids': self.fa[:2], 'codes': {}, 'pages': 1, 'complete': True}}
        raw.update(changes)
        return raw

    def load(self, raw=None, inputs=None, now=None, **snap):
        inputs = inputs or self.inputs
        a = inputs['availability']
        values = dict(captured_at_utc=a['captured_at_utc'], league_id='92103', team_id=self.team,
                      player_fingerprint=a['player_fingerprint'], roster_fingerprint=a['roster_fingerprint'],
                      lineup_date=a['lineup_date'], player_codes={self.fa[0]: 'O'}, waiver_ids=[])
        values.update(snap)
        folder = Path(tempfile.mkdtemp())
        (folder / SNAPSHOT_NAME).write_text(json.dumps(build_snapshot(raw or self.raw(), **values)))
        return load_transaction_snapshot(folder / SNAPSHOT_NAME, inputs, now or self.now), folder

    def reasons(self, evidence):
        return evidence_reasons(self.inputs, evidence, self.team, 1, self.now)

    def test_synchronized_snapshot_is_verified(self):
        status, _ = self.load()
        self.assertEqual(status['status'], 'verified', status['reasons'])
        ev = status['evidence']
        self.assertEqual((ev['adds_used'], ev['remaining_adds'], ev['waiver_priority'], ev['empty_il_slots']), (0, 4, 1, 2))
        self.assertEqual(set(ev['cannot_cut_site_ids']), set(self.raw()['cant_cut']))
        self.assertEqual(ev['direct_il_site_ids'], sorted(self.fa[:2]))
        self.assertEqual(ev['status_by_site_id'][self.ids['Kristaps Porziņģis']], 'O')
        self.assertEqual(ev['timing_by_site_id'][self.fa[2]]['effective_at_utc'], self.captured)

    def test_stale_or_unsynchronized_snapshot_rejected(self):
        status, _ = self.load(captured_at_utc='2026-10-20T13:00:00Z')
        self.assertEqual(status['reasons'], ['snapshot_not_synchronized_with_player_capture'])
        status, _ = self.load(player_fingerprint='0000')
        self.assertEqual(status['status'], 'rejected')
        status, _ = self.load(now=self.now + timedelta(minutes=40))
        self.assertEqual(status['freshness'], 'stale')
        self.assertEqual(status['status'], 'verified')   # still bound evidence; engines mark plans provisional by age
        self.assertEqual(self.reasons(status['evidence']), [])

    def test_incomplete_capture_rejected_and_falls_back(self):
        status, folder = self.load(self.raw(team_text=['', 'You have used 1 of 3 IL positions on your roster', '', '']))
        self.assertEqual(status['status'], 'rejected')
        self.assertIn('incomplete_capture:acquisitions', status['reasons'])
        self.assertIn('incomplete_capture:waiver_priority', status['reasons'])
        status, _ = self.load(self.raw(il_eligible={'ids': [], 'codes': {}, 'pages': 40, 'complete': False}))
        self.assertIn('incomplete_capture:il_eligible_available', status['reasons'])
        evidence, info = resolve_evidence(self.inputs, folder, self.now)
        self.assertIsNone(evidence)
        self.assertEqual(info['fallback'], 'none')
        evidence, info = resolve_evidence(self.inputs, ROOT / REPORTS, self.now)
        self.assertIn(info['status'], {'missing', 'rejected', 'verified'})

    def test_ownership_changes_rejected(self):
        roster = self.raw()['roster']
        roster[0] = {**roster[0], 'slot': 'BN'}
        status, _ = self.load(self.raw(roster=roster))
        self.assertEqual(status['reasons'], ['ownership_inconsistent:team_page_roster'])
        owned = next(p.site_player_id for p in self.inputs['site_players'] if p.fantasy_team_id and p.fantasy_team_id != self.team)
        status, _ = self.load(self.raw(il_eligible={'ids': [owned], 'codes': {}, 'pages': 1, 'complete': True}))
        self.assertEqual(status['reasons'], ['ownership_inconsistent:il_eligible_list'])
        changed = dict(self.inputs, site_players=[replace(p, availability_label='Kum Buckets', fantasy_team_id='3')
                                                  if p.site_player_id == self.fa[2] else p for p in self.inputs['site_players']])
        status, _ = self.load(inputs=changed)
        self.assertNotIn(self.fa[2], status['evidence']['timing_by_site_id'])   # owned players get no add timing

    def test_acquisition_budget_and_cant_cut(self):
        status, _ = self.load(self.raw(team_text=['You have used 4 of your 4 player adds for this week',
                              'You have used 1 of 3 IL positions on your roster', 'Waiver Priority: 3rd', 'Tue, Oct 20']))
        self.assertEqual((status['evidence']['adds_used'], status['evidence']['remaining_adds']), (4, 0))
        status, _ = self.load(self.raw(team_text=['You have used 0 of your 5 player adds for this week',
                              'You have used 1 of 3 IL positions on your roster', 'Waiver Priority: 1st', 'Tue, Oct 20']))
        self.assertEqual(status['reasons'], ['acquisition_budget_mismatch'])
        status, _ = self.load()
        self.assertIn(self.ids['Shai Gilgeous-Alexander'], status['evidence']['cannot_cut_site_ids'])
        self.assertNotIn(self.ids['Keegan Murray'], status['evidence']['cannot_cut_site_ids'])
        self.assertEqual(self.reasons(status['evidence']), [])
        self.assertEqual(self.reasons({**status['evidence'], 'adds_used': 5}), ['acquisition_count_invalid'])
        locks = self.raw()['roster']
        locks = [{**r, 'slot_class': 'pos-label Btn-disabled'} if r['id'] == self.ids['Keegan Murray'] else r for r in locks]
        status, _ = self.load(self.raw(roster=locks))
        self.assertIn(self.ids['Keegan Murray'], status['evidence']['locked_site_ids'])

    def test_waiver_without_time_is_provisional_and_out_status_flagged(self):
        changed = dict(self.inputs, site_players=[replace(p, availability_label='W') if p.site_player_id == self.fa[2] else p
                                                  for p in self.inputs['site_players']])
        status, _ = self.load(inputs=changed, waiver_ids=[self.fa[2]], player_codes={self.fa[2]: 'O'})
        self.assertEqual(status['status'], 'verified')
        self.assertIn('waiver_effective_time_unverified', status['missing_evidence'])
        self.assertIn('active_claims_unverified', status['missing_evidence'])
        self.assertNotIn(self.fa[2], status['evidence']['timing_by_site_id'])   # no waiver clear time is invented
        self.assertEqual(status['evidence']['status_by_site_id'][self.fa[2]], 'O')

    def test_actual_state_sums_final_started_games_only(self):
        row = lambda slot, game, fg='7/8': [slot, '1', game, [fg, '.875', '2/3', '.667', '1', '17', '5', '3', '1', '0', '2']]
        page = lambda team, rows: {'team': team, 'date': '2026-10-20', 'page_date': 'Tue, Oct 20', 'header_ok': True, 'rows': rows}
        raw = {'errors': [], 'pages': [page('1', [row('SG', 'L, 113-121 @ DET'), row('BN', 'W, 121-113 vs BOS'),
                                                   row('PG', '7:00 pm @ NYK'), row('F', '', '-/-')]), page('2', [row('C', '')])]}
        state = build_actual_state(raw, captured_at_utc=self.captured, team_ids=['1', '2'], dates=['2026-10-20'], completed_weeks=0)
        self.assertEqual(state['status'], 'captured', state['problems'])
        self.assertEqual(state['current_week_volumes']['1'], {'fgm': 7, 'fga': 8, 'ftm': 2, 'fta': 3, 'three_ptm': 1, 'pts': 17,
                         'reb': 5, 'ast': 3, 'stl': 1, 'blk': 0, 'turnovers': 2})
        raw['pages'][1]['rows'] = [row('C', 'Q3 4:12')]
        state = build_actual_state(raw, captured_at_utc=self.captured, team_ids=['1', '2', '3'], dates=['2026-10-20'], completed_weeks=0)
        self.assertEqual(state['status'], 'rejected')
        self.assertIn('page_missing:3/2026-10-20', state['problems'])
        self.assertIn('game_in_progress:2/2026-10-20/1', state['problems'])

    def test_plan_classes(self):
        self.assertEqual(classify_plan({'steps': []}), 'hold')
        self.assertEqual(classify_plan({'steps': [{'future_availability_conditional': False}], 'missing_verification': []}), 'verified_current')
        self.assertEqual(classify_plan({'steps': [{'future_availability_conditional': False}], 'missing_verification': ['x']}), 'provisional')
        self.assertEqual(classify_plan({'steps': [{'future_availability_conditional': True}], 'missing_verification': []}), 'conditional_future')

    def request(self, handler, path, method='GET'):
        request = object.__new__(handler)
        request.path, request.wfile, codes = path, BytesIO(), []
        request.send_response = codes.append
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        getattr(request, 'do_' + method)()
        return codes[0], json.loads(request.wfile.getvalue())

    def test_refresh_replaces_recommendations_or_keeps_previous(self):
        old = {'status': 'rejected', 'reasons': ['snapshot_not_synchronized_with_player_capture']}
        failing = make_handler(self.inputs, yahoo_status=old, root=ROOT, refresher=lambda: (_ for _ in ()).throw(RuntimeError('sign-in timeout')))
        code, value = self.request(failing, '/api/yahoo/refresh', 'POST')
        self.assertEqual((code, value['previous']), (502, old))
        self.assertEqual(self.request(failing, '/api/yahoo/status'), (200, old))
        status, _ = self.load()
        changed = dict(self.inputs, site_players=[replace(p, availability_label='Kum Buckets', fantasy_team_id='3')
                                                  if p.site_player_id == self.fa[2] else p for p in self.inputs['site_players']])
        handler = make_handler(self.inputs, yahoo_status=old, root=ROOT, refresher=lambda: None)
        with patch('fantasy_dashboard.web.server.load_inputs', return_value=changed), \
             patch('fantasy_dashboard.web.server.resolve_evidence', return_value=(status['evidence'], {'status': 'verified'})):
            code, value = self.request(handler, '/api/yahoo/refresh', 'POST')
        self.assertEqual((code, value['status']), (200, 'verified'))
        self.assertIn('actual_state', value)
        code, value = self.request(handler, '/api/yahoo/status')
        self.assertEqual((code, value['status']), (200, 'verified'))


if __name__ == '__main__':
    unittest.main()
