"""Plain-language summaries for the Waiver Advisor and Trade Lab pages.

`present(advice)` adds a `display` block to each strategy (and the advice) using only values the
advisor and audit already computed. It never changes ranking, values, moves or the audit itself.
Labels describe computed evidence; none of them is a probability.
"""
from __future__ import annotations

from fantasy_dashboard.league import LABELS

WORDS = {'FG%': 'FG%', 'FT%': 'FT%', '3PTM': 'threes', 'PTS': 'points', 'REB': 'rebounds',
         'AST': 'assists', 'STL': 'steals', 'BLK': 'blocks', 'TO': 'turnovers'}

YAHOO = {
    'verified_current': ('Ready now', 'good', 'Verified Yahoo data under 15 minutes old.'),
    'provisional': ('Provisional', 'warn', 'Yahoo evidence has gaps. Confirm availability and timing before acting.'),
    'conditional_future': ('Conditional', 'warn', 'Planned moves, not confirmed. Free-agent availability and '
                           'Yahoo timing must be rechecked before acting.'),
    'hold': ('Baseline', 'neutral', ''),
    'infeasible': ('Not executable', 'bad', 'This plan breaks a known Yahoo rule.'),
}

VERIFICATION = {
    'candidate_status_and_ownership_must_be_refreshed': 'Player status and ownership need a fresh Yahoo check',
    'effective_timing_unverified': 'Transaction dates are modelled, not confirmed Yahoo effective times',
    'future_availability_conditional': 'Another manager could add these players first',
    'transaction_evidence_not_fresh_for_action': 'Yahoo data is more than 15 minutes old',
    'transaction_evidence_missing': 'No Yahoo transaction data was captured',
    'transaction_evidence_stale_or_mismatched': 'Yahoo transaction data does not match the roster capture',
    'acquisition_count_unverified': 'Acquisitions used this week are not verified',
    'active_claims_unverified': 'Pending waiver claims are not verified',
    'waiver_claim_success_unverified': 'A waiver claim may not succeed',
    'waiver_priority_unverified': 'Waiver priority is not verified',
    'player_level_restrictions_unverified': 'Player-level restrictions are not verified',
    'direct_il_eligibility_unverified': 'Direct-to-IL eligibility is not verified',
    'selected_source_missing_or_invalid': 'A projection is missing for the selected source',
    'nba_schedule_missing': 'NBA schedule is missing',
    'nba_team_schedule_missing': "A player's NBA schedule is missing",
}


def signed(v) -> str:
    v = round(float(v or 0), 1)
    return '0' if v == 0 else ('+' if v > 0 else '−') + f'{abs(v):g}'


def _sign(v) -> int:
    v = round(float(v or 0), 6)
    return (v > 0) - (v < 0)


def _record(r) -> str:
    return '-'.join(str(int(x)) for x in r)


def verification_text(code: str) -> str:
    return VERIFICATION.get(code, code.replace('_', ' ').capitalize())


def sources(s: dict) -> list[dict]:
    """Full-plan category-win deltas by projection source (short term, rest of season)."""
    out = []
    rows = (s.get('audit') or {}).get('sensitivity') or []
    if rows:
        for key in ('selected', 'blended', 'hashtag', 'leaguewinner'):
            v = rows[0]['sources'].get(key)
            if v and not v.get('unavailable') and all(o['label'] != v['label'] for o in out):
                out.append({'key': key, 'label': v['label'], 'short_term': v['horizon_points'],
                            'season': v['season_points']})
        return out
    out.append({'key': 'selected', 'label': 'Selected', 'short_term': s['category_points_gained'],
                'season': s['season']['category_points_delta']})
    for key, label in (('hashtag', 'Hashtag'), ('leaguewinner', 'LeagueWinner')):
        v = (s.get('source_check') or {}).get(key)
        if v is not None:
            out.append({'key': key, 'label': label, 'short_term': v, 'season': None})
    return out


def outlook(values: list) -> tuple[str, str, str]:
    """(label, tone, agreement) for a set of rest-of-season deltas from different sources."""
    signs = {_sign(v) for v in values}
    agreement = 'unchecked' if len(values) < 2 else 'consistent' if len(signs) == 1 else \
        'mixed' if {1, -1} <= signs else 'partial'
    if signs == {1}:
        return 'Stronger', 'good', agreement
    if signs == {-1}:
        return 'Weaker', 'bad', agreement
    if signs <= {0}:
        return 'About the same', 'neutral', agreement
    if {1, -1} <= signs:
        return 'Mixed', 'warn', agreement
    return ('Leans stronger', 'neutral', agreement) if 1 in signs else ('Leans weaker', 'warn', agreement)


