"""Card text and tables for evaluated trades: players, balancing lines, benefit and risks.
"""
from __future__ import annotations

from fantasy_dashboard.league import DOUBTFUL_STATUSES, INACTIVE_STATUSES, LABELS
from fantasy_dashboard.trades.config import ACTIVE_LIMIT, PACKAGE_LABELS
from fantasy_dashboard.trades.ranking import _fill_dependent, _market_cost, _robust, market_label


def _player(ctx, pid, values, statuses, market=None):
    p, v = ctx.players[pid], ctx.proj[pid]
    s = v['values']
    m = (market or {}).get(pid) or {}
    ecr = (m.get('signals') or {}).get('ecr') or {}
    return {'site_player_id': pid, 'name': p.name, 'adp': m.get('rank'), 'ecr': ecr.get('rank'),
            'market_value': m.get('value'), 'market_disagree': m.get('disagree', False),
            'nba_team': p.nba_team, 'positions': list(p.eligible_positions),
            'slot': p.assigned_slot, 'yahoo_status': statuses.get(pid), 'coverage': v['coverage'],
            'per_game': {'gp': s['gp'], 'pts': s['pts'], 'reb': s['reb'], 'ast': s['ast'], 'three_ptm': s['three_ptm'],
                         'stl': s['stl'], 'blk': s['blk'], 'turnovers': s['turnovers'],
                         'fg_pct': s['fgm'] / s['fga'] if s['fga'] else None,
                         'ft_pct': s['ftm'] / s['fta'] if s['fta'] else None, 'fga': s['fga'], 'fta': s['fta']},
            'value': {k: values[pid][k] for k in ('value', 'per_game_z', 'replacement_position',
                                                  'replacement_player', 'team_games')},
            'z': values[pid]['z']}


def _cats(delta, sign):
    return [LABELS[c] for c, d in sorted(delta.items(), key=lambda kv: -abs(kv[1])) if d * sign > 0]


def _names(players):
    return ' + '.join(p['name'] for p in players)


def _balance_lines(t):
    """Plain-language roster balancing for both sides; numbers are rest-of-season category wins."""
    lines = []
    for side in ('me', 'partner'):
        b, who = t['balancing'][side], ('You' if side == 'me' else t['partner']['team_name'])
        if b['kind'] in ('drop', 'il'):
            p = b['player']
            what = (f"drop {p['name']} (asset value {p['value']['value']:.0f})" if b['kind'] == 'drop'
                    else f"move {p['name']} (Yahoo {p['yahoo_status']}) to IL; IL eligibility is unverified")
            line = f"{who} must {what} to stay at {ACTIVE_LIMIT} active players."
            if b.get('cost') is not None:
                line += (f" The exchange alone is worth {b['exchange_points']:+.1f} to {'you' if side == 'me' else 'them'};"
                         f" the {'drop' if b['kind'] == 'drop' else 'IL move'} changes that by {-b['cost']:+.1f}.")
            if len(b.get('options') or ()) > 1:
                alt = sorted(b['options'], key=lambda o: -o['points_delta'])[1]
                line += f" Next best: {alt['name']} ({alt['points_delta']:+.1f} overall)."
            lines.append(line)
        elif b['kind'] == 'open':
            f = b.get('fill')
            have = 'have' if side == 'me' else 'has'
            if f and f['used']:
                o = f['opportunity']
                line = (f"{who} {have} an open roster spot after the trade. Results include adding free agent "
                        f"{f['player']['name']}, worth {f['points_gain']:+.1f} over leaving it open "
                        f"({f['open_points']:+.1f} without him). Without any trade, the same add"
                        f"{' (dropping ' + o['drop'] + ')' if o['drop'] else ''} would be worth {o['points']:+.1f}, "
                        f"so ranking counts the trade at {t['effective'][side]:+.1f}. "
                        f"Conditional: one acquisition in Week {f['week']}"
                        f"{'' if side == 'me' else ' (their adds are unverified)'}; free-agent status is from the capture.")
            else:
                line = f"{who} {have} an open roster spot after the trade; results assume it stays open."
                if f:
                    line += (f" The best free-agent fill ({f['player']['name']}, {f['points_gain']:+.1f}) is not counted"
                             f"{': no acquisitions left that week' if not f['feasible'] else ''}.")
            lines.append(line)
    if t.get('streaming_slot'):
        lines.append('This 2-for-1 frees a useful streaming slot: even one permanent free-agent fill adds category wins, '
                     'and the Waiver Advisor can plan short-term streams for it.')
    return lines


