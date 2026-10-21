"""Market value: Yahoo preseason ADP and FantasyPros ECR mapped to one curve, and value gaps.
"""
from __future__ import annotations

from collections import Counter
from math import exp

from fantasy_dashboard.league import parse_utc
from fantasy_dashboard.sources.matching import normalize_name, normalize_team
from fantasy_dashboard.trades.config import MARKET_DECAY, VALUE_FLOOR, _ids

FP_TEAMS = {'NOR': 'NOP', 'UTH': 'UTA'}   # FantasyPros team codes that differ from Yahoo's
MARKET_SIGNALS = {'adp': 'Yahoo preseason ADP', 'ecr': 'FantasyPros consensus (ECR)'}
ECR_STALE_DAYS = 14      # an older ECR capture drops out of the blend (flagged)
DISAGREE_POINTS, DISAGREE_SHARE = 15.0, .35   # per-player signal spread that counts as disagreement


def _ordinal(ranked):
    """{pid: raw rank} -> {pid: position 1..n among the matched players}, ties averaged."""
    out, items = {}, sorted(ranked.items(), key=lambda kv: kv[1])
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1][1] == items[i][1]:
            j += 1
        for k in range(i, j + 1):
            out[items[k][0]] = (i + j) / 2 + 1
        i = j + 1
    return out


def ecr_matches(inputs):
    """FantasyPros rows keyed by Yahoo site ID: normalized name (team breaks duplicates), else first initial,
    surname and team for names Yahoo abbreviates ("N. Alexander-Walker"). Returns (matches, unmatched rows)."""
    ecr = inputs.get('ecr')
    if not ecr:
        return {}, []
    full, short = {}, {}
    for p in inputs['site_players']:
        n = normalize_name(p.name)
        full.setdefault(n, []).append(p)
        parts = n.split()
        if parts:
            short.setdefault((parts[0][0], ' '.join(parts[1:]), normalize_team(p.nba_team)), []).append(p)
    out, unmatched = {}, []
    for r in ecr['players']:
        n, team = normalize_name(r['name'] or ''), normalize_team(FP_TEAMS.get(r['team'], r['team']))
        c = full.get(n, [])
        if len(c) > 1:
            c = [p for p in c if normalize_team(p.nba_team) == team]
        if not c and n:
            parts = n.split()
            c = short.get((parts[0][0], ' '.join(parts[1:]), team), [])
        if len(c) == 1 and c[0].site_player_id not in out:
            out[c[0].site_player_id] = r
        else:
            unmatched.append({'name': r['name'], 'team': r['team'], 'rank_ecr': r['rank_ecr']})
    return out, unmatched


def market_values(inputs, ctx, as_of=None):
    """Perceived market value: how other managers are likely to value players, never this model's view.

    Signals: Yahoo preseason average draft position (public Draft Analysis, standard scoring) and FantasyPros
    expert consensus rankings (Roto/categories). Each is re-ranked to an ordinal position among the players on
    Yahoo's list (ties averaged), so both share one scale, then mapped to the auction-like curve
    100 x (e^-(pos-1)/40 - e^-(R-1)/40), R the rostered active players (replacement level). Market value is the
    mean of the available signals. Undrafted players count as replacement by ADP (an observation); a drafted
    player FantasyPros does not list is treated as missing for ECR, not as zero. ECR older than ECR_STALE_DAYS
    at the as-of time drops out. With neither signal, the mean projection-source rank is used and `basis` says
    so. Yahoo's league player list is never used: once games start its default order is season-to-date stats."""
    adp = (inputs.get('adp') or {}).get('players')
    ecr = inputs.get('ecr')
    as_of = as_of or getattr(ctx, 'as_of', None)
    if ecr and as_of is not None:
        age = (as_of - parse_utc(ecr['updated_at_utc'])).total_seconds() / 86400
        if age > ECR_STALE_DAYS:
            ecr = None
    level = sum(p.assigned_slot != 'IL' for p in ctx.players.values())

    def curve(r):
        return max(0.0, 100 * (exp(-(r - 1) / MARKET_DECAY) - exp(-(level - 1) / MARKET_DECAY)))
    universe = {p.site_player_id for p in inputs['site_players']} | set(ctx.site)
    adp_raw = {pid: (row.get('adp_preseason') or row.get('adp_all')) for pid, row in (adp or {}).items()}
    adp_raw = {pid: v for pid, v in adp_raw.items() if v and pid in universe}
    adp_pos = _ordinal(adp_raw)
    ecr_rows = ecr_matches({**inputs, 'ecr': ecr})[0] if ecr else {}
    ecr_pos = _ordinal({pid: r['rank_ecr'] for pid, r in ecr_rows.items() if pid in universe})
    out = {}
    for pid in ctx.site:
        sig = {}
        if adp is not None:
            sig['adp'] = {'pick': adp_raw.get(pid), 'position': adp_pos.get(pid),
                          'value': curve(adp_pos[pid]) if pid in adp_pos else 0.0}
        if ecr_rows and (pid in ecr_pos or not sig.get('adp', {}).get('value')):
            r = ecr_rows.get(pid) or {}
            sig['ecr'] = {'rank': r.get('rank_ecr'), 'position': ecr_pos.get(pid), 'std': r.get('rank_std'),
                          'value': curve(ecr_pos[pid]) if pid in ecr_pos else 0.0}
        if sig:
            vals = [s['value'] for s in sig.values()]
            value = sum(vals) / len(vals)
            basis = 'market_consensus' if len(sig) == 2 else ('yahoo_preseason_adp' if 'adp' in sig else 'fantasypros_ecr')
            pick = sig.get('adp', {}).get('pick')
        else:
            src = inputs['projections'].get(pid) or {}
            ranks = [r.raw.get(k) for r, k in ((src.get('LeagueWinner'), 'Rank'), (src.get('Hashtag'), 'R#'))
                     if r is not None and isinstance(r.raw.get(k), (int, float))]
            pick = sum(ranks) / len(ranks) if ranks else None
            value, basis, vals = (curve(pick) if pick else 0.0), 'projection_source_rank', []
        spread = max(vals) - min(vals) if len(vals) > 1 else 0.0
        out[pid] = {'rank': pick, 'value': value, 'basis': basis, 'signals': sig, 'spread': spread,
                    'missing': [k for k in MARKET_SIGNALS if (k == 'adp' and adp is not None or k == 'ecr' and ecr_rows)
                                and k not in sig],
                    'disagree': spread >= DISAGREE_POINTS and spread >= DISAGREE_SHARE * max(vals or [0])}
    return out