def _title(s: dict) -> str:
    if not s['moves']:
        return 'Keep the current roster'
    flips = [(w['week'], [WORDS.get(c, c) for c in w.get('categories_won', [])]) for w in s['weeks']]
    flips = [(week, cats) for week, cats in flips if cats]
    if flips:
        return 'Target ' + ' and '.join(f"Week {week} {' and '.join(cats)}" for week, cats in flips)
    if s['season']['category_points_delta'] > 0 or s['season']['margin_score_delta'] > 0:
        return 'Upgrade the roster for the rest of the season'
    return s['kind']


def _timeline(s: dict) -> list[dict]:
    groups, added = [], set()
    for m in s['moves']:
        when = m['when'].split(',')[0]
        tags = (['Stream'] if m['temporary'] else ['Permanent']) + \
               ([f"Yahoo {m['add_status']}"] if m.get('add_status') else []) + \
               (['Waiver claim'] if m.get('availability') == 'W' else [])
        item = {'add': m['add_name'], 'add_meta': f"{m['add_team']} · {'/'.join(m['add_positions'])}",
                'drop': m['drop_name'], 'tags': tags,
                'drop_tag': 'Ends stream' if m['drop_site_player_id'] in added else ''}
        added.add(m['add_site_player_id'])
        if groups and groups[-1]['when'] == when:
            groups[-1]['moves'].append(item)
        else:
            groups.append({'when': when, 'moves': [item]})
    return groups


def _margin(cat: str, value: float, unit: bool = True) -> str:
    if cat in ('FG%', 'FT%'):
        return f'{100 * value:.1f} percentage points'
    return f'{value:.1f} {WORDS.get(cat, cat)}' if unit else f'{value:.0f}'


def _risks(s: dict, src: list, season: dict) -> list[dict]:
    audit, risks = s.get('audit') or {}, []
    narrow = {(n['week'], n['category']): n for n in audit.get('narrow_margins', [])}
    for f in s.get('fragile_flips', []):
        text = f['reason'].replace('hashtag', 'Hashtag').replace('leaguewinner', 'LeagueWinner')
        n = narrow.get((f['week'], f['category']))
        if n:
            text += (f"; projected lead of only {_margin(f['category'], n['margin'])} "
                     f"(typical weekly spread {_margin(f['category'], n['typical_spread'], unit=False)})")
        risks.append({'label': f"Fragile Week {f['week']} {WORDS.get(f['category'], f['category'])} gain",
                      'text': text[0].upper() + text[1:] + '.', 'tone': 'warn'})
    if s['moves'] and not s.get('fragile_flips') and s['category_points_gained'] > 0:
        lower = [x for x in src if x['short_term'] < s['category_points_gained']]
        if lower:
            risks.append({'label': 'Short-term gain depends on projections', 'tone': 'warn',
                          'text': ', '.join(f"{x['label']} shows {signed(x['short_term'])}" for x in lower) + '.'})
    vals = [x for x in src if x['season'] is not None]
    if s['moves'] and season['label'] in ('Mixed', 'Leans weaker', 'Weaker'):
        neg = [x for x in vals if _sign(x['season']) < 0]
        rest = [x for x in vals if _sign(x['season']) >= 0]
        text = (', '.join(f"{x['label']} {signed(x['season'])}" for x in neg) +
                f" category wins over the remaining {season['weeks']} weeks")
        if rest:
            text += '; ' + ', '.join(f"{x['label']} {signed(x['season'])}" for x in rest)
        risks.append({'label': 'Mixed long-term projections' if season['label'] == 'Mixed' else 'Rest-of-season cost',
                      'text': text + '.', 'tone': 'warn' if season['label'] != 'Weaker' else 'bad'})
    simpler = (audit.get('summary') or {}).get('preferable_simpler_plan')
    if simpler:
        risks.append({'label': 'A simpler plan does as well', 'text': simpler + '.', 'tone': 'warn'})
    for r in audit.get('replacements', []):
        for flag in r['flags']:
            if flag.startswith('Asset downgrade'):
                lv = r['lineup_value']
                risks.append({'label': 'Long-term asset downgrade', 'tone': 'warn', 'text':
                              f"{r['incoming']} projects {lv['incoming']:.0f} rest-of-season lineup value against "
                              f"{lv['outgoing']:.0f} for {r['outgoing']}."})
            elif 'weaker per game' in flag:
                why = flag.split('; ', 1)[1] if '; ' in flag else flag
                risks.append({'label': 'Edge relies on games played', 'tone': 'warn',
                              'text': f"{r['incoming']} is weaker per game than {r['outgoing']}; {why}."})
            elif flag.startswith('Helps the short term only'):
                risks.append({'label': 'Short-term help only', 'tone': 'warn',
                              'text': f"{r['outgoing']} → {r['incoming']} does not improve the rest of the season on its own."})
            else:
                risks.append({'label': 'Small long-term loss ignored', 'tone': 'warn',
                              'text': f"{r['outgoing']} → {r['incoming']}: {flag}."})
    return risks


