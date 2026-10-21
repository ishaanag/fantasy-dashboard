"""Robust gain, tiers, labels, rank score and Pareto pruning for evaluated packages.
"""
from __future__ import annotations

from fantasy_dashboard.trades.config import MARKET_FLOOR, SOURCE_WEIGHTS


def _checks(t):
    return [x for x in (t.get('sources') or {}).values() if not x.get('unavailable')]


def _market_cost(t):
    """Market points I surrender net: what I send plus any player I must cut, minus what I receive."""
    m = t['value']['market']
    return m['give'] + m.get('my_cut', 0.0) - m['get']


def _fill_dependent(t, cfg):
    me_fb, them_fb = _fallback(t, 'me'), _fallback(t, 'partner')
    return (me_fb is not None and me_fb < cfg.material_gain) or (them_fb is not None and them_fb < cfg.partner_credible_gain)


def _robust(t, cfg):
    """My gain I can rely on: the blended effective gain, or less if either projection source alone shows less."""
    eff, checks = _effective(t, 'me', cfg), _checks(t)
    return min([eff] + [x['me_adjusted'] - _fill_discount(t, 'me', cfg) for x in checks])


def _narrow_share(t):
    up = [f for f in t['me']['flips'] if f['delta'] > 0]
    return sum(f['delta'] for f in up if f['narrow']) / sum(f['delta'] for f in up) if up else 0.0


def market_label(t, cfg):
    """One label from explicit computed criteria (gap > 0 favours me by market value)."""
    gap, me, cost, checks = t['value']['gap'], _robust(t, cfg), _market_cost(t), _checks(t)
    if cost > MARKET_FLOOR and cost > cfg.expensive_cost_per_win * max(me, .1):
        return 'Expensive improvement'
    if (_effective(t, 'partner', cfg) >= cfg.partner_credible_gain and not _fill_dependent(t, cfg)
            and all(x['partner_points'] >= 0 for x in checks)):
        return 'Mutually beneficial'
    if -gap > cfg.fair_band:
        return 'Market opportunity'
    return 'Market-balanced'


def _classify(t, cfg):
    """('recommended' | 'excluded', reason). Paying a market premium is never a reason to exclude; partner
    category gain is evidence, not a requirement. A partner who loses market value beyond viable_market_limit would
    not consider the trade, so it is never shown."""
    gap, me, cost, checks = t['value']['gap'], _robust(t, cfg), _market_cost(t), _checks(t)
    if _effective(t, 'me', cfg) < cfg.material_gain:
        return 'excluded', 'your rest-of-season gain is below the materiality threshold'
    if checks and all(x['me_points'] <= 0 for x in checks):
        return 'excluded', 'your gain disappears under each projection source alone'
    if gap > cfg.viable_market_limit:
        return 'excluded', 'partner would lose market value; not a realistic proposal'
    shaky = me < 2 * cfg.material_gain or _narrow_share(t) >= .5 or (checks and not all(x['me_points'] > 0 for x in checks))
    if cost > MARKET_FLOOR and cost > cfg.expensive_cost_per_win * max(me, .1) and shaky:
        return 'excluded', 'large market concession for a small, fragile or source-dependent gain'
    return 'recommended', None


def _fill_discount(t, side, cfg):
    """Opportunity cost of a used free-agent fill: what the same free agent is worth to that team as a plain
    waiver swap without the trade, plus part of the best fill's edge over the next-best one (so a ranking
    never rests on one specific free agent still being available)."""
    f = ((t.get('balancing') or {}).get(side) or {}).get('fill') or {}
    if not f.get('used'):
        return 0.0
    edge = max(0.0, f['net_adjusted'] - f['fallback']['net_adjusted'])
    return max(0.0, f['opportunity']['adjusted']) + (1 - cfg.fill_credit) * edge


def _effective(t, side, cfg):
    """Adjusted category-win change net of the fill opportunity cost. Equals adjusted_delta without a fill."""
    return t[side]['adjusted_delta'] - _fill_discount(t, side, cfg)


def _fallback(t, side):
    """Side's net gain if only the next-best free agent were available for its open spot."""
    f = ((t.get('balancing') or {}).get(side) or {}).get('fill') or {}
    return f['fallback']['net_adjusted'] if f.get('used') else None


def _score(t, cfg):
    """Transparent rank score in category wins; the parts are kept in t['ranking']. Order of importance:
    robust gain, market plausibility, market value surrendered, next three weeks, source agreement."""
    gap, checks = t['value']['gap'], _checks(t)
    held = [x['me_points'] > 0 for x in checks]
    bal = t.get('balancing') or {}
    steps = sum(b['kind'] in ('drop', 'il') or bool((b.get('fill') or {}).get('used')) for b in bal.values())
    parts = {'robust_gain': _robust(t, cfg),
             'plausibility': -3 * max(0.0, gap),
             'market_cost': -cfg.market_cost_weight * max(0.0, _market_cost(t)),
             'near_term': cfg.near_term_weight * max(-2.0, min(t['me']['near_term_delta'], 2.0)),
             'sources': (.5 if held and all(held) and len(held) == len(SOURCE_WEIGHTS) else
                         -.5 if held and not all(held) else 0.0),
             'partner_benefit': cfg.partner_weight * max(-2.0, min(_effective(t, 'partner', cfg), 3.0)),
             'complexity': -cfg.complication_cost * (len(t['gives_all']) + len(t['receives_all']) - 2 + steps),
             'simpler_option': -cfg.simpler_penalty if t.get('simpler_alternative') else 0.0}
    t['ranking'] = {k: round(v, 4) for k, v in parts.items()}
    return sum(parts.values())


def _pareto(cands, cfg):
    """Removes inferior variations: a package is dropped when another with the same partner and at least one
    shared player is no worse on my gain, market cost, plausibility, simplicity and (within a quarter win)
    the next three weeks, and better on one of the first four. Returns (kept, dropped)."""
    def dims(t):
        return (_effective(t, 'me', cfg), -max(0.0, _market_cost(t)), -max(0.0, t['value']['gap']),
                -(len(t['gives_all']) + len(t['receives_all'])), t['me']['near_term_delta'])
    d = {id(t): dims(t) for t in cands}
    buckets = {}
    for t in cands:
        for p in (*t['gives_all'], *t['receives_all']):
            buckets.setdefault((t['partner']['team_id'], p['site_player_id']), []).append(t)
    dropped = set()
    for t in cands:
        x = d[id(t)]
        for p in (*t['gives_all'], *t['receives_all']):
            for o in buckets[(t['partner']['team_id'], p['site_player_id'])]:
                if o is t:
                    continue
                y = d[id(o)]
                if all(y[i] >= x[i] - 1e-9 for i in range(4)) and y[4] >= x[4] - .25 and \
                        any(y[i] > x[i] + 1e-9 for i in range(4)):
                    dropped.add(id(t))
                    t['dominated_by'] = o['id']
                    break
            if id(t) in dropped:
                break
    return [t for t in cands if id(t) not in dropped], [t for t in cands if id(t) in dropped]
