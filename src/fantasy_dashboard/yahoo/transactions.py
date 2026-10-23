"""Read-only Yahoo transaction evidence bound to one player/roster capture."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import re

from fantasy_dashboard.league import EASTERN, VOLUMES, parse_utc
from fantasy_dashboard.yahoo.evidence import load_user_evidence

SNAPSHOT_NAME = "yahoo-transaction-snapshot.json"
FRESH_MINUTES = 15
ACTUAL_NAME = "yahoo-actual-state.json"
REQUIRED = ("roster", "acquisitions", "il", "waiver_priority", "cant_cut", "player_status", "il_eligible_available")

# Same-origin GETs only. Evaluated in the signed-in Yahoo tab (Playwright or Chrome).
TRANSACTION_JS = r"""async ({league, team}) => {
  const get = async p => { const r = await fetch(p, {credentials: 'include'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = new DOMParser().parseFromString(await r.text(), 'text/html');
    d.querySelectorAll('script,style,noscript').forEach(x => x.remove()); return d; };
  const text = d => (d.body?.textContent || '').replace(/\s+/g, ' ');
  const pages = async first => { const ids = [], codes = {}; let path = first, n = 0;
    while (path && n < 40) { const d = await get(path); n += 1;
      for (const r of d.querySelectorAll('#players-table tr')) { const a = r.querySelector('a.name[data-ys-playerid]');
        if (!a) continue; const id = a.getAttribute('data-ys-playerid'); ids.push(id);
        codes[id] = r.querySelector('.F-injury')?.textContent.trim() || ''; }
      const next = [...d.querySelectorAll('a')].find(a => a.textContent?.trim() === 'Next 25');
      const url = next ? new URL(next.getAttribute('href'), location.origin) : null;
      path = url && url.origin === location.origin && url.pathname === `/nba/${league}/players` ? url.pathname + url.search : null; }
    return {ids, codes, pages: n, complete: !path}; };
  const out = {errors: []};
  try { const d = await get(`/nba/${league}/${team}`), t = text(d);
    out.team_text = ['You have used \\d+ of your \\d+ player adds[^.]*', 'You have used \\d+ of \\d+ IL positions[^.]*',
      'Waiver Priority:\\s*\\d+(?:st|nd|rd|th)?', '(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [A-Z][a-z]{2} \\d{1,2}']
      .map(p => (t.match(new RegExp(p)) || [''])[0]);
    out.roster = [...d.querySelectorAll('table tr')].filter(r => r.querySelector('a.name[data-ys-playerid]')).map(r => {
      const l = r.querySelector('.pos-label');
      return {id: r.querySelector('a.name[data-ys-playerid]').getAttribute('data-ys-playerid'),
              slot: l?.textContent.trim() || '', code: r.querySelector('.F-injury')?.textContent.trim() || '',
              game: r.querySelector('.ysf-game-status')?.textContent.trim() || '', slot_class: String(l?.className || '')}; });
    const heads = [...d.querySelectorAll('h2,h3,h4,h5,caption,th,span,div')].filter(x => !x.children.length &&
      /^(Pending Transactions|Pending Waiver Claims|Waiver Claims)\b/i.test(x.textContent.trim()));
    out.pending_section = heads.length > 0;
    out.pending_ids = [...new Set(heads.flatMap(h => [...(h.closest('section,table,div')?.parentElement?.querySelectorAll('a[data-ys-playerid]') || [])]
      .map(a => a.getAttribute('data-ys-playerid'))))];
  } catch (e) { out.errors.push('team_page: ' + e.message); }
  try { const d = await get(`/nba/${league}/cantcutlist`);
    out.cant_cut = [...new Set([...d.querySelectorAll('a.name[data-ys-playerid]')].map(a => a.getAttribute('data-ys-playerid')))];
    out.cant_cut_paginated = [...d.querySelectorAll('a')].some(a => /^Next\b/.test(a.textContent?.trim() || ''));
  } catch (e) { out.errors.push('cant_cut_list: ' + e.message); }
  try { out.il_eligible = await pages(`/nba/${league}/players?status=STATE_IL&pos=P`); }
  catch (e) { out.errors.push('il_eligible_list: ' + e.message); }
  return out; }"""


# One league standings page and one completed matchup page per elapsed week.
# Results are accepted only when each page says Final, five pairs match the
# verified schedule, and cumulative standings equal the sum of weekly scores.
RESULTS_JS = r"""async ({league, completed}) => {
  const get = async p => { const r = await fetch(p, {credentials: 'include'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = new DOMParser().parseFromString(await r.text(), 'text/html');
    d.querySelectorAll('script,style,noscript').forEach(x => x.remove()); return d; };
  const id = href => { const m = new URL(href, location.origin).pathname.match(new RegExp(`^/nba/${league}/(\\d+)/?$`));
    return m ? m[1] : null; };
  const out = {errors: [], completed_pages: [], standings_records: {},
    standings_source: `/nba/${league}?lhst=stand&module=standings`};
  try { const d = await get(out.standings_source);
    const table = [...d.querySelectorAll('table')].find(t => /W-L-T/.test(t.querySelector('thead')?.textContent || ''));
    for (const row of table?.querySelectorAll('tbody tr') || []) {
      const team = [...row.querySelectorAll('a[href]')].map(a => id(a.getAttribute('href'))).find(Boolean);
      const record = [...row.querySelectorAll('td')].map(c => c.textContent.trim()).find(v => /^\d+-\d+-\d+$/.test(v));
      if (team && record) out.standings_records[team] = record.split('-').map(Number); }
  } catch (e) { out.errors.push('standings: ' + e.message); }
  for (let week = 1; week <= completed; week++) {
    const source = `/nba/${league}?lhst=matchups&matchup_week=${week}&module=matchups`;
    try { const d = await get(source), body = (d.body?.textContent || '').replace(/\s+/g, ' ');
      const matches = [];
      for (const view of [...d.querySelectorAll('a')].filter(a => /View Matchup/i.test(a.textContent))) {
        let block = view.parentElement, teams = [];
        while (block && block !== d.body) {
          teams = [...block.querySelectorAll('a[href]')].map(a => id(a.getAttribute('href'))).filter(Boolean);
          if (new Set(teams).size === 2) break;
          block = block.parentElement; }
        if (!block || block === d.body) continue;
        const lines = [...block.querySelectorAll('*')].filter(x => !x.children.length)
          .map(x => x.textContent.trim()).filter(Boolean);
        const at = lines.findIndex(x => /^vs$/i.test(x));
        const left = at < 0 ? null : lines.slice(0, at).reverse().find(x => /^\d{1,2}$/.test(x));
        const right = at < 0 ? null : lines.slice(at + 1).find(x => /^\d{1,2}$/.test(x));
        matches.push({teams: [...new Set(teams)], scores: [left, right], lines}); }
      out.completed_pages.push({week, source, final: body.includes('Final results'),
        week_header: body.includes(`Week ${week} Matchups`), matches});
    } catch (e) { out.errors.push(`week_${week}: ${e.message}`); } }
  return out; }"""


# Daily team pages carry per-player FGM/A and FTM/A, which the matchup page does not.
ACTUAL_JS = r"""async ({league, teams, dates}) => {
  const out = {errors: [], pages: []};
  for (const team of teams) for (const day of dates) {
    try { const r = await fetch(`/nba/${league}/${team}/team?date=${day}&stat1=S&stat2=D`, {credentials: 'include'});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = new DOMParser().parseFromString(await r.text(), 'text/html');
      d.querySelectorAll('script,style,noscript').forEach(x => x.remove());
      const t = [...d.querySelectorAll('table')].find(x => x.querySelector('a.name[data-ys-playerid]'));
      const head = (t?.querySelector('thead')?.textContent || '').replace(/\s+/g, ' ');
      const rows = [...(t?.querySelectorAll('tbody tr') || [])].filter(x => x.querySelector('a.name[data-ys-playerid]')).map(x => {
        const cells = [...x.querySelectorAll('td')].map(c => c.textContent.trim());
        const i = cells.findIndex(v => /^(\d+\/\d+|-\/-)$/.test(v));
        return [x.querySelector('.pos-label')?.textContent.trim() || '', x.querySelector('a.name').getAttribute('data-ys-playerid'),
                (x.querySelector('.ysf-game-status')?.textContent || '').replace(/\s+/g, ' ').trim(), i < 0 ? [] : cells.slice(i, i + 11)]; });
      out.pages.push({team: String(team), date: day, page_date: (d.body.textContent.match(/(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [A-Z][a-z]{2} \d{1,2}/) || [''])[0],
                      header_ok: /FGM\/A\*?\s*FG%\s*FTM\/A\*?\s*FT%\s*3PTM\s*PTS\s*REB\s*AST\s*ST\s*BLK\s*TO/.test(head), rows});
    } catch (e) { out.errors.push(`${team}/${day}: ${e.message}`); } }
  return out; }"""


def live_remaining_fraction(game: str) -> float | None:
    """Share of regulation still to play from Yahoo's live game cell; None when the clock cannot be read."""
    text = game.lower()
    if re.search(r"\b\d?ot\b|overtime", text):
        return 0.0
    if "half" in text:
        return 0.5
    if m := re.search(r"\bend (?:of )?(?:q|the )?([1-4])", text):
        return (4 - int(m[1])) / 4
    quarter = re.search(r"\bq([1-4])\b|\b([1-4])(?:st|nd|rd|th)\b", text)
    if not quarter:
        return None
    q = int(quarter[1] or quarter[2])
    clock = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    left_in_quarter = min(12.0, int(clock[1]) + int(clock[2]) / 60) if clock else 6.0
    return round(max(0.0, min(1.0, ((4 - q) * 12 + left_in_quarter) / 48)), 3)


def build_actual_state(raw: dict, *, captured_at_utc: str, team_ids: list, dates: list,
                       completed_weeks: int, pairings: dict | None = None, league_id: str | None = None) -> dict:
    """Verify completed scores and sum started-slot daily volumes from final and live games.

    A live game's partial box score is real and is summed; the game is also listed in in_progress_games
    with the share of regulation left, so the loader can project only the unplayed part."""
    problems = list(raw.get("errors", []))
    in_progress = []
    ids = set(map(str, team_ids))
    records = {t: [0, 0, 0] for t in team_ids}
    weekly = []
    pages = raw.get("completed_pages", [])
    if completed_weeks and (len(pages) != completed_weeks or pairings is None or
                            set(raw.get("standings_records", {})) != ids):
        problems.append("completed_week_pages_or_standings_missing")
    for week in range(1, completed_weeks + 1):
        matching = [p for p in pages if p.get("week") == week]
        if len(matching) != 1 or not matching[0].get("final") or not matching[0].get("week_header"):
            problems.append(f"week_{week}_not_verified_final")
            continue
        page = matching[0]
        observed = {}
        for match in page.get("matches", []):
            teams, scores = match.get("teams", []), match.get("scores", [])
            if len(teams) != 2 or len(set(teams)) != 2 or not set(teams) <= ids or len(scores) != 2 or any(
                    v is None or not str(v).isdigit() for v in scores):
                problems.append(f"week_{week}_matchup_unparsed")
                continue
            a, b = teams
            wa, wb = map(int, scores)
            if wa + wb > 9 or wa > 9 or wb > 9:
                problems.append(f"week_{week}_score_invalid")
                continue
            pair = tuple(sorted((a, b)))
            if pair in observed:
                problems.append(f"week_{week}_duplicate_pair")
                continue
            observed[pair] = (a, b, wa, wb, 9 - wa - wb)
        expected = set(pairings.get(week, ())) if pairings else set()
        if set(observed) != expected or len(observed) != 5:
            problems.append(f"week_{week}_pairings_incomplete_or_mismatched")
            continue
        results = {}
        for a, b, wa, wb, ties in observed.values():
            results[a], results[b] = [wa, wb, ties], [wb, wa, ties]
        if set(results) != ids or any(sum(v) != 9 for v in results.values()):
            problems.append(f"week_{week}_nonreciprocal")
            continue
        weekly.append(results)
        for team in team_ids:
            records[team] = [records[team][i] + results[team][i] for i in range(3)]
    if completed_weeks and (len(weekly) != completed_weeks or any(
            records[t] != raw.get("standings_records", {}).get(t) for t in team_ids)):
        problems.append("completed_results_do_not_reconcile_to_standings")
    seen = {(p["team"], p["date"]) for p in raw.get("pages", [])}
    if len(seen) != len(raw.get("pages", [])):
        problems.append("duplicate_daily_page")
    problems += [f"page_missing:{t}/{d}" for t in team_ids for d in dates if (t, d) not in seen]
    totals = {t: dict.fromkeys(VOLUMES, 0) for t in team_ids} if dates else None
    for page in raw.get("pages", []):
        if page.get("team") not in ids or page.get("date") not in dates:
            problems.append("unexpected_daily_page")
            continue
        day = date.fromisoformat(page["date"])
        why = ("header" if not page["header_ok"] else "no_rows" if not page["rows"] else
               "date" if day.strftime("%a, %b ") + str(day.day) != page["page_date"] else None)
        if why:
            problems.append(f"page_unverified_{why}:{page['team']}/{page['date']}")
            continue
        for slot, site_id, game, cells in page["rows"]:
            if slot in {"BN", "IL", "IL+"}:
                continue
            if len(cells) != 11:
                problems.append(f"row_unparsed:{page['team']}/{page['date']}/{site_id}")
                continue
            final = re.match(r"^[WL], \d+-\d+", game)
            live = bool(game) and not final and not re.match(r"^\d{1,2}:\d{2} [ap]m", game) and not game.startswith("PPD")
            if not (final or live):
                continue
            try:
                (fgm, fga), (ftm, fta) = ([0, 0] if v == "-/-" else list(map(int, v.split("/"))) for v in (cells[0], cells[2]))
                counts = [0 if v in {"-", ""} else int(float(v)) for v in cells[4:11]]
            except (ValueError, TypeError):
                problems.append(f"row_stats_invalid:{page['team']}/{page['date']}/{site_id}")
                continue
            for key, value in zip(VOLUMES, [fgm, fga, ftm, fta, *counts]):
                totals[page["team"]][key] += value
            if live:
                remaining = live_remaining_fraction(game)
                in_progress.append({"team": page["team"], "date": page["date"], "site_id": site_id, "game": game,
                                    "remaining_fraction": 0.5 if remaining is None else remaining,
                                    "clock_parsed": remaining is not None, "has_stats": any([fga, fta, *counts])})
    return {"schema_version": 1, "source": "Yahoo final matchup pages, standings, and daily team pages (final and live box scores)",
            "as_of_utc": captured_at_utc, "league_id": league_id, "team_ids": team_ids,
            "completed_weeks": completed_weeks, "records": records, "weekly_records": weekly,
            "current_week_volumes": totals, "in_progress_games": in_progress, "dates": dates,
            "sources": {"standings": raw.get("standings_source"),
                        "matchups": [p.get("source") for p in pages],
                        "daily_team_pages": f"/nba/{league_id}/{{team}}/team?date={{date}}&stat1=S&stat2=D" if dates else None},
            "status": "rejected" if problems else "captured", "problems": problems}


def build_snapshot(raw: dict, *, captured_at_utc: str, league_id: str, team_id: str, player_fingerprint: str,
                   roster_fingerprint: str, lineup_date: str, player_codes: dict, waiver_ids: list,
                   waiver_texts: dict | None = None) -> dict:
    """Turn raw page observations into explicit sections; never fills a missing section."""
    s: dict[str, dict] = {}
    miss = lambda why: {"status": "missing", "detail": why}
    team_text = raw.get("team_text") or ["", "", "", ""]
    roster = raw.get("roster")
    s["roster"] = ({"status": "observed", "site_ids": [r["id"] for r in roster], "slots": {r["id"]: r["slot"] for r in roster},
                    "page_date": team_text[3]} if roster else miss("team page roster rows not found"))
    m = re.search(r"used (\d+) of your (\d+) player adds", team_text[0])
    s["acquisitions"] = {"status": "observed", "used": int(m[1]), "limit": int(m[2]), "text": team_text[0]} if m else miss("weekly add counter not shown")
    m = re.search(r"used (\d+) of (\d+) IL positions", team_text[1])
    s["il"] = {"status": "observed", "used": int(m[1]), "capacity": int(m[2])} if m else miss("IL counter not shown")
    m = re.search(r"Waiver Priority:\s*(\d+)", team_text[2])
    s["waiver_priority"] = {"status": "observed", "value": int(m[1])} if m else miss("waiver priority not shown")
    cut = raw.get("cant_cut")
    s["cant_cut"] = ({"status": "observed", "site_ids": sorted(cut)} if cut and not raw.get("cant_cut_paginated")
                     else miss("Can't Cut List empty or paginated"))
    codes = {k: v for k, v in (player_codes or {}).items() if v}
    if roster:
        codes.update({r["id"]: r["code"] for r in roster if r["code"]})
    s["player_status"] = {"status": "observed", "by_site_id": dict(sorted(codes.items()))} if player_codes is not None else miss("player list status codes not captured")
    il = raw.get("il_eligible")
    s["il_eligible_available"] = ({"status": "observed", "site_ids": sorted(il["ids"]), "pages": il["pages"]}
                                  if il and il.get("complete") else miss("IL-eligible list incomplete"))
    if raw.get("pending_section"):
        s["pending_claims"] = {"status": "observed", "site_ids": sorted(raw.get("pending_ids", []))}
    elif roster and not waiver_ids:
        s["pending_claims"] = {"status": "observed_none", "site_ids": [], "basis": "no pending section and no waiver players"}
    else:
        s["pending_claims"] = {"status": "not_shown", "site_ids": []}
    locks = [r["id"] for r in roster or [] if "disabled" in r.get("slot_class", "").lower()]
    s["lineup_locks"] = {"status": "observed" if locks else "not_shown", "site_ids": locks,
                         "basis": "Yahoo lock marker" if locks else "no lock marker; schedule tipoffs applied"}
    if not waiver_ids:
        s["waiver_clear"] = {"status": "none_on_waivers", "by_site_id": {}}
    else:
        dates = {}
        for site_id in waiver_ids:
            hit = re.search(r"\b([A-Z][a-z]{2}) (\d{1,2})\b", (waiver_texts or {}).get(site_id, ""))
            if hit:
                day = datetime.strptime(f"{hit[1]} {hit[2]} {lineup_date[:4]}", "%b %d %Y").date()
                dates[site_id] = day.isoformat()
        s["waiver_clear"] = {"status": "date_only" if dates else "not_shown", "by_site_id": dates}
    return {"schema_version": 1, "captured_at_utc": captured_at_utc, "league_id": league_id, "team_id": team_id,
            "player_fingerprint": player_fingerprint, "roster_fingerprint": roster_fingerprint,
            "lineup_date": lineup_date, "capture_errors": raw.get("errors", []), "sections": s,
            "sources": {"team": f"/nba/{league_id}/{team_id}", "cant_cut": f"/nba/{league_id}/cantcutlist",
                        "il_eligible": f"/nba/{league_id}/players?status=STATE_IL"}}


def _week_number(inputs, day: date) -> int:
    for index, week in enumerate(inputs["weeks"], 1):
        if week.start <= day <= week.end:
            return index
    return 1 if day < inputs["weeks"][0].start else len(inputs["weeks"])


def _schedule_locks(inputs, roster, captured: datetime) -> set:
    day = captured.astimezone(EASTERN).date()
    return {p.site_player_id for p in roster
            if (game := inputs["schedule"].team_dates.get((p.nba_team, day))) and game.tipoff_utc <= captured}


def load_transaction_snapshot(path: Path, inputs: dict, now: datetime | None = None) -> dict:
    """Validate binding, completeness and ownership; return engine evidence or a rejection."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result = {"source": "yahoo_snapshot", "path": path.name, "evidence": None, "reasons": [], "missing_evidence": []}
    if not path.exists():
        return {**result, "status": "missing", "reasons": ["yahoo_snapshot_missing"]}
    snap = json.loads(path.read_text(encoding="utf-8"))
    team_id = inputs["league"]["my_team_id"]
    avail = inputs["availability"]
    result.update(captured_at_utc=snap.get("captured_at_utc"))
    try:
        captured = parse_utc(snap["captured_at_utc"])
        age = (now - captured).total_seconds() / 60
        result.update(age_minutes=round(age, 1), freshness="fresh" if 0 <= age <= FRESH_MINUTES else "stale")
    except (KeyError, TypeError, ValueError):
        return {**result, "status": "rejected", "reasons": ["snapshot_time_invalid"]}
    reject = lambda *why: {**result, "status": "rejected", "reasons": list(why)}
    if snap.get("schema_version") != 1 or snap.get("league_id") != inputs["league"]["league_id"] or snap.get("team_id") != team_id:
        return reject("snapshot_identity_mismatch")
    if (snap["captured_at_utc"] != avail["captured_at_utc"] or snap.get("player_fingerprint") != avail.get("player_fingerprint")
            or snap.get("roster_fingerprint") != avail.get("roster_fingerprint")):
        return reject("snapshot_not_synchronized_with_player_capture")
    sec = snap.get("sections", {})
    missing = [f"incomplete_capture:{name}" for name in REQUIRED if sec.get(name, {}).get("status") != "observed"]
    if missing:
        return {**reject(*missing), "capture_errors": snap.get("capture_errors", [])}
    roster = inputs["rosters"][team_id]
    pool = {p.site_player_id: p for p in inputs["site_players"]}
    available = {k for k, p in pool.items() if p.availability_label in {"FA", "W"} and not p.fantasy_team_id}
    if sec["roster"]["slots"] != {p.site_player_id: p.assigned_slot for p in roster}:
        return reject("ownership_inconsistent:team_page_roster")
    if sec["il"]["used"] != sum(p.assigned_slot == "IL" for p in roster) or sec["il"]["capacity"] != 3:
        return reject("ownership_inconsistent:il_occupancy")
    week = _week_number(inputs, captured.astimezone(EASTERN).date())
    acq = sec["acquisitions"]
    if acq["limit"] != inputs["weeks"][week - 1].max_adds or not 0 <= acq["used"] <= acq["limit"]:
        return reject("acquisition_budget_mismatch")
    if not set(sec["il_eligible_available"]["site_ids"]) <= available:
        return reject("ownership_inconsistent:il_eligible_list")
    if not set(sec["cant_cut"]["site_ids"]) <= set(pool):
        return reject("cant_cut_player_not_in_pool")
    claims = sec.get("pending_claims", {})
    if not set(claims.get("site_ids", [])) <= {k for k in available if pool[k].availability_label == "W"} | {p.site_player_id for p in roster}:
        return reject("ownership_inconsistent:pending_claims")
    waiver_ids = [k for k in available if pool[k].availability_label == "W"]
    if claims.get("status") not in {"observed", "observed_none"}:
        result["missing_evidence"].append("active_claims_unverified")
    if waiver_ids:
        result["missing_evidence"].append("waiver_effective_time_unverified")
    if sec.get("lineup_locks", {}).get("status") != "observed":
        result["missing_evidence"].append("lineup_lock_flags_not_shown_schedule_tipoffs_used")
    timing = {k: {"kind": "free_agent", "effective_at_utc": snap["captured_at_utc"],
                  "basis": "FA label at capture; verified Daily - Today lineup setting"}
              for k in available if pool[k].availability_label == "FA"}
    il_ids = sorted(sec["il_eligible_available"]["site_ids"])
    evidence = {
        "status": "yahoo_snapshot", "source": "Authenticated read-only Yahoo browser capture",
        "captured_at_utc": snap["captured_at_utc"], "player_fingerprint": snap["player_fingerprint"],
        "team_id": team_id, "league_id": snap["league_id"], "week": week, "adds_used": acq["used"],
        "weekly_acquisitions_limit": acq["limit"], "remaining_adds": acq["limit"] - acq["used"],
        "cannot_cut_site_ids": sorted(set(sec["cant_cut"]["site_ids"]) & {p.site_player_id for p in roster}),
        "league_cant_cut_site_ids": sec["cant_cut"]["site_ids"],
        "locked_site_ids": sorted(set(sec.get("lineup_locks", {}).get("site_ids", [])) | _schedule_locks(inputs, roster, captured)),
        "timing_by_site_id": timing, "empty_slot_allowed": True,
        "direct_il_site_ids": il_ids, "il_eligible_site_ids": il_ids,
        "active_claim_site_ids": sorted(claims.get("site_ids", [])),
        "waiver_priority": sec["waiver_priority"]["value"], "empty_il_slots": 3 - sec["il"]["used"],
        "status_by_site_id": sec["player_status"]["by_site_id"],
        "observed_date": captured.astimezone(EASTERN).date().isoformat(), "time_precision": "exact_utc",
        "missing_evidence": result["missing_evidence"]}
    return {**result, "status": "verified", "evidence": evidence}


def load_injury_statuses(path: Path, inputs: dict) -> tuple[dict, str | None]:
    """Use synchronized player statuses even if an unrelated transaction field is missing."""
    if not path.exists():
        return {}, None
    snap = json.loads(path.read_text(encoding="utf-8"))
    avail = inputs["availability"]
    if (snap.get("schema_version") != 1 or snap.get("league_id") != inputs["league"]["league_id"] or
            snap.get("captured_at_utc") != avail["captured_at_utc"] or
            snap.get("player_fingerprint") != avail.get("player_fingerprint") or
            snap.get("roster_fingerprint") != avail.get("roster_fingerprint")):
        return {}, None
    sec = snap.get("sections", {})
    roster = inputs["rosters"][inputs["league"]["my_team_id"]]
    if (sec.get("roster", {}).get("status") != "observed" or
            sec["roster"].get("slots") != {p.site_player_id: p.assigned_slot for p in roster} or
            sec.get("player_status", {}).get("status") != "observed"):
        return {}, None
    statuses = sec["player_status"].get("by_site_id")
    if not isinstance(statuses, dict) or not set(statuses) <= {p.site_player_id for p in inputs["site_players"]}:
        return {}, None
    return statuses, snap["captured_at_utc"]


def resolve_evidence(inputs: dict, reports: Path, now: datetime | None = None) -> tuple[dict | None, dict]:
    """Prefer a verified synchronized snapshot; else the bound user evidence (provisional); else none."""
    status = load_transaction_snapshot(reports / SNAPSHOT_NAME, inputs, now)
    if status["status"] == "verified":
        return status["evidence"], {k: v for k, v in status.items() if k != "evidence"}
    fallback = None
    for path in sorted(reports.glob("yahoo-transaction-evidence-user-*.json"), reverse=True):
        try:
            fallback = load_user_evidence(inputs, path)
            break
        except (ValueError, KeyError):
            continue
    info = {k: v for k, v in status.items() if k != "evidence"}
    info["fallback"] = "user_supplied_provisional" if fallback else "none"
    return fallback, info
