"""Read-only, signed-in Yahoo browser refresh for Phase 0 snapshots.

Uses a dedicated ignored Chrome profile. The user signs in in that window once;
credentials and cookies are never copied into a report or command line.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
import sys
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from .yahoo_browser import _fnv32
from .yahoo_transactions import ACTUAL_JS, ACTUAL_NAME, RESULTS_JS, SNAPSHOT_NAME, TRANSACTION_JS, build_actual_state, build_snapshot
from .schedule import load_fantasy_weeks, load_schedule
from .matchups import load_matchups


ROOT = Path(__file__).resolve().parents[2]
HOST = "basketball.fantasysports.yahoo.com"


from . import adp_capture


def lineup_date(header: str, today: date) -> str:
    match = re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+([A-Z][a-z]{2})\s+(\d{1,2})\b", header)
    if not match:
        raise ValueError("Yahoo roster date is unavailable")
    month_day = datetime.strptime(f"{match[1]} {match[2]}", "%b %d")
    choices = [date(year, month_day.month, month_day.day) for year in (today.year - 1, today.year, today.year + 1)]
    chosen = min(choices, key=lambda item: abs((item - today).days))
    if abs((chosen - today).days) > 31 or chosen.strftime("%a") not in header:
        raise ValueError("Yahoo roster date cannot be resolved safely")
    return chosen.isoformat()


def _roster_snapshot(page, league_id: str) -> dict:
    page.goto(f"https://{HOST}/nba/{league_id}/startingrosters", wait_until="domcontentloaded")
    page.locator('table[id^="Tst-team-"]').first.wait_for(timeout=30_000)
    observed = page.evaluate("""() => ({
      header: document.body.innerText.slice(0, 500),
      teams: [...document.querySelectorAll('table[id^="Tst-team-"]')].map(t => ({
        team_id: t.id.replace('Tst-team-', ''),
        name: t.parentElement?.querySelector('p a[href^="/nba/"]')?.textContent.trim() || '',
        rows: [...t.querySelectorAll('tbody tr')].filter(r => r.querySelector('a.name[data-ys-playerid]')).map(r => {
          const a = r.querySelector('a.name[data-ys-playerid]');
          return [t.id.replace('Tst-team-', ''), r.querySelector('td.pos')?.textContent.trim() || '',
                  a.getAttribute('data-ys-playerid'), a.textContent.trim(),
                  r.querySelector('.D-b .Fz-xxs')?.textContent.trim() || ''];
        })
      }))
    })""")
    teams = observed["teams"]
    if len(teams) != 10 or len({t["team_id"] for t in teams}) != 10 or any(not t["name"] or not t["rows"] for t in teams):
        raise ValueError("Yahoo roster page did not show ten complete teams")
    rows = [row for team in teams for row in team["rows"]]
    return {"teams": [{"team_id": t["team_id"], "name": t["name"]} for t in teams],
            "text": "\n".join("\t".join(row) for row in rows),
            "lineup_date": lineup_date(observed["header"], datetime.now(ZoneInfo("America/Toronto")).date())}


def _player_snapshot(page, league_id: str) -> tuple[str, int, bool, dict]:
    page.goto(f"https://{HOST}/nba/{league_id}/players", wait_until="domcontentloaded")
    page.locator("#statusselect").wait_for(timeout=30_000)
    page.locator("#statusselect").select_option("ALL")
    page.wait_for_function("""() => new URLSearchParams(location.search).get('status') === 'ALL' &&
      document.querySelectorAll('#players-table tr a.name[data-ys-playerid]').length > 0""", timeout=30_000)
    lines, seen_urls, extras = [], set(), {"codes": {}, "waiver_texts": {}}
    for page_number in range(1, 101):
        page.locator("#players-table tr a.name[data-ys-playerid]").first.wait_for(timeout=30_000)
        if page.url in seen_urls:
            raise ValueError("Yahoo player pagination looped")
        seen_urls.add(page.url)
        observed = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#players-table tr')]
            .filter(r => r.querySelector('a.name[data-ys-playerid]'))
            .map(r => { const a = r.querySelector('a.name[data-ys-playerid]');
              const cells = [...r.querySelectorAll('td')];
              return [a.getAttribute('data-ys-playerid'), a.textContent.trim(),
                      r.querySelector('.ysf-player-name .D-b .Fz-xxs')?.textContent.trim() ||
                      r.querySelector('.Fz-xxs')?.textContent.trim() || '',
                      cells[4]?.textContent.trim() || '']; });
          const next = [...document.querySelectorAll('a')].find(a => a.textContent?.trim() === 'Next 25');
          const codes = [...document.querySelectorAll('#players-table tr')].filter(r => r.querySelector('a.name[data-ys-playerid]'))
            .map(r => { const c = r.querySelectorAll('td')[4];
              return [r.querySelector('a.name[data-ys-playerid]').getAttribute('data-ys-playerid'),
                      r.querySelector('.F-injury')?.textContent.trim() || '',
                      ((c?.querySelector('[title]')?.getAttribute('title') || '') + ' ' + (c?.textContent || '')).trim()]; });
          return {status: document.querySelector('#statusselect')?.value, rows, codes,
                  next_href: next?.getAttribute('href') || null};
        }""")
        rows, next_href = observed["rows"], observed["next_href"]
        if observed["status"] != "ALL" or not rows or len(rows) > 25 or (next_href and len(rows) != 25):
            raise ValueError("Yahoo player page has an unexpected filter or size")
        for row_number, row in enumerate(rows, 1):
            if len(row) != 4 or any("\t" in str(value) or "\n" in str(value) for value in row):
                raise ValueError("Yahoo player row contains unsupported data")
            lines.append("\t".join([str(page_number), str(row_number), *row]))
        for site_id, code, owner_text in observed.get("codes", []):
            extras["codes"][site_id] = code
            if owner_text.startswith("W"):
                extras["waiver_texts"][site_id] = owner_text
        if not next_href:
            return "\n".join(lines), page_number, True, extras
        next_url = urljoin(page.url, next_href)
        parsed = urlparse(next_url)
        if parsed.scheme != "https" or parsed.hostname != HOST or parsed.path != f"/nba/{league_id}/players":
            raise ValueError("Yahoo pagination link left the league player list")
        page.goto(next_url, wait_until="domcontentloaded")
    raise ValueError("Yahoo player pagination exceeded 100 pages")


