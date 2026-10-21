"""Waiver Advisor presentation: plain-language labels derived only from computed advisor/audit output."""
import copy
import json
from pathlib import Path
from unittest import TestCase

from app.presentation import outlook, present

FIXTURE = Path(__file__).parent / 'fixtures' / 'advisor_2026-10-20T2239Z.json'   # live 22:39Z advisor output


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != 'display'}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


class SavedRecommendationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(FIXTURE.read_text())
        cls.advice = present(copy.deepcopy(cls.raw))
        cls.top = cls.advice['strategies'][0]

    def test_validation_example_is_readable_without_the_audit(self):
        s, d = self.top, self.top['display']
        self.assertEqual([(m['add_name'], m['drop_name']) for m in s['moves']],
                         [('Robert Williams III', 'Ajay Mitchell'), ('Paul Reed', 'Jakob Poeltl'),
                          ('Christian Braun', 'Robert Williams III')])
        self.assertEqual(d['short_term']['text'], '+1')                       # +1 short-term category win
        self.assertEqual(d['acquisitions']['value'], 3)                       # three acquisitions
        self.assertEqual(d['permanent']['value'], 2)                          # two permanent replacements
        self.assertEqual(d['permanent']['pairs'], ['Jakob Poeltl → Paul Reed', 'Ajay Mitchell → Christian Braun'])
        self.assertEqual((d['season']['label'], d['season']['agreement']), ('Mixed', 'mixed'))
        self.assertEqual(d['season']['detail'], 'Blended +2 · Hashtag +3 · LeagueWinner −2')
        labels = [r['label'] for r in d['risks']]
        self.assertEqual(labels[0], 'Fragile Week 2 rebounds gain')
        self.assertIn('Mixed long-term projections', labels)
        self.assertIn('Long-term asset downgrade', labels)
        self.assertEqual(d['yahoo']['label'], 'Conditional')                  # conditional, not confirmed
        self.assertIn('not confirmed', d['yahoo']['text'])
        self.assertEqual([g['when'] for g in d['timeline']], ['Tue Oct 27', 'Fri Oct 30'])
        self.assertEqual(d['timeline'][1]['moves'][0]['drop_tag'], 'Ends stream')

    def test_no_confident_stronger_claim_when_sources_disagree(self):
        why = ' '.join(self.top['display']['why'])
        self.assertNotIn('Improves the rest-of-season', why)
        self.assertIn('Rest of season: mixed', why)
        self.assertNotIn('margin score', why)

    def test_advisor_values_are_unchanged(self):
        self.assertEqual(_strip(self.advice), self.raw)

    def test_alternatives_explain_their_tradeoffs(self):
        upgrade = next(s for s in self.advice['strategies'] if s['kind'] == 'Permanent upgrade')
        self.assertIn('Uses 2 fewer acquisitions', upgrade['display']['why_consider'])
        self.assertIn('Keeps Ajay Mitchell', upgrade['display']['why_consider'])
        for s in self.advice['strategies'][1:]:
            if s['display']['season']['label'] == 'Weaker':                  # agreeing on a loss is no advantage
                self.assertNotIn('Projection sources agree more', s['display']['why_consider'])
        self.assertEqual(self.advice['display']['data_gaps'], ['Yahoo data is more than 15 minutes old'])


class OutlookTests(TestCase):
    def test_labels_follow_source_signs(self):
        self.assertEqual(outlook([2, 3, 1])[:2], ('Stronger', 'good'))
        self.assertEqual(outlook([2, 3, -2])[0], 'Mixed')
        self.assertEqual(outlook([-1, -4, -1])[0], 'Weaker')
        self.assertEqual(outlook([0, 0, 0])[0], 'About the same')
        self.assertEqual(outlook([1, 0, 0])[0], 'Leans stronger')
        self.assertEqual(outlook([2])[2], 'unchecked')

    def test_hold_verdict_without_audit(self):
        raw = json.loads(FIXTURE.read_text())
        alt = {k: v for k, v in raw['strategies'][3].items() if k != 'audit'}
        alt['why_not'] = ['Net value is below the bar']
        advice = present({**raw, 'strategies': [], 'closest_alternatives': [alt], 'verdict': 'hold'})
        self.assertEqual(advice['display']['headline'], 'Hold: no move clears the bar')
        d = alt['display']
        self.assertEqual(d['season']['agreement'], 'unchecked')
        self.assertEqual([x['label'] for x in d['sources']], ['Selected', 'Hashtag', 'LeagueWinner'])
        self.assertEqual(d['why_not'], ['Net value is below the bar'])
        self.assertEqual(present({'strategies': []})['display']['headline'], 'Hold: no move clears the bar')


class PageTests(TestCase):
    def test_progressive_disclosure_markup(self):
        from app.dashboard import HTML
        for marker in ('id="advisorResults"', 'id="advisorHorizon"', "'Why this plan?'", "'Detailed audit'",
                       "'Compare with holding'", 'Refresh Yahoo'):
            self.assertIn(marker, HTML)
        for retired in ('id="weeklyPlan"', 'id="singleMoves"', 'Rolling Horizon'):
            self.assertNotIn(retired, HTML)