def describe(s: dict) -> dict:
    src = sources(s)
    vals = [x['season'] for x in src if x['season'] is not None]
    label, tone, agreement = outlook(vals)
    season = {'label': label, 'tone': tone, 'agreement': agreement, 'weeks': s['season']['weeks'],
              'blended': s['season']['category_points_delta'],
              'detail': ' · '.join(f"{x['label']} {signed(x['season'])}" for x in src if x['season'] is not None)}
    if _sign(s['season']['category_points_delta']) != _sign(s['season']['margin_score_delta']):
        season['margin_note'] = (f"Category margins move the other way (margin score "
                                 f"{s['season']['margin_score_delta']:+.2f}); the win count swings on close categories.")
    changed = [w for w in s['weeks'] if list(w['before']) != list(w['after'])]
    gained = s['category_points_gained']
    short = {'value': gained, 'text': signed(gained), 'tone': 'good' if gained > 0 else 'bad' if gained < 0 else 'neutral',
             'weeks': f"Weeks {s['weeks'][0]['week']}–{s['weeks'][-1]['week']}" if len(s['weeks']) > 1
             else f"Week {s['weeks'][0]['week']}",
             'detail': '; '.join(f"Week {w['week']} vs {w['opponent_name']} {_record(w['before'])} → {_record(w['after'])}"
                                 for w in changed) or 'No matchup result changes'}
    used = [w for w in s['weeks'] if w.get('acquisitions_used_after')]
    streams = sum(1 for m in s['moves'] if m['temporary'])
    adds = {'value': s['acquisitions'],
            'detail': ' · '.join(f"Week {w['week']}: {w['acquisitions_used_after']} of {w['acquisition_limit']} used"
                                 for w in used) + (f" · {streams} stream{'s' if streams != 1 else ''}" if streams else '')}
    net = (s.get('audit') or {}).get('summary', {}).get('net_changes') or [
        f'{d} → {a}' for d, a in zip(s['final_roster_changes']['dropped'], s['final_roster_changes']['added'])]
    why = [line.replace(' over the horizon', f" in {short['weeks']}") for line in s.get('why', [])
           if 'margin score' not in line]
    if s['moves']:
        why.append(f"Rest of season: {label.lower()} ({season['detail']} category wins over {season['weeks']} weeks)")
    added = {m['add_name'] for m in s['moves']}
    brief = ('Add ' + ', '.join(m['add_name'] + (' (stream)' if m['temporary'] else '') for m in s['moves']) +
             '; drop ' + ', '.join(m['drop_name'] for m in s['moves'] if m['drop_name'] not in added)) if s['moves'] else ''
    yahoo = YAHOO.get(s['eligibility_class'], (s['eligibility_class'], 'warn', ''))
    return {'title': _title(s), 'brief': brief, 'why': why, 'kind': s['kind'] if s['moves'] else 'Baseline', 'short_term': short,
            'season': season, 'acquisitions': adds,
            'permanent': {'value': len(s['final_roster_changes']['added']), 'pairs': net},
            'timeline': _timeline(s), 'sources': src, 'risks': _risks(s, src, season),
            'yahoo': {'label': yahoo[0], 'tone': yahoo[1], 'text': yahoo[2],
                      'gaps': [verification_text(c) for c in s.get('missing_verification', [])]},
            'why_not': s.get('why_not', [])}


