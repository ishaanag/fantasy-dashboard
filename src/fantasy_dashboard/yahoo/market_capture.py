"""Read-only capture of Yahoo's public Draft Analysis page (average draft position, standard scoring).

The Trade Lab judges fairness the way another manager is likely to: by preseason ADP. Yahoo's
league player list cannot be used for that once games start, because its default order becomes
season-to-date stats. This page is public, is only read, and its Preseason column is fixed once
the season begins, so a capture is refreshed at most weekly.

Usage: .venv/bin/python -m fantasy_dashboard.yahoo.market_capture [--ecr-only]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from fantasy_dashboard.inputs import BROWSER_PROFILE, REPORTS, ROOT

ADP_NAME = "yahoo-draft-analysis.json"
ADP_URL = "https://basketball.fantasysports.yahoo.com/nba/draftanalysis?type=standard"
MAX_AGE_DAYS = 7

# Pages through the table with its "next" arrow; stops after two pages without any drafted player.
ADP_JS = r"""async () => {
  const table = () => document.querySelector('table');
  const footer = () => { const t = table(); return t && (t.nextElementSibling || t.parentElement.nextElementSibling); };
  const lab = () => { const f = footer(), x = f && f.querySelector('span'); return x ? x.innerText : null; };
  const header = [...table().querySelectorAll('thead th')].map(th => th.innerText.trim());
  const rows = new Map(); let pages = 0, empty = 0, ended = false;
  const num = s => { const x = parseFloat(String(s).replace('%', '')); return Number.isFinite(x) ? x : null; };
  for (let i = 0; i < 60; i++) {
    pages++;
    let drafted = 0;
    for (const tr of table().querySelectorAll('tbody tr')) {
      const a = tr.querySelector('a[href*="/players/"]');
      const id = a && (a.getAttribute('href').match(/players\/(\d+)/) || [])[1];
      if (!id) continue;
      const c = [...tr.cells].map(x => x.innerText.trim());
      const row = {site_player_id: id, name: c[0].split('\n')[0], yahoo_rank: num(c[1]), drafted_pct: num(c[4]),
                   adp_preseason: num(c[5]), adp_all: num(c[6])};
      if (row.adp_preseason !== null || row.adp_all !== null) drafted++;
      rows.set(id, row);
    }
    empty = drafted ? 0 : empty + 1;
    const f = footer(), next = f && f.querySelectorAll('button')[1];
    if (!next || next.disabled) { ended = true; break; }
    if (empty >= 2) break;
    const label = lab();
    next.click();
    const sleep = () => new Promise(r => setTimeout(r, 200));
    for (let k = 0; k < 75 && (lab() === label || lab() === null); k++) await sleep();
    for (let k = 0; k < 75 && !(table() && table().querySelector('tbody tr a[href*="/players/"]')); k++) await sleep();
  }
  return {header, pages, ended, rows: [...rows.values()]};
}"""


def validate(raw: dict) -> list[dict]:
    """Checks the captured table before anything is saved."""
    expected = ['Player', 'Rank', 'Pos Rank', 'CER', '%Drafted', 'Preseason', 'All Drafts']
    header = [h for h in raw.get('header', []) if h in expected]
    if header != expected:
        raise ValueError(f"Draft Analysis columns changed: {raw.get('header')}")
    rows = [r for r in raw['rows'] if str(r.get('site_player_id', '')).isdigit()]
    drafted = [r for r in rows if r['adp_preseason'] is not None or r['adp_all'] is not None]
    if len(drafted) < 100 or not any(r['yahoo_rank'] == 1 for r in rows):
        raise ValueError(f"Draft Analysis capture looks incomplete ({len(drafted)} drafted players)")
    if any(not (1 <= (r['adp_preseason'] or r['adp_all']) <= 400) for r in drafted):
        raise ValueError('Draft Analysis ADP values out of range')
    return rows


def save(output: Path, raw: dict, captured_at: datetime | None = None) -> Path:
    rows = validate(raw)
    stamp = (captured_at or datetime.now(timezone.utc)).isoformat().replace('+00:00', 'Z')
    doc = {'schema_version': 1, 'captured_at_utc': stamp, 'source_url': ADP_URL,
           'scoring': 'Yahoo standard scoring ADP', 'pages': raw['pages'], 'players': rows,
           'note': 'Preseason ADP is fixed once the season starts; All Drafts includes later drafts.'}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile('w', dir=output, prefix='.adp-', delete=False, encoding='utf-8') as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        temp = Path(fh.name)
    os.chmod(temp, 0o600)
    os.replace(temp, output / ADP_NAME)
    return output / ADP_NAME


def is_stale(output: Path, now: datetime | None = None) -> bool:
    path = output / ADP_NAME
    if not path.exists():
        return True
    try:
        stamp = json.loads(path.read_text(encoding='utf-8'))['captured_at_utc']
        age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return age.days >= MAX_AGE_DAYS
    except (ValueError, KeyError):
        return True


def capture(page, output: Path) -> Path:
    """Read-only: opens the public page, pages through the table, validates, saves atomically."""
    page.goto(ADP_URL, wait_until='domcontentloaded')
    page.locator('table tbody tr a[href*="/players/"]').first.wait_for(timeout=30000)   # rows start as placeholders
    return save(output, page.evaluate(ADP_JS))


def load_adp(reports: Path) -> dict | None:
    """{site_player_id: row} plus metadata, or None when no capture exists."""
    path = reports / ADP_NAME
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding='utf-8'))
    return {'captured_at_utc': doc['captured_at_utc'], 'source_url': doc['source_url'],
            'players': {r['site_player_id']: r for r in doc['players']}}


# FantasyPros expert consensus rankings (ECR), Roto/categories, rest of season. Public page, plain GET, read-only.
# Until FantasyPros publishes in-season ROS rankings the page serves its final draft ECR and says so
# ('fallback_for': 'ROS'); the capture records that, and the trade lab treats it as a dated signal.
ECR_NAME = "fantasypros-ecr.json"
ECR_URL = "https://www.fantasypros.com/nba/rankings/ros-overall.php"
ECR_MAX_AGE_DAYS = 3


def parse_ecr(html: str) -> dict:
    start = html.find('var ecrData = ')
    if start < 0:
        raise ValueError('FantasyPros page has no ecrData block')
    start += len('var ecrData = ')
    data = json.loads(html[start:html.find('};', start) + 1])
    rows = []
    for p in data.get('players', []):
        try:
            rank = float(p['rank_ecr'])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({'name': p.get('player_name'), 'team': p.get('player_team_id'), 'positions': p.get('player_positions'),
                     'nbacom_id': p.get('player_nbacom_id') or None, 'rank_ecr': rank,
                     'rank_min': float(p['rank_min']) if p.get('rank_min') else None,
                     'rank_max': float(p['rank_max']) if p.get('rank_max') else None,
                     'rank_std': float(p['rank_std']) if p.get('rank_std') else None})
    if data.get('sport') != 'NBA' or data.get('scoring') != 'ROTO':
        raise ValueError(f"Unexpected FantasyPros rankings ({data.get('sport')}, {data.get('scoring')})")
    if len(rows) < 300 or not any(r['rank_ecr'] == 1 for r in rows) or not data.get('last_updated_ts'):
        raise ValueError(f'FantasyPros rankings look incomplete ({len(rows)} players)')
    updated = datetime.fromtimestamp(int(data['last_updated_ts']), timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'ranking_type': data.get('type'), 'fallback_for': data.get('fallback_for'), 'scoring': data['scoring'],
            'experts': data.get('total_experts'), 'updated_at_utc': updated, 'players': rows}


def save_ecr(output: Path, parsed: dict, captured_at: datetime | None = None) -> Path:
    stamp = (captured_at or datetime.now(timezone.utc)).isoformat().replace('+00:00', 'Z')
    doc = {'schema_version': 1, 'captured_at_utc': stamp, 'source_url': ECR_URL, **parsed}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile('w', dir=output, prefix='.ecr-', delete=False, encoding='utf-8') as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        temp = Path(fh.name)
    os.chmod(temp, 0o600)
    os.replace(temp, output / ECR_NAME)
    return output / ECR_NAME


def capture_ecr(output: Path) -> Path:
    from urllib.request import Request, urlopen
    with urlopen(Request(ECR_URL, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30) as resp:
        return save_ecr(output, parse_ecr(resp.read().decode('utf-8', 'replace')))


def ecr_is_stale(output: Path, now: datetime | None = None) -> bool:
    try:
        stamp = json.loads((output / ECR_NAME).read_text(encoding='utf-8'))['captured_at_utc']
    except (OSError, ValueError, KeyError):
        return True
    age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return age.days >= ECR_MAX_AGE_DAYS


def load_ecr(reports: Path) -> dict | None:
    path = reports / ECR_NAME
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def main() -> None:
    parser = argparse.ArgumentParser(description='Capture Yahoo Draft Analysis (ADP) and FantasyPros ECR, read-only')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--ecr-only', action='store_true', help='refresh only the FantasyPros consensus rankings')
    args = parser.parse_args()
    if args.ecr_only:
        path = capture_ecr(args.root / REPORTS)
        doc = load_ecr(path.parent)
        print(json.dumps({'status': 'captured', 'path': str(path), 'players': len(doc['players']),
                          'ranking_type': doc['ranking_type'], 'updated_at_utc': doc['updated_at_utc']}))
        return
    from playwright.sync_api import sync_playwright
    profile = args.root / BROWSER_PROFILE
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), channel='chrome', headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            path = capture(page, args.root / REPORTS)
        finally:
            context.close()
    doc = json.loads(path.read_text(encoding='utf-8'))
    print(json.dumps({'status': 'captured', 'path': str(path), 'players': len(doc['players']),
                      'drafted': sum(r['adp_preseason'] is not None for r in doc['players'])}))


if __name__ == '__main__':
    main()