def _save(output: Path, roster: dict, players: str, pages: int, league_id: str, transactions: dict | None = None) -> Path:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    roster_name = "yahoo-browser-fresh-rosters.tsv"
    players_name = "yahoo-browser-all-players.tsv"
    manifest_name = "yahoo-browser-players-capture.json"
    manifest = {
        "schema_version": 1, "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "league_id": league_id, "lineup_date": roster["lineup_date"], "player_filter": "ALL",
        "roster_source_url": f"https://{HOST}/nba/{league_id}/startingrosters",
        "players_source_url": f"https://{HOST}/nba/{league_id}/players",
        "teams": roster["teams"],
        "rosters": {"file": roster_name, "fnv32": _fnv32(roster["text"])},
        "players": {"file": players_name, "fnv32": _fnv32(players),
                    "page_count": pages, "row_count": len(players.splitlines()), "last_page_no_next": True},
    }
    names = [roster_name, players_name, manifest_name]
    snapshot = None
    if transactions is not None:
        waiver_ids = [line.split("\t")[2] for line in players.splitlines() if line.split("\t")[-1] == "W"]
        snapshot = build_snapshot(transactions["raw"], captured_at_utc=manifest["captured_at_utc"], league_id=league_id,
                                  team_id=transactions["team_id"], player_fingerprint=manifest["players"]["fnv32"],
                                  roster_fingerprint=manifest["rosters"]["fnv32"], lineup_date=roster["lineup_date"],
                                  player_codes=transactions.get("codes"), waiver_ids=waiver_ids,
                                  waiver_texts=transactions.get("waiver_texts"))
        names.append(SNAPSHOT_NAME)
    actual = None
    if transactions is not None and transactions.get("actual") is not None:
        info = transactions["actual"]
        actual = build_actual_state(info["raw"], captured_at_utc=manifest["captured_at_utc"], team_ids=info["teams"],
                                    dates=info["dates"], completed_weeks=info["completed_weeks"],
                                    pairings=info["pairings"], league_id=league_id)
        actual["player_fingerprint"] = manifest["players"]["fnv32"]
        actual["roster_fingerprint"] = manifest["rosters"]["fnv32"]
        names.append(ACTUAL_NAME)
    with tempfile.TemporaryDirectory(prefix="phase0-", dir=output) as temporary:
        stage = Path(temporary)
        if actual is not None:
            (stage / ACTUAL_NAME).write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if snapshot is not None:
            (stage / SNAPSHOT_NAME).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / roster_name).write_text(roster["text"] + "\n", encoding="utf-8")
        (stage / players_name).write_text(players + "\n", encoding="utf-8")
        (stage / manifest_name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for name in names:
            os.chmod(stage / name, 0o600)
            os.replace(stage / name, output / name)
    return output / manifest_name


def _actual_capture(page, root: Path, league_id: str, teams: list, team_names: list) -> dict | None:
    """Current-week volumes are captured only after the period's first tipoff (Phase 2 contract)."""
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("America/Toronto")).date()
    weeks = load_fantasy_weeks(root / "YAHOO_FANTASY_CALENDAR_AND_LINEUP_LOCKS_2026-27.md")
    current = next((w for w in weeks if w.start <= today <= w.end), None)
    schedule = load_schedule(root / "nba_2026_27_team_schedule.csv")
    if current is None:
        return None
    started = any(g.tipoff_utc <= now and current.start <= g.date <= current.end for g in schedule.games)
    completed = current.number - 1
    if not started and not completed:
        return None
    dates = ([(current.start + timedelta(days=i)).isoformat() for i in range((today - current.start).days + 1)]
             if started else [])
    raw = page.evaluate(RESULTS_JS, {"league": league_id, "completed": completed}) if completed else {}
    if dates:
        daily = page.evaluate(ACTUAL_JS, {"league": league_id, "teams": teams, "dates": dates})
        raw["pages"] = daily["pages"]
        raw.setdefault("errors", []).extend(daily["errors"])
    # The verified opponent CSV uses the original league-capture names; current
    # Yahoo display names can change without changing a team ID.
    league_capture = json.loads((root / "data" / "reports" / "yahoo-browser-capture.json").read_text(encoding="utf-8"))
    names = {str(t["team_id"]): t["name"] for t in league_capture["teams"]}
    if set(names) != set(map(str, teams)):
        raise ValueError("Completed-result team IDs do not match the league capture")
    pairings = load_matchups(root / "H2H-Matchups.csv", names).pairings
    return {"raw": raw, "teams": teams, "dates": dates, "completed_weeks": completed, "pairings": pairings}


