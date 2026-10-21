import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture
"""Phase 4B multi-player packages: roster balancing, conservation, lineups, reciprocity, pruning and search."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import json
from math import comb
from pathlib import Path
from unittest import TestCase

from app.dashboard import HTML
from app.deterministic import forecast
from app.phase1 import (CATEGORIES, EASTERN, ROOT, SLOTS, RosterPlayer, _dated_team_totals, assign_lineup,
                        eligible, load_inputs, resolve_projection)
from app.trades import (ACTIVE_LIMIT, IL_LIMIT, MARKET_FLOOR, VALUE_FLOOR, TradeConfig, _bal, _Context, _effective,
                        _similar, evaluate, market_values, side_value, trade_lab, value_gap)
from app.waiver import _utc

UTC = timezone.utc
FIXTURE = Path(__file__).parent / 'fixtures' / 'trades_4a_2026-10-20T1411Z.json'
PARTNERS = ('3', '5', '9')   # Kum Buckets, Show Time, The Flaggot's


def ids(players):
    return {p['site_player_id'] for p in players}


class MultiPackageTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.as_of = _utc(cls.inputs['availability']['captured_at_utc'])
        cls.bases = {w: forecast(cls.inputs, hashtag_weight=w, as_of=cls.as_of) for w in (.5, 1.0, 0.0)}
        cls.kw = dict(as_of=cls.as_of, now=cls.as_of, base=cls.bases[.5],
                      source_bases={1.0: cls.bases[1.0], 0.0: cls.bases[0.0]})
        # Three partners keep the fixture fast while every package type, drop, fill and card rule is exercised.
        cls.result = trade_lab(cls.inputs, config=TradeConfig(partner_ids=PARTNERS, result_limit=12, per_player_limit=4,
                                                              keep_finalists=True), **cls.kw)
        cls.cards = cls.result['cards']   # 4D: nothing forced onto the page
        # Roster mechanics are checked on every source-checked finalist, since few multi-player packages
        # survive the 10% partner-loss cap to become cards.
        cls.finalists = cls.result['finalists']
        cls.me = cls.inputs['league']['my_team_id']
        cls.eff = cls.as_of.astimezone(EASTERN).date() + timedelta(days=3)
        cls.ctx = _Context(cls.inputs, cls.bases[.5], .5, cls.as_of, None, cls.eff)
        for c in cls.finalists:
            for s in ('me', 'partner'):
                f = c['balancing'][s].get('fill')
                if f and f['used']:
                    cls.ctx.register(f['player']['site_player_id'])
        cls.by_type = {k: [c for c in cls.finalists if c['package'] == k] for k in ('1-1', '2-1', '1-2', '2-2')}

    def sides(self, c):
        partner = c['partner']['team_id']
        g, r = ids(c['gives_all']), ids(c['receives_all'])
        out = {}
        for side, team, out_ids, in_ids in (('me', self.me, g, r), ('partner', partner, r, g)):
            bal = _bal(c['balancing'][side])
            before = {p.site_player_id for p in self.inputs['rosters'][team]}
            il_before = {p.site_player_id for p in self.inputs['rosters'][team] if p.assigned_slot == 'IL'}
            after = (before - out_ids - set(bal.get('drops', ()))) | in_ids | set(bal.get('adds', ()))
            il_after = il_before | set(bal.get('il', ()))
            out[side] = {'team': team, 'before': before, 'after': after, 'bal': bal, 'out': out_ids, 'in': in_ids,
                         'active_after': after - il_after, 'il_after': il_after}
        return out

    def test_every_package_type_competes_and_five_cards_show(self):
        by_type = self.result['diagnostics']['by_type']
        self.assertTrue(all(by_type[k].get('finalists') for k in ('1-1', '2-1', '1-2', '2-2')), by_type)
        self.assertTrue(all(self.result['best_by_type'].values()))
        self.assertTrue(self.by_type['2-1'] and self.by_type['1-2'] and self.by_type['2-2'])
        self.assertLessEqual(len(self.result['cards']), 12)   # 4D: never padded to result_limit
        self.assertEqual(self.result['phase'], '4D')

    def test_roster_conservation_capacity_and_mechanics(self):
        for c in self.cards:
            s = self.sides(c)
            me, them = s['me'], s['partner']
            self.assertTrue(me['out'] <= me['before'] and them['out'] <= them['before'])
            self.assertFalse(me['out'] & them['out'])
            dropped = set(me['bal'].get('drops', ())) | set(them['bal'].get('drops', ()))
            added = set(me['bal'].get('adds', ())) | set(them['bal'].get('adds', ()))
            self.assertEqual(me['before'] | them['before'] | added, me['after'] | them['after'] | dropped)
            self.assertFalse(me['after'] & them['after'])
            for side in (me, them):
                self.assertLessEqual(len(side['active_after']), ACTIVE_LIMIT)
                self.assertLessEqual(len(side['il_after']), IL_LIMIT)
                self.assertFalse(set(side['bal'].get('drops', ())) & side['in'])   # never cut an incoming player
            kinds = (c['balancing']['me']['kind'], c['balancing']['partner']['kind'])
            if c['package'] in ('1-1', '2-2'):   # roster sizes unchanged: no balancing
                self.assertEqual(kinds, ('none', 'none'))
            elif c['package'] == '2-1':
                self.assertEqual(kinds[0], 'open')
                self.assertIn(kinds[1], ('drop', 'il'))      # every partner starts with 14 active players
            else:
                self.assertIn(kinds[0], ('drop', 'il'))
                self.assertEqual(kinds[1], 'open')
        for e in self.result['all_evaluated']:          # mechanics hold for every replayed package
            k = (e['balancing']['me']['kind'], e['balancing']['partner']['kind'])
            self.assertIn(k, {'1-1': {('none', 'none')}, '2-2': {('none', 'none')},
                              '2-1': {('open', 'drop'), ('open', 'il')},
                              '1-2': {('drop', 'open'), ('il', 'open')}}[e['package']])

    def test_independent_phase2_totals_for_both_sides(self):
        eff_time = datetime.combine(self.eff, time(0), EASTERN).astimezone(UTC)
        site = {p.site_player_id: p for p in self.inputs['site_players']}
        for c in (self.by_type['2-1'][0], self.by_type['1-2'][0]):
            for side, s in self.sides(c).items():
                incoming = s['in'] | set(s['bal'].get('adds', ()))
                roster = [*self.inputs['rosters'][s['team']],
                          *(RosterPlayer(s['team'], p, site[p].name, 'BN', site[p].eligible_positions, site[p].nba_team)
                            for p in incoming)]
                proj = [(p, resolve_projection(self.inputs['projections'][p.site_player_id], .5)) for p in roster]
                leaving = s['out'] | set(s['bal'].get('drops', ())) | set(s['bal'].get('il', ()))
                raws = self.ctx.team_weeks(s['team'], tuple(s['out']), tuple(s['in']), s['bal'].get('drops'),
                                           s['bal'].get('adds'), s['bal'].get('il'))['raw']
                for wi, week in enumerate(self.ctx.weeks[:4]):     # includes the effective-day split in Week 1
                    totals, _, _ = _dated_team_totals(roster, proj, self.inputs['schedule'], week, self.as_of,
                                                      self.ctx.engine.snapshot_day, {p: eff_time for p in incoming},
                                                      {p: eff_time for p in leaving}, injury_inputs=self.inputs)
                    for cat in CATEGORIES:
                        self.assertAlmostEqual(raws[wi]['categories'][cat], totals['categories'][cat], places=6)

    def test_legal_lineups_shooting_and_turnovers(self):
        eng = self.ctx.engine
        for c in (self.by_type['2-1'][0], self.by_type['1-2'][0]):
            for s in self.sides(c).values():
                for day in self.ctx.week_days[2]:
                    _, starters = eng.day(day, frozenset(s['active_after']), {}, {})
                    self.assertLessEqual(len(starters), 10)
                    placed = assign_lineup([eng.players[p] for p in starters], {p: eng.score[p] for p in starters},
                                           require_full=False)
                    self.assertEqual(set(placed), set(starters))
                    used = Counter(placed.values())
                    self.assertTrue(all(used[slot] <= SLOTS.count(slot) for slot in used))
                    self.assertTrue(all(eligible(slot, eng.players[p].eligible_positions) for p, slot in placed.items()))
                raws = self.ctx.team_weeks(s['team'], tuple(s['out']), tuple(s['in']), s['bal'].get('drops'),
                                           s['bal'].get('adds'), s['bal'].get('il'))['raw']
                for raw in raws:
                    v = raw['shooting_volumes']
                    self.assertAlmostEqual(raw['categories']['fg_pct'], v['fgm'] / v['fga'])
                    self.assertAlmostEqual(raw['categories']['ft_pct'], v['ftm'] / v['fta'])
            changed = self.changed(c)
            for row, mine in zip(self.ctx.rows(self.me, changed), changed[self.me]):
                to, theirs = mine['categories']['turnovers'], row['theirs']['turnovers']
                self.assertEqual(row['outcomes']['turnovers'], 'W' if to < theirs else 'L' if to > theirs else 'T')

    def changed(self, c):
        out = {}
        for s in self.sides(c).values():
            out[s['team']] = self.ctx.team_weeks(s['team'], tuple(s['out']), tuple(s['in']), s['bal'].get('drops'),
                                                 s['bal'].get('adds'), s['bal'].get('il'))['raw']
        return out

    def test_reciprocal_matchups_after_multi_trades(self):
        for c in (self.by_type['2-1'][0], self.by_type['1-2'][0]):
            changed, partner = self.changed(c), c['partner']['team_id']
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

    def test_replacement_opportunity_cost_and_drop_cost(self):
        fills = [(c, s) for c in self.cards for s in ('me', 'partner')
                 if (c['balancing'][s].get('fill') or {}).get('used')]
        self.assertTrue(fills)
        site = {p.site_player_id: p for p in self.inputs['site_players']}
        owned = {p.site_player_id for r in self.inputs['rosters'].values() for p in r}
        cfg = TradeConfig()
        for c, s in fills:
            f = c['balancing'][s]['fill']
            fa = f['player']['site_player_id']
            self.assertNotIn(fa, owned)
            self.assertEqual(site[fa].availability_label, 'FA')     # never a waiver player
            self.assertTrue(f['consumes_acquisition'])
            team = self.me if s == 'me' else c['partner']['team_id']
            drop = next(p.site_player_id for p in self.inputs['rosters'][team] if p.name == f['opportunity']['drop'])
            raws = self.ctx.team_weeks(team, (), (), (drop,), (fa,))
            base = sum(r['points'] for r in self.ctx.rows(team, {}))
            alone = sum(r['points'] for r in self.ctx.rows(team, {team: raws['raw']}))
            self.assertAlmostEqual(alone - base, f['opportunity']['points'])
            edge = max(0.0, f['net_adjusted'] - f['fallback']['net_adjusted'])
            expected = c[s]['adjusted_delta'] - max(0.0, f['opportunity']['adjusted']) - (1 - cfg.fill_credit) * edge
            self.assertAlmostEqual(c['effective'][s], expected)
            self.assertAlmostEqual(f['net_adjusted'], c[s]['adjusted_delta'] - max(0.0, f['opportunity']['adjusted']))
        for c in self.cards:          # drop cost = exchange alone minus the balanced result
            for s in ('me', 'partner'):
                b = c['balancing'][s]
                if b['kind'] in ('drop', 'il'):
                    self.assertAlmostEqual(b['exchange_points'] - b['cost'], c[s]['points_delta'])
                    chosen = max(b['options'], key=lambda o: (o['points_delta'], o['adjusted_delta']))
                    self.assertEqual(chosen['points_delta'], c[s]['points_delta'])

    def test_pruning_uniqueness_and_distinct_cards(self):
        r = self.result
        self.assertEqual(len({e['id'] for e in r['all_evaluated']}), len(r['all_evaluated']))
        mine = r['diagnostics']['my_tradeable']
        theirs = [sum(p.assigned_slot != 'IL' for p in self.inputs['rosters'][t]) for t in r['diagnostics']['partners']]
        self.assertEqual(tuple(r['diagnostics']['partners']), PARTNERS)
        gen = r['diagnostics']['by_type']
        self.assertEqual(gen['1-1']['generated'], sum(mine * n for n in theirs))
        self.assertEqual(gen['2-1']['generated'], sum(comb(mine, 2) * n for n in theirs))
        self.assertEqual(gen['1-2']['generated'], sum(mine * comb(n, 2) for n in theirs))
        self.assertEqual(gen['2-2']['generated'], sum(comb(mine, 2) * comb(n, 2) for n in theirs))
        self.assertEqual(r['diagnostics']['pairs_screened'], sum(v['feasible'] for v in gen.values()))
        seen_r, given = set(), Counter()
        for c in r['cards']:
            self.assertEqual(set(c['sources']), {'hashtag', 'leaguewinner'})
            if c.get('alternative'):
                continue
            self.assertFalse(ids(c['receives_all']) & seen_r)
            seen_r |= ids(c['receives_all'])
            given.update(ids(c['gives_all']))
            self.assertEqual(c['tier'], 'recommended')      # nothing below threshold is forced onto the page
            self.assertGreaterEqual(c['effective']['me'], TradeConfig().material_gain)
        self.assertFalse(r['verification']['executable'])
        self.assertLessEqual(max(given.values()), 4)    # the fixture's per_player_limit
        a = {'partner': {'team_id': '3'}, 'gives_all': [{'site_player_id': 'A'}], 'receives_all': [{'site_player_id': 'X'}]}
        b = {'partner': {'team_id': '3'}, 'gives_all': [{'site_player_id': 'A'}, {'site_player_id': 'B'}],
             'receives_all': [{'site_player_id': 'X'}]}
        vals = {'A': {'value': 120}, 'B': {'value': VALUE_FLOOR - 1}, 'X': {'value': 130}}
        self.assertTrue(_similar(a, b, vals))
        vals['B'] = {'value': 60}
        self.assertFalse(_similar(a, b, vals))
        self.assertEqual(side_value({'A': {'value': 50}, 'B': {'value': -75}}, ('A', 'B')), 50)
        self.assertEqual(side_value({'B': {'value': -75}}, 'B'), -75)                      # 4A single-player value kept

    def test_mutual_requires_fallback_fill_and_multi_compared_with_one_for_one(self):
        cfg = TradeConfig()
        for c in self.cards:
            if c['package'] != '1-1':
                self.assertEqual(len(c['one_for_one_subsets']), len(c['gives_all']) * len(c['receives_all']))
                alt = c['simpler_alternative']
                if alt:
                    self.assertGreaterEqual(alt['me_adjusted'], c['effective']['me'] - cfg.simpler_band)
                    self.assertLessEqual(alt['asset_cost'], c['asset_cost'])
            if c['label'] == 'Mutually beneficial':
                for s, floor in (('me', cfg.material_gain), ('partner', cfg.partner_credible_gain)):
                    f = c['balancing'][s].get('fill')
                    if f and f['used']:
                        self.assertGreaterEqual(f['fallback']['net_adjusted'], floor)
        streaming = [c for c in self.cards if c.get('streaming_slot')]
        for c in streaming:
            self.assertEqual(c['package'], '2-1')
            self.assertTrue(any('streaming slot' in x for x in c['display']['balancing']))

    def test_projection_source_consistency(self):
        c = self.by_type['2-1'][0]
        sctx = _Context(self.inputs, self.bases[1.0], 1.0, self.as_of, None, self.eff)
        mb, tb = _bal(c['balancing']['me']), _bal(c['balancing']['partner'])
        for a in (*mb.get('adds', ()), *tb.get('adds', ())):
            sctx.register(a)
        r = evaluate(sctx, TradeConfig(), self.me, c['partner']['team_id'], tuple(ids(c['gives_all'])),
                     tuple(ids(c['receives_all'])), mb, tb)
        self.assertEqual(r['me']['points_delta'], c['sources']['hashtag']['me_points'])
        self.assertEqual(r['partner']['points_delta'], c['sources']['hashtag']['partner_points'])

    def test_market_value_follows_adp_and_consensus_and_blocks_lopsided_trades(self):
        market = market_values(self.inputs, self.ctx)
        pid = {p.name: p.site_player_id for p in self.inputs['site_players']}
        adp = {n: market[pid[n]]['rank'] for n in ('Cade Cunningham', 'Chet Holmgren', 'Myles Turner', 'Ivica Zubac',
                                                   'Julius Randle', 'Deni Avdija')}
        self.assertEqual(adp, {'Cade Cunningham': 6.0, 'Chet Holmgren': 32.1, 'Myles Turner': 108.8,
                               'Ivica Zubac': 59.0, 'Julius Randle': 68.5, 'Deni Avdija': 39.5})
        self.assertTrue(all(market[p]['basis'] == 'market_consensus' for p in self.ctx.players
                            if self.ctx.players[p].assigned_slot != 'IL'))
        self.assertGreater(market[pid['Cade Cunningham']]['value'], 1.8 * market[pid['Chet Holmgren']]['value'])
        ranked = sorted((m['signals']['adp'] for m in market.values() if m['rank']), key=lambda a: a['pick'])
        self.assertTrue(all(a['value'] >= b['value'] for a, b in zip(ranked, ranked[1:])))
        mv = lambda *names: sum(market[pid[n]]['value'] for n in names)
        # The two trades flagged as lopsided: the partner would give far more draft value than they get.
        self.assertGreater(value_gap(mv('Ivica Zubac', 'Julius Randle'), mv('Myles Turner'), MARKET_FLOOR), .6)
        self.assertGreater(value_gap(mv('Cade Cunningham', 'Deni Avdija'), mv('Chet Holmgren'), MARKET_FLOOR), .6)
        shown = {c['id'] for c in self.cards}
        for c in self.cards:
            self.assertLessEqual(c['value']['gap'], TradeConfig().viable_market_limit)
            self.assertEqual(c['value']['basis'], 'market_consensus')
        for gives, gets in ((('Myles Turner',), ('Ivica Zubac', 'Julius Randle')),
                            (('Chet Holmgren',), ('Cade Cunningham', 'Deni Avdija'))):
            self.assertNotIn('+'.join(sorted(pid[n] for n in gives)) + '-' + '+'.join(sorted(pid[n] for n in gets)), shown)
        from app.trades import _classify
        cfg = TradeConfig()
        both = {'hashtag': {'me_points': 4, 'me_adjusted': 4, 'partner_points': 2},
                'leaguewinner': {'me_points': 3, 'me_adjusted': 3, 'partner_points': 2}}

        def t(gap, model):
            return {'me': {'adjusted_delta': 4.0, 'flips': []}, 'partner': {'adjusted_delta': 3.0}, 'sources': both,
                    'value': {'gap': gap, 'model_gap': model, 'market': {'give': 50.0, 'get': 50.0, 'my_cut': 0.0}}}
        self.assertEqual(_classify(t(.5, 0), cfg)[0], 'excluded')          # partner would see it as lopsided
        self.assertEqual(_classify(t(0, -.3), cfg)[0], 'recommended')      # model overpay is a risk, not a block
        self.assertEqual(_classify(t(.1, .1), cfg)[0], 'recommended')

    def test_low_usage_guard(self):
        from app.trades import _classify
        cfg = TradeConfig()
        t = {'me': {'adjusted_delta': 3.0, 'flips': []}, 'partner': {'adjusted_delta': 1.0},
             'value': {'gap': -.2, 'market': {'give': 50.0, 'get': 40.0, 'my_cut': 0.0}}}
        self.assertEqual(_classify(t, cfg)[0], 'recommended')
        t['low_usage'] = [{'name': 'Star', 'starts': 20, 'games': 60, 'value': 150}]
        self.assertEqual(_classify(t, cfg)[0], 'recommended')     # 4D: shown as an asset-sacrifice risk instead

    def test_page_shows_packages_and_balancing(self):
        for marker in ("'Roster balancing'", "'Compared with 1-for-1 parts'", "'Roster balancing options'",
                       'gives_all', 'package_label', 'net of what any free-agent fill would be worth'):
            self.assertIn(marker, HTML)


class SearchAndExceptionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = load_inputs(ROOT)
        cls.as_of = _utc(cls.inputs['availability']['captured_at_utc'])
        cls.base = forecast(cls.inputs, hashtag_weight=.5, as_of=cls.as_of)
        cls.me = cls.inputs['league']['my_team_id']
        cls.kw = dict(as_of=cls.as_of, now=cls.as_of, base=cls.base)
        roster = sorted((p for p in cls.inputs['rosters'][cls.me] if p.assigned_slot != 'IL'), key=lambda p: p.name)
        cls.keep = [p.site_player_id for p in roster[:4]]
        cls.protect = frozenset(p.site_player_id for p in roster[4:])

    def test_small_exhaustive_search_matches_staged_search(self):
        common = dict(partner_ids=('3',), protected_ids=self.protect, finalists=0)
        full = trade_lab(self.inputs, config=TradeConfig(shortlist=None, drop_options=None, **common), **self.kw)
        staged = trade_lab(self.inputs, config=TradeConfig(shortlist=20, multi_shortlist=40, partner_floor=0, **common),
                           **self.kw)
        self.assertEqual(len(full['all_evaluated']), sum(full['diagnostics']['by_type'][k]['feasible'] for k in
                                                          ('1-1', '2-1', '1-2', '2-2')))
        exact = {e['id']: e for e in full['all_evaluated']}
        same_drop = 0
        for e in staged['all_evaluated']:
            x = exact[e['id']]
            if e['balancing'] == x['balancing']:
                same_drop += 1
                self.assertEqual((e['me'], e['partner_adjusted'], e['me_effective']),
                                 (x['me'], x['partner_adjusted'], x['me_effective']))
            else:   # fewer drop options can only find an equal or worse drop for the side choosing it
                side = 'me' if e['balancing']['me']['kind'] in ('drop', 'il') else 'partner'
                self.assertLessEqual(e['me_points'] if side == 'me' else 0, x['me_points'] if side == 'me' else 0)
        self.assertGreaterEqual(same_drop / len(staged['all_evaluated']), .9)
        for typ in ('1-1', '2-1', '1-2', '2-2'):  # recall of the best exact packages of each type
            best = [e['id'] for e in sorted(full['all_evaluated'], key=lambda e: -e['score'])
                    if e['package'] == typ and e['me_effective'] >= 1.5 and e['gap'] <= TradeConfig().viable_market_limit
                    and (e['model_gap'] >= -TradeConfig().viable_model_floor
                         or e['screen_me'] >= TradeConfig().floor_screen_gate)][:3]   # could still qualify
            kept = {e['id'] for e in staged['all_evaluated']}
            self.assertGreaterEqual(sum(i in kept for i in best), min(2, len(best)), typ)

    def test_empty_slot_il_move_and_cant_cut_exceptions(self):
        inputs = {k: v for k, v in self.inputs.items() if k != '_phase2_plan_cache'}   # forecast plan cache is per roster set
        inputs['rosters'] = {t: list(r) for t, r in self.inputs['rosters'].items()}
        bench = [p for p in inputs['rosters']['3'] if p.assigned_slot == 'BN'][0]
        inputs['rosters']['3'] = [replace(p, assigned_slot='IL') if p is bench else p for p in inputs['rosters']['3']]
        base = forecast(inputs, hashtag_weight=.5, as_of=self.as_of)
        run = trade_lab(inputs, as_of=self.as_of, now=self.as_of, base=base,
                        config=TradeConfig(partner_ids=('3',), protected_ids=self.protect, package_types=('2-1',),
                                           finalists=0))
        self.assertTrue(run['all_evaluated'])
        for e in run['all_evaluated']:      # partner has 13 active players: taking two needs no drop
            self.assertEqual(e['balancing']['partner']['kind'], 'none')
        cheapest = sorted(self.keep)
        evidence = {'status_by_site_id': {cheapest[0]: 'O'}, 'league_cant_cut_site_ids': cheapest[1:3]}
        run = trade_lab(self.inputs, evidence=evidence, **self.kw,
                        config=TradeConfig(partner_ids=('3',), protected_ids=self.protect | {cheapest[3]},
                                           package_types=('1-2',), drop_options=None, finalists=0))
        kinds = Counter((e['balancing']['me']['kind'], e['balancing']['me']['player']) for e in run['all_evaluated'])
        self.assertTrue(kinds)
        for (kind, pid), _ in kinds.items():
            self.assertNotIn(pid, cheapest[1:3])          # Can't Cut players are never dropped
            self.assertIn(kind, ('drop', 'il'))
            if kind == 'il':
                self.assertEqual(pid, cheapest[0])        # only the O-status player can move to IL
        self.assertIn(('il', cheapest[0]), kinds)          # IL move preferred over dropping the same player
        self.assertGreater(run['diagnostics']['by_type']['1-2'].get('infeasible_no_legal_drop', 0), 0)
        self.assertTrue(run['balancing']['cant_cut_verified'])

    def test_phase4a_regression_on_one_for_one(self):
        saved = json.loads(FIXTURE.read_text())
        run = trade_lab(self.inputs, config=TradeConfig(package_types=('1-1',), finalists=16, result_limit=6), **self.kw)
        old = {e['id']: e for e in saved['all_evaluated']}
        new = {e['id']: e for e in run['all_evaluated']}
        for k in set(new) & set(old):   # identical replay numbers; the 4A model value gap is kept as model_gap
            e = new[k]
            self.assertEqual((old[k]['me'], old[k]['me_points'], old[k]['partner_adjusted']),
                             (e['me'], e['me_points'], e['partner_adjusted']))
            self.assertAlmostEqual(old[k]['gap'], e['model_gap'])
        ctx = _Context(self.inputs, self.base, .5, self.as_of, None, self.as_of.astimezone(EASTERN).date() + timedelta(days=3))
        market = market_values(self.inputs, ctx)
        cfg = TradeConfig()
        for k in set(old) - set(new):  # every 4A trade no longer replayed cannot qualify
            give, get = k.split('-')
            self.assertTrue(value_gap(market[get]['value'], market[give]['value'], MARKET_FLOOR) > cfg.viable_market_limit
                            or old[k]['gap'] < -cfg.viable_model_floor, k)
        for e in new.values():          # and every 1-for-1 replayed now is viable
            self.assertLessEqual(e['gap'], cfg.viable_market_limit)
            self.assertTrue(e['model_gap'] >= -cfg.viable_model_floor - 1e-9 or e['screen_me'] >= cfg.floor_screen_gate)


class TogglesAndTopFiveTests(TestCase):
    def test_route_passes_types_and_caches_each_combination(self):
        from unittest.mock import patch
        from app.dashboard import make_handler
        from test_trades import _get
        inputs = {'league': {'my_team_id': '4'}, 'availability': {'captured_at_utc': '2026-10-20T14:11:47Z'}}
        handler = make_handler(inputs)
        with patch('app.dashboard.trade_lab', return_value={'cards': []}) as lab, \
                patch('app.dashboard.forecast', return_value={}):
            codes, _ = _get(handler, '/api/trades?hashtag_weight=0.5&partners=3&types=2-1,1-1')
            _get(handler, '/api/trades?hashtag_weight=0.5&partners=3&types=1-1,2-1')
            self.assertEqual(lab.call_args.kwargs['config'].package_types, ('1-1', '2-1'))
            _get(handler, '/api/trades?hashtag_weight=0.5&partners=3')
            self.assertEqual(lab.call_args.kwargs['config'].package_types, ('1-1', '2-1', '1-2', '2-2'))
            bad, body = _get(handler, '/api/trades?hashtag_weight=0.5&types=3-1')
        self.assertEqual(codes, [200])
        self.assertEqual(lab.call_count, 2)
        self.assertEqual(bad, [400])
        for marker in ('id="tradeTypes"', 'value="2-1"', 'id="tradeApply"', 'id="tradeStatus"', 'function tradeWorking',
                       'function tradeFiltersChanged', 'tradeSeq', 'data-set="tradePartner:1"', 'Untouchable players',
                       'Choose at least one team and one trade type', "e('tradeApply').onclick=loadTrades"):
            self.assertIn(marker, HTML)

    def test_cards_are_never_forced(self):
        inputs = load_inputs(ROOT)
        as_of = _utc(inputs['availability']['captured_at_utc'])
        run = trade_lab(inputs, as_of=as_of, now=as_of,
                        config=TradeConfig(partner_ids=('8',), package_types=('1-1',)))
        self.assertLessEqual(len(run['cards']), TradeConfig().result_limit)
        self.assertEqual(run['qualifying_cards'], len(run['cards']))
        self.assertTrue(all(c['tier'] == 'recommended' for c in run['cards']))
        self.assertNotIn('high_upside', run)                                   # partner market losses are never shown
        for c in run['cards']:
            self.assertEqual(set(c['sources']), {'hashtag', 'leaguewinner'})     # never an unchecked package
        if not run['cards']:
            self.assertTrue(run['headline'].startswith('HOLD'))
