"""Two-team replay context, the linear screen, side metrics and z-score model value.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from math import sqrt

from fantasy_dashboard.league import (
    BASE_POSITIONS,
    CATEGORIES,
    EASTERN,
    INACTIVE_STATUSES,
    SLOTS,
    VOLUMES,
    RosterPlayer,
    eligible,
)
from fantasy_dashboard.model.day_engine import DayEngine, date_range, period_totals
from fantasy_dashboard.model.forecast import (
    category_margin,
    category_result,
    parse_record,
    relative_strength,
    seed_order,
    win_pct,
    with_actual_volumes,
)
from fantasy_dashboard.model.lineups import lineup_score, resolve_projection
from fantasy_dashboard.trades.config import COUNTING, _ids
from fantasy_dashboard.yahoo.evidence import pool_audit


def _vector(raw):
    v = {**raw['shooting_volumes'], **{c: raw['categories'][c] for c in COUNTING}}
    return [v[k] for k in VOLUMES]


def _points(score):
    return score[0] + .5 * score[2]


class _Context:
    """One projection source: deterministic base, shared day engine and per-team weekly totals."""

    def __init__(self, inputs, base, weight, as_of, actual_state, eff_day):
        self.inputs, self.base, self.weight, self.as_of = inputs, base, weight, as_of
        self.teams = sorted(inputs['teams'], key=int)
        self.players = {p.site_player_id: p for t in self.teams for p in inputs['rosters'][t]}
        self.proj = {pid: resolve_projection(inputs['projections'][pid], weight) for pid in self.players}
        snapshot_day = date.fromisoformat(inputs['availability']['lineup_date'])
        self.site = {p.site_player_id: p for p in inputs['site_players']}
        # The engine gets its own maps so free-agent fills never enter the rostered asset-value pool.
        self.engine = DayEngine(inputs['schedule'], dict(self.players), dict(self.proj), as_of, snapshot_day, inputs)
        self.first = base['actual_completed_weeks'] + 1
        today = as_of.astimezone(EASTERN).date()
        self.weeks = inputs['weeks'][self.first-1:18]
        self.week_days = [list(date_range(max(today, w.start), w.end)) for w in self.weeks]
        self.eff_day = eff_day
        self.actual = ((actual_state or {}).get('current_week_volumes') or {}) if self.first == self.weeks[0].number else {}
        self.pairings, self.history, self.base_cats = {}, {}, {}
        for m in base['weekly_matchups']:
            a, b = m['teams']
            s = parse_record(m['score'])
            self.pairings.setdefault(m['week'], []).append((a, b))
            self.history.setdefault(m['week'], {})[a] = s
            self.history[m['week']][b] = [s[1], s[0], s[2]]
            if m['status'] == 'projected':
                for t in (a, b):
                    self.base_cats[(m['week'], t)] = m['totals'][t]['categories']
        self.base_seed = {t['team_id']: t['projected_seed'] for t in base['teams']}
        self.base_record = {t['team_id']: list(t['projected_record']) for t in base['teams']}
        self.team_games = Counter()
        sched = inputs['schedule']
        for (nba, day), game in sched.team_dates.items():
            if eff_day <= day <= self.weeks[-1].end and game.tipoff_utc > as_of:
                self.team_games[nba] += 1
        self.cache, self.row_cache = {}, {}
        self.base_raw = {t: self.team_weeks(t) for t in self.teams}
        for t in self.teams:   # HOLD must reproduce the deterministic forecast for every team
            for w, raw in zip(self.weeks, self.base_raw[t]['raw']):
                expected = self.base_cats[(w.number, t)]
                for c in CATEGORIES:
                    x, y = raw['categories'][c], expected[c]
                    if (x is None) != (y is None) or (y is not None and abs(x - y) > 1e-6 * max(1, abs(y))):
                        raise AssertionError('Trade engine totals diverged from the deterministic forecast')
        replay = self.season({})
        if replay['records'] != self.base_record or replay['seeds'] != self.base_seed:
            raise AssertionError('Trade standings replay diverged from the deterministic forecast')
        self.base_season = replay
        self.base_totals = {t: self.season_totals(self.base_raw[t]['raw']) for t in self.teams}
        self.base_strength = relative_strength(self.base_totals)

    def register(self, pid):
        """Make an unowned free agent available to the day engine as a bench add."""
        eng = self.engine
        if pid in eng.players:
            return
        sp = self.site[pid]
        proj = resolve_projection(self.inputs['projections'][pid], self.weight)
        eng.players[pid] = RosterPlayer('FA', pid, sp.name, 'BN', tuple(sp.eligible_positions), sp.nba_team)
        eng.projections[pid] = proj
        v = proj['values']
        eng.vec[pid] = tuple(v['gp'] / 82 * v[k] for k in VOLUMES)
        eng.score[pid] = max(1e-6, lineup_score(v))
        eng.slots[pid] = tuple(j for j, s in enumerate(SLOTS) if eligible(s, sp.eligible_positions))

    def team_weeks(self, team, gives=(), receives=(), drops=(), adds=(), il=()):
        """Weekly totals with `gives` (and balancing drops / IL moves) replaced by `receives`
        (and free-agent `adds`) from the effective day on. Strings or tuples of site IDs."""
        gives, receives, drops, adds, il = (_ids(x) for x in (gives, receives, drops, adds, il))
        key = (team, gives, receives, drops, adds, il)
        if key in self.cache:
            return self.cache[key]
        before = frozenset(p.site_player_id for p in self.inputs['rosters'][team])
        after = (before - set(gives) - set(drops) - set(il)) | set(receives) | set(adds)
        raws, starts, after_starts = [], Counter(), []
        for wi, (w, days) in enumerate(zip(self.weeks, self.week_days)):
            total, late = [0.0] * len(VOLUMES), Counter()
            for day in days:
                volumes, starters = self.engine.day(day, after if day >= self.eff_day else before, {}, {})
                for i, v in enumerate(volumes):
                    total[i] += v
                for p in starters:
                    starts[p] += 1
                    if day >= self.eff_day:
                        late[p] += 1
            raw = period_totals(total)
            if wi == 0 and self.actual.get(team):
                raw = with_actual_volumes(raw, self.actual[team])
            raws.append(raw)
            after_starts.append(late)
        result = {'raw': raws, 'starts': starts, 'after_starts': after_starts}
        self.cache[key] = result
        return result

    def season_totals(self, raws):
        v = [0.0] * len(VOLUMES)
        for raw in raws:
            for i, x in enumerate(_vector(raw)):
                v[i] += x
        return period_totals(v)

    def opponent(self, team, week):
        return next(b if a == team else a for a, b in self.pairings[week] if team in (a, b))

    def rows(self, team, changed):
        key = (team, tuple(sorted((t, id(r)) for t, r in changed.items())))
        if not changed and key in self.row_cache:
            return self.row_cache[key]
        out = []
        for wi, w in enumerate(self.weeks):
            opp = self.opponent(team, w.number)
            mine = (changed.get(team) or self.base_raw[team]['raw'])[wi]['categories']
            theirs = (changed.get(opp) or self.base_raw[opp]['raw'])[wi]['categories']
            wins, losses, ties, outcomes = category_result(mine, theirs)
            out.append({'week': w.number, 'opponent': opp, 'score': [wins, losses, ties],
                        'points': wins + .5 * ties, 'outcomes': outcomes, 'theirs': theirs,
                        'margins': {c: category_margin(mine, theirs, c) for c in CATEGORIES}})
        if not changed:
            self.row_cache[key] = out
        return out

    def season(self, changed):
        """Full-league records and seeds; only matchups involving changed teams are recomputed."""
        hist = {w: dict(v) for w, v in self.history.items()}
        for t in changed:
            for row in self.rows(t, changed):
                s = row['score']
                hist[row['week']][t], hist[row['week']][row['opponent']] = s, [s[1], s[0], s[2]]
        weeks = sorted(hist)
        records = {t: [sum(hist[w][t][i] for w in weeks) for i in range(3)] for t in self.teams}
        if any(sum(r) != 162 for r in records.values()) or \
                sum(r[0] for r in records.values()) != sum(r[1] for r in records.values()):
            raise ValueError('Season category decisions are incomplete or nonreciprocal')
        ranked = seed_order(records, [hist[w] for w in weeks])
        return {'records': records, 'seeds': {t: i for i, t in enumerate(ranked, 1)}}


def _raw_value(v, league):
    """Per-game category contributions; shooting is makes above league average at the player's volume."""
    return {'fg_pct': v['fgm'] - league[0] * v['fga'], 'ft_pct': v['ftm'] - league[1] * v['fta'],
            **{c: -v[c] if c == 'turnovers' else v[c] for c in COUNTING}}


