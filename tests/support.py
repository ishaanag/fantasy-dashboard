"""Shared test helpers: repository paths, the served page source and an in-process route call."""
from io import BytesIO
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED_REPORTS = ROOT / 'data' / 'reports' / 'archive-2026-10-20T1411Z'
FIXTURES = Path(__file__).parent / 'fixtures'
STATIC = ROOT / 'src' / 'fantasy_dashboard' / 'web' / 'static'
PAGE = '\n'.join((STATIC / name).read_text(encoding='utf-8') for name in ('index.html', 'app.css', 'app.js'))


def get_json(handler, path):
    """Call a GET route on a `make_handler` class without a socket; returns (status codes, decoded JSON)."""
    request = object.__new__(handler)
    request.path = path
    request.wfile, codes = BytesIO(), []
    request.send_response = codes.append
    request.send_header = lambda *args: None
    request.end_headers = lambda: None
    request.do_GET()
    return codes, json.loads(request.wfile.getvalue() or b'null')