def _why_consider(s: dict, top: dict) -> list[str]:
    d, t, out = s['display'], top['display'], []
    if s['acquisitions'] < top['acquisitions']:
        n = top['acquisitions'] - s['acquisitions']
        out.append(f"Uses {n} fewer acquisition{'s' if n > 1 else ''}")
    if d['short_term']['value'] >= t['short_term']['value'] and d['short_term']['value'] > 0:
        out.append('Same short-term gain' if d['short_term']['value'] == t['short_term']['value'] else 'More short-term gain')
    if d['permanent']['value'] < t['permanent']['value']:
        out.append('Fewer permanent roster changes')
    kept = [p for p in top['final_roster_changes']['dropped'] if p not in s['final_roster_changes']['dropped']]
    if kept:
        out.append('Keeps ' + ' and '.join(kept))
    rank = {'consistent': 0, 'partial': 1, 'unchecked': 2, 'mixed': 3}
    if rank[d['season']['agreement']] < rank[t['season']['agreement']] and d['season']['tone'] in ('good', 'neutral'):
        out.append('Projection sources agree more')
    if any(r['label'] == 'Long-term asset downgrade' for r in t['risks']) and \
            not any(r['label'] == 'Long-term asset downgrade' for r in d['risks']):
        out.append('No asset downgrade')
    if d['season']['blended'] > t['season']['blended']:
        out.append('Stronger rest of season on the selected projections')
    return out


def present(advice: dict) -> dict:
    """Attach `display` blocks in place and return the advice."""
    hold = [advice['hold']] if advice.get('hold') else []
    for s in [*advice.get('strategies', []), *hold, *advice.get('closest_alternatives', [])]:
        s['display'] = describe(s)
    strategies = advice.get('strategies', [])
    for s in strategies[1:]:
        s['display']['why_consider'] = _why_consider(s, strategies[0])
    n = len(strategies)
    advice['display'] = {
        'headline': (f"{n} strateg{'y' if n == 1 else 'ies'} beat holding your roster" if n else
                     'Hold: no move clears the bar'),
        'matchups': [{'week': w['week'], 'opponent': w['opponent_name'], 'record': _record(w['projected']),
                      'days': w['days_remaining'], 'adds_left': w['acquisitions_available']}
                     for w in advice.get('horizon_weeks', [])],
        'data_gaps': [verification_text(c) for c in advice.get('evidence_reasons', [])],
    }
    return advice


# --- Trade Lab: presentation only --------------------------------------------------------------
# `trade_briefs(result)` returns a copy of a `trade_lab()` result whose cards each carry an additive `brief`
# block built only from fields the optimizer already computed. Ordering, scores, tiers and labels are untouched.

MARKET_EVEN_PCT = 5.0   # display band only: within ±5% of the larger side reads as "about even"
MARKET_BASIS = ('Yahoo preseason ADP and FantasyPros consensus rank, each mapped to the same 0–100 curve and '
                'averaged; a side counts its best player in full and each other player at half.')
RANK_WORDS = {   # part: (when it lifts a card, when it holds a card back)
    'robust_gain': ('a larger gain for you', 'a smaller gain for you'),
    'partner_benefit': ('a better result for them', 'a worse result for them'),
    'market_cost': ('less market value given up', 'more market value given up'),
    'plausibility': ('a better market balance for them', 'a worse market balance for them'),
    'near_term': ('more help in the next three weeks', 'less help in the next three weeks'),
    'sources': ('stronger source agreement', 'weaker source agreement'),
    'complexity': ('a simpler package', 'a more complex package'),
    'simpler_option': ('no simpler 1-for-1 doing the same', 'a simpler 1-for-1 alternative'),
}


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _num(x: float) -> str:
    return f"{x:g}" if float(x).is_integer() else f"{x:.1f}"


def _trade_player(p: dict) -> dict:
    return {'id': p['site_player_id'], 'name': p['name'], 'pos': '/'.join(p.get('positions') or ()),
            'team': p.get('nba_team'), 'adp': p.get('adp'), 'ecr': p.get('ecr'), 'market': p.get('market_value'),
            'status': p.get('yahoo_status'), 'slot': p.get('slot')}


