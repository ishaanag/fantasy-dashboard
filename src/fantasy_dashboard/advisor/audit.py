"""Recommendation audit: explains a Waiver Advisor plan with legal counterfactuals.

Called by `advise()` for the shown strategies only (at most five). Every counterfactual reuses
the advisor's own engine, HOLD baseline, opponents, schedule, projection weight and as-of time,
so its numbers are directly comparable with the advisor's card. Nothing here changes ranking.

A step is the advisor tuple (period index, action day, add id, drop id, effective stamp, timing assumed).
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations

from fantasy_dashboard.league import CATEGORIES
from fantasy_dashboard.model.day_engine import DayEngine, horizon_scores, period_totals, score_against
from fantasy_dashboard.model.forecast import with_actual_volumes
from fantasy_dashboard.model.lineups import lineup_score, resolve_projection

ORDER = {'L': 0, 'T': 1, 'W': 2}
FULL_SUBSET_LIMIT = 5          # enumerate every sub-plan up to this many steps; larger plans get the key ones
NARROW = .05                   # margin under 5% of a typical weekly spread
SOURCES = (('hashtag', 'Hashtag', 1.0), ('leaguewinner', 'LeagueWinner', 0.0), ('blended', 'Blended', .5))
MARKET_NOTE = ('Trade or market value is not quantified: the inputs hold projections, not trade values, '
               'roles or return dates.')


def rewire(steps, keep):
    """Keep `keep` (indices, chronological). A player added by an omitted step never joins, so a later
    kept step that dropped him drops the player he would have replaced instead."""
    replaced, out, notes = {}, [], []
    for i, s in enumerate(steps):
        drop = s[3]
        while drop in replaced:
            drop = replaced[drop]
        if i in keep:
            if drop != s[3]:
                notes.append((s[3], drop))
            out.append((s[0], s[1], s[2], drop, s[4], s[5]))
        else:
            replaced[s[2]] = drop
    return tuple(out), notes


def final_roster(initial, steps):
    roster = set(initial)
    for s in steps:
        roster.discard(s[3])
        roster.add(s[2])
    return frozenset(roster)


def _wlt(weeks):
    return [sum(w['score'][i] for w in weeks) for i in range(3)]


def _per_game(values):
    gp = values['gp']
    return {'gp': round(gp, 1),
            'fg_pct': round(values['fgm'] / values['fga'], 3) if values['fga'] else None,
            'ft_pct': round(values['ftm'] / values['fta'], 3) if values['fta'] else None,
            'fga': round(values['fga'], 1), 'fta': round(values['fta'], 1),
            **{k: round(values[k], 2) for k in ('three_ptm', 'pts', 'reb', 'ast', 'stl', 'blk', 'turnovers')},
            'lineup_value_per_game': round(lineup_score(values) / (gp / 82), 3) if gp else None}


class Auditor:
    """Holds advisor state shared by all audited plans: one HOLD, one engine, one cache per source."""

    def __init__(self, ctx):
        self.c = ctx
        self.labels = ctx.labels
        self.hold_starts_by_day = self._daily((), ctx.initial_ids)
        self.sources = {}

    # ---------- shared evaluation ----------
    def name(self, pid):
        return self.c.players[pid].name

    def check(self, steps):
        """Return a reason string when the sequence is not legal under the advisor's own rules."""
        c = self.c
        roster, dropped, counts = set(c.initial_ids), set(), Counter()
        for wi, day, add, drop, stamp, _ in steps:
            if add in roster or add in dropped:
                return f'{self.name(add)} would already be on the roster or was dropped earlier'
            if drop not in roster:
                return f'{self.name(drop)} is not on the roster on {day:%b} {day.day}'
            if drop in c.initial_ids and drop not in c.droppable:
                return f'{self.name(drop)} is protected or core and cannot be dropped by the advisor'
            counts[wi] += 1
            if counts[wi] > c.budgets[wi]:
                return f'exceeds the Week {c.weeks[wi].number} acquisition limit'
            if not c.drop_ok(drop, wi, day, stamp):
                return f'{self.name(drop)} is locked on {day:%b} {day.day}'
            roster.discard(drop)
            roster.add(add)
            dropped.add(drop)
        return None

    def _daily(self, steps, ids):
        since = {s[2]: s[4] for s in steps}
        until = {s[3]: s[4] for s in steps if s[3] is not None}
        counts = Counter()
        for days in self.c.week_days:
            for day, _ in self.c.engine.period(days, ids | set(since), since, until)[1]:
                counts[day] += 1
        return counts

    def evaluate(self, steps):
        c = self.c
        h, starts = c.horizon_eval(steps)
        c.replay(steps, h)       # every counterfactual must match the dated lineup engine
        final = final_roster(c.initial_ids, steps)
        s = c.season(final)
        a = c.assess(steps, h, starts, final)
        flips = []
        for wi, w in enumerate(c.weeks):
            for cat in CATEGORIES:
                before, after = c.hold_h[wi]['outcomes'][cat], h[wi]['outcomes'][cat]
                if before != after:
                    flips.append({'week': w.number, 'category': self.labels[cat], 'before': before, 'after': after})
        shift = {}
        for cat in CATEGORIES:
            net = sum(ORDER[x['outcomes'][cat]] - ORDER[y['outcomes'][cat]] for x, y in zip(s, c.hold_s))
            if net:
                shift[self.labels[cat]] = net / 2      # in category wins: L→W is +1, L→T is +0.5
        daily = self._daily(steps, c.initial_ids)
        days = sorted(set(daily) | set(self.hold_starts_by_day))
        return {
            'steps': steps, 'h': h, 's': s, 'a': a, 'final': final,
            'horizon_points': a['gain'], 'season_points': a['season_points'],
            'season_margin': round(a['season_soft'], 3), 'value': round(a['value'], 3),
            'horizon_record': _wlt(h), 'season_record_delta': [x - y for x, y in zip(_wlt(s), _wlt(c.hold_s))],
            'flips': flips, 'season_category_shift': shift,
            'starts': {'total': sum(daily.values()), 'delta': a['starts'],
                       'daily_delta': [[d.isoformat(), daily[d] - self.hold_starts_by_day[d]] for d in days
                                       if daily[d] != self.hold_starts_by_day[d]]},
        }

    def row(self, ev, label, roles, notes=(), reason=None):
        c = self.c
        out = {'label': label, 'roles': roles, 'feasible': reason is None, 'infeasible_reason': reason,
               'moves': [{'add': self.name(s[2]), 'drop': self.name(s[3]), 'date': s[1].isoformat(),
                          'week': c.weeks[s[0]].number} for s in (ev['steps'] if ev else ())],
               'rewired': [f'drops {self.name(new)} instead of {self.name(old)}, who never joins'
                           for old, new in notes]}
        if ev:
            out.update({'acquisitions': len(ev['steps']), 'horizon_points': ev['horizon_points'],
                        'horizon_record': ev['horizon_record'], 'season_points': ev['season_points'],
                        'season_record_delta': ev['season_record_delta'], 'season_margin': ev['season_margin'],
                        'value': ev['value'], 'flips': ev['flips'], 'season_category_shift': ev['season_category_shift'],
                        'starts': ev['starts'],
                        'final_roster': {'added': sorted(self.name(p) for p in ev['final'] - c.initial_ids),
                                         'dropped': sorted(self.name(p) for p in c.initial_ids - ev['final'])}})
        return out

    # ---------- projection sources ----------
    def source(self, key, weight):
        if key in self.sources:
            return self.sources[key]
        c = self.c
        ids = set(c.initial_ids) | set(c.all_opponent_ids) | set(c.candidate_ids)
        proj = {}
        for pid in ids:
            try:
                proj[pid] = resolve_projection(c.inputs['projections'][pid], weight)
            except (ValueError, KeyError):
                pass
        if not (set(c.initial_ids) | set(c.all_opponent_ids)) <= set(proj):
            self.sources[key] = None
            return None
        eng = DayEngine(c.schedule, {p: c.all_players[p] for p in proj}, proj, c.as_of, c.snapshot_day, c.inputs)
        opp_h = []
        for wi, w in enumerate(c.weeks):
            raw = period_totals(eng.period(c.week_days[wi], c.opponent_rosters[wi])[0])
            vols = c.current_volumes.get(c.opponent[w.number])
            if wi == 0 and vols:
                raw = with_actual_volumes(raw, vols)
            opp_h.append(raw['categories'])
        opp_s = [period_totals(eng.period(days, roster)[0])['categories']
                 for days, roster in zip(c.later_days, c.later_opponent_rosters)]
        cache = {}

        def run(steps):
            if not {s[2] for s in steps} <= set(proj):
                return None
            h, _ = horizon_scores(eng, c.week_days, opp_h, steps, c.initial_ids, c.actual, c.scales)
            final = final_roster(c.initial_ids, steps)
            if final not in cache:
                cache[final] = [score_against(period_totals(eng.period(d, final)[0]), o, c.scales)
                                for d, o in zip(c.later_days, opp_s)]
            return h, cache[final]
        hold = run(())
        self.sources[key] = (run, hold, proj)
        return self.sources[key]

    def source_rows(self, ev):
        """Short-horizon and post-horizon deltas for one plan under each available projection source."""
        c, rows = self.c, {}
        rows['selected'] = {'label': 'Selected weight' if abs(c.weight - .5) > 1e-9 else 'Blended',
                            'horizon_points': ev['horizon_points'], 'season_points': ev['season_points'],
                            'season_margin': ev['season_margin']}
        for key, label, w in SOURCES:
            if abs(w - c.weight) < 1e-9:
                continue
            src = self.source(key, w)
            got = src and src[0](ev['steps'])
            if not got:
                rows[key] = {'label': label, 'unavailable': True}
                continue
            (h, s), (hh, hs) = got, src[1]
            rows[key] = {'label': label,
                         'horizon_points': sum(x['points'] for x in h) - sum(x['points'] for x in hh),
                         'season_points': sum(x['points'] for x in s) - sum(x['points'] for x in hs),
                         'season_margin': round(sum(x['soft'] for x in s) - sum(x['soft'] for x in hs), 3),
                         '_h': h, '_hh': hh}
        return rows

    # ---------- players ----------
    def player_sources(self, pid):
        out = {}
        for key, _, w in SOURCES[:2]:
            try:
                out[key] = _per_game(resolve_projection(self.c.inputs['projections'][pid], w)['values'])
            except (ValueError, KeyError):
                out[key] = None
        h, l, flags = out['hashtag'], out['leaguewinner'], []
        if h is None or l is None:
            flags.append('only one source projects him')
        else:
            if abs(h['gp'] - l['gp']) >= 15:
                flags.append(f"GP {h['gp']:g} vs {l['gp']:g}")
            a, b = h['lineup_value_per_game'] or 0, l['lineup_value_per_game'] or 0
            if max(abs(a), abs(b)) > 0 and abs(a - b) / max(abs(a), abs(b)) >= .25:
                flags.append(f'lineup value {a:.2f} vs {b:.2f}')
            for k, lab in (('pts', 'PTS'), ('reb', 'REB'), ('ast', 'AST'), ('three_ptm', '3PTM'),
                           ('stl', 'STL'), ('blk', 'BLK')):
                if max(h[k], l[k]) >= .5 and abs(h[k] - l[k]) / max(h[k], l[k]) >= .3:
                    flags.append(f'{lab} {h[k]:g} vs {l[k]:g}')
        return {'name': self.name(pid), 'hashtag': h, 'leaguewinner': l, 'disagreement': flags}

    def player_starts(self, steps, pid):
        """Lineup starts this player gets: horizon under `steps`, then post-horizon on the final roster."""
        c = self.c
        since = {s[2]: s[4] for s in steps}
        until = {s[3]: s[4] for s in steps if s[3] is not None}
        ids = c.initial_ids | set(since)
        count = sum(p == pid for days in c.week_days for _, p in c.engine.period(days, ids, since, until)[1])
        final = final_roster(c.initial_ids, steps)
        later = sum(p == pid for days in c.later_days for _, p in c.engine.period(days, final)[1])
        gp = c.projections[pid]['values']['gp']
        return {'horizon': count, 'post_horizon': later, 'expected_after_gp': round((count + later) * gp / 82, 1)}

    def replacement(self, k, net_steps, net_ev):
        c, step = self.c, net_steps[k]
        add, drop = step[2], step[3]
        vin, gin = c.season_value(add)
        vout, gout = c.season_value(drop)
        pin, pout = c.projections[add]['values'], c.projections[drop]['values']
        alone = (step,)
        reason = self.check(alone)
        ind = None if reason else self.evaluate(alone)
        rest, _ = rewire(net_steps, frozenset(range(len(net_steps))) - {k})
        without = self.evaluate(rest) if len(net_steps) > 1 else None
        shared = set(c.players[add].eligible_positions) & set(c.players[drop].eligible_positions)
        same_pos = [p for p in c.pool if set(c.players[p].eligible_positions) & set(c.players[drop].eligible_positions)]
        flags = []
        if vin < vout:
            flags.append(f'Asset downgrade: {self.name(add)} projects {vin:.0f} rest-of-season lineup value '
                         f'against {vout:.0f} for {self.name(drop)}')
        elif _per_game(pin)['lineup_value_per_game'] < _per_game(pout)['lineup_value_per_game']:
            flags.append(f"{self.name(add)} is weaker per game; his edge rests on projected GP "
                         f"({pin['gp']:.1f} vs {pout['gp']:.1f}) and {gin} vs {gout} scheduled games")
        if ind and ind['horizon_points'] > 0 and ind['season_points'] <= 0:
            flags.append('Helps the short term only; on its own it does not improve the rest of the season')
        if ind and -c.tau < ind['season_margin'] < 0:
            flags.append(f"Its rest-of-season margin loss ({ind['season_margin']:+.2f}) is below the ranking's "
                         f'{c.tau:g} noise allowance, so ranking treats it as free')
        delta = (None if without is None else
                 {'horizon_points': net_ev['horizon_points'] - without['horizon_points'],
                  'season_points': net_ev['season_points'] - without['season_points'],
                  'season_margin': round(net_ev['season_margin'] - without['season_margin'], 3)})
        return {
            'incoming': self.name(add), 'outgoing': self.name(drop), 'date': step[1].isoformat(),
            'shared_positions': sorted(shared),
            'per_game': {'incoming': _per_game(pin), 'outgoing': _per_game(pout)},
            'remaining_games': {'incoming': gin, 'outgoing': gout},
            'expected_remaining_gp': {'incoming': round(pin['gp'] / 82 * gin, 1),
                                      'outgoing': round(pout['gp'] / 82 * gout, 1)},
            'lineup_value': {'incoming': round(vin, 1), 'outgoing': round(vout, 1)},
            'usable_starts': {'incoming': self.player_starts(net_steps, add),
                              'outgoing': self.player_starts((), drop)},
            'independent': (self.row(ind, 'This swap alone', ['independent']) if ind else
                            {'feasible': False, 'infeasible_reason': reason}),
            'within_plan': delta,
            'scarcity': {'incoming_pool_rank': 1 + sum(v > c.pool_value.get(add, 0) for v in c.pool_value.values()),
                         'pool_size': len(c.pool_value),
                         'available_at_outgoing_positions_at_least_as_valuable':
                             sum(c.pool_value[p] >= vout for p in same_pos),
                         'best_available_at_outgoing_positions': round(max((c.pool_value[p] for p in same_pos),
                                                                            default=0.0), 1)},
            'market_value': MARKET_NOTE, 'flags': flags}

    # ---------- one plan ----------
    def audit(self, rec, card):
        c, steps = self.c, rec[0]
        n = len(steps)
        full = self.evaluate(steps)
        for key, mine in (('category_points_gained', full['horizon_points']),
                          ('usable_starts_gained', full['starts']['delta'])):
            if abs(card[key] - mine) > 1e-9:
                raise AssertionError(f'Audit {key} diverged from the advisor card')
        if abs(card['season']['category_points_delta'] - full['season_points']) > 1e-9 or \
                abs(card['season']['margin_score_delta'] - full['season_margin']) > 1e-3:
            raise AssertionError('Audit rest-of-season totals diverged from the advisor card')
        final = full['final']
        net_keep = frozenset(i for i, s in enumerate(steps) if s[2] in final)
        everything = frozenset(range(n))
        if n <= FULL_SUBSET_LIMIT:
            subsets = [frozenset(x) for size in range(1, n) for x in combinations(range(n), size)]
        else:
            subsets = list({*(frozenset(range(k)) for k in range(1, n)), *(frozenset({k}) for k in range(n)),
                            *(everything - {k} for k in range(n)), net_keep} - {everything, frozenset()})
        alts = {}
        for keep in subsets:
            st, notes = rewire(steps, keep)
            reason = self.check(st)
            roles = [r for r, ok in (('prefix', keep == frozenset(range(len(keep)))), ('single', len(keep) == 1),
                                     ('omit', len(keep) == n - 1), ('net_permanent', keep == net_keep)) if ok]
            alts[keep] = {'steps': st, 'notes': notes, 'reason': reason, 'roles': roles,
                          'ev': None if reason else self.evaluate(st)}

        def ev_of(keep):
            return full if keep == everything else None if not keep else alts[keep]['ev']

        def delta(a, b):
            if a is None:
                return None
            return {k: round(a[k] - (b[k] if b else 0), 3)
                    for k in ('horizon_points', 'season_points', 'season_margin', 'value')}

        def verdict(d):
            h, s, m = d['horizon_points'], d['season_points'], d['season_margin']
            if (h > 0 and s >= 0) or (s > 0 and h >= 0):
                return 'adds value'
            if (h > 0 > s) or (s > 0 > h):
                return 'tradeoff'
            if h < 0 or s < 0:
                return 'hurts'
            return 'margins only' if m > .25 else 'costs margin' if m < -.25 else 'no measurable value'

        transactions = []
        for k, s in enumerate(steps):
            chrono = delta(ev_of(frozenset(range(k + 1))), ev_of(frozenset(range(k))))
            omit = alts.get(everything - {k}) if n > 1 else None
            leave = (chrono if n == 1 else delta(full, omit['ev']) if omit and omit['ev'] else None)
            single = alts.get(frozenset({k})) if n > 1 else {'ev': full, 'reason': None, 'notes': []}
            transactions.append({
                'step': k + 1, 'add': self.name(s[2]), 'drop': self.name(s[3]), 'date': s[1].isoformat(),
                'temporary': s[2] not in final,
                'chronological': chrono,
                'leave_one_out': leave,
                'leave_one_out_note': (omit['reason'] if omit and omit['reason'] else
                                       '; '.join(self.row(None, '', [], omit['notes'])['rewired']) if omit else ''),
                'independent': delta(single['ev'], None) if single and single['ev'] else None,
                'independent_note': (single['reason'] if single and single['reason'] else
                                     '; '.join(self.row(None, '', [], single['notes'])['rewired']) if single else ''),
                'verdict': (verdict(leave) if leave else 'required by a later move'),
            })
        singles = [t['independent'] for t in transactions]
        interaction = None
        if n > 1 and all(singles):
            interaction = {k: round(full[k] - sum(x[k] for x in singles), 3)
                           for k in ('horizon_points', 'season_points', 'season_margin')}
            interaction['nonlinear'] = abs(interaction['horizon_points']) >= .5 or abs(interaction['season_points']) >= .5

        # Shorter legal plans that match or beat the full plan.
        fh, fs = full['horizon_points'], full['season_points']
        simpler = []
        for keep, a in alts.items():
            ev = a['ev']
            if ev is None:
                continue
            h, s = ev['horizon_points'], ev['season_points']
            tier = ('preferable' if h >= fh and s >= fs else
                    'comparable' if (h >= fh and s >= fs - 1) or (h >= fh - .5 and s >= fs) else None)
            if tier is None and 'net_permanent' not in a['roles']:
                continue
            kept = sorted(self.name(p) for p in (c.initial_ids - final) - (c.initial_ids - ev['final']))
            trade = [f'saves {n - len(a["steps"])} acquisition{"s" if n - len(a["steps"]) != 1 else ""}']
            if kept:
                trade.append('keeps ' + ', '.join(kept))
            trade.append(f'short term {h - fh:+g}, rest of season {s - fs:+g} category wins, '
                         f"margin score {ev['season_margin'] - full['season_margin']:+.2f} vs the full plan")
            row = self.row(ev, ' then '.join(f'+{self.name(x[2])} / −{self.name(x[3])}' for x in a['steps']),
                           a['roles'], a['notes'])
            row.update({'tier': tier or 'reference', 'tradeoffs': trade, '_keep': keep})
            simpler.append(row)
        rank = {'preferable': 0, 'comparable': 1, 'reference': 2}
        simpler.sort(key=lambda r: (rank[r['tier']], -(r['horizon_points'] + r['season_points']),
                                    r['acquisitions'], -r['season_margin']))
        simpler = simpler[:3]

        # Permanent replacements: the net plan after temporary streams are removed.
        net_steps, _ = rewire(steps, net_keep)
        net_ev = full if net_keep == everything else (alts[net_keep]['ev'] if net_keep else None)
        replacements = [self.replacement(k, net_steps, net_ev) for k in range(len(net_steps))] if net_ev else []
        temporary = [{'player': self.name(s[2]), 'added': s[1].isoformat(),
                      'dropped': next(x[1].isoformat() for x in steps if x[3] == s[2]),
                      'starts': self.player_starts(steps, s[2])['horizon']}
                     for s in steps if s[2] not in final]

        # Projection-source robustness for the plan and its key counterfactuals.
        checked = [('Recommended plan', full)]
        if net_ev is not None and net_keep != everything:
            checked.append(('Permanent changes only', net_ev))
        for r in simpler:
            ev = alts[r['_keep']]['ev']
            if ev is not net_ev:
                checked.append((r['label'], ev))
        for t, k in zip(transactions, range(n)):
            omit = alts.get(everything - {k}) if n > 1 else None
            if omit and omit['ev'] is not None and omit['ev'] is not net_ev and \
                    all(omit['ev'] is not x for _, x in checked):
                checked.append((f"Without +{t['add']} / −{t['drop']}", omit['ev']))
        sensitivity, source_flags = [], []
        full_rows = None
        for label, ev in checked:
            rows = self.source_rows(ev)
            if full_rows is None:
                full_rows = rows
            sensitivity.append({'plan': label, 'sources': {k: {x: y for x, y in v.items() if not x.startswith('_')}
                                                          for k, v in rows.items()}})
        for f in full['flips']:
            if f['after'] != 'W':
                continue
            wi = next(i for i, w in enumerate(c.weeks) if w.number == f['week'])
            cat = next(k for k, v in self.labels.items() if v == f['category'])
            lost = [v['label'] for k, v in full_rows.items() if '_h' in v and not
                    (v['_h'][wi]['outcomes'][cat] == 'W' and v['_hh'][wi]['outcomes'][cat] != 'W')]
            if lost and not any(x['week'] == f['week'] and x['category'] == f['category']
                                for x in card['fragile_flips']):
                source_flags.append(f"Week {f['week']} {f['category']} win is not a flip under {' or '.join(lost)}")
        for k, v in full_rows.items():
            if k == 'selected' or v.get('unavailable'):
                continue
            if full['season_points'] > 0 and v['season_points'] <= 0:
                source_flags.append(f"Rest-of-season gain disappears under {v['label']} ({v['season_points']:+g})")
            if full['horizon_points'] > 0 and v['horizon_points'] <= 0:
                source_flags.append(f"Short-term gain disappears under {v['label']} ({v['horizon_points']:+g})")
        narrow = [{'week': w.number, 'category': self.labels[cat], 'outcome': full['h'][wi]['outcomes'][cat],
                   'margin': round(full['h'][wi]['margins'][cat], 4), 'typical_spread': round(c.scales[cat], 4)}
                  for wi, w in enumerate(c.weeks) for cat in CATEGORIES
                  if full['h'][wi]['margins'][cat] is not None
                  and abs(full['h'][wi]['margins'][cat]) <= NARROW * c.scales[cat]]
        involved = list(dict.fromkeys([*(s[2] for s in steps), *(s[3] for s in steps if s[3] in c.initial_ids)]))
        players = [self.player_sources(p) for p in involved]

        warnings = [f"{f['category']} in Week {f['week']} is fragile: {f['reason']}" for f in card['fragile_flips']]
        warnings += source_flags
        warnings += [f"{p['name']}: Hashtag and LeagueWinner disagree ({'; '.join(p['disagreement'])})"
                     for p in players if p['disagreement']]
        for r in replacements:
            warnings += [f"{r['outgoing']} → {r['incoming']}: {x}" for x in r['flags']]
        for t in transactions:
            if t['verdict'] in ('no measurable value', 'hurts', 'costs margin'):
                warnings.append(f"Step {t['step']} (+{t['add']} / −{t['drop']}): {t['verdict']} when removed from the plan")
        if interaction and interaction['nonlinear']:
            warnings.append('Moves interact: the full plan differs from the sum of its moves made alone')
        warnings += [f"Simpler plan is {r['tier']}: {r['label']}" for r in simpler if r['tier'] == 'preferable']
        if card['eligibility_class'] != 'verified_current':
            warnings.append('Yahoo evidence is provisional: ' + (', '.join(card['missing_verification']) or
                                                                 card['eligibility_class']))
        best_simple = next((r for r in simpler if r['tier'] == 'preferable'), None)
        return {
            'summary': {
                'short_term': {'category_wins': full['horizon_points'],
                               'weeks': [{'week': w['week'], 'before': w['before'], 'after': w['after']}
                                         for w in card['weeks']]},
                'rest_of_season': {'category_wins': full['season_points'], 'margin_score': full['season_margin'],
                                   'weeks': len(c.later_days)},
                'acquisitions': n, 'temporary_adds': len(temporary),
                'net_changes': [f"{r['outgoing']} → {r['incoming']}" for r in replacements],
                'preferable_simpler_plan': best_simple['label'] if best_simple else None,
                'confidence_limits': ['Deterministic projections; fragility checks are diagnostics, not probabilities.',
                                      'Rest-of-season figures assume no further moves by you after the horizon.']},
            'transactions': transactions, 'interaction': interaction,
            'temporary': temporary, 'replacements': replacements,
            'simpler_alternatives': [{k: v for k, v in r.items() if k != '_keep'} for r in simpler],
            'sensitivity': sensitivity, 'narrow_margins': narrow, 'player_sources': players,
            'warnings': warnings,
            'scope': {'horizon_weeks': [w.number for w in c.weeks], 'post_horizon_weeks': c.later_numbers,
                      'as_of_utc': c.as_of.isoformat().replace('+00:00', 'Z'), 'projection_weight': c.weight,
                      'baseline': 'HOLD on the same capture, opponents, schedule and projection weight'},
        }
