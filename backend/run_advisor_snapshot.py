"""Run the Waiver Advisor on the saved live capture exactly as the dashboard does; print a summary.

Usage: PYTHONPATH=backend .venv/bin/python backend/run_advisor_snapshot.py [out.json]
"""
import json
import sys
from datetime import datetime, timezone
from time import perf_counter

from app.advisor import advise
from app.dashboard import load_captured_actual
from app.phase1 import ROOT, load_inputs
from app.yahoo_transactions import resolve_evidence

inputs = load_inputs(ROOT)
evidence, _ = resolve_evidence(inputs, ROOT / 'data' / 'reports')
actual, _ = load_captured_actual(inputs, ROOT / 'data' / 'reports')
as_of = datetime.fromisoformat(actual['as_of_utc'].replace('Z', '+00:00')) if actual else None
t = perf_counter()
result = advise(inputs, weight=.5, as_of=as_of, actual_state=actual, evidence=evidence,
                now=datetime.now(timezone.utc))
print(f'elapsed {perf_counter() - t:.1f}s; capture {result["capture_at_utc"]}; horizon weeks',
      [w['week'] for w in result['horizon_weeks']])
for i, s in enumerate(result['strategies'], 1):
    print(i, s['kind'], ' | '.join(f"+{m['add_name']} -{m['drop_name']} {m['date']}" for m in s['moves']),
          'h', s['category_points_gained'], 'season', s['season']['category_points_delta'],
          s['season']['margin_score_delta'], 'rank', s['rank_value'])
if len(sys.argv) > 1:
    with open(sys.argv[1], 'w') as fh:
        json.dump(result, fh, indent=1, default=str)