def refresh(root: Path, league_id: str, login_timeout_seconds: int = 180, team_id: str = "4") -> Path:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install the optional browser dependency: pip install -e 'backend[browser]'") from exc
    if not league_id.isdigit() or not team_id.isdigit():
        raise ValueError("League ID must be numeric")
    profile = root / "data" / "cache" / "yahoo-browser-profile"
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"https://{HOST}/nba/{league_id}/startingrosters", wait_until="domcontentloaded")
            try:
                page.locator('table[id^="Tst-team-"]').first.wait_for(timeout=login_timeout_seconds * 1000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("Yahoo sign-in or league access was not completed in the dedicated Chrome window") from exc
            if adp_capture.is_stale(root / "data" / "reports"):   # public ADP page; weekly at most
                try:
                    adp_capture.capture(page, root / "data" / "reports")
                except Exception as exc:  # ADP is optional; the trade lab falls back and says so
                    print(f"ADP capture skipped: {exc}", file=sys.stderr)
            if adp_capture.ecr_is_stale(root / "data" / "reports"):   # public FantasyPros ECR; plain GET
                try:
                    adp_capture.capture_ecr(root / "data" / "reports")
                except Exception as exc:  # optional; market value falls back to ADP and says so
                    print(f"FantasyPros ECR capture skipped: {exc}", file=sys.stderr)
            first = _roster_snapshot(page, league_id)
            players, pages, complete, extras = _player_snapshot(page, league_id)
            raw = page.evaluate(TRANSACTION_JS, {"league": league_id, "team": team_id})
            actual = _actual_capture(page, root, league_id, [t["team_id"] for t in first["teams"]], first["teams"])
            second = _roster_snapshot(page, league_id)
            if first != second or not complete:
                raise ValueError("Yahoo roster changed during capture; run the refresh again")
            return _save(root / "data" / "reports", second, players, pages, league_id,
                         {"raw": raw, "team_id": team_id, "actual": actual, **extras})
        finally:
            context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh signed-in Yahoo roster and full All Players list")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--league-id", default="92103")
    parser.add_argument("--login-timeout-seconds", type=int, default=180)
    parser.add_argument("--team-id", default="4")
    args = parser.parse_args()
    path = refresh(args.root.resolve(), args.league_id, args.login_timeout_seconds, args.team_id)
    print(json.dumps({"status": "captured", "manifest": str(path)}, sort_keys=True))


if __name__ == "__main__":
    main()