def asset_values(ctx, statuses) -> dict:
    """Rest-of-season value over replacement, independent of any one roster.

    z-scores are taken over rostered non-IL players; replacement level is the best
    projectable, schedulable, active free agent at the player's scarcest position.
    Value = (summed per-game z x GP/82 - replacement) x team games after the effective day.
    """
    pool = [pid for pid, p in ctx.players.items() if p.assigned_slot != 'IL']
    vals = {pid: ctx.proj[pid]['values'] for pid in ctx.players}
    fga, fta = sum(vals[p]['fga'] for p in pool), sum(vals[p]['fta'] for p in pool)
    league = (sum(vals[p]['fgm'] for p in pool) / fga if fga else 0, sum(vals[p]['ftm'] for p in pool) / fta if fta else 0)
    raw = {pid: _raw_value(v, league) for pid, v in vals.items()}
    mean = {c: sum(raw[p][c] for p in pool) / len(pool) for c in CATEGORIES}
    sd = {c: sqrt(sum((raw[p][c] - mean[c]) ** 2 for p in pool) / len(pool)) for c in CATEGORIES}

    def zed(r):
        return {c: (r[c] - mean[c]) / sd[c] if sd[c] else 0.0 for c in CATEGORIES}
    schedule = ctx.inputs['schedule']
    replacement = {pos: None for pos in BASE_POSITIONS}
    by_id = {p.site_player_id: p for p in ctx.inputs['site_players']}
    for row in pool_audit(ctx.inputs, ctx.weight)['rows']:
        pid = row['site_player_id']
        if row['owner_team_id'] or row['availability'] not in ('FA', 'W') or not row['projectable'] or \
                by_id[pid].nba_team not in schedule.team_counts or statuses.get(pid) in INACTIVE_STATUSES:
            continue
        try:
            v = resolve_projection(ctx.inputs['projections'][pid], ctx.weight)['values']
        except (ValueError, KeyError):
            continue
        per = sum(zed(_raw_value(v, league)).values()) * v['gp'] / 82
        for pos in by_id[pid].eligible_positions:
            if pos in replacement and (replacement[pos] is None or per > replacement[pos][0]):
                replacement[pos] = (per, row['name'])
    floor = min(sum(zed(raw[p]).values()) * vals[p]['gp'] / 82 for p in pool)
    out = {}
    for pid, p in ctx.players.items():
        z = zed(raw[pid])
        per = sum(z.values()) * vals[pid]['gp'] / 82
        options = [(replacement[pos][0] if replacement.get(pos) else floor, pos,
                    replacement[pos][1] if replacement.get(pos) else None)
                   for pos in p.eligible_positions if pos in BASE_POSITIONS] or [(floor, None, None)]
        level, pos, name = min(options)
        games = ctx.team_games[p.nba_team]
        out[pid] = {'z': z, 'per_game_z': sum(z.values()), 'per_team_game': per, 'replacement': level,
                    'replacement_position': pos, 'replacement_player': name, 'team_games': games,
                    'value': (per - level) * games}
    return out


