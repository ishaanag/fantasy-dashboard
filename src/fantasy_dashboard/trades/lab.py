"""Trade Lab: read-only 1-for-1, 2-for-1, 1-for-2 and 2-for-2 trade evaluation for both managers.

Multi-player packages are balanced explicitly: the side receiving an extra player must
drop one (or move an IL-eligible player to IL); the side left with an open spot is
evaluated with it open, and with the best current free agent as a conditional,
acquisition-consuming fill.

Both rosters are replayed through the advisor's day engine (legal daily slots, GP/82
availability, summed shooting volumes, known locks) from an assumed effective date,
against every scheduled opponent, and the league standings are re-ranked with the
deterministic forecast's rules. Market value (how other managers see players: Yahoo
preseason ADP and FantasyPros consensus ranks) is kept apart from that competitive
benefit and from the z-score model value. Nothing is sent to Yahoo and no acceptance
probability is produced.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from itertools import combinations
from time import perf_counter

from fantasy_dashboard.league import BASE_POSITIONS, CATEGORIES, EASTERN, INACTIVE_STATUSES, LABELS, iso_utc, parse_utc
from fantasy_dashboard.model.forecast import forecast, win_pct
from fantasy_dashboard.model.lineups import lineup_score, resolve_projection
from fantasy_dashboard.parallel import parallel_map
from fantasy_dashboard.trades.cards import _describe, _names, _player
from fantasy_dashboard.trades.config import (
    ACTIVE_LIMIT,
    IL_LIMIT,
    IL_MOVE_STATUSES,
    MARKET_FLOOR,
    PACKAGES,
    SOURCE_WEIGHTS,
    UTC,
    VALUE_FLOOR,
    TradeConfig,
    _ids,
    trade_rules,
)
from fantasy_dashboard.trades.market import (
    MARKET_SIGNALS,
    fairness_label,
    market_summary,
    market_values,
    side_value,
    value_gap,
)
from fantasy_dashboard.trades.ranking import _classify, _effective, _market_cost, _pareto, _robust, _score, market_label
from fantasy_dashboard.trades.replay import _Context, _screen_rates, _side, asset_values, evaluate, screen
from fantasy_dashboard.yahoo.evidence import pool_audit


def verification(inputs, evidence, yahoo_status, now, max_age_minutes=15) -> dict:
    """Trades are never executable here; this only says how current the roster evidence is."""
    gaps = []
    if (yahoo_status or {}).get('status') != 'verified':
        gaps.append('yahoo_snapshot_not_verified')
    stamp = (evidence or {}).get('captured_at_utc')
    age = None
    if not stamp:
        gaps.append('transaction_evidence_missing')
    else:
        try:
            age = (now - parse_utc(stamp)).total_seconds() / 60
            if age < 0 or age > max_age_minutes:
                gaps.append('snapshot_not_fresh')
        except (TypeError, ValueError):
            gaps.append('transaction_evidence_time_invalid')
    if stamp and stamp != inputs['availability']['captured_at_utc']:
        gaps.append('evidence_not_bound_to_roster_capture')
    return {'basis': 'current_snapshot' if not gaps else 'provisional_offline',
            'roster_captured_at_utc': inputs['availability']['captured_at_utc'],
            'evidence_age_minutes': None if age is None else round(age, 1),
            'eligibility_class': 'conditional_future', 'executable': False,
            'data_gaps': gaps,
            'always_unverified': ['partner_acceptance', 'commissioner_review', 'effective_date',
                                  'pending_trades_and_trade_blocks', 'keeper_implications',
                                  'injury_return_dates']}


def _acquisition_budget(inputs, evidence, eff_day):
    """Adds left in the scoring period of the effective day. Trades themselves use no acquisitions."""
    week = next((w for w in inputs['weeks'][:18] if w.start <= eff_day <= w.end), None)
    if week is None:
        return None, 0, ['effective_day_outside_regular_season']
    ev = evidence or {}
    used, ev_week = ev.get('adds_used'), ev.get('week')
    if isinstance(used, int) and ev_week == week.number:
        return week.number, max(0, week.max_adds - used), []
    if isinstance(used, int) and isinstance(ev_week, int) and ev_week < week.number:
        return week.number, week.max_adds, []
    return week.number, week.max_adds, ['acquisition_count_unverified']


def free_agent_fills(ctx, statuses, limit):
    """Currently listed free agents (never waiver players) for an open spot: the best by projected
    rest-of-season lineup value, plus the best at each base position."""
    rows, waivers = [], 0
    for row in pool_audit(ctx.inputs, ctx.weight)['rows']:
        pid = row['site_player_id']
        if row['owner_team_id']:
            continue
        if row['availability'] == 'W':
            waivers += 1
            continue
        sp = ctx.site.get(pid)
        if row['availability'] != 'FA' or not row['projectable'] or statuses.get(pid) in INACTIVE_STATUSES or \
                sp is None or not ctx.team_games[sp.nba_team]:
            continue
        try:
            v = resolve_projection(ctx.inputs['projections'][pid], ctx.weight)['values']
        except (ValueError, KeyError):
            continue
        rows.append((max(0.0, lineup_score(v)) * v['gp'] / 82 * ctx.team_games[sp.nba_team], pid))
    rows.sort(key=lambda r: (-r[0], r[1]))
    chosen = [pid for _, pid in rows[:limit]]
    for pos in BASE_POSITIONS if limit else ():
        best = next((pid for _, pid in rows if pos in ctx.site[pid].eligible_positions), None)
        if best and best not in chosen:
            chosen.append(best)
    return chosen, waivers


def _bal(b):
    if b['kind'] in ('drop', 'il'):
        return {'drops' if b['kind'] == 'drop' else 'il': (b['player']['site_player_id'],)}
    f = b.get('fill')
    return {'adds': (f['player']['site_player_id'],)} if f and f['used'] else {}


def _similar(a, b, values):
    """Same partner, and one side identical while the other differs only by near-replacement players."""
    if a['partner']['team_id'] != b['partner']['team_id']:
        return False
    ga, gb = {p['site_player_id'] for p in a['gives_all']}, {p['site_player_id'] for p in b['gives_all']}
    ra, rb = {p['site_player_id'] for p in a['receives_all']}, {p['site_player_id'] for p in b['receives_all']}
    for same, x, y in ((ra == rb, ga, gb), (ga == gb, ra, rb)):
        if same and x != y and all(values[p]['value'] <= VALUE_FLOOR for p in x ^ y):
            return True
    return False


def trade_lab(inputs: dict, *, team_id=None, weight: float = .5, as_of=None, actual_state=None,
              evidence=None, yahoo_status=None, config: TradeConfig = TradeConfig(), now=None,
              base=None, source_bases=None) -> dict:
    """Rank realistic 1-for-1, 2-for-1 and 1-for-2 trades for my team against HOLD. Read-only.

    Staged search: (1) every unique package with hard roster feasibility, (2) a cheap asset-value
    and linear lineup screen with per-type and per-partner quotas, (3) exact two-team replay with
    the best legal drop among the cheapest few, (4) cached team-week totals, distinct-card and
    equivalent-package pruning, (5) finalist balancing detail, 1-for-1 comparison and
    single-source rechecks. `base` / `source_bases` may be the dashboard's cached forecasts.
    """
    started = perf_counter()
    cfg = config
    team_id = team_id or inputs['league']['my_team_id']
    if team_id not in inputs['teams']:
        raise ValueError('Unknown team')
    as_of = as_of or parse_utc(inputs['availability']['captured_at_utc'])
    now = now or datetime.now(UTC)
    if as_of.tzinfo is None or now.tzinfo is None:
        raise ValueError('As-of and current times must include a timezone')
    as_of, now = as_of.astimezone(UTC), now.astimezone(UTC)
    rules = trade_rules(inputs)
    today = as_of.astimezone(EASTERN).date()
    if today > rules['deadline']:
        raise ValueError(f"The trade deadline ({rules['deadline'].isoformat()}) has passed")
    eff_day = today + timedelta(days=cfg.acceptance_days + rules['reject_days'])
    stamp = iso_utc(as_of)
    if not (base and base.get('hashtag_weight') == weight and base.get('as_of_utc') == stamp):
        base = forecast(inputs, hashtag_weight=weight, as_of=as_of, actual_state=actual_state)
    if base['actual_completed_weeks'] >= 18 or eff_day > inputs['weeks'][17].end:
        raise ValueError('No regular-season games remain after the earliest trade effective date')
    ctx = _Context(inputs, base, weight, as_of, actual_state, eff_day)
    partners = tuple(config.partner_ids or [t for t in ctx.teams if t != team_id])
    if any(p not in inputs['teams'] or p == team_id for p in partners):
        raise ValueError('Choose trade partners from the other league teams')
    mine_ids = {p.site_player_id for p in inputs['rosters'][team_id]}
    if not set(cfg.protected_ids) <= mine_ids:
        raise ValueError('Protected players must be on your roster')
    ev = evidence or {}
    statuses = ev.get('status_by_site_id') or inputs.get('injury_statuses', {})
    values = asset_values(ctx, statuses)
    market = market_values(inputs, ctx, as_of)

    def mval(ids, signal=None):
        """Market value of one side: the best player in full, each further player at depth_discount (the side that
        gets the best player in a deal is the one managers see as winning it)."""
        vals = sorted(((market[p]['signals'].get(signal) or market[p])['value'] if signal else market[p]['value']
                       for p in _ids(ids)), reverse=True)
        return sum(v * (1 if i == 0 else cfg.depth_discount) for i, v in enumerate(vals))

    def market_ok(c):
        """The partner would not lose more than viable_market_limit by market value. Paying the partner a
        market premium never removes a package."""
        return c['market_gap'] <= cfg.viable_market_limit

    def viable(c):
        """Replayed: market_ok, and either within viable_model_floor of roster-neutral model value or (past it)
        a linear-screen gain of at least floor_screen_gate for you. The model value alone never decides."""
        return market_ok(c) and (cfg.viable_model_floor is None or c['gap'] >= -cfg.viable_model_floor or
                                 (cfg.floor_screen_gate is not None and c['screen_me'] >= cfg.floor_screen_gate))
    skipped = Counter()
    protected = frozenset(cfg.protected_ids)

    def tradeable(team, protect=frozenset()):
        out = []
        for p in inputs['rosters'][team]:
            if p.site_player_id in protect:
                skipped['protected'] += 1
            elif p.assigned_slot == 'IL' and not cfg.include_injured_reserve:
                skipped['injured_reserve_slot'] += 1
            else:
                out.append(p.site_player_id)
        return out
    mine = tradeable(team_id, protected)
    theirs_by = {p: tradeable(p) for p in partners}
    week_games = []
    for days in ctx.week_days:
        later = {d for d in days if d >= eff_day}
        week_games.append(Counter(nba for (nba, day), g in inputs['schedule'].team_dates.items()
                                  if day in later and g.tipoff_utc > as_of))
    rates = {t: _screen_rates(ctx, t) for t in (team_id, *partners)}

    # Roster-balancing inputs. Yahoo's league-wide Can't Cut list limits drops (not trades).
    cant_cut_ids = ev.get('league_cant_cut_site_ids')
    cant_cut = frozenset(cant_cut_ids or ())
    il_rule = frozenset(ev.get('user_supplied_il_eligible_statuses') or IL_MOVE_STATUSES)
    rosters = {t: inputs['rosters'][t] for t in ctx.teams}
    active = {t: sum(p.assigned_slot != 'IL' for p in rosters[t]) for t in ctx.teams}
    il_used = {t: sum(p.assigned_slot == 'IL' for p in rosters[t]) for t in ctx.teams}
    lineup_value = {}
    for t in (team_id, *partners):
        started_ = sum(ctx.base_raw[t]['after_starts'], Counter())
        lineup_value[t] = {p.site_player_id: started_[p.site_player_id] * ctx.engine.score[p.site_player_id]
                           for p in rosters[t]}
    budget_week, budget, budget_gaps = _acquisition_budget(inputs, evidence, eff_day)

    drop_cache = {}

    def drop_options(team, out):
        """Legal ways to clear one active spot, cheapest projected lineup loss first (IL move before drop)."""
        key = (team, frozenset(out))
        if key in drop_cache:
            return drop_cache[key]
        opts = drop_cache[key] = []
        for p in rosters[team]:
            pid = p.site_player_id
            if pid in out or p.assigned_slot == 'IL' or (team == team_id and pid in protected):
                continue
            if statuses.get(pid) in il_rule and il_used[team] < IL_LIMIT:
                opts.append(('il', pid, lineup_value[team][pid]))
            if pid not in cant_cut:
                opts.append(('drop', pid, lineup_value[team][pid]))
        opts.sort(key=lambda o: (o[2], o[0] != 'il', o[1]))
        return opts

    def lost(o):
        return max(0.0, values[o[1]]['value']) if o and o[0] == 'drop' else 0.0

    # Free agents (never waiver players) that could fill a spot a 2-for-1 or 1-for-2 leaves open.
    multi = any(PACKAGES[k][0] != PACKAGES[k][1] for k in cfg.package_types)
    fills, waiver_players = free_agent_fills(ctx, statuses, cfg.fill_candidates) if multi else ([], 0)
    for pid in fills:
        ctx.register(pid)

    waiver_cache = {}

    def waiver_alternative(team, fa):
        """The same free agent added without any trade, dropping the team's cheapest legal player."""
        if (team, fa) not in waiver_cache:
            opts = [o for o in drop_options(team, set()) if o[0] == 'drop']
            if not opts:
                waiver_cache[team, fa] = {'adjusted': 0.0, 'points': 0.0, 'drop': None}
            else:
                raws = ctx.team_weeks(team, (), (), (opts[0][1],), (fa,))
                changed = {team: raws['raw']}
                s_ = _side(ctx, cfg, team, changed, ctx.season(changed), raws, (), ())
                waiver_cache[team, fa] = {'adjusted': s_['adjusted_delta'], 'points': s_['points_delta'],
                                          'drop': ctx.players[opts[0][1]].name}
        return waiver_cache[team, fa]

    def fill_open_side(side, team, partner, gives, gets, r, mb, tb):
        """Best free-agent fill for an open spot. Used in the result only when it helps and an acquisition is
        available; the open-spot result is kept beside it."""
        tried = []
        for fa in fills:
            add = {'adds': (fa,)}
            rr = evaluate(ctx, cfg, team_id, partner, gives, gets, {**mb, **add} if side == 'me' else mb,
                          {**tb, **add} if side == 'partner' else tb)
            opp = waiver_alternative(team, fa)
            tried.append((rr[side]['adjusted_delta'] - max(0.0, opp['adjusted']), rr[side]['points_delta'], fa, rr, opp))
        if not tried:
            return r, None
        tried.sort(key=lambda x: (-x[0], -x[1], x[2]))
        net, pts, fa, rr, opp = tried[0]
        adj = rr[side]['adjusted_delta']
        net2, pts2, fa2, _, _ = tried[1] if len(tried) > 1 else (r[side]['adjusted_delta'], r[side]['points_delta'], None, r, None)
        feasible = budget > 0 if side == 'me' else True
        gaps = ['free_agent_status_is_a_dated_observation'] + (
            (budget_gaps + ([] if feasible else ['no_acquisitions_left_in_effective_week'])) if side == 'me'
            else ['partner_acquisitions_unverified'])
        used = feasible and net > r[side]['adjusted_delta']
        sp = ctx.site[fa]
        fill = {'player': {'site_player_id': fa, 'name': sp.name, 'nba_team': sp.nba_team,
                           'positions': list(sp.eligible_positions), 'yahoo_status': statuses.get(fa)},
                'used': used, 'feasible': feasible, 'consumes_acquisition': True, 'week': budget_week,
                'acquisitions_left': budget if side == 'me' else None, 'gaps': gaps,
                'open_points': r[side]['points_delta'], 'open_adjusted': r[side]['adjusted_delta'],
                'points_gain': pts - r[side]['points_delta'], 'adjusted_gain': adj - r[side]['adjusted_delta'],
                'opportunity': opp, 'net_adjusted': net,
                'fallback': {'name': ctx.site[fa2].name if fa2 else None, 'net_adjusted': net2,
                             'points_gain': pts2 - r[side]['points_delta']}}
        return (rr if used else r), fill

    # 1-2. Unique packages, hard feasibility, cheap screen.
    t0 = perf_counter()
    candidates, stage = [], {k: Counter() for k in cfg.package_types}
    for partner in partners:
        for typ in cfg.package_types:
            n_give, n_get = PACKAGES[typ]
            for gives in combinations(sorted(mine), n_give):
                for gets in combinations(sorted(theirs_by[partner]), n_get):
                    stage[typ]['generated'] += 1
                    plan, reason = {}, None
                    for side, team, out, inc in (('me', team_id, gives, gets), ('partner', partner, gets, gives)):
                        need = active[team] - len(out) + len(inc) - ACTIVE_LIMIT
                        plan[side] = {'need': need, 'options': drop_options(team, set(out)) if need > 0 else []}
                        if need > 1:
                            reason = 'needs_more_than_one_drop'
                        elif need == 1 and not plan[side]['options']:
                            reason = 'no_legal_drop'
                    if reason:
                        stage[typ]['infeasible_' + reason] += 1
                        continue
                    md, pd = (plan[s]['options'][0] if plan[s]['options'] else None for s in ('me', 'partner'))
                    mf, pf = ((fills[0],) if fills and plan[s]['need'] < 0 else () for s in ('me', 'partner'))
                    give_v, get_v = side_value(values, gives), side_value(values, gets)
                    stage[typ]['feasible'] += 1
                    cand = {'type': typ, 'partner': partner, 'give': gives, 'get': gets, 'plan': plan,
                            'gap': value_gap(get_v - lost(md), give_v - lost(pd)),
                            'market_gap': value_gap(mval(gets), mval(gives) - (mval(pd[1]) if pd and pd[0] == 'drop'
                                                                                 else 0), MARKET_FLOOR),
                            '_screen': (md, pd, mf, pf)}
                    # The lineup screen is the costly part, so it runs only for packages that could qualify
                    # (every package in exhaustive mode), in parallel.
                    if cfg.shortlist is None or market_ok(cand):
                        candidates.append(cand)

    def screen_one(c):
        md, pd, mf, pf = c['_screen']
        return (screen(ctx, team_id, c['give'] + ((md[1],) if md else ()), c['get'] + mf, rates[team_id], week_games),
                screen(ctx, c['partner'], c['get'] + ((pd[1],) if pd else ()), c['give'] + pf, rates[c['partner']],
                       week_games))
    for c, (sm, sp) in zip(candidates, parallel_map(screen_one, candidates, cfg.workers)):
        c['screen_me'], c['screen_partner'] = sm, sp
        del c['_screen']
    screen_seconds = perf_counter() - t0

    def screen_key(c):
        return (-(c['screen_me'] + .5 * max(-2, min(c['screen_partner'], 3))), c['partner'], c['give'], c['get'])
    # The screen is linear, so it keeps near-misses; quotas keep every package type and partner represented.
    # Viable = could still qualify: the partner would not see it as lopsided by draft position, and you are not
    # giving up far more model value. No screen-score cut: every viable package is replayed by default.
    plausible = sorted((c for c in candidates if viable(c)), key=screen_key)
    for c in plausible:
        stage[c['type']]['plausible'] += 1
    if cfg.shortlist is None:
        shortlist = sorted(candidates, key=screen_key)
    elif cfg.replay_all_viable:
        shortlist = plausible
    else:
        shortlist = []
        for typ in cfg.package_types:
            group = [c for c in plausible if c['type'] == typ]
            chosen = group[:cfg.shortlist if typ == '1-1' else cfg.multi_shortlist]
            taken, per = {id(c) for c in chosen}, Counter(c['partner'] for c in chosen)
            for c in group:
                if per[c['partner']] < cfg.partner_floor and id(c) not in taken:
                    chosen.append(c)
                    taken.add(id(c))
                    per[c['partner']] += 1
            shortlist += chosen

    # 3. Exact two-team replay; the side that must clear a spot picks its best legal drop.
    t0 = perf_counter()
    base_after_me = sum(ctx.base_raw[team_id]['after_starts'], Counter())
    evaluated, drops_replayed = [], 0

    def replay_one(c):
        drops_replayed = 0
        partner, gives, gets, plan = c['partner'], c['give'], c['get'], c['plan']
        side = 'me' if plan['me']['options'] else 'partner' if plan['partner']['options'] else None
        tried, choice, opts = [], None, ()

        def choose_drop(other):
            """The clearing side keeps its best legal option, with the other side's balancing fixed."""
            nonlocal drops_replayed     # counted per package; summed by the caller
            best, rows = None, []
            for kind, pid, proxy in opts:
                bal = {'drops': (pid,)} if kind == 'drop' else {'il': (pid,)}
                r_ = evaluate(ctx, cfg, team_id, partner, gives, gets, bal if side == 'me' else other,
                              bal if side == 'partner' else other)
                drops_replayed += 1
                s = r_[side]
                rows.append({'kind': kind, 'site_player_id': pid, 'name': ctx.players[pid].name,
                             'points_delta': s['points_delta'], 'adjusted_delta': s['adjusted_delta']})
                k = (s['points_delta'], s['adjusted_delta'], kind == 'il', -proxy)
                if best is None or k > best[0]:
                    best = (k, r_, (kind, pid))
            return best[1], best[2], rows
        if side is None:
            r = evaluate(ctx, cfg, team_id, partner, gives, gets)
        else:
            opts = plan[side]['options'] if cfg.drop_options is None else plan[side]['options'][:cfg.drop_options]
            r, choice, tried = choose_drop(None)
        give_v, get_v = side_value(values, gives), side_value(values, gets)
        balancing = {}
        for s_ in ('me', 'partner'):
            need = plan[s_]['need']
            balancing[s_] = {'kind': 'open' if need < 0 else 'none', 'open_spots': max(0, -need), 'player': None,
                             'value_lost': 0.0, 'options': []}
        if choice:
            kind, pid = choice
            balancing[side].update({'kind': kind, 'player': _player(ctx, pid, values, statuses, market), 'options': tried,
                                    'value_lost': max(0.0, values[pid]['value']) if kind == 'drop' else 0.0})
        for s_ in ('me', 'partner'):
            if balancing[s_]['kind'] == 'open' and fills:
                team_ = team_id if s_ == 'me' else partner
                r, balancing[s_]['fill'] = fill_open_side(s_, team_, partner, gives, gets, r,
                                                          _bal(balancing['me']), _bal(balancing['partner']))
                if choice and balancing[s_]['fill']['used']:
                    # Re-choose the drop with the fill in place (a head-to-head meeting can change its value).
                    r2, choice2, tried = choose_drop(_bal(balancing[s_]))
                    if choice2 == choice:
                        r = r2
                    else:
                        choice = choice2
                        kind, pid = choice
                        balancing[side].update({'kind': kind, 'player': _player(ctx, pid, values, statuses, market),
                                                'value_lost': max(0.0, values[pid]['value']) if kind == 'drop' else 0.0})
                        r_open = evaluate(ctx, cfg, team_id, partner, gives, gets, _bal(balancing['me']) if s_ != 'me'
                                          else None, _bal(balancing['partner']) if s_ != 'partner' else None)
                        r, balancing[s_]['fill'] = fill_open_side(s_, team_, partner, gives, gets, r_open,
                                                                  _bal(balancing['me']) if s_ != 'me' else {},
                                                                  _bal(balancing['partner']) if s_ != 'partner' else {})
                    balancing[side]['options'] = tried
        if choice:      # the chosen option shows the final result
            for o in balancing[side]['options']:
                if o['site_player_id'] == choice[1] and o['kind'] == choice[0]:
                    o['points_delta'], o['adjusted_delta'] = r[side]['points_delta'], r[side]['adjusted_delta']
        t = r
        my_drop, their_drop = balancing['me']['value_lost'], balancing['partner']['value_lost']
        model_gap = value_gap(get_v - my_drop, give_v - their_drop)
        cut = {s_: (balancing[s_]['player']['site_player_id'] if balancing[s_]['kind'] == 'drop' else None)
               for s_ in ('me', 'partner')}
        m_their_drop, m_my_cut = mval(cut['partner'] or ()), mval(cut['me'] or ())
        m_get, m_give = mval(gets), mval(gives)
        gap = value_gap(m_get, m_give - m_their_drop, MARKET_FLOOR)
        by_signal = {k: value_gap(mval(gets, k), mval(gives, k) - mval(cut['partner'] or (), k), MARKET_FLOOR)
                     for k in MARKET_SIGNALS if all(k in market[x]['signals'] for x in (*gives, *gets))}
        gives_all = sorted((_player(ctx, g, values, statuses, market) for g in gives), key=lambda p: -p['value']['value'])
        gets_all = sorted((_player(ctx, g, values, statuses, market) for g in gets), key=lambda p: -p['value']['value'])
        top_in = max(values[g]['value'] for g in gets)
        low = []
        for g in gives:
            games = ctx.team_games[ctx.players[g].nba_team]
            if games and values[g]['value'] >= top_in and base_after_me[g] / games < cfg.low_usage_share:
                low.append({'name': ctx.players[g].name, 'starts': base_after_me[g], 'games': games,
                            'value': values[g]['value']})
        t.update({'id': '+'.join(gives) + '-' + '+'.join(gets), 'package': c['type'],
                  'gives': gives_all[0], 'gives_all': gives_all, 'receives': gets_all[0], 'receives_all': gets_all,
                  'screen': {'me': c['screen_me'], 'partner': c['screen_partner']},
                  'value': {'give': give_v, 'get': get_v, 'my_drop': my_drop, 'partner_drop': their_drop,
                            'exchanged_gap': value_gap(get_v, give_v), 'model_gap': model_gap,
                            'model_label': fairness_label(model_gap, get_v - my_drop, give_v - their_drop, cfg),
                            'gap': gap, 'label': fairness_label(gap, m_get, m_give - m_their_drop, cfg, MARKET_FLOOR),
                            'basis': next(iter(market.values()))['basis'] if market else None,
                            'market': {'get': m_get, 'give': m_give, 'partner_drop': m_their_drop,
                                       'partner_cut': m_their_drop, 'my_cut': m_my_cut,
                                       'partner_surplus': m_give - m_get - m_their_drop,
                                       'my_cost': m_give + m_my_cut - m_get, 'by_signal': by_signal,
                                       'signals_disagree': len(by_signal) > 1 and
                                       max(by_signal.values()) - min(by_signal.values()) > .25,
                                       'get_ranks': [market[x]['rank'] for x in gets],
                                       'give_ranks': [market[x]['rank'] for x in gives]}},
                  'balancing': balancing, 'low_usage': low})
        t['score'] = _score(t, cfg)
        t['effective'] = {s_: _effective(t, s_, cfg) for s_ in ('me', 'partner')}
        t['_drop_replays'] = drops_replayed
        return t

    for t in parallel_map(replay_one, shortlist, cfg.workers):
        stage[t['package']]['replayed'] += 1
        for s_ in ('me', 'partner'):
            if (t['balancing'][s_].get('fill') or {}).get('used'):
                stage[t['package']]['fill_used_' + s_] += 1
        drops_replayed += t.pop('_drop_replays')
        evaluated.append(t)
    replay_seconds = perf_counter() - t0
    evaluated.sort(key=lambda t: (-t['score'], t['id']))

    # 5. Finalists: inferior variations removed (Pareto), a few places for every package type among realistic
    # packages, and a few for difficult sells (the partner loses market value).
    t0 = perf_counter()
    open_, dominated = _pareto([t for t in evaluated if _classify(t, cfg)[0] != 'excluded'], cfg)
    realistic = [t for t in open_ if t['value']['gap'] <= cfg.fair_band]
    room = cfg.finalists
    finalists = [t for t in realistic if -t['value']['gap'] > cfg.fair_band][:min(room, cfg.premium_finalists)]
    taken = {t['id'] for t in finalists}
    for typ in (cfg.package_types if room else ()):
        have = sum(t['package'] == typ for t in finalists)
        extra = [t for t in realistic if t['package'] == typ and t['id'] not in taken][:max(0, cfg.type_reserve - have)]
        finalists += extra
        taken |= {t['id'] for t in extra}
    rest = [t for t in realistic if t['id'] not in taken][:max(0, room - len(finalists))]
    finalists += rest
    taken |= {t['id'] for t in rest}
    if cfg.finalists:       # enough source-checked packages to always show result_limit cards
        backfill = [t for t in evaluated if t['id'] not in taken][:max(0, 2 * cfg.result_limit - len(finalists))]
        finalists += backfill
    for t in finalists:
        stage[t['package']]['finalists'] += 1

    # Balancing detail: exchange-only value, drop cost, conditional free-agent fills, 1-for-1 subsets.
    for t in finalists:
        partner = t['partner']['team_id']
        gives = tuple(p['site_player_id'] for p in t['gives_all'])
        gets = tuple(p['site_player_id'] for p in t['receives_all'])
        mb, tb = _bal(t['balancing']['me']), _bal(t['balancing']['partner'])
        for side in ('me', 'partner'):
            b = t['balancing'][side]
            if b['kind'] in ('drop', 'il'):
                nd = evaluate(ctx, cfg, team_id, partner, gives, gets, None if side == 'me' else mb,
                              None if side == 'partner' else tb)
                b['exchange_points'] = nd[side]['points_delta']
                b['cost'] = nd[side]['points_delta'] - t[side]['points_delta']
        fill = t['balancing']['me'].get('fill')
        t['streaming_slot'] = bool(fill and fill['used'] and fill['points_gain'] >= .5)
        t['simpler_alternative'] = None
        if t['package'] != '1-1':
            cost = t['value']['give'] + t['value']['my_drop'] - t['value']['get']
            subsets = []
            for g in gives:
                for r_id in gets:
                    rr = evaluate(ctx, cfg, team_id, partner, g, r_id)
                    subsets.append({'id': f'{g}-{r_id}', 'title': f"{ctx.players[g].name} for {ctx.players[r_id].name}",
                                    'me_adjusted': rr['me']['adjusted_delta'],
                                    'partner_adjusted': rr['partner']['adjusted_delta'],
                                    'asset_cost': values[g]['value'] - values[r_id]['value']})
            subsets.sort(key=lambda a: (-a['me_adjusted'], a['asset_cost']))
            t['one_for_one_subsets'], t['asset_cost'] = subsets, cost
            simpler = [a for a in subsets if a['me_adjusted'] >= _effective(t, 'me', cfg) - cfg.simpler_band
                       and a['asset_cost'] <= cost and a['partner_adjusted'] >= min(0.0, t['partner']['adjusted_delta'])]
            t['simpler_alternative'] = simpler[0] if simpler else None
    balance_seconds = perf_counter() - t0

    # Projection-source sensitivity with the same package and balancing.
    t0 = perf_counter()
    contexts = {}
    for key, label, w in (SOURCE_WEIGHTS if finalists else ()):
        try:
            b = (source_bases or {}).get(w)
            if not (b and b.get('as_of_utc') == stamp and b.get('hashtag_weight') == w):
                b = forecast(inputs, hashtag_weight=w, as_of=as_of, actual_state=actual_state)
            sctx = _Context(inputs, b, w, as_of, actual_state, eff_day)
            for pid in fills:
                try:
                    sctx.register(pid)
                except (ValueError, KeyError):
                    pass      # a fill without this source's projection makes that check unavailable
            contexts[key] = (label, sctx, asset_values(sctx, statuses))
        except (ValueError, KeyError) as exc:
            contexts[key] = (label, None, str(exc))
    for t in finalists:
        t['sources'] = {}
        gives = tuple(p['site_player_id'] for p in t['gives_all'])
        gets = tuple(p['site_player_id'] for p in t['receives_all'])
        mb, tb = _bal(t['balancing']['me']), _bal(t['balancing']['partner'])
        for key, (label, sctx, sval) in contexts.items():
            if sctx is None:
                t['sources'][key] = {'label': label, 'unavailable': sval[:200]}
                continue
            try:
                if any(a not in sctx.engine.players for a in (*mb.get('adds', ()), *tb.get('adds', ()))):
                    raise ValueError('free-agent fill has no projection under this source')
                r = evaluate(sctx, cfg, team_id, t['partner']['team_id'], gives, gets, mb, tb)
            except (ValueError, KeyError) as exc:
                t['sources'][key] = {'label': label, 'unavailable': str(exc)[:200]}
                continue
            drop = lambda b: max(0.0, sval[b['player']['site_player_id']]['value']) if b['kind'] == 'drop' else 0.0
            g_v, r_v = side_value(sval, gives), side_value(sval, gets)
            t['sources'][key] = {'label': label, 'me_points': r['me']['points_delta'],
                                 'me_adjusted': r['me']['adjusted_delta'], 'partner_points': r['partner']['points_delta'],
                                 'value_gap': value_gap(r_v - drop(t['balancing']['me']),
                                                        g_v - drop(t['balancing']['partner']))}
        held = [s['me_points'] > 0 for s in t['sources'].values() if not s.get('unavailable')]
        t['robustness'] = ('Unchecked' if not held else 'Holds under both sources' if all(held) and len(held) == 2
                           else 'Holds under one source checked' if all(held)
                           else 'Fails under both sources' if not any(held) else 'Mixed across sources')
        t['score'] = _score(t, cfg)
        t['effective'] = {s_: _effective(t, s_, cfg) for s_ in ('me', 'partner')}
    source_seconds = perf_counter() - t0

    # Tiers, distinct cards (equivalent packages folded in), HOLD. Nothing is forced onto the page.
    excluded, final_ids = Counter(), {t['id'] for t in finalists}
    for t in evaluated:
        t['tier'], reason = _classify(t, cfg) if t['id'] in final_ids else ('unchecked', 'not a source-checked finalist')
        t['tier_reason'] = reason
        t['label'] = market_label(t, cfg) if t['tier'] == 'recommended' else None
        if reason:
            excluded[reason] += 1
    folded = 0

    def pick(tier, limit):
        nonlocal folded
        out, gives_seen, gets_seen = [], Counter(), set()
        for t in sorted((t for t in evaluated if t['tier'] == tier), key=lambda t: (-t['score'], t['id'])):
            if len(out) >= limit:
                break
            twin = next((s_ for s_ in out if _similar(s_, t, values)), None)
            if twin:
                twin.setdefault('similar', []).append({'id': t['id'], 'title': f"{_names(t['gives_all'])} for {_names(t['receives_all'])}",
                                                       'me_adjusted': t['me']['adjusted_delta'],
                                                       'partner_adjusted': t['partner']['adjusted_delta']})
                folded += 1
                continue
            gs = [p['site_player_id'] for p in t['gives_all']]
            rs = [p['site_player_id'] for p in t['receives_all']]
            if any(gives_seen[g] >= cfg.per_player_limit for g in gs) or any(r in gets_seen for r in rs):
                continue
            gives_seen.update(gs)
            gets_seen.update(rs)
            t['display'] = _describe(t, cfg, ctx)
            t['alternative'] = False
            out.append(t)
            stage[t['package']]['shown'] += 1
        return out
    shown = pick('recommended', cfg.result_limit)
    ranked = [t for t in evaluated if t['tier'] == 'recommended']
    ranked.sort(key=lambda t: (-t['score'], t['id']))
    qualifying = len(shown)
    for t in evaluated:
        if t['tier'] == 'recommended':
            stage[t['package']][t['tier']] += 1
    best_by_type = {}
    for typ in cfg.package_types:
        top = next((t for t in ranked if t['package'] == typ), None)
        best_by_type[typ] = top and {'id': top['id'], 'title': f"{_names(top['gives_all'])} for {_names(top['receives_all'])}",
                                     'partner': top['partner']['team_name'], 'tier': top['tier'],
                                     'me_adjusted': top['me']['adjusted_delta'], 'me_effective': top['effective']['me'],
                                     'partner_adjusted': top['partner']['adjusted_delta'],
                                     'partner_effective': top['effective']['partner'],
                                     'value_label': top['value']['label'], 'label': top['label'],
                                     'score': round(top['score'], 3)}
    hold_side = {'team_id': team_id, 'team_name': inputs['teams'][team_id],
                 'record': ctx.base_record[team_id], 'seed': ctx.base_seed[team_id],
                 'win_pct': win_pct(ctx.base_record[team_id]),
                 'strength_rank': {c: ctx.base_strength[team_id][c]['rank'] for c in CATEGORIES}}
    ranks = hold_side['strength_rank']
    hold_side['strongest'] = [LABELS[c] for c in sorted(CATEGORIES, key=lambda c: ranks[c] or 99)[:3]]
    hold_side['weakest'] = [LABELS[c] for c in sorted(CATEGORIES, key=lambda c: -(ranks[c] or 0))[:3]]
    if qualifying:
        top = shown[0]
        headline = (f"Best idea: {_names(top['gives_all'])} for {_names(top['receives_all'])} with "
                    f"{top['partner']['team_name']} ({top['display']['label'].lower()}; {_robust(top, cfg):+.1f} category "
                    f"wins for you, {_market_cost(top):+.0f} market points surrendered).")
    else:
        headline = 'HOLD: no realistic trade clears the gain and source checks.'
    balancing_gaps = sorted(set((['cant_cut_list_unverified'] if cant_cut_ids is None else []) + budget_gaps +
                                ['free_agent_status_is_a_dated_observation', 'partner_acquisitions_unverified'] +
                                (['il_eligibility_unverified'] if any(t['balancing'][s]['kind'] == 'il'
                                                                      for t in evaluated for s in ('me', 'partner')) else [])))
    return {
        'phase': '4D', 'team_id': team_id, 'team_name': inputs['teams'][team_id], 'hashtag_weight': weight,
        'as_of_utc': stamp, 'headline': headline, 'hold': hold_side,
        'cards': shown, 'qualifying_cards': qualifying, 'best_by_type': best_by_type,
        **({'finalists': finalists} if cfg.keep_finalists else {}),
        'market': market_summary(inputs, ctx, market, as_of),
        'options': {'partners': [{'team_id': t, 'team_name': inputs['teams'][t]} for t in ctx.teams if t != team_id],
                    'selected_partners': list(partners), 'protected_ids': sorted(cfg.protected_ids),
                    'package_types': list(cfg.package_types),
                    'roster': [{'site_player_id': p.site_player_id, 'name': p.name, 'slot': p.assigned_slot,
                                'value': values[p.site_player_id]['value']}
                               for p in sorted(inputs['rosters'][team_id], key=lambda p: -values[p.site_player_id]['value'])]},
        'counts': {'recommended': sum(t['tier'] == 'recommended' for t in evaluated),
                   'by_label': dict(Counter(t['label'] for t in evaluated if t.get('label')))},
        'timing': {'today': today.isoformat(), 'earliest_effective_day': eff_day.isoformat(),
                   'trade_deadline': rules['deadline'].isoformat(), 'review': rules['review'],
                   'reject_days': rules['reject_days'], 'assumed_acceptance_days': cfg.acceptance_days,
                   'remaining_weeks': [w.number for w in ctx.weeks]},
        'balancing': {'active_limit': ACTIVE_LIMIT, 'il_limit': IL_LIMIT, 'cant_cut_verified': cant_cut_ids is not None,
                      'il_move_statuses': sorted(il_rule), 'effective_week': budget_week, 'my_acquisitions_left': budget,
                      'free_agent_fills_tried': [ctx.site[p].name for p in fills],
                      'waiver_players_not_used': waiver_players, 'gaps': balancing_gaps},
        'verification': verification(inputs, evidence, yahoo_status, now, cfg.evidence_max_age_minutes),
        'assumptions': [
            f"Trades take effect from {eff_day.isoformat()}: {cfg.acceptance_days} day for the partner to accept plus the "
            f"{rules['reject_days']}-day {rules['review'].lower()} reject window. Earlier games count for the original teams.",
            'Every other team keeps its deterministic forecast; any matchup with the trade partner is recomputed with both new rosters.',
            f"Each team keeps at most {ACTIVE_LIMIT} active players. The side taking two players clears a spot with its best "
            f"legal drop among its {cfg.drop_options or 'all'} cheapest options (Can't Cut players excluded when the list is "
            "captured), or an IL move for an O/INJ player when an IL slot is free (IL eligibility unverified).",
            'An open spot is scored as left open. A free-agent fill is shown separately: it uses one acquisition in the '
            'effective week, relies on free-agent status at capture, never uses waiver players, and counts only partly in ranking.',
            'Trades themselves use no acquisitions. Keeper value, manager preferences, injury return dates and pending trades are not modelled.',
            'Category-win changes are deterministic projections, not probabilities. Market value and your competitive '
            'benefit are kept apart. Market value is how other managers are likely to see players: the mean of Yahoo '
            'preseason ADP and FantasyPros consensus rankings, each re-ranked among Yahoo\'s players and put on one '
            'auction-like curve; on a side with two players the second counts at half (the side that gets the best '
            'player is the one managers see as winning); any player a side must cut counts against that side. Paying '
            'the partner a market premium is allowed and ranked by what it buys; a package where the partner loses '
            'more than 10% by market value is never shown. Model value (z-scores over replacement) is shown only as an '
            'asset-sacrifice risk. None of this predicts acceptance.'],
        'diagnostics': {'partners': list(partners), 'my_tradeable': len(mine), 'pairs_screened': sum(v['feasible'] for v in stage.values()),
                        'plausible_after_screen': len(plausible), 'replayed': len(evaluated),
                        'drop_options_replayed': drops_replayed, 'source_checked': len(finalists),
                        'folded_equivalent': folded, 'dominated_variations': len(dominated), 'by_type': {k: dict(v) for k, v in stage.items()},
                        'skipped_players': dict(skipped), 'excluded': dict(excluded), 'exhaustive': cfg.shortlist is None,
                        'seconds': {'screen': round(screen_seconds, 2), 'replay': round(replay_seconds, 2),
                                    'balancing': round(balance_seconds, 2), 'sources': round(source_seconds, 2),
                                    'total': round(perf_counter() - started, 2)},
                        'engine': {'computed_days': ctx.engine.computed, 'cache_hits': ctx.engine.hits,
                                   'team_weeks_cached': len(ctx.cache)}},
        'all_evaluated': [{'id': t['id'], 'package': t['package'], 'partner': t['partner']['team_id'],
                           'gives': _names(t['gives_all']), 'receives': _names(t['receives_all']),
                           'me': t['me']['adjusted_delta'], 'me_points': t['me']['points_delta'],
                           'partner_adjusted': t['partner']['adjusted_delta'], 'me_effective': t['effective']['me'],
                           'partner_effective': t['effective']['partner'], 'gap': t['value']['gap'],
                           'model_gap': t['value']['model_gap'],
                           'tier': t['tier'], 'score': t['score'], 'label': t.get('label'),
                           'robust': _robust(t, cfg), 'market_cost': _market_cost(t), 'screen_me': t['screen']['me'],
                           'partner_surplus': t['value']['market']['partner_surplus'],
                           'near_term': t['me']['near_term_delta'], 'dominated_by': t.get('dominated_by'),
                           'balancing': {s_: {'kind': b['kind'], 'player': (b['player'] or {}).get('site_player_id'),
                                              'fill': (b.get('fill') or {}).get('used') and b['fill']['player']['site_player_id']
                                              or None}
                                         for s_, b in t['balancing'].items()}} for t in evaluated],
    }
