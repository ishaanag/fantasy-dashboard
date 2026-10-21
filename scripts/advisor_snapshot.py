"""Run the Waiver Advisor on the saved live capture exactly as the dashboard does; print a summary.

Usage: .venv/bin/python scripts/advisor_snapshot.py [out.json]
"""
from datetime import datetime, timezone
import json
import sys
from time import perf_counter

from fantasy_dashboard.advisor.search import advise
from fantasy_dashboard.inputs import REPORTS, ROOT, load_inputs
from fantasy_dashboard.league import parse_utc
from fantasy_dashboard.web.server import load_captured_actual
from fantasy_dashboard.yahoo.transactions import resolve_evidence

inputs = load_inputs(ROOT)
evidence, _ = resolve_evidence(inputs, ROOT / REPORTS)
actual, _ = load_captured_actual(inputs, ROOT / REPORTS)
as_of = parse_utc(actual['as_of_utc']) if actual else None
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