def _screen_rates(ctx, team):
    """Baseline lineup-start share after the effective day, per rostered player."""
    base = ctx.base_raw[team]
    started = sum(base['after_starts'], Counter())
    return {pid: (started[pid] / ctx.team_games[ctx.players[pid].nba_team]
                  if ctx.team_games[ctx.players[pid].nba_team] else 0.0)
            for pid in (p.site_player_id for p in ctx.inputs['rosters'][team])}


def screen(ctx, team, gives, receives, rates, week_games):
    """Cheap linear estimate of a package's rest-of-season category-win change. Ranking aid only.

    `gives` may include a balancing drop; each incoming player starts at the rate of the
    remaining teammate with the nearest lineup score."""
    eng = ctx.engine
    gives, receives = _ids(gives), _ids(receives)
    pool = [p for p in rates if p not in gives and ctx.players[p].assigned_slot != 'IL']
    rate = {}
    for r in receives:
        near = min(pool, key=lambda p: abs(eng.score[p] - eng.score[r]), default=None)
        rate[r] = min(1.0, rates[near]) if near else 1.0
    delta = 0.0
    base_rows = ctx.rows(team, {})
    for wi, w in enumerate(ctx.weeks):
        out = {g: ctx.base_raw[team]['after_starts'][wi][g] for g in gives}
        add = {r: rate[r] * week_games[wi].get(eng.players[r].nba_team, 0) for r in receives}
        if not any(out.values()) and not any(add.values()):
            continue
        v = _vector(ctx.base_raw[team]['raw'][wi])
        for i in range(len(VOLUMES)):
            v[i] += sum(a * eng.vec[r][i] for r, a in add.items()) - sum(s * eng.vec[g][i] for g, s in out.items())
        opp = ctx.base_raw[ctx.opponent(team, w.number)]['raw'][wi]['categories']
        wins, _, ties, _ = category_result(period_totals(v)['categories'], opp)
        delta += wins + .5 * ties - base_rows[wi]['points']
    return delta