def _trade_market(t: dict) -> dict:
    v, m = t['value'], t['value']['market']
    pct, cost, gain = -100 * v['gap'], m['my_cost'], t['ranking']['robust_gain']
    if t['display']['label'] == 'Expensive improvement':
        band, text = 'expensive', 'High market cost for the gain'
    elif pct >= MARKET_EVEN_PCT:
        band, text = 'more', 'They receive more market value'
    elif pct <= -MARKET_EVEN_PCT:
        band, text = 'less', 'They receive less market value'
    else:
        band, text = 'even', 'About even by market value'
    return {'band': band, 'text': text, 'points': m['partner_surplus'], 'pct': pct, 'give': m['give'],
            'get': m['get'], 'my_cut': m.get('my_cut', 0), 'partner_cut': m.get('partner_cut', 0), 'my_cost': cost,
            'cost_per_win': cost / max(gain, .1) if cost > 0 else 0.0, 'signals_disagree': bool(m.get('signals_disagree')),
            'basis': MARKET_BASIS if v.get('basis') == 'market_consensus' else
            'Projection-source ranks (no market ranking capture was available).'}


def _trade_sources(t: dict) -> dict:
    rows = [{'label': x['label'], 'me': x.get('me_adjusted', x['me_points']), 'partner': x['partner_points']}
            for x in (t.get('sources') or {}).values() if not x.get('unavailable')]
    ok = [r for r in rows if r['me'] > 0]
    if not rows:
        state, text = 'unchecked', 'Not checked by source'
    elif len(ok) == len(rows):
        state, text = 'agree', 'Both sources show a gain' if len(rows) > 1 else f"{rows[0]['label']} shows a gain"
    elif ok:
        state, text = 'split', f"Only {ok[0]['label']} shows a gain"
    else:
        state, text = 'fails', 'Neither source shows a gain'
    return {'state': state, 'text': text, 'rows': rows}


def _trade_roster(t: dict) -> list[dict]:
    out = []
    for side in ('me', 'partner'):
        b = (t.get('balancing') or {}).get(side) or {'kind': 'none'}
        mine, who = side == 'me', ('You' if side == 'me' else t['partner']['team_name'])
        s, fill = ('' if mine else 's'), b.get('fill') or {}
        if b['kind'] == 'drop':
            out.append({'side': side, 'text': f"{who} drop{s} {b['player']['name']}"})
        elif b['kind'] == 'il':
            out.append({'side': side, 'text': f"{who} move{s} {b['player']['name']} to IL (eligibility unverified)"})
        elif b['kind'] == 'open' and fill.get('used'):
            out.append({'side': side, 'text': f"{who} fill{s} the open spot with free agent {fill['player']['name']}",
                        'conditional': 'their adds unverified' if not mine else 'needs an add that week'})
        elif b['kind'] == 'open':
            out.append({'side': side, 'text': f"{who} leave{s} {b.get('open_spots', 1)} roster spot open"})
    return out


def _half_up(x: float, d: int = 1) -> float:
    """Round half away from zero, matching the page's toFixed() so card text and tiles agree (−3.25 → −3.3)."""
    from decimal import ROUND_HALF_UP, Decimal
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP))


def _sg(x: float, d: int = 1) -> str:
    v = _half_up(x, d)
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.{d}f}"


def _trade_why(t: dict, market: dict, weeks: int) -> str:
    me, d = t['me'], t['display']
    up = sorted(((v, k) for k, v in me['category_delta'].items() if v > 0), reverse=True)
    down = sorted((v, k) for k, v in me['category_delta'].items() if v < 0)
    weak = [k for v, k in up[:3] if (me['strength_before'].get(k) or 0) >= 6]
    parts = []
    if up or down:
        bits = [f"+{_num(v)} {LABELS[k]}" + (f" (you rank {_ordinal(me['strength_before'][k])})" if k in weak[:1] else '')
                for v, k in up[:3]] + [f"−{_num(-v)} {LABELS[k]}" for v, k in down[:2]]
        parts.append(f"Weekly category results over {weeks} weeks: {', '.join(bits)}.")
    P, pts, pct = d['partner'], market['points'], abs(market['pct'])
    top = max(t['gives_all'], key=lambda p: p.get('market_value') or 0)['name']
    if market['band'] == 'expensive':
        parts.append(f"You give up {market['my_cost']:.0f} market points, {market['cost_per_win']:.0f} per win.")
    elif market['band'] == 'more':
        parts.append(f"{P} receives {pts:.0f} more market points ({pct:.0f}%), led by {top}.")
    elif market['band'] == 'less':
        parts.append(f"{P} gives up {-pts:.0f} market points ({pct:.0f}%).")
    else:
        parts.append(f"By market value the sides are within {max(pct, 1):.0f}%.")
    change = t['effective']['partner']
    if change >= .5:
        parts.append(f"They also project {_sg(change)} category wins" +
                     (f", mostly {' and '.join(d.get('partner_gained', [])[:2])}." if d.get('partner_gained') else '.'))
    elif change < 0:
        parts.append(f"The model has them {_half_up(-change):.1f} category wins worse.")
    return ' '.join(parts)


