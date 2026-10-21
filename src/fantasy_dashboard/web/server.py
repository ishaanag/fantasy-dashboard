"""Local, read-only dashboard server: the page (Overview, Matchups, Waiver Advisor, Trade Lab) and its JSON routes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse

from fantasy_dashboard.advisor import AdvisorConfig, advise
from fantasy_dashboard.inputs import MATCHUPS, REPORTS, ROOT, load_inputs
from fantasy_dashboard.league import parse_utc
from fantasy_dashboard.model.forecast import forecast, resolve_actual_state
from fantasy_dashboard.model.lineups import rank_league
from fantasy_dashboard.trades import TradeConfig, trade_lab
from fantasy_dashboard.trades import verification as trade_verification
from fantasy_dashboard.web.presentation import present, trade_briefs
from fantasy_dashboard.yahoo.transactions import ACTUAL_NAME, resolve_evidence

STATIC = Path(__file__).parent / 'static'
# The page and its assets, read once at import: path -> (MIME type, bytes).
ASSETS = {'/': ('text/html; charset=utf-8', (STATIC / 'index.html').read_bytes()),
          '/static/app.css': ('text/css; charset=utf-8', (STATIC / 'app.css').read_bytes()),
          '/static/app.js': ('text/javascript; charset=utf-8', (STATIC / 'app.js').read_bytes())}
JSON = 'application/json; charset=utf-8'
TRADE_TYPES = ('1-1', '2-1', '1-2', '2-2')


def load_captured_actual(inputs: dict, reports: Path):
    """Use captured actual state only when bound to the same capture; the forecast re-validates it."""
    path = reports / ACTUAL_NAME
    if not path.exists():
        return None, 'not_captured'
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('as_of_utc') != inputs['availability']['captured_at_utc']:
        return None, 'stale_not_bound_to_capture'
    if value.get('status') != 'captured':
        return None, 'rejected: ' + ', '.join(value.get('problems', []))[:300]
    if value.get('league_id') is not None and value['league_id'] != inputs['league']['league_id']:
        return None, 'rejected: actual_league_mismatch'
    if value.get('team_ids') is not None and set(value['team_ids']) != set(inputs['teams']):
        return None, 'rejected: actual_team_ids_mismatch'
    for key in ('player_fingerprint', 'roster_fingerprint'):
        if value.get(key) is not None and value[key] != inputs['availability'].get(key):
            return None, 'rejected: actual_capture_fingerprint_mismatch'
    completed = value.get('completed_weeks', 0)
    if completed:
        sources = value.get('sources', {})
        league = inputs['league']['league_id']
        expected = [f'/nba/{league}?lhst=matchups&matchup_week={w}&module=matchups'
                    for w in range(1, completed + 1)]
        if (sources.get('standings') != f'/nba/{league}?lhst=stand&module=standings' or
                sources.get('matchups') != expected):
            return None, 'rejected: completed_result_source_lineage_missing'
    try:
        resolve_actual_state(inputs, parse_utc(value['as_of_utc']), value)
    except (ValueError, KeyError, TypeError) as exc:
        return None, 'rejected: ' + str(exc)[:300]
    return value, 'captured'


def _id_list(query: dict, name: str) -> tuple:
    return tuple(sorted(x for x in query.get(name, [''])[0].split(',') if x))


def make_handler(inputs: dict, actual_state: dict | None = None, matchup_path: Path | None = None,
                 transaction_evidence: dict | None = None, yahoo_status: dict | None = None,
                 root: Path | None = None, refresher=None):
    refresh_lock = threading.Lock()

    def refresh_yahoo():
        """Read-only capture, then reload. On any failure the previous snapshot stays in use."""
        nonlocal inputs, transaction_evidence, yahoo_status, actual_state
        if not refresh_lock.acquire(blocking=False):
            raise RuntimeError('A Yahoo refresh is already running')
        try:
            if refresher is None or root is None:
                raise RuntimeError('Yahoo refresh needs the local dashboard root and browser dependency')
            refresher()
            fresh = load_inputs(root)
            evidence, status = resolve_evidence(fresh, root / REPORTS)
            actual, status['actual_state'] = load_captured_actual(fresh, root / REPORTS)
            inputs, transaction_evidence, yahoo_status, actual_state = fresh, evidence, status, actual
            cached_deterministic.cache_clear()
            cached_trades.cache_clear()
            return status
        finally:
            refresh_lock.release()

    def captured_as_of():
        timestamp = actual_state.get('as_of_utc') if actual_state else None
        return timestamp, parse_utc(timestamp) if timestamp else None

    @lru_cache(maxsize=8)
    def cached_deterministic(weight: float, timestamp: str | None):
        return forecast(inputs, matchup_path=matchup_path, hashtag_weight=weight,
                        as_of=parse_utc(timestamp) if timestamp else None, actual_state=actual_state)

    @lru_cache(maxsize=16)   # one entry per team/type toggle combination
    def cached_trades(weight: float, timestamp: str | None, partners: tuple, protect: tuple, types: tuple = ()):
        bases = {}
        for source_weight in (1.0, 0.0):   # shared with the page's source toggle cache
            try:
                bases[source_weight] = cached_deterministic(source_weight, timestamp)
            except (ValueError, KeyError):
                pass
        return trade_lab(inputs, team_id=inputs['league']['my_team_id'], weight=weight,
                         as_of=parse_utc(timestamp) if timestamp else None,
                         actual_state=actual_state, evidence=transaction_evidence, yahoo_status=yahoo_status,
                         config=TradeConfig(partner_ids=partners or None, protected_ids=frozenset(protect),
                                            **({'package_types': types} if types else {})),
                         base=cached_deterministic(weight, timestamp), source_bases=bases)

    def with_actual_state_reason(message: str) -> str:
        """Name the real cause when a forecast fails because the captured actual state was rejected."""
        reason = (yahoo_status or {}).get('actual_state') or ''
        if actual_state is None and reason.startswith('rejected'):
            message += f' (the captured actual state was {reason})'
        return message

    def deterministic_route(query):
        weight = float(query.get('hashtag_weight', ['0.5'])[0])
        timestamp = query.get('as_of_utc', [captured_as_of()[0]])[0]
        try:
            # Additive identity fields for the page; the cached forecast is not mutated.
            return {**cached_deterministic(weight, timestamp), 'my_team_id': inputs['league']['my_team_id'],
                    'league_name': inputs['league'].get('league_name')}
        except (ValueError, KeyError) as exc:
            raise ValueError(with_actual_state_reason(str(exc))) from exc

    def advisor_route(query):
        weight = float(query.get('hashtag_weight', ['0.5'])[0])
        horizon = query.get('horizon', ['auto'])[0]
        timestamp, as_of = captured_as_of()
        try:
            value = advise(inputs, team_id=inputs['league']['my_team_id'], weight=weight, as_of=as_of,
                           actual_state=actual_state, evidence=transaction_evidence,
                           matchup_path=matchup_path, now=datetime.now(timezone.utc),
                           config=AdvisorConfig(horizon=None if horizon == 'auto' else int(horizon)),
                           base=cached_deterministic(weight, timestamp))
        except (ValueError, KeyError) as exc:
            raise ValueError(with_actual_state_reason(str(exc))) from exc
        value['yahoo'] = yahoo_status
        return present(value)   # additive `display` labels; advisor values unchanged

    def trades_route(query):
        weight = float(query.get('hashtag_weight', ['0.5'])[0])
        types = ()
        if 'types' in query:
            asked = {x for x in query['types'][0].split(',') if x}
            types = tuple(t for t in TRADE_TYPES if t in asked)
            if not types or asked - set(types):
                raise ValueError('Choose trade types from 1-1, 2-1, 1-2 and 2-2')
        result = cached_trades(weight, captured_as_of()[0], _id_list(query, 'partners'), _id_list(query, 'protect'), types)
        # Cached analysis; verification is recomputed so evidence age stays current.
        return {**trade_briefs(result), 'yahoo': yahoo_status,
                'verification': trade_verification(inputs, transaction_evidence, yahoo_status, datetime.now(timezone.utc))}

    def rankings_route(query):
        weight = float(query.get('hashtag_weight', ['0.5'])[0])
        requested = query['week'][0] if 'week' in query else None
        week = requested if requested in {'regular', 'all'} else int(requested) if requested is not None else None
        as_of = parse_utc(query['as_of_utc'][0]) if 'as_of_utc' in query else None
        return rank_league(inputs, weight, week, as_of)

    routes = {'/api/deterministic': deterministic_route, '/api/advisor': advisor_route,
              '/api/trades': trades_route, '/api/rankings': rankings_route}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ASSETS:
                return self._send(200, *ASSETS[parsed.path])
            if parsed.path == '/api/yahoo/status':
                return self._json(200, yahoo_status or {'status': 'missing', 'reasons': ['yahoo_snapshot_missing']})
            route = routes.get(parsed.path)
            if route is None:
                return self._send(404, 'text/plain; charset=utf-8', b'Not found')
            try:
                value = route(parse_qs(parsed.query))
            except (ValueError, KeyError) as exc:
                return self._json(400, {'error': str(exc)})
            self._json(200, value, allow_nan=False)

        def do_POST(self):
            if urlparse(self.path).path != '/api/yahoo/refresh':
                return self._send(404, 'text/plain; charset=utf-8', b'Not found')
            try:
                value, status = refresh_yahoo(), 200
            except Exception as exc:  # report and keep the previous snapshot
                value, status = {'error': f'{type(exc).__name__}: {exc}', 'previous': yahoo_status}, 502
            self._json(status, value)

        def _json(self, status, value, allow_nan=True):
            self._send(status, JSON, json.dumps(value, ensure_ascii=False, allow_nan=allow_nan).encode('utf-8'))

        def _send(self, status, mime, content):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(content)
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description='Local, read-only Big Baller League dashboard')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--actual-state', type=Path, help='Verified current standings, weekly results and current-week totals JSON')
    parser.add_argument('--league-id', default='92103')
    parser.add_argument('--team-id', default='4')
    args = parser.parse_args()
    root = args.root.resolve()
    inputs = load_inputs(root)
    actual_state = json.loads(args.actual_state.read_text(encoding='utf-8')) if args.actual_state else None
    evidence, yahoo_status = resolve_evidence(inputs, root / REPORTS)
    if actual_state is None:
        actual_state, yahoo_status['actual_state'] = load_captured_actual(inputs, root / REPORTS)
    else:
        yahoo_status['actual_state'] = 'supplied_by_flag'

    def refresher():
        from fantasy_dashboard.yahoo.refresh import refresh  # needs the optional Playwright dependency
        refresh(root, args.league_id, team_id=args.team_id)

    server = ThreadingHTTPServer(('127.0.0.1', args.port),
                                 make_handler(inputs, actual_state, root / MATCHUPS, evidence, yahoo_status, root, refresher))
    print(f'Dashboard: http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