def market_summary(inputs, ctx, market, as_of):
    """Source coverage, freshness and assumptions for the market-value measure (rostered players)."""
    rostered = [pid for pid, p in ctx.players.items() if p.assigned_slot != 'IL']
    adp, ecr = inputs.get('adp'), inputs.get('ecr')
    ecr_unmatched = ecr_matches(inputs)[1] if ecr else []
    age = (as_of - parse_utc(ecr['updated_at_utc'])).total_seconds() / 86400 if ecr else None
    count = lambda k: sum(k in market[p]['signals'] and market[p]['signals'][k]['position'] is not None for p in rostered)
    return {
        'signals': [
            {'key': 'adp', 'label': MARKET_SIGNALS['adp'], 'available': adp is not None,
             'captured_at_utc': (adp or {}).get('captured_at_utc'), 'rostered_covered': count('adp'),
             'note': 'Preseason ADP is fixed once the season starts; undrafted players count as replacement level.'},
            {'key': 'ecr', 'label': MARKET_SIGNALS['ecr'], 'available': bool(ecr) and age is not None and age <= ECR_STALE_DAYS,
             'captured_at_utc': (ecr or {}).get('captured_at_utc'), 'updated_at_utc': (ecr or {}).get('updated_at_utc'),
             'age_days': None if age is None else round(age, 1), 'experts': (ecr or {}).get('experts'),
             'ranking_type': (ecr or {}).get('ranking_type'), 'fallback_for': (ecr or {}).get('fallback_for'),
             'rostered_covered': count('ecr'), 'unmatched_top_200': [u for u in ecr_unmatched if u['rank_ecr'] <= 200],
             'note': ('FantasyPros has not published rest-of-season rankings yet; its page serves the final draft '
                      'consensus.' if (ecr or {}).get('fallback_for') == 'ROS' else 'Rest-of-season consensus.')}],
        'rostered': len(rostered),
        'basis_counts': dict(Counter(market[p]['basis'] for p in rostered)),
        'rostered_disagreeing': sum(market[p]['disagree'] for p in rostered),
        'curve': f'100 x (e^-(position-1)/{MARKET_DECAY:.0f} - e^-(R-1)/{MARKET_DECAY:.0f}), R = rostered active players',
        'stale_after_days': ECR_STALE_DAYS}


def side_value(values, ids):
    """Asset value of one side of a package. A lone player keeps his (possibly negative) 4A value; in a
    two-player side each counts at least replacement level, since either could be swapped for a free agent."""
    ids = _ids(ids)
    return values[ids[0]]['value'] if len(ids) == 1 else sum(max(0.0, values[p]['value']) for p in ids)


def value_gap(get, give, floor=VALUE_FLOOR):
    """Positive when I receive more value than I give, as a share of the larger asset."""
    return (get - give) / max(abs(get), abs(give), floor)


def fairness_label(gap, get, give, cfg, floor=VALUE_FLOOR):
    if get <= floor and give <= floor:
        return 'Both near replacement level'
    if abs(gap) <= cfg.fair_band:
        return 'Fair'
    if gap > 0:
        return 'Favours you' if gap <= cfg.overpay_limit else 'Lopsided toward you'
    return 'You pay a premium' if gap >= -cfg.overpay_limit else 'Overpay'