def _narrow(cfg, cat, margin, theirs):
    if margin is None:
        return False
    if cat.endswith('_pct'):
        return abs(margin) < cfg.narrow_pct
    return abs(margin) < max(1.0, cfg.narrow_count * abs(theirs or 0))


def _side(ctx, cfg, team, changed, season, raws, gives, receives):
    gives, receives = _ids(gives), _ids(receives)
    base_rows, new_rows = ctx.rows(team, {}), ctx.rows(team, changed)
    cat_delta, flips, adjusted, fragile = Counter(), [], 0.0, 0
    value = {'W': 1.0, 'T': .5, 'L': 0.0}
    for b, n in zip(base_rows, new_rows):
        for c in CATEGORIES:
            d = value[n['outcomes'][c]] - value[b['outcomes'][c]]
            if not d:
                continue
            cat_delta[c] += d
            row = n if d > 0 else b
            narrow = _narrow(cfg, c, row['margins'][c], row['theirs'][c])
            fragile += narrow
            adjusted += d * (cfg.fragile_discount if narrow else 1)
            flips.append({'week': n['week'], 'opponent': n['opponent'], 'category': c,
                          'before': b['outcomes'][c], 'after': n['outcomes'][c], 'delta': d,
                          'margin_after': n['margins'][c], 'narrow': bool(narrow)})
    points = sum(r['points'] for r in new_rows) - sum(r['points'] for r in base_rows)
    near = [{'week': n['week'], 'opponent': n['opponent'], 'before': b['score'], 'after': n['score'],
             'delta': n['points'] - b['points']} for b, n in list(zip(base_rows, new_rows))[:3]]
    totals = ctx.season_totals(raws['raw'])
    others = {**ctx.base_totals, **{t: ctx.season_totals(changed[t]) for t in changed}}
    strength = relative_strength(others)[team]
    after, before_after = sum(raws['after_starts'], Counter()), sum(ctx.base_raw[team]['after_starts'], Counter())

    def games(ids):
        return sum(ctx.team_games[ctx.players[p].nba_team] for p in ids)
    return {'team_id': team, 'team_name': ctx.inputs['teams'][team], 'points_delta': points,
            'adjusted_delta': round(adjusted, 6), 'fragile_flips': fragile, 'category_delta': dict(cat_delta),
            'flips': sorted(flips, key=lambda f: (f['narrow'], -abs(f['delta']), f['week'])), 'near_term': near,
            'near_term_delta': sum(x['delta'] for x in near),
            'record_before': ctx.base_record[team], 'record_after': season['records'][team],
            'seed_before': ctx.base_seed[team], 'seed_after': season['seeds'][team],
            'win_pct_before': win_pct(ctx.base_record[team]), 'win_pct_after': win_pct(season['records'][team]),
            'lineup_starts_before': sum(ctx.base_raw[team]['starts'].values()),
            'lineup_starts_after': sum(raws['starts'].values()),
            'incoming_starts': sum(after[p] for p in receives), 'incoming_games': games(receives),
            'outgoing_starts': sum(before_after[p] for p in gives), 'outgoing_games': games(gives),
            'season_categories_after': totals['categories'],
            'strength_before': {c: ctx.base_strength[team][c]['rank'] for c in CATEGORIES},
            'strength_after': {c: strength[c]['rank'] for c in CATEGORIES}}


def evaluate(ctx, cfg, me, partner, gives, receives, mine_bal=None, their_bal=None):
    """Exact two-team replay of one package with each side's roster balancing
    ({'drops': ids, 'il': ids, 'adds': ids}); every other team keeps its forecast totals."""
    gives, receives = _ids(gives), _ids(receives)
    mb, tb = mine_bal or {}, their_bal or {}
    mine = ctx.team_weeks(me, gives, receives, mb.get('drops'), mb.get('adds'), mb.get('il'))
    theirs = ctx.team_weeks(partner, receives, gives, tb.get('drops'), tb.get('adds'), tb.get('il'))
    changed = {me: mine['raw'], partner: theirs['raw']}
    season = ctx.season(changed)
    return {'me': _side(ctx, cfg, me, changed, season, mine, gives, receives),
            'partner': _side(ctx, cfg, partner, changed, season, theirs, receives, gives),
            'seeds_after': season['seeds']}