def _ranks(players):
    def one(p):
        bits = [f"ADP {p['adp']:.0f}" if p.get('adp') else 'undrafted']
        if p.get('ecr'):
            bits.append(f"ECR {p['ecr']:.0f}")
        return f"{p['name']} ({', '.join(bits)})"
    return ', '.join(one(p) for p in players)


TIER_LABELS = {'excluded': 'Below threshold', 'unchecked': 'Unchecked'}


def _describe(t, cfg, ctx):
    me, them, gives, gets, m = t['me'], t['partner'], t['gives_all'], t['receives_all'], t['value']['market']
    gained, lost = _cats(me['category_delta'], 1), _cats(me['category_delta'], -1)
    their_gain = _cats(them['category_delta'], 1)
    robust, eff, cost = _robust(t, cfg), t['effective']['me'], _market_cost(t)
    surplus = m['partner_surplus']
    label = market_label(t, cfg) if t.get('tier') == 'recommended' else TIER_LABELS.get(t.get('tier'), 'Below threshold')

    my_gain = (f"{robust:+.1f} category wins over the rest of the season"
               + (f" ({eff:+.1f} on the blended projection; the lower figure is what holds under each source)" if eff - robust >= .05 else '')
               + f"; next three weeks {me['near_term_delta']:+.1f}."
               + (f" Improves {', '.join(gained[:4])}" if gained else '') + (f"; costs {', '.join(lost[:3])}." if lost else '.'))
    cut = f" plus {m['my_cut']:.0f} for the player you cut" if m.get('my_cut', 0) >= .5 else ''

    def pts(x):
        return f"{x:.0f} market point" + ('' if round(x) == 1 else 's')
    give_up = (f"You send {m['give']:.0f} market points{cut} and receive {m['get']:.0f}: "
               + (f"{pts(cost)} surrendered" if cost >= .5 else f"you gain {pts(-cost)}" if cost <= -.5 else 'even') +
               f". {_ranks(gives)}.")
    their_cut = f" after cutting a player worth {m['partner_cut']:.0f}" if m.get('partner_cut', 0) >= .5 else ''
    consider = []
    if -t['value']['gap'] > cfg.fair_band:
        consider.append(f"By draft position and consensus rankings they receive the more valuable side ({_ranks(gives)} "
                        f"for {_ranks(gets)}).")
    elif t['value']['gap'] <= cfg.fair_band and t['value']['gap'] >= -cfg.fair_band:
        consider.append('By draft position and consensus rankings the two sides are close.')
    else:
        consider.append(f"By conventional value they give up more ({_ranks(gets)} for {_ranks(gives)}); expect pushback.")
    if them['incoming_games']:
        consider.append(f"{_names(gives)} would start about {them['incoming_starts']} of {them['incoming_games']} "
                        f"remaining team games for them.")
    if their_gain and t['effective']['partner'] >= 0:
        weak = [LABELS[c] for c, d in them['category_delta'].items()
                if d > 0 and LABELS[c] in their_gain[:3] and (them['strength_before'][c] or 0) >= 6]
        consider.append(f"The model has them improving {', '.join(their_gain[:3])}" +
                        (f", a weakness for them ({', '.join(weak[:2])})" if weak else '') +
                        f" (net {t['effective']['partner']:+.1f} category wins).")
    elif them['adjusted_delta'] >= 0:
        consider.append('The model shows no category-win loss for them.')
    benefit = []
    if gained:
        weak = [LABELS[c] for c, d in me['category_delta'].items()
                if d > 0 and LABELS[c] in gained[:3] and (me['strength_before'][c] or 0) >= 6]
        benefit.append(f"Schedule-aware replay adds {', '.join(gained[:3])}" +
                       (f", where you rank in the bottom half ({', '.join(weak[:2])})" if weak else '') + '.')
    benefit.append(f"Daily lineups: {_names(gets)} would start {me['incoming_starts']} of {me['incoming_games']} remaining "
                   f"team games for you; {_names(gives)} would have started {me['outgoing_starts']} of {me['outgoing_games']}.")
    if me['seed_after'] != me['seed_before']:
        benefit.append(f"Your projected seed moves from #{me['seed_before']} to #{me['seed_after']}.")
    rival = abs(them['seed_before'] - me['seed_before']) <= 2
    if rival and t['effective']['partner'] < 0:
        benefit.append(f"It also weakens {them['team_name']}, a nearby standings rival (seed #{them['seed_before']}).")
    risks = []
    if label == 'Expensive improvement':
        risks.append(f"Expensive: {cost:.0f} market points for {robust:+.1f} wins "
                     f"({cost / max(robust, .1):.0f} per win; the line is {cfg.expensive_cost_per_win:.0f}).")
    for p in (*gives, *gets):
        if p['yahoo_status'] in INACTIVE_STATUSES | DOUBTFUL_STATUSES:
            risks.append(f"{p['name']} is listed {p['yahoo_status']} on Yahoo; his return date is not modelled.")
        if p['slot'] == 'IL':
            risks.append(f"{p['name']} is on injured reserve; IL slot use after the trade is unverified.")
        if p['coverage'] != 'two_sources':
            risks.append(f"{p['name']} has only one projection source.")
        if p.get('market_disagree'):
            risks.append(f"ADP and consensus rankings disagree on {p['name']}; his market value is less certain.")
    if (m.get('signals_disagree')):
        risks.append('ADP and consensus rankings point different ways on this package\'s balance.')
    for u in t.get('low_usage') or ():
        risks.append(f"Asset sacrifice: {u['name']} starts only {u['starts']} of {u['games']} remaining games for you, so the "
                     f"replay charges little for losing him; injuries could make him matter again.")
    model = t['value'].get('model_gap', 0.0)
    if model < -cfg.overpay_limit:
        risks.append('Asset sacrifice: by roster-neutral model value you give up much more than you receive.')
    alt = t.get('simpler_alternative')
    if alt:
        risks.append(f"Simpler option: {alt['title']} alone gives {alt['me_adjusted']:+.1f} for you at lower asset cost.")
    if t['package'] == '2-1':
        risks.append(f"{them['team_name']} takes two players and must clear a roster spot; they may value depth less.")
    elif t['package'] == '1-2':
        risks.append('You take two players and must clear a roster spot; the cut is part of the result above.')
    narrow = [f for f in me['flips'] if f['narrow'] and f['delta'] > 0]
    if narrow:
        risks.append(f"{len(narrow)} of your gained category results are within a narrow margin.")
    checks = t.get('sources') or {}
    if any(not x.get('unavailable') and x['me_points'] <= 0 for x in checks.values()):
        risks.append('One projection source shows no gain for you.')
    if _fill_dependent(t, cfg):
        risks.append('The result leans on one specific free agent still being available.')
    if rival and t['effective']['partner'] >= cfg.material_gain:
        risks.append(f"It strengthens {them['team_name']}, a nearby standings rival (seed #{them['seed_before']}).")
    if t['effective']['partner'] < 0:
        risks.append(f"The model has {them['team_name']} losing {-t['effective']['partner']:.1f} wins; they may see it later.")
    if any(r['opponent'] == them['team_id'] for r in me['near_term']):
        risks.append(f"You play {them['team_name']} soon; the trade changes that matchup for both teams.")
    if all(not x['delta'] for x in me['near_term']):
        risks.append('No change in the next three weeks; the gain comes later in the season.')
    elif me['near_term_delta'] < 0:
        risks.append(f"Costs {-me['near_term_delta']:.1f} category wins over the next three weeks before the gain arrives.")
    they_receive = (f"{them['team_name']} {'gains' if surplus >= 0 else 'loses'} {pts(abs(surplus))}{their_cut} "
                    f"({abs(100 * t['value']['gap']):.0f}% of the larger side). They get {_ranks(gives)}; "
                    f"they give {_ranks(gets)}.")
    return {'title': f"{_names(gives)} for {_names(gets)}", 'partner': them['team_name'],
            'package_label': PACKAGE_LABELS[t['package']], 'label': label, 'tier_label': label,
            'my_gain': my_gain, 'give_up': give_up, 'they_receive': they_receive,
            'why_consider': consider, 'why_benefit': benefit, 'risks': risks,
            'why': consider + benefit, 'warnings': risks,
            'gained': gained, 'lost': lost, 'partner_gained': their_gain, 'partner_lost': _cats(them['category_delta'], -1),
            'balancing': _balance_lines(t), 'robustness': t.get('robustness', 'Unchecked')}
