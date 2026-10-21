"""Phase 3A single-move audit and deterministic, read-only evaluation."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from math import exp, isfinite
import json
from pathlib import Path

from .deterministic import forecast
from .phase1 import CATEGORIES, EASTERN, RosterPlayer, _dated_team_totals, resolve_projection
from .simulator import _expected_with_actual, category_result


def _utc(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Transaction time needs an offset')
    return stamp.astimezone(timezone.utc)


def pool_audit(inputs: dict, weight: float) -> dict:
    """Coverage is keyed by Yahoo site ID; source rows are never guessed."""
    players = inputs.get('site_players', [])
    manual = inputs.get('pool_manual_review', [])
    ambiguous = {str(row.get('candidate_site_player_id')) for row in manual if row.get('candidate_site_player_id')}
    for row in manual:
        ambiguous.update(map(str, row.get('candidate_site_player_ids', [])))
    rows = []
    counts = Counter()
    for player in players:
        sources = inputs['projections'].get(player.site_player_id, {})
        issues = []
        if player.site_player_id in ambiguous:
            issues.append('ambiguous_mapping')
        if not sources:
            issues.append('missing_projection')
        elif any(source.gp == 0 for source in sources.values()):
            issues.append('gp_zero')
        try:
            if sources:
                projection = resolve_projection(sources, weight)
                if projection['values']['gp'] == 0 and 'gp_zero' not in issues:
                    issues.append('gp_zero')
            else:
                projection = None
        except ValueError:
            projection = None
            issues.append('selected_source_missing_or_invalid')
        if len(sources) == 1:
            issues.append('one_source_only')
        for issue in issues:
            counts[issue] += 1
        rows.append({'site_player_id': player.site_player_id, 'name': player.name,
                     'availability': player.availability_label, 'owner_team_id': player.fantasy_team_id,
                     'nba_team': player.nba_team, 'eligible_positions': player.eligible_positions,
                     'sources': sorted(sources), 'issues': issues, 'projectable': projection is not None and
                     not any(i in issues for i in ('ambiguous_mapping', 'gp_zero'))})
    return {'counts': dict(counts), 'rows': rows, 'player_count': len(rows),
            'snapshot_at_utc': inputs['availability']['captured_at_utc'],
            'pagination_verified': inputs['availability']['status'] == 'verified'}


def load_user_evidence(inputs: dict, path: Path) -> dict | None:
    """Bind dated user observations to already captured Yahoo site IDs."""
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding='utf-8'))
    team_id = inputs['league']['my_team_id']
    if (raw.get('schema_version') != 1 or raw.get('league_id') != inputs['league']['league_id'] or
            raw.get('team_id') != team_id or raw.get('team_name') != inputs['teams'][team_id] or
            raw.get('team_url') != f"https://basketball.fantasysports.yahoo.com/nba/{raw['league_id']}/{team_id}"):
        raise ValueError('User transaction evidence does not match captured Yahoo team')
    roster = inputs['rosters'][team_id]
    by_name = {p.name: p.site_player_id for p in roster}
    names = raw['cannot_cut_team_player_names']
    if len(names) != len(set(names)) or any(name not in by_name for name in names):
        raise ValueError('Cannot-cut names must match unique captured roster players')
    il_occupied = sum(p.assigned_slot == 'IL' for p in roster)
    if raw['observed_il_occupancy'] != il_occupied or raw['il_capacity'] != 3:
        raise ValueError('User IL occupancy differs from captured roster')
    if raw['weekly_acquisitions_limit'] != 4 or not 0 <= raw['weekly_acquisitions_used'] <= 4:
        raise ValueError('User acquisition count or league limit is invalid')
    observed = date.fromisoformat(raw['observed_date'])
    captured = _utc(inputs['availability']['captured_at_utc']).astimezone(EASTERN).date()
    if observed != captured:
        raise ValueError('User transaction evidence date differs from player capture date')
    return {**raw, 'status': 'user_supplied_partial',
            'player_fingerprint': raw['analyzed_player_capture_fingerprint'],
            'cannot_cut_site_ids': [by_name[name] for name in names],
            'adds_used': raw['weekly_acquisitions_used'],
            'remaining_adds': raw['weekly_acquisitions_limit'] - raw['weekly_acquisitions_used'],
            'empty_il_slots': raw['il_capacity'] - il_occupied}


def _evidence_reasons(inputs, evidence, team_id, week, as_of):
    if evidence is None:
        return ['transaction_evidence_missing']
    reasons = []
    if evidence.get('status') == 'user_supplied_partial':
        if (evidence.get('team_id') != team_id or evidence.get('league_id') != inputs['league']['league_id'] or
                evidence.get('player_fingerprint') != inputs['availability'].get('player_fingerprint') or
                date.fromisoformat(evidence['observed_date']) != as_of.astimezone(EASTERN).date()):
            return ['transaction_evidence_stale_or_mismatched']
        if week != 1:
            return ['transaction_evidence_wrong_week']
        return ['evidence_time_day_precision', 'ownership_refresh_needed',
                'player_level_restrictions_unverified', 'active_claims_unverified']
    required = ('captured_at_utc', 'player_fingerprint', 'team_id', 'week', 'adds_used',
                'cannot_cut_site_ids', 'locked_site_ids', 'timing_by_site_id',
                'empty_slot_allowed', 'direct_il_site_ids', 'il_eligible_site_ids',
                'active_claim_site_ids')
    if any(key not in evidence for key in required):
        reasons.append('transaction_evidence_incomplete')
        return reasons
    if (evidence['captured_at_utc'] != inputs['availability']['captured_at_utc'] or
            evidence['player_fingerprint'] != inputs['availability'].get('player_fingerprint') or
            evidence['team_id'] != team_id or evidence['week'] != week):
        reasons.append('transaction_evidence_stale_or_mismatched')
    if _utc(evidence['captured_at_utc']) > as_of:
        reasons.append('transaction_evidence_after_as_of')
    if not isinstance(evidence['adds_used'], int) or not 0 <= evidence['adds_used'] <= 4:
        reasons.append('acquisition_count_invalid')
    return reasons


OUT_STATUSES = {'O', 'OFS', 'INJ'}


def classify_plan(plan: dict) -> str:
    """Verified current move, provisional move, or conditional future plan."""
    steps = plan.get('steps') or []
    missing = set(plan.get('missing_verification') or [])
    if not steps:
        return 'hold'
    if 'future_availability_conditional' in missing or any(s.get('future_availability_conditional') for s in steps):
        return 'conditional_future'
    return 'provisional' if missing else 'verified_current'


def _margin(a, b, category):
    x, y = a.get(category), b.get(category)
    if x is None or y is None:
        return None
    return y-x if category == 'turnovers' else x-y


def _smooth(margins, scales):
    # Fixed per-category floors keep zero-dispersion fixtures finite. Heuristic only.
    return sum(1/(1+exp(-max(-8, min(8, margin / max(scales.get(cat, 1),
                     .001 if cat.endswith('_pct') else .1)))))
               for cat, margin in margins.items() if margin is not None)


def _scales(base):
    result = {}
    for cat in CATEGORIES:
        values = [p['totals'][team]['categories'][cat] for p in base['periods'].values()
                  if isinstance(p, dict) and 'totals' in p for team in p['totals']]
        values = sorted(v for v in values if v is not None and isfinite(v))
        result[cat] = max((values[3*len(values)//4] - values[len(values)//4]) if len(values) >= 4 else 0,
                          .001 if cat.endswith('_pct') else .1)
    return result


def evaluate_singles(inputs: dict, *, team_id: str, weight: float = .5, as_of=None,
                     actual_state=None, evidence=None, matchup_path=None,
                     add_ids=None, drop_ids=None, now=None, freshness_minutes=15) -> dict:
    """Enumerate all selected Yahoo pool x roster moves; evaluate only verified legal moves."""
    as_of = as_of or _utc(inputs['availability']['captured_at_utc'])
    if as_of.tzinfo is None:
        raise ValueError('as_of must include a timezone')
    as_of = as_of.astimezone(timezone.utc)
    if team_id not in inputs['rosters'] or team_id != inputs['league']['my_team_id']:
        raise ValueError('Use the authenticated Yahoo manager team ID')
    base = forecast(inputs, matchup_path=matchup_path, hashtag_weight=weight,
                    as_of=as_of, actual_state=actual_state)
    current_week = base['actual_completed_weeks'] + 1
    roster = inputs['rosters'][team_id]
    roster_ids = {p.site_player_id for p in roster}
    audit = pool_audit(inputs, weight)
    by_id = {p.site_player_id: p for p in inputs.get('site_players', [])}
    audit_by_id = {r['site_player_id']: r for r in audit['rows']}
    common = _evidence_reasons(inputs, evidence, team_id, current_week, as_of)
    partial = bool(evidence and evidence.get('status') == 'user_supplied_partial')
    if evidence and evidence.get('adds_used', 4) >= 4:
        common.append('acquisition_budget_exhausted')
    stale = []
    if evidence and not partial and not common and now is not None:
        age = now - _utc(evidence['captured_at_utc'])
        if age < timedelta() or age > timedelta(minutes=freshness_minutes):
            stale.append('transaction_evidence_not_fresh_for_action')
    statuses = evidence.get('status_by_site_id', {}) if evidence else {}
    if add_ids is None:
        adds = [p.site_player_id for p in by_id.values() if p.availability_label in {'FA', 'W'}]
    else:
        adds = list(add_ids)
    choices = [None, *(p.site_player_id for p in roster)] if drop_ids is None else list(drop_ids)
    scales = _scales(base)
    base_matchups = {m['week']: m for m in base['weekly_matchups'] if team_id in m['teams']}
    base_record = next(t['projected_record'] for t in base['teams'] if t['team_id'] == team_id)
    results = [{'add_site_player_id': None, 'drop_site_player_id': None, 'feasible': True,
                'actionable': True, 'reason_codes': [], 'label': 'HOLD', 'budget_used': 0, 'eligibility_class': 'hold',
                'remaining_adds': None if not evidence else 4-evidence['adds_used'],
                'season_record': base_record, 'current_week': base_matchups[current_week]['score'] if base_matchups[current_week]['teams'][0] == team_id else [base_matchups[current_week]['score'][1], base_matchups[current_week]['score'][0], base_matchups[current_week]['score'][2]]}]
    for add_id in adds:
        player = by_id.get(add_id)
        row = audit_by_id.get(add_id)
        for drop_id in choices:
            soft = list(common) if partial else list(stale)
            reasons = [] if partial else list(common)
            if player is None:
                reasons.append('not_in_complete_yahoo_pool')
            elif player.fantasy_team_id or player.availability_label not in {'FA', 'W'} or add_id in roster_ids:
                reasons.append('owned_or_unavailable')
            if player and player.nba_team not in inputs['schedule'].team_counts:
                reasons.append('nba_team_schedule_missing')
            if row and not row['projectable']:
                reasons.extend(i for i in row['issues'] if i != 'one_source_only')
            if drop_id is not None and drop_id not in roster_ids:
                reasons.append('drop_not_on_roster')
            if evidence and (partial or not common):
                if drop_id in evidence['cannot_cut_site_ids']:
                    reasons.append('cannot_cut')
                if not partial and drop_id in evidence['locked_site_ids']:
                    reasons.append('lineup_locked')
                if not partial and add_id in evidence['active_claim_site_ids']:
                    reasons.append('active_claim')
                if partial and drop_id in roster_ids:
                    drop_player = next(p for p in roster if p.site_player_id == drop_id)
                    game = inputs['schedule'].team_dates.get((drop_player.nba_team, as_of.astimezone(EASTERN).date()))
                    if (drop_player.assigned_slot not in {'BN', 'IL'} and game and game.tipoff_utc <= as_of):
                        reasons.append('lineup_locked')
            timing = evidence.get('timing_by_site_id', {}).get(add_id) if evidence else None
            effective = None
            assumptions = []
            if player and not reasons and partial:
                effective = as_of if player.availability_label == 'FA' else as_of + timedelta(days=1)
                soft.append('effective_timing_unverified')
                assumptions.append('Hypothetical FA effective now' if player.availability_label == 'FA' else
                                   'Hypothetical waiver effective 24 hours after as-of; one-day rule does not establish processing time or claim success')
            elif player and not reasons:
                if not timing or timing.get('kind') != ('waiver' if player.availability_label == 'W' else 'free_agent'):
                    soft.append('effective_timing_unverified')
                else:
                    try:
                        effective = _utc(timing['effective_at_utc'])
                    except (KeyError, ValueError, TypeError):
                        soft.append('effective_timing_unverified')
                    if player.availability_label == 'W' and timing.get('priority_verified') is not True:
                        soft.append('waiver_priority_unverified')
                if effective is None:
                    # Provisional model boundary, never a claimed Yahoo processing time.
                    effective = as_of if player.availability_label == 'FA' else as_of + timedelta(days=1)
                    assumptions.append('Hypothetical effective time; Yahoo did not show one')
            if player and statuses.get(add_id) in OUT_STATUSES:
                soft.append('candidate_status_out')
            placement = 'replace'
            if drop_id is None and player and evidence and (partial or not common):
                active = sum(p.assigned_slot != 'IL' for p in roster)
                il = len(roster)-active
                if active < 14 and evidence.get('empty_slot_allowed') is True:
                    placement = 'empty_slot'
                elif il < 3 and add_id in evidence.get('direct_il_site_ids', []) and add_id in evidence.get('il_eligible_site_ids', []):
                    placement = 'direct_il'
                else:
                    reasons.append('direct_il_eligibility_unverified' if partial and il < 3 else 'no_verified_empty_or_il_slot')
            if drop_id is not None and drop_id in roster_ids and player:
                drop = next(p for p in roster if p.site_player_id == drop_id)
                active_after = sum(p.assigned_slot != 'IL' for p in roster) - (drop.assigned_slot != 'IL') + 1
                if active_after > 14:
                    reasons.append('active_roster_full')
            item = {'add_site_player_id': add_id, 'add_name': player.name if player else None,
                    'drop_site_player_id': drop_id, 'drop_name': next((p.name for p in roster if p.site_player_id == drop_id), None),
                    'availability': player.availability_label if player else None,
                    'projection_issues': row['issues'] if row else ['missing_pool_row'],
                    'feasible': not reasons, 'actionable': not reasons and not soft,
                    'eligibility_class': 'infeasible' if reasons else 'provisional' if soft else 'verified_current',
                    'add_status': statuses.get(add_id, '') if player else None,
                    'reason_codes': sorted(set(reasons + soft)), 'assumptions': assumptions,
                    'placement': placement, 'effective_at_utc': effective.isoformat().replace('+00:00','Z') if effective else None,
                    'budget_used': 1, 'remaining_adds': 4-evidence['adds_used']-1 if evidence and (partial or not common) else None}
            if reasons:
                results.append(item)
                continue
            new_player = RosterPlayer(team_id, add_id, player.name, 'IL' if placement == 'direct_il' else 'BN',
                                      player.eligible_positions, player.nba_team)
            scenario = list(roster) + [new_player]
            projected = [(p, resolve_projection(inputs['projections'][p.site_player_id], weight)) for p in scenario]
            baseline_projected = [(p, resolve_projection(inputs['projections'][p.site_player_id], weight)) for p in roster]
            records = list(base_record)
            weeks = {}
            for week in range(current_week, 19):
                m = base_matchups[week]
                opponent = m['teams'][1] if m['teams'][0] == team_id else m['teams'][0]
                raw, detail, days = _dated_team_totals(scenario, projected, inputs['schedule'], inputs['weeks'][week-1],
                                                         as_of, date.fromisoformat(inputs['availability']['lineup_date']),
                                                         available_from_by_id={add_id: effective},
                                                         available_until_by_id={drop_id: effective} if drop_id else None, injury_inputs=inputs)
                if week == current_week and actual_state:
                    raw = _expected_with_actual(raw, actual_state.get('current_week_volumes', {}).get(team_id))
                other = m['totals'][opponent]
                score = list(category_result(raw['categories'], other['categories'])[:3])
                if m['teams'][0] != team_id:
                    score = [score[0], score[1], score[2]]
                before = m['score'] if m['teams'][0] == team_id else [m['score'][1], m['score'][0], m['score'][2]]
                records = [records[i]+score[i]-before[i] for i in range(3)]
                margins = {cat: _margin(raw['categories'], other['categories'], cat) for cat in CATEGORIES}
                old_margins = {cat: _margin(m['totals'][team_id]['categories'], other['categories'], cat) for cat in CATEGORIES}
                old_raw, old_detail, old_days = _dated_team_totals(roster, baseline_projected, inputs['schedule'],
                                                inputs['weeks'][week-1], as_of, date.fromisoformat(inputs['availability']['lineup_date']), injury_inputs=inputs)
                starts = {(day['date'], s['site_player_id']) for day in days for s in day['modeled_starts']}
                old_starts = {(day['date'], s['site_player_id']) for day in old_days for s in day['modeled_starts']}
                weeks[str(week)] = {'opponent_team_id': opponent, 'before': before, 'after': score,
                                    'delta_category_points': score[0]+.5*score[2]-before[0]-.5*before[2],
                                    'categories': {cat: {'before': m['totals'][team_id]['categories'][cat],
                                                         'after': raw['categories'][cat],
                                                         'delta': None if raw['categories'][cat] is None or m['totals'][team_id]['categories'][cat] is None else raw['categories'][cat]-m['totals'][team_id]['categories'][cat],
                                                         'directed_margin_before': old_margins[cat],
                                                         'directed_margin_after': margins[cat]} for cat in CATEGORIES},
                                    'smooth_margin_before': _smooth(old_margins, scales),
                                    'smooth_margin_after': _smooth(margins, scales),
                                    'new_starts': sorted(starts-old_starts), 'displaced_starts': sorted(old_starts-starts)}
            item.update({'weeks': weeks, 'season_record': records,
                         'season_delta_category_points': records[0]+.5*records[2]-base_record[0]-.5*base_record[2],
                         'conditional_season_assumption': 'Added player stays; no later transactions or availability changes.'})
            results.append(item)
    results.sort(key=lambda r: (r.get('feasible') is not True,
                -r.get('weeks', {}).get(str(current_week), {}).get('delta_category_points', 0),
                -r.get('weeks', {}).get(str(current_week), {}).get('smooth_margin_after', 0)))
    return {'as_of_utc': base['as_of_utc'], 'capture_at_utc': audit['snapshot_at_utc'],
            'team_id': team_id, 'team_name': inputs['teams'][team_id], 'week': current_week,
            'next_week': current_week+1 if current_week < 18 else None,
            'hashtag_weight': weight, 'pool_audit': {'counts': audit['counts'], 'player_count': audit['player_count']},
            'pool_rows': audit['rows'],
            'evidence_status': 'user_supplied_provisional' if partial else 'verified_transaction_evidence' if not common else 'unverified',
            'missing_evidence': (evidence.get('missing_evidence', []) if evidence else []) + stale,
            'evidence_reasons': common, 'evidence_source': evidence.get('source') if evidence else None,
            'evidence_observed_date': evidence.get('observed_date') if evidence else None,
            'evidence_time_precision': evidence.get('time_precision') if evidence else None,
            'il_eligible_status_rule': evidence.get('user_supplied_il_eligible_statuses') if evidence else None,
            'remaining_adds': 4-evidence['adds_used'] if evidence else None,
            'empty_il_slots': evidence.get('empty_il_slots') if evidence else None,
            'waiver_priority': evidence.get('waiver_priority') if evidence else None,
            'waiver_period_days': evidence.get('waiver_period_days') if evidence else None,
            'scales': scales, 'rows': results}
