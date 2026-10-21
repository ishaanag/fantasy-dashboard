import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture
from datetime import datetime, timezone
from pathlib import Path
import unittest

from app.phase1 import load_inputs
from app.waiver import evaluate_singles, load_user_evidence, pool_audit, _smooth

ROOT = Path('/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard')


class WaiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.team = cls.inputs['league']['my_team_id']
        cls.audit = pool_audit(cls.inputs, .5)
        cls.add = next(p for p in cls.audit['rows'] if p['availability'] == 'FA' and p['projectable'])
        cls.drop = next(p for p in cls.inputs['rosters'][cls.team] if p.assigned_slot == 'BN')
        cls.captured = cls.inputs['availability']['captured_at_utc']

    def evidence(self, **changes):
        result = {'captured_at_utc': self.captured,
                  'player_fingerprint': self.inputs['availability']['player_fingerprint'],
                  'team_id': self.team, 'week': 1, 'adds_used': 0,
                  'cannot_cut_site_ids': [], 'locked_site_ids': [],
                  'timing_by_site_id': {self.add['site_player_id']:
                      {'kind': 'free_agent', 'effective_at_utc': self.captured}},
                  'empty_slot_allowed': False, 'direct_il_site_ids': [],
                  'il_eligible_site_ids': [], 'active_claim_site_ids': []}
        result.update(changes)
        return result

    def run_move(self, evidence=None, add=None, drop=None):
        return evaluate_singles(self.inputs, team_id=self.team, evidence=evidence,
             add_ids=[add or self.add['site_player_id']],
             drop_ids=[self.drop.site_player_id if drop is None else drop],
             matchup_path=ROOT/'H2H-Matchups.csv')

    def test_capture_coverage_and_nonactionable_live_state(self):
        self.assertEqual(self.audit['player_count'], 744)
        self.assertGreater(self.audit['counts']['missing_projection'], 0)
        self.assertGreater(self.audit['counts']['one_source_only'], 0)
        result = self.run_move()
        move = next(r for r in result['rows'] if r['add_site_player_id'])
        self.assertFalse(move['actionable'])
        self.assertIn('transaction_evidence_missing', move['reason_codes'])

    def test_budget_owner_cannot_cut_and_fingerprint(self):
        move = next(r for r in self.run_move(self.evidence(adds_used=4))['rows'] if r['add_site_player_id'])
        self.assertIn('acquisition_budget_exhausted', move['reason_codes'])
        move = next(r for r in self.run_move(self.evidence(cannot_cut_site_ids=[self.drop.site_player_id]))['rows'] if r['add_site_player_id'])
        self.assertIn('cannot_cut', move['reason_codes'])
        move = next(r for r in self.run_move(self.evidence(player_fingerprint='old'))['rows'] if r['add_site_player_id'])
        self.assertIn('transaction_evidence_stale_or_mismatched', move['reason_codes'])
        owned = next(p for p in self.audit['rows'] if p['owner_team_id'] and p['site_player_id'] != self.drop.site_player_id)
        move = next(r for r in self.run_move(self.evidence(), add=owned['site_player_id'])['rows'] if r['add_site_player_id'])
        self.assertIn('owned_or_unavailable', move['reason_codes'])

    def test_user_evidence_resolves_site_ids_and_stays_provisional(self):
        evidence = load_user_evidence(self.inputs, ROOT/'data/reports/yahoo-transaction-evidence-user-2026-10-20.json')
        self.assertEqual(evidence['remaining_adds'], 4)
        self.assertEqual(evidence['player_fingerprint'], self.inputs['availability']['player_fingerprint'])
        self.assertEqual(evidence['empty_il_slots'], 2)
        roster = {p.name: p.site_player_id for p in self.inputs['rosters'][self.team]}
        self.assertEqual(set(evidence['cannot_cut_site_ids']),
            {roster[name] for name in ('Scottie Barnes','Shai Gilgeous-Alexander','Chet Holmgren','Kawhi Leonard')})
        protected = next(r for r in self.run_move(evidence)['rows'] if r['add_site_player_id'])
        self.assertFalse(protected['actionable'])
        self.assertIn('cannot_cut', protected['reason_codes'])
        legal_drop = roster['Keegan Murray']
        result = self.run_move(evidence, drop=legal_drop)
        move = next(r for r in result['rows'] if r['add_site_player_id'])
        self.assertEqual(result['evidence_status'], 'user_supplied_provisional')
        self.assertTrue(move['feasible'])
        self.assertFalse(move['actionable'])
        self.assertIn('effective_timing_unverified', move['reason_codes'])
        self.assertEqual(move['remaining_adds'], 3)
        self.assertIn('1', move['weeks'])

    def test_verified_direct_il_fixture_and_unverified_empty_slot(self):
        add_id = self.add['site_player_id']
        blocked = evaluate_singles(self.inputs, team_id=self.team, evidence=self.evidence(),
            add_ids=[add_id], drop_ids=[None], matchup_path=ROOT/'H2H-Matchups.csv')
        move = next(r for r in blocked['rows'] if r['add_site_player_id'])
        self.assertIn('no_verified_empty_or_il_slot', move['reason_codes'])
        fixture = self.evidence(direct_il_site_ids=[add_id], il_eligible_site_ids=[add_id])
        result = evaluate_singles(self.inputs, team_id=self.team, evidence=fixture,
            add_ids=[add_id], drop_ids=[None], matchup_path=ROOT/'H2H-Matchups.csv')
        move = next(r for r in result['rows'] if r['add_site_player_id'])
        self.assertTrue(move['feasible'])
        self.assertEqual(move['placement'], 'direct_il')
        self.assertEqual(move['weeks']['1']['new_starts'], [])

    def test_verified_fixture_recalculates_and_respects_effective_time(self):
        result = self.run_move(self.evidence())
        move = next(r for r in result['rows'] if r['add_site_player_id'])
        self.assertTrue(move['feasible'])
        self.assertEqual(sum(move['season_record']), 162)
        self.assertEqual(len(move['weeks']), 18)
        self.assertEqual(set(move['weeks']['1']['categories']), {'fg_pct','ft_pct','three_ptm','pts','reb','ast','stl','blk','turnovers'})
        late = self.evidence(timing_by_site_id={self.add['site_player_id']:
             {'kind': 'free_agent', 'effective_at_utc': '2026-10-26T04:00:00Z'}})
        delayed = next(r for r in self.run_move(late)['rows'] if r['add_site_player_id'])
        self.assertEqual(delayed['weeks']['1']['before'], delayed['weeks']['1']['after'])
        self.assertEqual(delayed['weeks']['1']['new_starts'], [])
        self.assertEqual(delayed['weeks']['1']['displaced_starts'], [])
        self.assertNotEqual(_smooth({'turnovers': 1}, {'turnovers': 1}),
                            _smooth({'turnovers': 1}, {'turnovers': 10}))


if __name__ == '__main__':
    unittest.main()
