"""Trade Lab benchmark (Phase 4B, viability updated in 4D): runtime and search coverage on the saved capture.

Usage: PYTHONPATH=backend .venv/bin/python backend/run_trade_benchmark.py [partner_id ...]
Compares the default staged search with (a) every plausible package replayed and (b) every package
replayed for the named partners (default 3 and 10), and reports recall of the best exact packages.
"""
import json
import sys
from time import perf_counter

from app.deterministic import forecast
from app.phase1 import ROOT, load_inputs
from app.trades import TradeConfig, trade_lab
from app.waiver import _utc


def top(rows, typ, k):
    cfg = TradeConfig()   # could qualify: material gain and not lopsided toward me by market value
    ok = [e for e in rows if e['package'] == typ and e['me_effective'] >= 1.5 and e['gap'] <= cfg.viable_market_limit]
    return [e['id'] for e in sorted(ok, key=lambda e: -e['score'])[:k]]


def recall(full, staged, k=10):
    kept = {e['id'] for e in staged}
    return {typ: f"{sum(i in kept for i in top(full, typ, k))}/{len(top(full, typ, k))}" for typ in ('1-1', '2-1', '1-2', '2-2')}


def main(partners):
    inputs = load_inputs(ROOT)
    as_of = _utc(inputs['availability']['captured_at_utc'])
    t0 = perf_counter()
    bases = {w: forecast(inputs, hashtag_weight=w, as_of=as_of) for w in (.5, 1.0, 0.0)}
    forecasts = perf_counter() - t0
    kw = dict(as_of=as_of, now=as_of, base=bases[.5], source_bases={1.0: bases[1.0], 0.0: bases[0.0]})
    out = {'forecast_seconds_three_sources': round(forecasts, 1)}
    default = trade_lab(inputs, **kw)
    out['default'] = {k: default['diagnostics'][k] for k in ('pairs_screened', 'plausible_after_screen', 'replayed',
                                                              'drop_options_replayed', 'source_checked', 'seconds', 'by_type')}
    plaus = trade_lab(inputs, config=TradeConfig(shortlist=10**6, multi_shortlist=10**6, finalists=0), **kw)
    out['all_plausible'] = {'replayed': plaus['diagnostics']['replayed'], 'seconds': plaus['diagnostics']['seconds'],
                            'recall_top10_of_default': recall(plaus['all_evaluated'], default['all_evaluated'])}
    for p in partners:
        cfg = dict(partner_ids=(p,), finalists=0)
        full = trade_lab(inputs, config=TradeConfig(shortlist=None, **cfg), **kw)
        staged = trade_lab(inputs, config=TradeConfig(**cfg), **kw)
        out[f'exhaustive_partner_{p}'] = {
            'replayed': full['diagnostics']['replayed'], 'seconds': full['diagnostics']['seconds']['total'],
            'staged_replayed': staged['diagnostics']['replayed'], 'staged_seconds': staged['diagnostics']['seconds']['total'],
            'recall_top5_of_staged': recall(full['all_evaluated'], staged['all_evaluated'], 5),
            'best_exact': {typ: (top(full['all_evaluated'], typ, 1) or [None])[0] for typ in ('1-1', '2-1', '1-2', '2-2')}}
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main(sys.argv[1:] or ['3', '10'])
