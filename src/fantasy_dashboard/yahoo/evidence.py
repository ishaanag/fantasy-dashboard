"""Transaction evidence and pool eligibility shared by the Waiver Advisor and Trade Lab.

`pool_audit` keys projection coverage by Yahoo site ID; `load_user_evidence` binds the dated pre-snapshot user
observations (the offline fallback); `evidence_reasons` names what keeps a plan provisional; `classify_plan`
assigns the eligibility class shown on every recommendation.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import json
from pathlib import Path

from fantasy_dashboard.league import EASTERN, parse_utc
from fantasy_dashboard.model.lineups import resolve_projection


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
    captured = parse_utc(inputs['availability']['captured_at_utc']).astimezone(EASTERN).date()
    if observed != captured:
        raise ValueError('User transaction evidence date differs from player capture date')
    return {**raw, 'status': 'user_supplied_partial',
            'player_fingerprint': raw['analyzed_player_capture_fingerprint'],
            'cannot_cut_site_ids': [by_name[name] for name in names],
            'adds_used': raw['weekly_acquisitions_used'],
            'remaining_adds': raw['weekly_acquisitions_limit'] - raw['weekly_acquisitions_used'],
            'empty_il_slots': raw['il_capacity'] - il_occupied}


def evidence_reasons(inputs, evidence, team_id, week, as_of):
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
    if parse_utc(evidence['captured_at_utc']) > as_of:
        reasons.append('transaction_evidence_after_as_of')
    if not isinstance(evidence['adds_used'], int) or not 0 <= evidence['adds_used'] <= 4:
        reasons.append('acquisition_count_invalid')
    return reasons


def classify_plan(plan: dict) -> str:
    """Verified current move, provisional move, or conditional future plan."""
    steps = plan.get('steps') or []
    missing = set(plan.get('missing_verification') or [])
    if not steps:
        return 'hold'
    if 'future_availability_conditional' in missing or any(s.get('future_availability_conditional') for s in steps):
        return 'conditional_future'
    return 'provisional' if missing else 'verified_current'