def _trade_natural(t: dict, part: str, b: dict) -> tuple[str, str]:
    return {'robust_gain': (_sg(t['ranking']['robust_gain']), 'wins for you'),
            'partner_benefit': (_sg(t['effective']['partner']), 'wins for them'),
            'market_cost': (f"{max(0.0, b['market']['my_cost']):.0f}", 'market points'),
            'plausibility': (f"{_sg(b['market']['pct'], 0)}%", 'market value to them'),
            'near_term': (_sg(t['me']['near_term_delta']), 'wins in three weeks'),
            'sources': (b['sources']['text'].lower(), ''),
            'complexity': (f"{len(t['gives_all']) + len(t['receives_all'])} players, {len(b['roster'])} moves", ''),
            'simpler_option': ('simpler 1-for-1' if t.get('simpler_alternative') else 'none', '')}[part]


def _trade_rank_reason(t: dict, nxt: dict, i: int, bt: dict, bn: dict) -> str:
    diff = {k: t['ranking'].get(k, 0) - nxt['ranking'].get(k, 0) for k in RANK_WORDS}
    up, down = max(diff, key=diff.get), min(diff, key=diff.get)

    def pair(k):
        (a, unit), (b, _) = _trade_natural(t, k, bt), _trade_natural(nxt, k, bn)
        return f"{a} vs {b}" + (f" {unit}" if unit else '')
    text = f"Ranked above #{i + 2} for {RANK_WORDS[up][0]} ({pair(up)})"
    if diff[down] <= -.25:
        text += f", despite {RANK_WORDS[down][1]} ({pair(down)})"
    return text + '.'


def trade_briefs(result: dict) -> dict:
    """Copy of a Trade Lab result with an additive `brief` on each card. Never mutates `result`."""
    cards = result.get('cards') or []
    weeks = (result.get('timing') or {}).get('remaining_weeks') or []
    effective = (result.get('timing') or {}).get('earliest_effective_day')
    briefs = []
    for t in cards:
        b = {'send': [_trade_player(p) for p in t['gives_all']], 'receive': [_trade_player(p) for p in t['receives_all']],
             'gain': {'robust': t['ranking']['robust_gain'], 'blended': t['effective']['me'],
                      'next_three': t['me']['near_term_delta'], 'weeks': len(weeks),
                      'first_week': weeks[0] if weeks else None, 'last_week': weeks[-1] if weeks else None,
                      'effective_day': effective},
             'partner': {'change': t['effective']['partner'], 'gained': t['display'].get('partner_gained', []),
                         'lost': t['display'].get('partner_lost', []), 'tension': t['effective']['partner'] < 0},
             'market': _trade_market(t), 'sources': _trade_sources(t), 'roster': _trade_roster(t),
             'narrow_gains': sum(1 for f in t['me']['flips'] if f['narrow'] and f['delta'] > 0)}
        b['why'] = _trade_why(t, b['market'], len(weeks))
        briefs.append(b)
    out = []
    for i, (t, b) in enumerate(zip(cards, briefs)):
        b['rank_reason'] = (_trade_rank_reason(t, cards[i + 1], i, b, briefs[i + 1]) if i + 1 < len(cards) else None)
        ids = {p['id'] for p in (*b['send'], *b['receive'])}
        b['shares'] = [{'card': j + 1, 'players': [p['name'] for p in (*o['send'], *o['receive']) if p['id'] in ids]}
                       for j, o in enumerate(briefs) if j != i and ids & {p['id'] for p in (*o['send'], *o['receive'])}]
        out.append({**t, 'brief': b})
    return {**result, 'cards': out}
