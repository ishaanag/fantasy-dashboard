"""Phase 4A/4B Trade Intelligence: read-only 1-for-1, 2-for-1 and 1-for-2 trade evaluation for both managers.

4B adds multi-player packages with explicit roster balancing: the side receiving an
extra player must drop one (or move an IL-eligible player to IL); the side left with an
open spot is evaluated with it open, and with the best current free agent as a
conditional, acquisition-consuming fill.

Both rosters are replayed through the advisor's Phase 2 day engine (legal daily
slots, GP/82 availability, summed shooting volumes, known locks) from an assumed
effective date, against every scheduled opponent, and the league standings are
re-ranked with the deterministic forecast's rules. Asset value is a separate,
interpretable value-over-replacement measure. Nothing is sent to Yahoo and no
acceptance probability or trade-market value is produced.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations
from math import exp, isfinite, sqrt
import multiprocessing
import os
import warnings
from time import perf_counter

from .advisor import BASE_POSITIONS, DOUBTFUL_STATUSES, INACTIVE_STATUSES, LABELS, _Engine, _categories, _days
from .deterministic import _win_pct, forecast, relative_strength
from .phase1 import CATEGORIES, EASTERN, SLOTS, VOLUMES, RosterPlayer, _lineup_score, eligible, resolve_projection
from .simulator import _expected_with_actual, _record, category_result
from .matching import normalize_name, normalize_team
from .waiver import _margin, _utc, pool_audit

UTC = timezone.utc
COUNTING = ('three_ptm', 'pts', 'reb', 'ast', 'stl', 'blk', 'turnovers')
SOURCE_WEIGHTS = (('hashtag', 'Hashtag', 1.0), ('leaguewinner', 'LeagueWinner', 0.0))
VALUE_FLOOR = 25.0   # value-over-replacement units; keeps fairness ratios finite near zero
PACKAGES = {'1-1': (1, 1), '2-1': (2, 1), '1-2': (1, 2), '2-2': (2, 2)}   # (players I send, players I receive)
PACKAGE_LABELS = {'1-1': '1-for-1', '2-1': '2-for-1', '1-2': '1-for-2', '2-2': '2-for-2'}
ACTIVE_LIMIT, IL_LIMIT = 14, 3        # league settings: 10 starting slots + 4 BN; 3 IL
IL_MOVE_STATUSES = frozenset({'O', 'INJ'})   # default IL-move rule when evidence supplies none; unverified
MARKET_DECAY, MARKET_FLOOR = 40.0, 10.0      # draft-slot value curve (auction-like) and fairness floor; heuristics


_JOB = None   # (function, items) inherited by forked replay workers; never pickled


def _replay_chunk(bounds):
    fn, items = _JOB
    return [fn(items[k]) for k in range(*bounds)]


def _replay_all(fn, items, workers):
    """Exact replays are independent, so they run in forked worker processes that inherit the prepared context
    (copy-on-write; nothing is pickled but the results). Falls back to one process if forking is unavailable."""
    global _JOB
    if workers <= 1 or len(items) < 200 or 'fork' not in multiprocessing.get_all_start_methods():
        return [fn(c) for c in items]
    size = max(20, len(items) // (workers * 6))
    chunks = [(k, min(k + size, len(items))) for k in range(0, len(items), size)]
    _JOB = (fn, items)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)   # fork from a threaded server: workers only compute
            with multiprocessing.get_context('fork').Pool(workers) as pool:
                return [t for part in pool.imap(_replay_chunk, chunks) for t in part]
    except (OSError, RuntimeError):
        return [fn(c) for c in items]
    finally:
        _JOB = None


def _ids(x):
    if not x:
        return ()
    return (x,) if isinstance(x, str) else tuple(sorted(x))


@dataclass(frozen=True)
class TradeConfig:
    """Defaults are the product behaviour. Thresholds are declared heuristics, not calibrated."""
    partner_ids: tuple[str, ...] | None = None    # default: all nine other teams
    protected_ids: frozenset = frozenset()         # my untouchables (manual)
    include_injured_reserve: bool = False          # IL-slotted players stay out of 1-for-1 swaps
    acceptance_days: int = 1                       # assumed partner response before the reject window
    shortlist: int | None = 300                    # 1-for-1 trades given the full replay; None = every package
    package_types: tuple[str, ...] = ('1-1', '2-1', '1-2', '2-2')
    replay_all_viable: bool = True                 # replay every viable package (the quotas below apply only when False)
    workers: int = max(1, min(8, (os.cpu_count() or 2) - 1))   # parallel replay processes; 1 = in-process
    viable_model_floor: float | None = .6          # model (z-score) give-up beyond which a package is not replayed
    viable_market_limit: float = .10               # most the partner may lose by market value; beyond it, never shown
    depth_discount: float = .5                     # market weight of each player after the best one on a side
    floor_screen_gate: float | None = .5           # past the model floor, still replay when the screen gives you this much
    multi_shortlist: int = 900                     # per multi-player package type (checked against an exhaustive run)
    partner_floor: int = 30                        # each partner keeps its best screened packages of every type
    drop_options: int | None = None                # legal drops replayed exactly per package; None = every legal drop
    fill_candidates: int = 8                       # free agents tried for an open spot (plus best per position)
    finalists: int = 24                            # leading candidates re-run under each source alone
    premium_finalists: int = 4                     # realistic finalist places kept for partner-premium packages
    keep_finalists: bool = False                   # also return every source-checked finalist (tests, diagnostics)
    type_reserve: int = 4                          # finalist places kept for each package type
    result_limit: int = 5                          # cards always shown; closest checked packages fill any gap
    per_player_limit: int = 2                      # cards that give away the same player of mine
    simpler_band: float = .5                       # a 1-for-1 within this many wins is "similar"
    simpler_penalty: float = 1.5
    fill_credit: float = .5                        # share of the best fill's edge over the next-best fill in rank score
    low_usage_share: float = .6                    # outgoing start share below which his cost looks artificially small
    material_gain: float = 1.5                     # rest-of-season category wins (W + T/2)
    partner_credible_gain: float = .5
    fair_band: float = .15
    mutual_fair_band: float = .20
    overpay_limit: float = .35                     # model-value labels only (asset-sacrifice risk)
    market_cost_weight: float = .02                # rank: category wins charged per market point you surrender
    expensive_cost_per_win: float = 12.0           # market points per robust category win above which it is expensive
    near_term_weight: float = .25                  # rank credit per next-three-weeks category win (capped at +/-2)
    partner_weight: float = .25                    # rank credit per partner category win (supporting evidence)
    complication_cost: float = .25                 # rank cost per extra player and per drop, IL move or fill
    narrow_count: float = .03                      # flip margin under 3% of the opponent total is fragile
    narrow_pct: float = .004                       # or under 0.4 percentage points
    fragile_discount: float = .5
    evidence_max_age_minutes: int = 15

    def __post_init__(self):
        if self.acceptance_days < 0 or self.finalists < 0 or self.result_limit < 1 or self.per_player_limit < 1:
            raise ValueError('Trade limits must be positive')
        if self.shortlist is not None and self.shortlist < 1:
            raise ValueError('Shortlist must be positive or None for an exhaustive run')
        if not self.package_types or set(self.package_types) - set(PACKAGES) or self.multi_shortlist < 1 or \
                self.partner_floor < 0 or (self.drop_options is not None and self.drop_options < 1) or \
                self.fill_candidates < 0 or self.type_reserve < 0:
            raise ValueError('Package types must be 1-1, 2-1, 1-2 or 2-2 and package limits must be positive')
        if self.premium_finalists < 0 or not 0 <= self.depth_discount <= 1:
            raise ValueError('Premium finalists must be nonnegative and the depth discount within 0 to 1')
        if any(not isfinite(v) or v < 0 for v in (self.material_gain, self.partner_credible_gain, self.fair_band,
               self.mutual_fair_band, self.overpay_limit, self.narrow_count, self.narrow_pct, self.fragile_discount,
               self.viable_market_limit, self.market_cost_weight, self.expensive_cost_per_win, self.near_term_weight,
               self.partner_weight, self.complication_cost, *(() if self.viable_model_floor is None
                                                              else (self.viable_model_floor,)))):
            raise ValueError('Trade thresholds must be finite and nonnegative')


def trade_rules(inputs: dict) -> dict:
    """Verified Yahoo trade settings; analysis stops if they were not captured."""
    rows = {r['field']: r['browser'] for r in inputs['league'].get('settings_comparison', [])
            if r.get('status') == 'match'}
    try:
        deadline = datetime.strptime(rows['Trade End Date'], '%B %d, %Y').date()
        reject_days = int(rows['Trade Reject Time'].split()[0])
        review = rows['Trade Review']
    except (KeyError, ValueError, IndexError):
        raise ValueError('Trade end date, review and reject time are not verified in the Yahoo capture')
    return {'deadline': deadline, 'reject_days': reject_days, 'review': review,
            'keeper_league': rows.get('Keeper Settings', '').startswith('Yes'),
            'draft_pick_trades': rows.get('Allow Draft Pick Trades')}


def _vector(raw):
    v = {**raw['shooting_volumes'], **{c: raw['categories'][c] for c in COUNTING}}
    return [v[k] for k in VOLUMES]


def _points(score):
    return score[0] + .5 * score[2]


class _Context:
    """One projection source: deterministic base, shared day engine and per-team weekly totals."""

    def __init__(self, inputs, base, weight, as_of, actual_state, eff_day):
        self.inputs, self.base, self.weight, self.as_of = inputs, base, weight, as_of
        self.teams = sorted(inputs['teams'], key=int)
        self.players = {p.site_player_id: p for t in self.teams for p in inputs['rosters'][t]}
        self.proj = {pid: resolve_projection(inputs['projections'][pid], weight) for pid in self.players}
        snapshot_day = date.fromisoformat(inputs['availability']['lineup_date'])
        self.site = {p.site_player_id: p for p in inputs['site_players']}
        # The engine gets its own maps so free-agent fills never enter the rostered asset-value pool.
        self.engine = _Engine(inputs['schedule'], dict(self.players), dict(self.proj), as_of, snapshot_day, inputs)
        self.first = base['actual_completed_weeks'] + 1
        today = as_of.astimezone(EASTERN).date()
        self.weeks = inputs['weeks'][self.first-1:18]
        self.week_days = [list(_days(max(today, w.start), w.end)) for w in self.weeks]
        self.eff_day = eff_day
        self.actual = ((actual_state or {}).get('current_week_volumes') or {}) if self.first == self.weeks[0].number else {}
        self.pairings, self.history, self.base_cats = {}, {}, {}
        for m in base['weekly_matchups']:
            a, b = m['teams']
            s = _record(m['score'])
            self.pairings.setdefault(m['week'], []).append((a, b))
            self.history.setdefault(m['week'], {})[a] = s
            self.history[m['week']][b] = [s[1], s[0], s[2]]
            if m['status'] == 'projected':
                for t in (a, b):
                    self.base_cats[(m['week'], t)] = m['totals'][t]['categories']
        self.base_seed = {t['team_id']: t['projected_seed'] for t in base['teams']}
        self.base_record = {t['team_id']: list(t['projected_record']) for t in base['teams']}
        self.team_games = Counter()
        sched = inputs['schedule']
        for (nba, day), game in sched.team_dates.items():
            if eff_day <= day <= self.weeks[-1].end and game.tipoff_utc > as_of:
                self.team_games[nba] += 1
        self.cache, self.row_cache = {}, {}
        self.base_raw = {t: self.team_weeks(t) for t in self.teams}
        for t in self.teams:   # HOLD must reproduce the deterministic forecast for every team
            for w, raw in zip(self.weeks, self.base_raw[t]['raw']):
                expected = self.base_cats[(w.number, t)]
                for c in CATEGORIES:
                    x, y = raw['categories'][c], expected[c]
                    if (x is None) != (y is None) or (y is not None and abs(x - y) > 1e-6 * max(1, abs(y))):
                        raise AssertionError('Trade engine totals diverged from the deterministic forecast')
        replay = self.season({})
        if replay['records'] != self.base_record or replay['seeds'] != self.base_seed:
            raise AssertionError('Trade standings replay diverged from the deterministic forecast')
        self.base_season = replay
        self.base_totals = {t: self.season_totals(self.base_raw[t]['raw']) for t in self.teams}
        self.base_strength = relative_strength(self.base_totals)

    def register(self, pid):
        """Make an unowned free agent available to the day engine as a bench add."""
        eng = self.engine
        if pid in eng.players:
            return
        sp = self.site[pid]
        proj = resolve_projection(self.inputs['projections'][pid], self.weight)
        eng.players[pid] = RosterPlayer('FA', pid, sp.name, 'BN', tuple(sp.eligible_positions), sp.nba_team)
        eng.projections[pid] = proj
        v = proj['values']
        eng.vec[pid] = tuple(v['gp'] / 82 * v[k] for k in VOLUMES)
        eng.score[pid] = max(1e-6, _lineup_score(v))
        eng.slots[pid] = tuple(j for j, s in enumerate(SLOTS) if eligible(s, sp.eligible_positions))

    def team_weeks(self, team, gives=(), receives=(), drops=(), adds=(), il=()):
        """Weekly totals with `gives` (and balancing drops / IL moves) replaced by `receives`
        (and free-agent `adds`) from the effective day on. Strings or tuples of site IDs."""
        gives, receives, drops, adds, il = (_ids(x) for x in (gives, receives, drops, adds, il))
        key = (team, gives, receives, drops, adds, il)
        if key in self.cache:
            return self.cache[key]
        before = frozenset(p.site_player_id for p in self.inputs['rosters'][team])
        after = (before - set(gives) - set(drops) - set(il)) | set(receives) | set(adds)
        raws, starts, after_starts = [], Counter(), []
        for wi, (w, days) in enumerate(zip(self.weeks, self.week_days)):
            total, late = [0.0] * len(VOLUMES), Counter()
            for day in days:
                volumes, starters = self.engine.day(day, after if day >= self.eff_day else before, {}, {})
                for i, v in enumerate(volumes):
                    total[i] += v
                for p in starters:
                    starts[p] += 1
                    if day >= self.eff_day:
                        late[p] += 1
            raw = _categories(total)
            if wi == 0 and self.actual.get(team):
                raw = _expected_with_actual(raw, self.actual[team])
            raws.append(raw)
            after_starts.append(late)
        result = {'raw': raws, 'starts': starts, 'after_starts': after_starts}
        self.cache[key] = result
        return result

    def season_totals(self, raws):
        v = [0.0] * len(VOLUMES)
        for raw in raws:
            for i, x in enumerate(_vector(raw)):
                v[i] += x
        return _categories(v)

    def opponent(self, team, week):
        return next(b if a == team else a for a, b in self.pairings[week] if team in (a, b))

    def rows(self, team, changed):
        key = (team, tuple(sorted((t, id(r)) for t, r in changed.items())))
        if not changed and key in self.row_cache:
            return self.row_cache[key]
        out = []
        for wi, w in enumerate(self.weeks):
            opp = self.opponent(team, w.number)
            mine = (changed.get(team) or self.base_raw[team]['raw'])[wi]['categories']
            theirs = (changed.get(opp) or self.base_raw[opp]['raw'])[wi]['categories']
            wins, losses, ties, outcomes = category_result(mine, theirs)
            out.append({'week': w.number, 'opponent': opp, 'score': [wins, losses, ties],
                        'points': wins + .5 * ties, 'outcomes': outcomes, 'theirs': theirs,
                        'margins': {c: _margin(mine, theirs, c) for c in CATEGORIES}})
        if not changed:
            self.row_cache[key] = out
        return out

    def season(self, changed):
        """Full-league records and seeds; only matchups involving changed teams are recomputed."""
        hist = {w: dict(v) for w, v in self.history.items()}
        for t in changed:
            for row in self.rows(t, changed):
                s = row['score']
                hist[row['week']][t], hist[row['week']][row['opponent']] = s, [s[1], s[0], s[2]]
        weeks = sorted(hist)
        records = {t: [sum(hist[w][t][i] for w in weeks) for i in range(3)] for t in self.teams}
        if any(sum(r) != 162 for r in records.values()) or \
                sum(r[0] for r in records.values()) != sum(r[1] for r in records.values()):
            raise ValueError('Season category decisions are incomplete or nonreciprocal')
        ranked = sorted(records, key=lambda t: (-_win_pct(records[t]),
                        *(-_win_pct(hist[w][t]) for w in reversed(weeks)), int(t)))
        return {'records': records, 'seeds': {t: i for i, t in enumerate(ranked, 1)}}


def _raw_value(v, league):
    """Per-game category contributions; shooting is makes above league average at the player's volume."""
    return {'fg_pct': v['fgm'] - league[0] * v['fga'], 'ft_pct': v['ftm'] - league[1] * v['fta'],
            **{c: -v[c] if c == 'turnovers' else v[c] for c in COUNTING}}


def asset_values(ctx, statuses) -> dict:
    """Rest-of-season value over replacement, independent of any one roster.

    z-scores are taken over rostered non-IL players; replacement level is the best
    projectable, schedulable, active free agent at the player's scarcest position.
    Value = (summed per-game z x GP/82 - replacement) x team games after the effective day.
    """
    pool = [pid for pid, p in ctx.players.items() if p.assigned_slot != 'IL']
    vals = {pid: ctx.proj[pid]['values'] for pid in ctx.players}
    fga, fta = sum(vals[p]['fga'] for p in pool), sum(vals[p]['fta'] for p in pool)
    league = (sum(vals[p]['fgm'] for p in pool) / fga if fga else 0, sum(vals[p]['ftm'] for p in pool) / fta if fta else 0)
    raw = {pid: _raw_value(v, league) for pid, v in vals.items()}
    mean = {c: sum(raw[p][c] for p in pool) / len(pool) for c in CATEGORIES}
    sd = {c: sqrt(sum((raw[p][c] - mean[c]) ** 2 for p in pool) / len(pool)) for c in CATEGORIES}

    def zed(r):
        return {c: (r[c] - mean[c]) / sd[c] if sd[c] else 0.0 for c in CATEGORIES}
    schedule = ctx.inputs['schedule']
    replacement = {pos: None for pos in BASE_POSITIONS}
    by_id = {p.site_player_id: p for p in ctx.inputs['site_players']}
    for row in pool_audit(ctx.inputs, ctx.weight)['rows']:
        pid = row['site_player_id']
        if row['owner_team_id'] or row['availability'] not in ('FA', 'W') or not row['projectable'] or \
                by_id[pid].nba_team not in schedule.team_counts or statuses.get(pid) in INACTIVE_STATUSES:
            continue
        try:
            v = resolve_projection(ctx.inputs['projections'][pid], ctx.weight)['values']
        except (ValueError, KeyError):
            continue
        per = sum(zed(_raw_value(v, league)).values()) * v['gp'] / 82
        for pos in by_id[pid].eligible_positions:
            if pos in replacement and (replacement[pos] is None or per > replacement[pos][0]):
                replacement[pos] = (per, row['name'])
    floor = min(sum(zed(raw[p]).values()) * vals[p]['gp'] / 82 for p in pool)
    out = {}
    for pid, p in ctx.players.items():
        z = zed(raw[pid])
        per = sum(z.values()) * vals[pid]['gp'] / 82
        options = [(replacement[pos][0] if replacement.get(pos) else floor, pos,
                    replacement[pos][1] if replacement.get(pos) else None)
                   for pos in p.eligible_positions if pos in BASE_POSITIONS] or [(floor, None, None)]
        level, pos, name = min(options)
        games = ctx.team_games[p.nba_team]
        out[pid] = {'z': z, 'per_game_z': sum(z.values()), 'per_team_game': per, 'replacement': level,
                    'replacement_position': pos, 'replacement_player': name, 'team_games': games,
                    'value': (per - level) * games}
    return out


FP_TEAMS = {'NOR': 'NOP', 'UTH': 'UTA'}   # FantasyPros team codes that differ from Yahoo's
MARKET_SIGNALS = {'adp': 'Yahoo preseason ADP', 'ecr': 'FantasyPros consensus (ECR)'}
ECR_STALE_DAYS = 14      # an older ECR capture drops out of the blend (flagged)
DISAGREE_POINTS, DISAGREE_SHARE = 15.0, .35   # per-player signal spread that counts as disagreement


def _ordinal(ranked):
    """{pid: raw rank} -> {pid: position 1..n among the matched players}, ties averaged."""
    out, items = {}, sorted(ranked.items(), key=lambda kv: kv[1])
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1][1] == items[i][1]:
            j += 1
        for k in range(i, j + 1):
            out[items[k][0]] = (i + j) / 2 + 1
        i = j + 1
    return out


def ecr_matches(inputs):
    """FantasyPros rows keyed by Yahoo site ID: normalized name (team breaks duplicates), else first initial,
    surname and team for names Yahoo abbreviates ("N. Alexander-Walker"). Returns (matches, unmatched rows)."""
    ecr = inputs.get('ecr')
    if not ecr:
        return {}, []
    full, short = {}, {}
    for p in inputs['site_players']:
        n = normalize_name(p.name)
        full.setdefault(n, []).append(p)
        parts = n.split()
        if parts:
            short.setdefault((parts[0][0], ' '.join(parts[1:]), normalize_team(p.nba_team)), []).append(p)
    out, unmatched = {}, []
    for r in ecr['players']:
        n, team = normalize_name(r['name'] or ''), normalize_team(FP_TEAMS.get(r['team'], r['team']))
        c = full.get(n, [])
        if len(c) > 1:
            c = [p for p in c if normalize_team(p.nba_team) == team]
        if not c and n:
            parts = n.split()
            c = short.get((parts[0][0], ' '.join(parts[1:]), team), [])
        if len(c) == 1 and c[0].site_player_id not in out:
            out[c[0].site_player_id] = r
        else:
            unmatched.append({'name': r['name'], 'team': r['team'], 'rank_ecr': r['rank_ecr']})
    return out, unmatched


def market_values(inputs, ctx, as_of=None):
    """Perceived market value: how other managers are likely to value players, never this model's view.

    Signals: Yahoo preseason average draft position (public Draft Analysis, standard scoring) and FantasyPros
    expert consensus rankings (Roto/categories). Each is re-ranked to an ordinal position among the players on
    Yahoo's list (ties averaged), so both share one scale, then mapped to the auction-like curve
    100 x (e^-(pos-1)/40 - e^-(R-1)/40), R the rostered active players (replacement level). Market value is the
    mean of the available signals. Undrafted players count as replacement by ADP (an observation); a drafted
    player FantasyPros does not list is treated as missing for ECR, not as zero. ECR older than ECR_STALE_DAYS
    at the as-of time drops out. With neither signal, the mean projection-source rank is used and `basis` says
    so. Yahoo's league player list is never used: once games start its default order is season-to-date stats."""
    adp = (inputs.get('adp') or {}).get('players')
    ecr = inputs.get('ecr')
    as_of = as_of or getattr(ctx, 'as_of', None)
    if ecr and as_of is not None:
        age = (as_of - _utc(ecr['updated_at_utc'])).total_seconds() / 86400
        if age > ECR_STALE_DAYS:
            ecr = None
    level = sum(p.assigned_slot != 'IL' for p in ctx.players.values())

    def curve(r):
        return max(0.0, 100 * (exp(-(r - 1) / MARKET_DECAY) - exp(-(level - 1) / MARKET_DECAY)))
    universe = {p.site_player_id for p in inputs['site_players']} | set(ctx.site)
    adp_raw = {pid: (row.get('adp_preseason') or row.get('adp_all')) for pid, row in (adp or {}).items()}
    adp_raw = {pid: v for pid, v in adp_raw.items() if v and pid in universe}
    adp_pos = _ordinal(adp_raw)
    ecr_rows = ecr_matches({**inputs, 'ecr': ecr})[0] if ecr else {}
    ecr_pos = _ordinal({pid: r['rank_ecr'] for pid, r in ecr_rows.items() if pid in universe})
    out = {}
    for pid in ctx.site:
        sig = {}
        if adp is not None:
            sig['adp'] = {'pick': adp_raw.get(pid), 'position': adp_pos.get(pid),
                          'value': curve(adp_pos[pid]) if pid in adp_pos else 0.0}
        if ecr_rows and (pid in ecr_pos or not sig.get('adp', {}).get('value')):
            r = ecr_rows.get(pid) or {}
            sig['ecr'] = {'rank': r.get('rank_ecr'), 'position': ecr_pos.get(pid), 'std': r.get('rank_std'),
                          'value': curve(ecr_pos[pid]) if pid in ecr_pos else 0.0}
        if sig:
            vals = [s['value'] for s in sig.values()]
            value = sum(vals) / len(vals)
            basis = 'market_consensus' if len(sig) == 2 else ('yahoo_preseason_adp' if 'adp' in sig else 'fantasypros_ecr')
            pick = sig.get('adp', {}).get('pick')
        else:
            src = inputs['projections'].get(pid) or {}
            ranks = [r.raw.get(k) for r, k in ((src.get('LeagueWinner'), 'Rank'), (src.get('Hashtag'), 'R#'))
                     if r is not None and isinstance(r.raw.get(k), (int, float))]
            pick = sum(ranks) / len(ranks) if ranks else None
            value, basis, vals = (curve(pick) if pick else 0.0), 'projection_source_rank', []
        spread = max(vals) - min(vals) if len(vals) > 1 else 0.0
        out[pid] = {'rank': pick, 'value': value, 'basis': basis, 'signals': sig, 'spread': spread,
                    'missing': [k for k in MARKET_SIGNALS if (k == 'adp' and adp is not None or k == 'ecr' and ecr_rows)
                                and k not in sig],
                    'disagree': spread >= DISAGREE_POINTS and spread >= DISAGREE_SHARE * max(vals or [0])}
    return out


def market_summary(inputs, ctx, market, as_of):
    """Source coverage, freshness and assumptions for the market-value measure (rostered players)."""
    rostered = [pid for pid, p in ctx.players.items() if p.assigned_slot != 'IL']
    adp, ecr = inputs.get('adp'), inputs.get('ecr')
    ecr_unmatched = ecr_matches(inputs)[1] if ecr else []
    age = (as_of - _utc(ecr['updated_at_utc'])).total_seconds() / 86400 if ecr else None
    count = lambda k: sum(k in market[p]['signals'] and market[p]['signals'][k]['position'] is not None for p in rostered)
    return {
        'signals': [
            {'key': 'adp', 'label': MARKET_SIGNALS['adp'], 'available': adp is not None,
             'captured_at_utc': (adp or {}).get('captured_at_utc'), 'rostered_covered': count('adp'),
             'note': 'Preseason ADP is fixed once the season starts; undrafted players count as replacement level.'},
            {'key': 'ecr', 'label': MARKET_SIGNALS['ecr'], 'available': bool(ecr) and age is not None and age <= ECR_STALE_DAYS,
             'captured_at_utc': (ecr or {}).get('captured_at_utc'), 'updated_at_utc': (ecr or {}).get('updated_at_utc'),
             'age_days': None if age is None else round(age, 1), 'experts': (ecr or {}).get('experts'),
             'ranking_type': (ecr or {}).get('ranking_type'), 'fallback_for': (ecr or {}).get('fallback_for'),
             'rostered_covered': count('ecr'), 'unmatched_top_200': [u for u in ecr_unmatched if u['rank_ecr'] <= 200],
             'note': ('FantasyPros has not published rest-of-season rankings yet; its page serves the final draft '
                      'consensus.' if (ecr or {}).get('fallback_for') == 'ROS' else 'Rest-of-season consensus.')}],
        'rostered': len(rostered),
        'basis_counts': dict(Counter(market[p]['basis'] for p in rostered)),
        'rostered_disagreeing': sum(market[p]['disagree'] for p in rostered),
        'curve': f'100 x (e^-(position-1)/{MARKET_DECAY:.0f} - e^-(R-1)/{MARKET_DECAY:.0f}), R = rostered active players',
        'stale_after_days': ECR_STALE_DAYS}


def side_value(values, ids):
    """Asset value of one side of a package. A lone player keeps his (possibly negative) 4A value; in a
    two-player side each counts at least replacement level, since either could be swapped for a free agent."""
    ids = _ids(ids)
    return values[ids[0]]['value'] if len(ids) == 1 else sum(max(0.0, values[p]['value']) for p in ids)


def value_gap(get, give, floor=VALUE_FLOOR):
    """Positive when I receive more value than I give, as a share of the larger asset."""
    return (get - give) / max(abs(get), abs(give), floor)


def fairness_label(gap, get, give, cfg, floor=VALUE_FLOOR):
    if get <= floor and give <= floor:
        return 'Both near replacement level'
    if abs(gap) <= cfg.fair_band:
        return 'Fair'
    if gap > 0:
        return 'Favours you' if gap <= cfg.overpay_limit else 'Lopsided toward you'
    return 'You pay a premium' if gap >= -cfg.overpay_limit else 'Overpay'


def _screen_rates(ctx, team):
    """Baseline lineup-start share after the effective day, per rostered player."""
    base = ctx.base_raw[team]
    started = sum(base['after_starts'], Counter())
    return {pid: (started[pid] / ctx.team_games[ctx.players[pid].nba_team]
                  if ctx.team_games[ctx.players[pid].nba_team] else 0.0)
            for pid in (p.site_player_id for p in ctx.inputs['rosters'][team])}


def screen(ctx, team, gives, receives, rates, week_games):
    """Cheap linear estimate of a package's rest-of-season category-win change. Ranking aid only.

    `gives` may include a balancing drop; each incoming player starts at the rate of the
    remaining teammate with the nearest lineup score."""
    eng = ctx.engine
    gives, receives = _ids(gives), _ids(receives)
    pool = [p for p in rates if p not in gives and ctx.players[p].assigned_slot != 'IL']
    rate = {}
    for r in receives:
        near = min(pool, key=lambda p: abs(eng.score[p] - eng.score[r]), default=None)
        rate[r] = min(1.0, rates[near]) if near else 1.0
    delta = 0.0
    base_rows = ctx.rows(team, {})
    for wi, w in enumerate(ctx.weeks):
        out = {g: ctx.base_raw[team]['after_starts'][wi][g] for g in gives}
        add = {r: rate[r] * week_games[wi].get(eng.players[r].nba_team, 0) for r in receives}
        if not any(out.values()) and not any(add.values()):
            continue
        v = _vector(ctx.base_raw[team]['raw'][wi])
        for i in range(len(VOLUMES)):
            v[i] += sum(a * eng.vec[r][i] for r, a in add.items()) - sum(s * eng.vec[g][i] for g, s in out.items())
        opp = ctx.base_raw[ctx.opponent(team, w.number)]['raw'][wi]['categories']
        wins, _, ties, _ = category_result(_categories(v)['categories'], opp)
        delta += wins + .5 * ties - base_rows[wi]['points']
    return delta


def _narrow(cfg, cat, margin, theirs):
    if margin is None:
        return False
    if cat.endswith('_pct'):
        return abs(margin) < cfg.narrow_pct
    return abs(margin) < max(1.0, cfg.narrow_count * abs(theirs or 0))


def _side(ctx, cfg, team, changed, season, raws, gives, receives):
    gives, receives = _ids(gives), _ids(receives)
    base_rows, new_rows = ctx.rows(team, {}), ctx.rows(team, changed)
    cat_delta, flips, adjusted, fragile = Counter(), [], 0.0, 0
    value = {'W': 1.0, 'T': .5, 'L': 0.0}
    for b, n in zip(base_rows, new_rows):
        for c in CATEGORIES:
            d = value[n['outcomes'][c]] - value[b['outcomes'][c]]
            if not d:
                continue
            cat_delta[c] += d
            row = n if d > 0 else b
            narrow = _narrow(cfg, c, row['margins'][c], row['theirs'][c])
            fragile += narrow
            adjusted += d * (cfg.fragile_discount if narrow else 1)
            flips.append({'week': n['week'], 'opponent': n['opponent'], 'category': c,
                          'before': b['outcomes'][c], 'after': n['outcomes'][c], 'delta': d,
                          'margin_after': n['margins'][c], 'narrow': bool(narrow)})
    points = sum(r['points'] for r in new_rows) - sum(r['points'] for r in base_rows)
    near = [{'week': n['week'], 'opponent': n['opponent'], 'before': b['score'], 'after': n['score'],
             'delta': n['points'] - b['points']} for b, n in list(zip(base_rows, new_rows))[:3]]
    totals = ctx.season_totals(raws['raw'])
    others = {**ctx.base_totals, **{t: ctx.season_totals(changed[t]) for t in changed}}
    strength = relative_strength(others)[team]
    after, before_after = sum(raws['after_starts'], Counter()), sum(ctx.base_raw[team]['after_starts'], Counter())

    def games(ids):
        return sum(ctx.team_games[ctx.players[p].nba_team] for p in ids)
    return {'team_id': team, 'team_name': ctx.inputs['teams'][team], 'points_delta': points,
            'adjusted_delta': round(adjusted, 6), 'fragile_flips': fragile, 'category_delta': dict(cat_delta),
            'flips': sorted(flips, key=lambda f: (f['narrow'], -abs(f['delta']), f['week'])), 'near_term': near,
            'near_term_delta': sum(x['delta'] for x in near),
            'record_before': ctx.base_record[team], 'record_after': season['records'][team],
            'seed_before': ctx.base_seed[team], 'seed_after': season['seeds'][team],
            'win_pct_before': _win_pct(ctx.base_record[team]), 'win_pct_after': _win_pct(season['records'][team]),
            'lineup_starts_before': sum(ctx.base_raw[team]['starts'].values()),
            'lineup_starts_after': sum(raws['starts'].values()),
            'incoming_starts': sum(after[p] for p in receives), 'incoming_games': games(receives),
            'outgoing_starts': sum(before_after[p] for p in gives), 'outgoing_games': games(gives),
            'season_categories_after': totals['categories'],
            'strength_before': {c: ctx.base_strength[team][c]['rank'] for c in CATEGORIES},
            'strength_after': {c: strength[c]['rank'] for c in CATEGORIES}}


def evaluate(ctx, cfg, me, partner, gives, receives, mine_bal=None, their_bal=None):
    """Exact two-team replay of one package with each side's roster balancing
    ({'drops': ids, 'il': ids, 'adds': ids}); every other team keeps its forecast totals."""
    gives, receives = _ids(gives), _ids(receives)
    mb, tb = mine_bal or {}, their_bal or {}
    mine = ctx.team_weeks(me, gives, receives, mb.get('drops'), mb.get('adds'), mb.get('il'))
    theirs = ctx.team_weeks(partner, receives, gives, tb.get('drops'), tb.get('adds'), tb.get('il'))
    changed = {me: mine['raw'], partner: theirs['raw']}
    season = ctx.season(changed)
    return {'me': _side(ctx, cfg, me, changed, season, mine, gives, receives),
            'partner': _side(ctx, cfg, partner, changed, season, theirs, receives, gives),
            'seeds_after': season['seeds']}


def verification(inputs, evidence, yahoo_status, now, max_age_minutes=15) -> dict:
    """Trades are never executable here; this only says how current the roster evidence is."""
    gaps = []
    if (yahoo_status or {}).get('status') != 'verified':
        gaps.append('yahoo_snapshot_not_verified')
    stamp = (evidence or {}).get('captured_at_utc')
    age = None
    if not stamp:
        gaps.append('transaction_evidence_missing')
    else:
        try:
            age = (now - _utc(stamp)).total_seconds() / 60
            if age < 0 or age > max_age_minutes:
                gaps.append('snapshot_not_fresh')
        except (TypeError, ValueError):
            gaps.append('transaction_evidence_time_invalid')
    if stamp and stamp != inputs['availability']['captured_at_utc']:
        gaps.append('evidence_not_bound_to_roster_capture')
    return {'basis': 'current_snapshot' if not gaps else 'provisional_offline',
            'roster_captured_at_utc': inputs['availability']['captured_at_utc'],
            'evidence_age_minutes': None if age is None else round(age, 1),
            'eligibility_class': 'conditional_future', 'executable': False,
            'data_gaps': gaps,
            'always_unverified': ['partner_acceptance', 'commissioner_review', 'effective_date',
                                  'pending_trades_and_trade_blocks', 'keeper_implications',
                                  'injury_return_dates']}


def _checks(t):
    return [x for x in (t.get('sources') or {}).values() if not x.get('unavailable')]


def _market_cost(t):
    """Market points I surrender net: what I send plus any player I must cut, minus what I receive."""
    m = t['value']['market']
    return m['give'] + m.get('my_cut', 0.0) - m['get']


def _fill_dependent(t, cfg):
    me_fb, them_fb = _fallback(t, 'me'), _fallback(t, 'partner')
    return (me_fb is not None and me_fb < cfg.material_gain) or (them_fb is not None and them_fb < cfg.partner_credible_gain)


def _robust(t, cfg):
    """My gain I can rely on: the blended effective gain, or less if either projection source alone shows less."""
    eff, checks = _effective(t, 'me', cfg), _checks(t)
    return min([eff] + [x['me_adjusted'] - _fill_discount(t, 'me', cfg) for x in checks])


def _narrow_share(t):
    up = [f for f in t['me']['flips'] if f['delta'] > 0]
    return sum(f['delta'] for f in up if f['narrow']) / sum(f['delta'] for f in up) if up else 0.0


def market_label(t, cfg):
    """One label from explicit computed criteria (gap > 0 favours me by market value)."""
    gap, me, cost, checks = t['value']['gap'], _robust(t, cfg), _market_cost(t), _checks(t)
    if cost > MARKET_FLOOR and cost > cfg.expensive_cost_per_win * max(me, .1):
        return 'Expensive improvement'
    if (_effective(t, 'partner', cfg) >= cfg.partner_credible_gain and not _fill_dependent(t, cfg)
            and all(x['partner_points'] >= 0 for x in checks)):
        return 'Mutually beneficial'
    if -gap > cfg.fair_band:
        return 'Market opportunity'
    return 'Market-balanced'


def _classify(t, cfg):
    """('recommended' | 'excluded', reason). Paying a market premium is never a reason to exclude; partner
    category gain is evidence, not a requirement. A partner who loses market value beyond viable_market_limit would
    not consider the trade, so it is never shown."""
    gap, me, cost, checks = t['value']['gap'], _robust(t, cfg), _market_cost(t), _checks(t)
    if _effective(t, 'me', cfg) < cfg.material_gain:
        return 'excluded', 'your rest-of-season gain is below the materiality threshold'
    if checks and all(x['me_points'] <= 0 for x in checks):
        return 'excluded', 'your gain disappears under each projection source alone'
    if gap > cfg.viable_market_limit:
        return 'excluded', 'partner would lose market value; not a realistic proposal'
    shaky = me < 2 * cfg.material_gain or _narrow_share(t) >= .5 or (checks and not all(x['me_points'] > 0 for x in checks))
    if cost > MARKET_FLOOR and cost > cfg.expensive_cost_per_win * max(me, .1) and shaky:
        return 'excluded', 'large market concession for a small, fragile or source-dependent gain'
    return 'recommended', None


def _fill_discount(t, side, cfg):
    """Opportunity cost of a used free-agent fill: what the same free agent is worth to that team as a plain
    waiver swap without the trade, plus part of the best fill's edge over the next-best one (so a ranking
    never rests on one specific free agent still being available)."""
    f = ((t.get('balancing') or {}).get(side) or {}).get('fill') or {}
    if not f.get('used'):
        return 0.0
    edge = max(0.0, f['net_adjusted'] - f['fallback']['net_adjusted'])
    return max(0.0, f['opportunity']['adjusted']) + (1 - cfg.fill_credit) * edge


def _effective(t, side, cfg):
    """Adjusted category-win change net of the fill opportunity cost. Equals adjusted_delta without a fill."""
    return t[side]['adjusted_delta'] - _fill_discount(t, side, cfg)


def _fallback(t, side):
    """Side's net gain if only the next-best free agent were available for its open spot."""
    f = ((t.get('balancing') or {}).get(side) or {}).get('fill') or {}
    return f['fallback']['net_adjusted'] if f.get('used') else None


def _score(t, cfg):
    """Transparent rank score in category wins; the parts are kept in t['ranking']. Order of importance:
    robust gain, market plausibility, market value surrendered, next three weeks, source agreement."""
    gap, checks = t['value']['gap'], _checks(t)
    held = [x['me_points'] > 0 for x in checks]
    bal = t.get('balancing') or {}
    steps = sum(b['kind'] in ('drop', 'il') or bool((b.get('fill') or {}).get('used')) for b in bal.values())
    parts = {'robust_gain': _robust(t, cfg),
             'plausibility': -3 * max(0.0, gap),
             'market_cost': -cfg.market_cost_weight * max(0.0, _market_cost(t)),
             'near_term': cfg.near_term_weight * max(-2.0, min(t['me']['near_term_delta'], 2.0)),
             'sources': (.5 if held and all(held) and len(held) == len(SOURCE_WEIGHTS) else
                         -.5 if held and not all(held) else 0.0),
             'partner_benefit': cfg.partner_weight * max(-2.0, min(_effective(t, 'partner', cfg), 3.0)),
             'complexity': -cfg.complication_cost * (len(t['gives_all']) + len(t['receives_all']) - 2 + steps),
             'simpler_option': -cfg.simpler_penalty if t.get('simpler_alternative') else 0.0}
    t['ranking'] = {k: round(v, 4) for k, v in parts.items()}
    return sum(parts.values())


def _pareto(cands, cfg):
    """Removes inferior variations: a package is dropped when another with the same partner and at least one
    shared player is no worse on my gain, market cost, plausibility, simplicity and (within a quarter win)
    the next three weeks, and better on one of the first four. Returns (kept, dropped)."""
    def dims(t):
        return (_effective(t, 'me', cfg), -max(0.0, _market_cost(t)), -max(0.0, t['value']['gap']),
                -(len(t['gives_all']) + len(t['receives_all'])), t['me']['near_term_delta'])
    d = {id(t): dims(t) for t in cands}
    buckets = {}
    for t in cands:
        for p in (*t['gives_all'], *t['receives_all']):
            buckets.setdefault((t['partner']['team_id'], p['site_player_id']), []).append(t)
    dropped = set()
    for t in cands:
        x = d[id(t)]
        for p in (*t['gives_all'], *t['receives_all']):
            for o in buckets[(t['partner']['team_id'], p['site_player_id'])]:
                if o is t:
                    continue
                y = d[id(o)]
                if all(y[i] >= x[i] - 1e-9 for i in range(4)) and y[4] >= x[4] - .25 and \
                        any(y[i] > x[i] + 1e-9 for i in range(4)):
                    dropped.add(id(t))
                    t['dominated_by'] = o['id']
                    break
            if id(t) in dropped:
                break
    return [t for t in cands if id(t) not in dropped], [t for t in cands if id(t) in dropped]


def _player(ctx, pid, values, statuses, market=None):
    p, v = ctx.players[pid], ctx.proj[pid]
    s = v['values']
    m = (market or {}).get(pid) or {}
    ecr = (m.get('signals') or {}).get('ecr') or {}
    return {'site_player_id': pid, 'name': p.name, 'adp': m.get('rank'), 'ecr': ecr.get('rank'),
            'market_value': m.get('value'), 'market_disagree': m.get('disagree', False),
            'nba_team': p.nba_team, 'positions': list(p.eligible_positions),
            'slot': p.assigned_slot, 'yahoo_status': statuses.get(pid), 'coverage': v['coverage'],
            'per_game': {'gp': s['gp'], 'pts': s['pts'], 'reb': s['reb'], 'ast': s['ast'], 'three_ptm': s['three_ptm'],
                         'stl': s['stl'], 'blk': s['blk'], 'turnovers': s['turnovers'],
                         'fg_pct': s['fgm'] / s['fga'] if s['fga'] else None,
                         'ft_pct': s['ftm'] / s['fta'] if s['fta'] else None, 'fga': s['fga'], 'fta': s['fta']},
            'value': {k: values[pid][k] for k in ('value', 'per_game_z', 'replacement_position',
                                                  'replacement_player', 'team_games')},
            'z': values[pid]['z']}


def _cats(delta, sign):
    return [LABELS[c] for c, d in sorted(delta.items(), key=lambda kv: -abs(kv[1])) if d * sign > 0]


def _names(players):
    return ' + '.join(p['name'] for p in players)


def _balance_lines(t):
    """Plain-language roster balancing for both sides; numbers are rest-of-season category wins."""
    lines = []
    for side in ('me', 'partner'):
        b, who = t['balancing'][side], ('You' if side == 'me' else t['partner']['team_name'])
        if b['kind'] in ('drop', 'il'):
            p = b['player']
            what = (f"drop {p['name']} (asset value {p['value']['value']:.0f})" if b['kind'] == 'drop'
                    else f"move {p['name']} (Yahoo {p['yahoo_status']}) to IL; IL eligibility is unverified")
            line = f"{who} must {what} to stay at {ACTIVE_LIMIT} active players."
            if b.get('cost') is not None:
                line += (f" The exchange alone is worth {b['exchange_points']:+.1f} to {'you' if side == 'me' else 'them'};"
                         f" the {'drop' if b['kind'] == 'drop' else 'IL move'} changes that by {-b['cost']:+.1f}.")
            if len(b.get('options') or ()) > 1:
                alt = sorted(b['options'], key=lambda o: -o['points_delta'])[1]
                line += f" Next best: {alt['name']} ({alt['points_delta']:+.1f} overall)."
            lines.append(line)
        elif b['kind'] == 'open':
            f = b.get('fill')
            have = 'have' if side == 'me' else 'has'
            if f and f['used']:
                o = f['opportunity']
                line = (f"{who} {have} an open roster spot after the trade. Results include adding free agent "
                        f"{f['player']['name']}, worth {f['points_gain']:+.1f} over leaving it open "
                        f"({f['open_points']:+.1f} without him). Without any trade, the same add"
                        f"{' (dropping ' + o['drop'] + ')' if o['drop'] else ''} would be worth {o['points']:+.1f}, "
                        f"so ranking counts the trade at {t['effective'][side]:+.1f}. "
                        f"Conditional: one acquisition in Week {f['week']}"
                        f"{'' if side == 'me' else ' (their adds are unverified)'}; free-agent status is from the capture.")
            else:
                line = f"{who} {have} an open roster spot after the trade; results assume it stays open."
                if f:
                    line += (f" The best free-agent fill ({f['player']['name']}, {f['points_gain']:+.1f}) is not counted"
                             f"{': no acquisitions left that week' if not f['feasible'] else ''}.")
            lines.append(line)
    if t.get('streaming_slot'):
        lines.append('This 2-for-1 frees a useful streaming slot: even one permanent free-agent fill adds category wins, '
                     'and the Waiver Advisor can plan short-term streams for it.')
    return lines


def _ranks(players):
    def one(p):
        bits = [f"ADP {p['adp']:.0f}" if p.get('adp') else 'undrafted']
        if p.get('ecr'):
            bits.append(f"ECR {p['ecr']:.0f}")
        return f"{p['name']} ({', '.join(bits)})"
    return ', '.join(one(p) for p in players)


TIER_LABELS = {'excluded': 'Below threshold', 'unchecked': 'Unchecked'}


def _describe(t, cfg, ctx):
    me, them, gives, gets, m = t['me'], t['partner'], t['gives_all'], t['receives_all'], t['value']['market']
    gained, lost = _cats(me['category_delta'], 1), _cats(me['category_delta'], -1)
    their_gain = _cats(them['category_delta'], 1)
    robust, eff, cost = _robust(t, cfg), t['effective']['me'], _market_cost(t)
    surplus = m['partner_surplus']
    label = market_label(t, cfg) if t.get('tier') == 'recommended' else TIER_LABELS.get(t.get('tier'), 'Below threshold')

    my_gain = (f"{robust:+.1f} category wins over the rest of the season"
               + (f" ({eff:+.1f} on the blended projection; the lower figure is what holds under each source)" if eff - robust >= .05 else '')
               + f"; next three weeks {me['near_term_delta']:+.1f}."
               + (f" Improves {', '.join(gained[:4])}" if gained else '') + (f"; costs {', '.join(lost[:3])}." if lost else '.'))
    cut = f" plus {m['my_cut']:.0f} for the player you cut" if m.get('my_cut', 0) >= .5 else ''

    def pts(x):
        return f"{x:.0f} market point" + ('' if round(x) == 1 else 's')
    give_up = (f"You send {m['give']:.0f} market points{cut} and receive {m['get']:.0f}: "
               + (f"{pts(cost)} surrendered" if cost >= .5 else f"you gain {pts(-cost)}" if cost <= -.5 else 'even') +
               f". {_ranks(gives)}.")
    their_cut = f" after cutting a player worth {m['partner_cut']:.0f}" if m.get('partner_cut', 0) >= .5 else ''
    consider = []
    if -t['value']['gap'] > cfg.fair_band:
        consider.append(f"By draft position and consensus rankings they receive the more valuable side ({_ranks(gives)} "
                        f"for {_ranks(gets)}).")
    elif t['value']['gap'] <= cfg.fair_band and t['value']['gap'] >= -cfg.fair_band:
        consider.append('By draft position and consensus rankings the two sides are close.')
    else:
        consider.append(f"By conventional value they give up more ({_ranks(gets)} for {_ranks(gives)}); expect pushback.")
    if them['incoming_games']:
        consider.append(f"{_names(gives)} would start about {them['incoming_starts']} of {them['incoming_games']} "
                        f"remaining team games for them.")
    if their_gain and t['effective']['partner'] >= 0:
        weak = [LABELS[c] for c, d in them['category_delta'].items()
                if d > 0 and LABELS[c] in their_gain[:3] and (them['strength_before'][c] or 0) >= 6]
        consider.append(f"The model has them improving {', '.join(their_gain[:3])}" +
                        (f", a weakness for them ({', '.join(weak[:2])})" if weak else '') +
                        f" (net {t['effective']['partner']:+.1f} category wins).")
    elif them['adjusted_delta'] >= 0:
        consider.append('The model shows no category-win loss for them.')
    benefit = []
    if gained:
        weak = [LABELS[c] for c, d in me['category_delta'].items()
                if d > 0 and LABELS[c] in gained[:3] and (me['strength_before'][c] or 0) >= 6]
        benefit.append(f"Schedule-aware replay adds {', '.join(gained[:3])}" +
                       (f", where you rank in the bottom half ({', '.join(weak[:2])})" if weak else '') + '.')
    benefit.append(f"Daily lineups: {_names(gets)} would start {me['incoming_starts']} of {me['incoming_games']} remaining "
                   f"team games for you; {_names(gives)} would have started {me['outgoing_starts']} of {me['outgoing_games']}.")
    if me['seed_after'] != me['seed_before']:
        benefit.append(f"Your projected seed moves from #{me['seed_before']} to #{me['seed_after']}.")
    rival = abs(them['seed_before'] - me['seed_before']) <= 2
    if rival and t['effective']['partner'] < 0:
        benefit.append(f"It also weakens {them['team_name']}, a nearby standings rival (seed #{them['seed_before']}).")
    risks = []
    if label == 'Expensive improvement':
        risks.append(f"Expensive: {cost:.0f} market points for {robust:+.1f} wins "
                     f"({cost / max(robust, .1):.0f} per win; the line is {cfg.expensive_cost_per_win:.0f}).")
    for p in (*gives, *gets):
        if p['yahoo_status'] in INACTIVE_STATUSES | DOUBTFUL_STATUSES:
            risks.append(f"{p['name']} is listed {p['yahoo_status']} on Yahoo; his return date is not modelled.")
        if p['slot'] == 'IL':
            risks.append(f"{p['name']} is on injured reserve; IL slot use after the trade is unverified.")
        if p['coverage'] != 'two_sources':
            risks.append(f"{p['name']} has only one projection source.")
        if p.get('market_disagree'):
            risks.append(f"ADP and consensus rankings disagree on {p['name']}; his market value is less certain.")
    if (m.get('signals_disagree')):
        risks.append('ADP and consensus rankings point different ways on this package\'s balance.')
    for u in t.get('low_usage') or ():
        risks.append(f"Asset sacrifice: {u['name']} starts only {u['starts']} of {u['games']} remaining games for you, so the "
                     f"replay charges little for losing him; injuries could make him matter again.")
    model = t['value'].get('model_gap', 0.0)
    if model < -cfg.overpay_limit:
        risks.append('Asset sacrifice: by roster-neutral model value you give up much more than you receive.')
    alt = t.get('simpler_alternative')
    if alt:
        risks.append(f"Simpler option: {alt['title']} alone gives {alt['me_adjusted']:+.1f} for you at lower asset cost.")
    if t['package'] == '2-1':
        risks.append(f"{them['team_name']} takes two players and must clear a roster spot; they may value depth less.")
    elif t['package'] == '1-2':
        risks.append('You take two players and must clear a roster spot; the cut is part of the result above.')
    narrow = [f for f in me['flips'] if f['narrow'] and f['delta'] > 0]
    if narrow:
        risks.append(f"{len(narrow)} of your gained category results are within a narrow margin.")
    checks = t.get('sources') or {}
    if any(not x.get('unavailable') and x['me_points'] <= 0 for x in checks.values()):
        risks.append('One projection source shows no gain for you.')
    if _fill_dependent(t, cfg):
        risks.append('The result leans on one specific free agent still being available.')
    if rival and t['effective']['partner'] >= cfg.material_gain:
        risks.append(f"It strengthens {them['team_name']}, a nearby standings rival (seed #{them['seed_before']}).")
    if t['effective']['partner'] < 0:
        risks.append(f"The model has {them['team_name']} losing {-t['effective']['partner']:.1f} wins; they may see it later.")
    if any(r['opponent'] == them['team_id'] for r in me['near_term']):
        risks.append(f"You play {them['team_name']} soon; the trade changes that matchup for both teams.")
    if all(not x['delta'] for x in me['near_term']):
        risks.append('No change in the next three weeks; the gain comes later in the season.')
    elif me['near_term_delta'] < 0:
        risks.append(f"Costs {-me['near_term_delta']:.1f} category wins over the next three weeks before the gain arrives.")
    they_receive = (f"{them['team_name']} {'gains' if surplus >= 0 else 'loses'} {pts(abs(surplus))}{their_cut} "
                    f"({abs(100 * t['value']['gap']):.0f}% of the larger side). They get {_ranks(gives)}; "
                    f"they give {_ranks(gets)}.")
    return {'title': f"{_names(gives)} for {_names(gets)}", 'partner': them['team_name'],
            'package_label': PACKAGE_LABELS[t['package']], 'label': label, 'tier_label': label,
            'my_gain': my_gain, 'give_up': give_up, 'they_receive': they_receive,
            'why_consider': consider, 'why_benefit': benefit, 'risks': risks,
            'why': consider + benefit, 'warnings': risks,
            'gained': gained, 'lost': lost, 'partner_gained': their_gain, 'partner_lost': _cats(them['category_delta'], -1),
            'balancing': _balance_lines(t), 'robustness': t.get('robustness', 'Unchecked')}


def _acquisition_budget(inputs, evidence, eff_day):
    """Adds left in the scoring period of the effective day. Trades themselves use no acquisitions."""
    week = next((w for w in inputs['weeks'][:18] if w.start <= eff_day <= w.end), None)
    if week is None:
        return None, 0, ['effective_day_outside_regular_season']
    ev = evidence or {}
    used, ev_week = ev.get('adds_used'), ev.get('week')
    if isinstance(used, int) and ev_week == week.number:
        return week.number, max(0, week.max_adds - used), []
    if isinstance(used, int) and isinstance(ev_week, int) and ev_week < week.number:
        return week.number, week.max_adds, []
    return week.number, week.max_adds, ['acquisition_count_unverified']


def free_agent_fills(ctx, statuses, limit):
    """Currently listed free agents (never waiver players) for an open spot: the best by projected
    rest-of-season lineup value, plus the best at each base position."""
    rows, waivers = [], 0
    for row in pool_audit(ctx.inputs, ctx.weight)['rows']:
        pid = row['site_player_id']
        if row['owner_team_id']:
            continue
        if row['availability'] == 'W':
            waivers += 1
            continue
        sp = ctx.site.get(pid)
        if row['availability'] != 'FA' or not row['projectable'] or statuses.get(pid) in INACTIVE_STATUSES or \
                sp is None or not ctx.team_games[sp.nba_team]:
            continue
        try:
            v = resolve_projection(ctx.inputs['projections'][pid], ctx.weight)['values']
        except (ValueError, KeyError):
            continue
        rows.append((max(0.0, _lineup_score(v)) * v['gp'] / 82 * ctx.team_games[sp.nba_team], pid))
    rows.sort(key=lambda r: (-r[0], r[1]))
    chosen = [pid for _, pid in rows[:limit]]
    for pos in BASE_POSITIONS if limit else ():
        best = next((pid for _, pid in rows if pos in ctx.site[pid].eligible_positions), None)
        if best and best not in chosen:
            chosen.append(best)
    return chosen, waivers


def _bal(b):
    if b['kind'] in ('drop', 'il'):
        return {'drops' if b['kind'] == 'drop' else 'il': (b['player']['site_player_id'],)}
    f = b.get('fill')
    return {'adds': (f['player']['site_player_id'],)} if f and f['used'] else {}


def _similar(a, b, values):
    """Same partner, and one side identical while the other differs only by near-replacement players."""
    if a['partner']['team_id'] != b['partner']['team_id']:
        return False
    ga, gb = {p['site_player_id'] for p in a['gives_all']}, {p['site_player_id'] for p in b['gives_all']}
    ra, rb = {p['site_player_id'] for p in a['receives_all']}, {p['site_player_id'] for p in b['receives_all']}
    for same, x, y in ((ra == rb, ga, gb), (ga == gb, ra, rb)):
        if same and x != y and all(values[p]['value'] <= VALUE_FLOOR for p in x ^ y):
            return True
    return False


def trade_lab(inputs: dict, *, team_id=None, weight: float = .5, as_of=None, actual_state=None,
              evidence=None, yahoo_status=None, config: TradeConfig = TradeConfig(), now=None,
              base=None, source_bases=None) -> dict:
    """Rank realistic 1-for-1, 2-for-1 and 1-for-2 trades for my team against HOLD. Read-only.

    Staged search: (1) every unique package with hard roster feasibility, (2) a cheap asset-value
    and linear lineup screen with per-type and per-partner quotas, (3) exact two-team replay with
    the best legal drop among the cheapest few, (4) cached team-week totals, distinct-card and
    equivalent-package pruning, (5) finalist balancing detail, 1-for-1 comparison and
    single-source rechecks. `base` / `source_bases` may be the dashboard's cached forecasts.
    """
    started = perf_counter()
    cfg = config
    team_id = team_id or inputs['league']['my_team_id']
    if team_id not in inputs['teams']:
        raise ValueError('Unknown team')
    as_of = as_of or _utc(inputs['availability']['captured_at_utc'])
    now = now or datetime.now(UTC)
    if as_of.tzinfo is None or now.tzinfo is None:
        raise ValueError('As-of and current times must include a timezone')
    as_of, now = as_of.astimezone(UTC), now.astimezone(UTC)
    rules = trade_rules(inputs)
    today = as_of.astimezone(EASTERN).date()
    if today > rules['deadline']:
        raise ValueError(f"The trade deadline ({rules['deadline'].isoformat()}) has passed")
    eff_day = today + timedelta(days=cfg.acceptance_days + rules['reject_days'])
    stamp = as_of.isoformat().replace('+00:00', 'Z')
    if not (base and base.get('hashtag_weight') == weight and base.get('as_of_utc') == stamp):
        base = forecast(inputs, hashtag_weight=weight, as_of=as_of, actual_state=actual_state)
    if base['actual_completed_weeks'] >= 18 or eff_day > inputs['weeks'][17].end:
        raise ValueError('No regular-season games remain after the earliest trade effective date')
    ctx = _Context(inputs, base, weight, as_of, actual_state, eff_day)
    partners = tuple(config.partner_ids or [t for t in ctx.teams if t != team_id])
    if any(p not in inputs['teams'] or p == team_id for p in partners):
        raise ValueError('Choose trade partners from the other league teams')
    mine_ids = {p.site_player_id for p in inputs['rosters'][team_id]}
    if not set(cfg.protected_ids) <= mine_ids:
        raise ValueError('Protected players must be on your roster')
    ev = evidence or {}
    statuses = ev.get('status_by_site_id') or inputs.get('injury_statuses', {})
    values = asset_values(ctx, statuses)
    market = market_values(inputs, ctx, as_of)

    def mval(ids, signal=None):
        """Market value of one side: the best player in full, each further player at depth_discount (the side that
        gets the best player in a deal is the one managers see as winning it)."""
        vals = sorted(((market[p]['signals'].get(signal) or market[p])['value'] if signal else market[p]['value']
                       for p in _ids(ids)), reverse=True)
        return sum(v * (1 if i == 0 else cfg.depth_discount) for i, v in enumerate(vals))

    def market_ok(c):
        """The partner would not lose more than viable_market_limit by market value. Paying the partner a
        market premium never removes a package."""
        return c['market_gap'] <= cfg.viable_market_limit

    def viable(c):
        """Replayed: market_ok, and either within viable_model_floor of roster-neutral model value or (past it)
        a linear-screen gain of at least floor_screen_gate for you. The model value alone never decides."""
        return market_ok(c) and (cfg.viable_model_floor is None or c['gap'] >= -cfg.viable_model_floor or
                                 (cfg.floor_screen_gate is not None and c['screen_me'] >= cfg.floor_screen_gate))
    skipped = Counter()
    protected = frozenset(cfg.protected_ids)

    def tradeable(team, protect=frozenset()):
        out = []
        for p in inputs['rosters'][team]:
            if p.site_player_id in protect:
                skipped['protected'] += 1
            elif p.assigned_slot == 'IL' and not cfg.include_injured_reserve:
                skipped['injured_reserve_slot'] += 1
            else:
                out.append(p.site_player_id)
        return out
    mine = tradeable(team_id, protected)
    theirs_by = {p: tradeable(p) for p in partners}
    week_games = []
    for days in ctx.week_days:
        later = {d for d in days if d >= eff_day}
        week_games.append(Counter(nba for (nba, day), g in inputs['schedule'].team_dates.items()
                                  if day in later and g.tipoff_utc > as_of))
    rates = {t: _screen_rates(ctx, t) for t in (team_id, *partners)}

    # Roster-balancing inputs. Yahoo's league-wide Can't Cut list limits drops (not trades).
    cant_cut_ids = ev.get('league_cant_cut_site_ids')
    cant_cut = frozenset(cant_cut_ids or ())
    il_rule = frozenset(ev.get('user_supplied_il_eligible_statuses') or IL_MOVE_STATUSES)
    rosters = {t: inputs['rosters'][t] for t in ctx.teams}
    active = {t: sum(p.assigned_slot != 'IL' for p in rosters[t]) for t in ctx.teams}
    il_used = {t: sum(p.assigned_slot == 'IL' for p in rosters[t]) for t in ctx.teams}
    lineup_value = {}
    for t in (team_id, *partners):
        started_ = sum(ctx.base_raw[t]['after_starts'], Counter())
        lineup_value[t] = {p.site_player_id: started_[p.site_player_id] * ctx.engine.score[p.site_player_id]
                           for p in rosters[t]}
    budget_week, budget, budget_gaps = _acquisition_budget(inputs, evidence, eff_day)

    drop_cache = {}

    def drop_options(team, out):
        """Legal ways to clear one active spot, cheapest projected lineup loss first (IL move before drop)."""
        key = (team, frozenset(out))
        if key in drop_cache:
            return drop_cache[key]
        opts = drop_cache[key] = []
        for p in rosters[team]:
            pid = p.site_player_id
            if pid in out or p.assigned_slot == 'IL' or (team == team_id and pid in protected):
                continue
            if statuses.get(pid) in il_rule and il_used[team] < IL_LIMIT:
                opts.append(('il', pid, lineup_value[team][pid]))
            if pid not in cant_cut:
                opts.append(('drop', pid, lineup_value[team][pid]))
        opts.sort(key=lambda o: (o[2], o[0] != 'il', o[1]))
        return opts

    def lost(o):
        return max(0.0, values[o[1]]['value']) if o and o[0] == 'drop' else 0.0

    # Free agents (never waiver players) that could fill a spot a 2-for-1 or 1-for-2 leaves open.
    multi = any(PACKAGES[k][0] != PACKAGES[k][1] for k in cfg.package_types)
    fills, waiver_players = free_agent_fills(ctx, statuses, cfg.fill_candidates) if multi else ([], 0)
    for pid in fills:
        ctx.register(pid)

    waiver_cache = {}

    def waiver_alternative(team, fa):
        """The same free agent added without any trade, dropping the team's cheapest legal player."""
        if (team, fa) not in waiver_cache:
            opts = [o for o in drop_options(team, set()) if o[0] == 'drop']
            if not opts:
                waiver_cache[team, fa] = {'adjusted': 0.0, 'points': 0.0, 'drop': None}
            else:
                raws = ctx.team_weeks(team, (), (), (opts[0][1],), (fa,))
                changed = {team: raws['raw']}
                s_ = _side(ctx, cfg, team, changed, ctx.season(changed), raws, (), ())
                waiver_cache[team, fa] = {'adjusted': s_['adjusted_delta'], 'points': s_['points_delta'],
                                          'drop': ctx.players[opts[0][1]].name}
        return waiver_cache[team, fa]

    def fill_open_side(side, team, partner, gives, gets, r, mb, tb):
        """Best free-agent fill for an open spot. Used in the result only when it helps and an acquisition is
        available; the open-spot result is kept beside it."""
        tried = []
        for fa in fills:
            add = {'adds': (fa,)}
            rr = evaluate(ctx, cfg, team_id, partner, gives, gets, {**mb, **add} if side == 'me' else mb,
                          {**tb, **add} if side == 'partner' else tb)
            opp = waiver_alternative(team, fa)
            tried.append((rr[side]['adjusted_delta'] - max(0.0, opp['adjusted']), rr[side]['points_delta'], fa, rr, opp))
        if not tried:
            return r, None
        tried.sort(key=lambda x: (-x[0], -x[1], x[2]))
        net, pts, fa, rr, opp = tried[0]
        adj = rr[side]['adjusted_delta']
        net2, pts2, fa2, _, _ = tried[1] if len(tried) > 1 else (r[side]['adjusted_delta'], r[side]['points_delta'], None, r, None)
        feasible = budget > 0 if side == 'me' else True
        gaps = ['free_agent_status_is_a_dated_observation'] + (
            (budget_gaps + ([] if feasible else ['no_acquisitions_left_in_effective_week'])) if side == 'me'
            else ['partner_acquisitions_unverified'])
        used = feasible and net > r[side]['adjusted_delta']
        sp = ctx.site[fa]
        fill = {'player': {'site_player_id': fa, 'name': sp.name, 'nba_team': sp.nba_team,
                           'positions': list(sp.eligible_positions), 'yahoo_status': statuses.get(fa)},
                'used': used, 'feasible': feasible, 'consumes_acquisition': True, 'week': budget_week,
                'acquisitions_left': budget if side == 'me' else None, 'gaps': gaps,
                'open_points': r[side]['points_delta'], 'open_adjusted': r[side]['adjusted_delta'],
                'points_gain': pts - r[side]['points_delta'], 'adjusted_gain': adj - r[side]['adjusted_delta'],
                'opportunity': opp, 'net_adjusted': net,
                'fallback': {'name': ctx.site[fa2].name if fa2 else None, 'net_adjusted': net2,
                             'points_gain': pts2 - r[side]['points_delta']}}
        return (rr if used else r), fill

    # 1-2. Unique packages, hard feasibility, cheap screen.
    t0 = perf_counter()
    candidates, stage = [], {k: Counter() for k in cfg.package_types}
    for partner in partners:
        for typ in cfg.package_types:
            n_give, n_get = PACKAGES[typ]
            for gives in combinations(sorted(mine), n_give):
                for gets in combinations(sorted(theirs_by[partner]), n_get):
                    stage[typ]['generated'] += 1
                    plan, reason = {}, None
                    for side, team, out, inc in (('me', team_id, gives, gets), ('partner', partner, gets, gives)):
                        need = active[team] - len(out) + len(inc) - ACTIVE_LIMIT
                        plan[side] = {'need': need, 'options': drop_options(team, set(out)) if need > 0 else []}
                        if need > 1:
                            reason = 'needs_more_than_one_drop'
                        elif need == 1 and not plan[side]['options']:
                            reason = 'no_legal_drop'
                    if reason:
                        stage[typ]['infeasible_' + reason] += 1
                        continue
                    md, pd = (plan[s]['options'][0] if plan[s]['options'] else None for s in ('me', 'partner'))
                    mf, pf = ((fills[0],) if fills and plan[s]['need'] < 0 else () for s in ('me', 'partner'))
                    give_v, get_v = side_value(values, gives), side_value(values, gets)
                    stage[typ]['feasible'] += 1
                    cand = {'type': typ, 'partner': partner, 'give': gives, 'get': gets, 'plan': plan,
                            'gap': value_gap(get_v - lost(md), give_v - lost(pd)),
                            'market_gap': value_gap(mval(gets), mval(gives) - (mval(pd[1]) if pd and pd[0] == 'drop'
                                                                                 else 0), MARKET_FLOOR),
                            '_screen': (md, pd, mf, pf)}
                    # The lineup screen is the costly part, so it runs only for packages that could qualify
                    # (every package in exhaustive mode), in parallel.
                    if cfg.shortlist is None or market_ok(cand):
                        candidates.append(cand)

    def screen_one(c):
        md, pd, mf, pf = c['_screen']
        return (screen(ctx, team_id, c['give'] + ((md[1],) if md else ()), c['get'] + mf, rates[team_id], week_games),
                screen(ctx, c['partner'], c['get'] + ((pd[1],) if pd else ()), c['give'] + pf, rates[c['partner']],
                       week_games))
    for c, (sm, sp) in zip(candidates, _replay_all(screen_one, candidates, cfg.workers)):
        c['screen_me'], c['screen_partner'] = sm, sp
        del c['_screen']
    screen_seconds = perf_counter() - t0

    def screen_key(c):
        return (-(c['screen_me'] + .5 * max(-2, min(c['screen_partner'], 3))), c['partner'], c['give'], c['get'])
    # The screen is linear, so it keeps near-misses; quotas keep every package type and partner represented.
    # Viable = could still qualify: the partner would not see it as lopsided by draft position, and you are not
    # giving up far more model value. No screen-score cut: every viable package is replayed by default.
    plausible = sorted((c for c in candidates if viable(c)), key=screen_key)
    for c in plausible:
        stage[c['type']]['plausible'] += 1
    if cfg.shortlist is None:
        shortlist = sorted(candidates, key=screen_key)
    elif cfg.replay_all_viable:
        shortlist = plausible
    else:
        shortlist = []
        for typ in cfg.package_types:
            group = [c for c in plausible if c['type'] == typ]
            chosen = group[:cfg.shortlist if typ == '1-1' else cfg.multi_shortlist]
            taken, per = {id(c) for c in chosen}, Counter(c['partner'] for c in chosen)
            for c in group:
                if per[c['partner']] < cfg.partner_floor and id(c) not in taken:
                    chosen.append(c)
                    taken.add(id(c))
                    per[c['partner']] += 1
            shortlist += chosen

    # 3. Exact two-team replay; the side that must clear a spot picks its best legal drop.
    t0 = perf_counter()
    base_after_me = sum(ctx.base_raw[team_id]['after_starts'], Counter())
    evaluated, drops_replayed = [], 0

    def replay_one(c):
        drops_replayed = 0
        partner, gives, gets, plan = c['partner'], c['give'], c['get'], c['plan']
        side = 'me' if plan['me']['options'] else 'partner' if plan['partner']['options'] else None
        tried, choice, opts = [], None, ()

        def choose_drop(other):
            """The clearing side keeps its best legal option, with the other side's balancing fixed."""
            nonlocal drops_replayed     # counted per package; summed by the caller
            best, rows = None, []
            for kind, pid, proxy in opts:
                bal = {'drops': (pid,)} if kind == 'drop' else {'il': (pid,)}
                r_ = evaluate(ctx, cfg, team_id, partner, gives, gets, bal if side == 'me' else other,
                              bal if side == 'partner' else other)
                drops_replayed += 1
                s = r_[side]
                rows.append({'kind': kind, 'site_player_id': pid, 'name': ctx.players[pid].name,
                             'points_delta': s['points_delta'], 'adjusted_delta': s['adjusted_delta']})
                k = (s['points_delta'], s['adjusted_delta'], kind == 'il', -proxy)
                if best is None or k > best[0]:
                    best = (k, r_, (kind, pid))
            return best[1], best[2], rows
        if side is None:
            r = evaluate(ctx, cfg, team_id, partner, gives, gets)
        else:
            opts = plan[side]['options'] if cfg.drop_options is None else plan[side]['options'][:cfg.drop_options]
            r, choice, tried = choose_drop(None)
        give_v, get_v = side_value(values, gives), side_value(values, gets)
        balancing = {}
        for s_ in ('me', 'partner'):
            need = plan[s_]['need']
            balancing[s_] = {'kind': 'open' if need < 0 else 'none', 'open_spots': max(0, -need), 'player': None,
                             'value_lost': 0.0, 'options': []}
        if choice:
            kind, pid = choice
            balancing[side].update({'kind': kind, 'player': _player(ctx, pid, values, statuses, market), 'options': tried,
                                    'value_lost': max(0.0, values[pid]['value']) if kind == 'drop' else 0.0})
        for s_ in ('me', 'partner'):
            if balancing[s_]['kind'] == 'open' and fills:
                team_ = team_id if s_ == 'me' else partner
                r, balancing[s_]['fill'] = fill_open_side(s_, team_, partner, gives, gets, r,
                                                          _bal(balancing['me']), _bal(balancing['partner']))
                if choice and balancing[s_]['fill']['used']:
                    # Re-choose the drop with the fill in place (a head-to-head meeting can change its value).
                    r2, choice2, tried = choose_drop(_bal(balancing[s_]))
                    if choice2 == choice:
                        r = r2
                    else:
                        choice = choice2
                        kind, pid = choice
                        balancing[side].update({'kind': kind, 'player': _player(ctx, pid, values, statuses, market),
                                                'value_lost': max(0.0, values[pid]['value']) if kind == 'drop' else 0.0})
                        r_open = evaluate(ctx, cfg, team_id, partner, gives, gets, _bal(balancing['me']) if s_ != 'me'
                                          else None, _bal(balancing['partner']) if s_ != 'partner' else None)
                        r, balancing[s_]['fill'] = fill_open_side(s_, team_, partner, gives, gets, r_open,
                                                                  _bal(balancing['me']) if s_ != 'me' else {},
                                                                  _bal(balancing['partner']) if s_ != 'partner' else {})
                    balancing[side]['options'] = tried
        if choice:      # the chosen option shows the final result
            for o in balancing[side]['options']:
                if o['site_player_id'] == choice[1] and o['kind'] == choice[0]:
                    o['points_delta'], o['adjusted_delta'] = r[side]['points_delta'], r[side]['adjusted_delta']
        t = r
        my_drop, their_drop = balancing['me']['value_lost'], balancing['partner']['value_lost']
        model_gap = value_gap(get_v - my_drop, give_v - their_drop)
        cut = {s_: (balancing[s_]['player']['site_player_id'] if balancing[s_]['kind'] == 'drop' else None)
               for s_ in ('me', 'partner')}
        m_their_drop, m_my_cut = mval(cut['partner'] or ()), mval(cut['me'] or ())
        m_get, m_give = mval(gets), mval(gives)
        gap = value_gap(m_get, m_give - m_their_drop, MARKET_FLOOR)
        by_signal = {k: value_gap(mval(gets, k), mval(gives, k) - mval(cut['partner'] or (), k), MARKET_FLOOR)
                     for k in MARKET_SIGNALS if all(k in market[x]['signals'] for x in (*gives, *gets))}
        gives_all = sorted((_player(ctx, g, values, statuses, market) for g in gives), key=lambda p: -p['value']['value'])
        gets_all = sorted((_player(ctx, g, values, statuses, market) for g in gets), key=lambda p: -p['value']['value'])
        top_in = max(values[g]['value'] for g in gets)
        low = []
        for g in gives:
            games = ctx.team_games[ctx.players[g].nba_team]
            if games and values[g]['value'] >= top_in and base_after_me[g] / games < cfg.low_usage_share:
                low.append({'name': ctx.players[g].name, 'starts': base_after_me[g], 'games': games,
                            'value': values[g]['value']})
        t.update({'id': '+'.join(gives) + '-' + '+'.join(gets), 'package': c['type'],
                  'gives': gives_all[0], 'gives_all': gives_all, 'receives': gets_all[0], 'receives_all': gets_all,
                  'screen': {'me': c['screen_me'], 'partner': c['screen_partner']},
                  'value': {'give': give_v, 'get': get_v, 'my_drop': my_drop, 'partner_drop': their_drop,
                            'exchanged_gap': value_gap(get_v, give_v), 'model_gap': model_gap,
                            'model_label': fairness_label(model_gap, get_v - my_drop, give_v - their_drop, cfg),
                            'gap': gap, 'label': fairness_label(gap, m_get, m_give - m_their_drop, cfg, MARKET_FLOOR),
                            'basis': next(iter(market.values()))['basis'] if market else None,
                            'market': {'get': m_get, 'give': m_give, 'partner_drop': m_their_drop,
                                       'partner_cut': m_their_drop, 'my_cut': m_my_cut,
                                       'partner_surplus': m_give - m_get - m_their_drop,
                                       'my_cost': m_give + m_my_cut - m_get, 'by_signal': by_signal,
                                       'signals_disagree': len(by_signal) > 1 and
                                       max(by_signal.values()) - min(by_signal.values()) > .25,
                                       'get_ranks': [market[x]['rank'] for x in gets],
                                       'give_ranks': [market[x]['rank'] for x in gives]}},
                  'balancing': balancing, 'low_usage': low})
        t['score'] = _score(t, cfg)
        t['effective'] = {s_: _effective(t, s_, cfg) for s_ in ('me', 'partner')}
        t['_drop_replays'] = drops_replayed
        return t

    for t in _replay_all(replay_one, shortlist, cfg.workers):
        stage[t['package']]['replayed'] += 1
        for s_ in ('me', 'partner'):
            if (t['balancing'][s_].get('fill') or {}).get('used'):
                stage[t['package']]['fill_used_' + s_] += 1
        drops_replayed += t.pop('_drop_replays')
        evaluated.append(t)
    replay_seconds = perf_counter() - t0
    evaluated.sort(key=lambda t: (-t['score'], t['id']))

    # 5. Finalists: inferior variations removed (Pareto), a few places for every package type among realistic
    # packages, and a few for difficult sells (the partner loses market value).
    t0 = perf_counter()
    open_, dominated = _pareto([t for t in evaluated if _classify(t, cfg)[0] != 'excluded'], cfg)
    realistic = [t for t in open_ if t['value']['gap'] <= cfg.fair_band]
    room = cfg.finalists
    finalists = [t for t in realistic if -t['value']['gap'] > cfg.fair_band][:min(room, cfg.premium_finalists)]
    taken = {t['id'] for t in finalists}
    for typ in (cfg.package_types if room else ()):
        have = sum(t['package'] == typ for t in finalists)
        extra = [t for t in realistic if t['package'] == typ and t['id'] not in taken][:max(0, cfg.type_reserve - have)]
        finalists += extra
        taken |= {t['id'] for t in extra}
    rest = [t for t in realistic if t['id'] not in taken][:max(0, room - len(finalists))]
    finalists += rest
    taken |= {t['id'] for t in rest}
    if cfg.finalists:       # enough source-checked packages to always show result_limit cards
        backfill = [t for t in evaluated if t['id'] not in taken][:max(0, 2 * cfg.result_limit - len(finalists))]
        finalists += backfill
    for t in finalists:
        stage[t['package']]['finalists'] += 1

    # Balancing detail: exchange-only value, drop cost, conditional free-agent fills, 1-for-1 subsets.
    for t in finalists:
        partner = t['partner']['team_id']
        gives = tuple(p['site_player_id'] for p in t['gives_all'])
        gets = tuple(p['site_player_id'] for p in t['receives_all'])
        mb, tb = _bal(t['balancing']['me']), _bal(t['balancing']['partner'])
        for side in ('me', 'partner'):
            b = t['balancing'][side]
            if b['kind'] in ('drop', 'il'):
                nd = evaluate(ctx, cfg, team_id, partner, gives, gets, None if side == 'me' else mb,
                              None if side == 'partner' else tb)
                b['exchange_points'] = nd[side]['points_delta']
                b['cost'] = nd[side]['points_delta'] - t[side]['points_delta']
        fill = t['balancing']['me'].get('fill')
        t['streaming_slot'] = bool(fill and fill['used'] and fill['points_gain'] >= .5)
        t['simpler_alternative'] = None
        if t['package'] != '1-1':
            cost = t['value']['give'] + t['value']['my_drop'] - t['value']['get']
            subsets = []
            for g in gives:
                for r_id in gets:
                    rr = evaluate(ctx, cfg, team_id, partner, g, r_id)
                    subsets.append({'id': f'{g}-{r_id}', 'title': f"{ctx.players[g].name} for {ctx.players[r_id].name}",
                                    'me_adjusted': rr['me']['adjusted_delta'],
                                    'partner_adjusted': rr['partner']['adjusted_delta'],
                                    'asset_cost': values[g]['value'] - values[r_id]['value']})
            subsets.sort(key=lambda a: (-a['me_adjusted'], a['asset_cost']))
            t['one_for_one_subsets'], t['asset_cost'] = subsets, cost
            simpler = [a for a in subsets if a['me_adjusted'] >= _effective(t, 'me', cfg) - cfg.simpler_band
                       and a['asset_cost'] <= cost and a['partner_adjusted'] >= min(0.0, t['partner']['adjusted_delta'])]
            t['simpler_alternative'] = simpler[0] if simpler else None
    balance_seconds = perf_counter() - t0

    # Projection-source sensitivity with the same package and balancing.
    t0 = perf_counter()
    contexts = {}
    for key, label, w in (SOURCE_WEIGHTS if finalists else ()):
        try:
            b = (source_bases or {}).get(w)
            if not (b and b.get('as_of_utc') == stamp and b.get('hashtag_weight') == w):
                b = forecast(inputs, hashtag_weight=w, as_of=as_of, actual_state=actual_state)
            sctx = _Context(inputs, b, w, as_of, actual_state, eff_day)
            for pid in fills:
                try:
                    sctx.register(pid)
                except (ValueError, KeyError):
                    pass      # a fill without this source's projection makes that check unavailable
            contexts[key] = (label, sctx, asset_values(sctx, statuses))
        except (ValueError, KeyError) as exc:
            contexts[key] = (label, None, str(exc))
    for t in finalists:
        t['sources'] = {}
        gives = tuple(p['site_player_id'] for p in t['gives_all'])
        gets = tuple(p['site_player_id'] for p in t['receives_all'])
        mb, tb = _bal(t['balancing']['me']), _bal(t['balancing']['partner'])
        for key, (label, sctx, sval) in contexts.items():
            if sctx is None:
                t['sources'][key] = {'label': label, 'unavailable': sval[:200]}
                continue
            try:
                if any(a not in sctx.engine.players for a in (*mb.get('adds', ()), *tb.get('adds', ()))):
                    raise ValueError('free-agent fill has no projection under this source')
                r = evaluate(sctx, cfg, team_id, t['partner']['team_id'], gives, gets, mb, tb)
            except (ValueError, KeyError) as exc:
                t['sources'][key] = {'label': label, 'unavailable': str(exc)[:200]}
                continue
            drop = lambda b: max(0.0, sval[b['player']['site_player_id']]['value']) if b['kind'] == 'drop' else 0.0
            g_v, r_v = side_value(sval, gives), side_value(sval, gets)
            t['sources'][key] = {'label': label, 'me_points': r['me']['points_delta'],
                                 'me_adjusted': r['me']['adjusted_delta'], 'partner_points': r['partner']['points_delta'],
                                 'value_gap': value_gap(r_v - drop(t['balancing']['me']),
                                                        g_v - drop(t['balancing']['partner']))}
        held = [s['me_points'] > 0 for s in t['sources'].values() if not s.get('unavailable')]
        t['robustness'] = ('Unchecked' if not held else 'Holds under both sources' if all(held) and len(held) == 2
                           else 'Holds under one source checked' if all(held)
                           else 'Fails under both sources' if not any(held) else 'Mixed across sources')
        t['score'] = _score(t, cfg)
        t['effective'] = {s_: _effective(t, s_, cfg) for s_ in ('me', 'partner')}
    source_seconds = perf_counter() - t0

    # Tiers, distinct cards (equivalent packages folded in), HOLD. Nothing is forced onto the page.
    excluded, final_ids = Counter(), {t['id'] for t in finalists}
    dominated_ids = {id(t) for t in dominated}
    for t in evaluated:
        t['tier'], reason = _classify(t, cfg) if t['id'] in final_ids else ('unchecked', 'not a source-checked finalist')
        t['tier_reason'] = reason
        t['label'] = market_label(t, cfg) if t['tier'] == 'recommended' else None
        if reason:
            excluded[reason] += 1
    folded = 0

    def pick(tier, limit):
        nonlocal folded
        out, gives_seen, gets_seen = [], Counter(), set()
        for t in sorted((t for t in evaluated if t['tier'] == tier), key=lambda t: (-t['score'], t['id'])):
            if len(out) >= limit:
                break
            twin = next((s_ for s_ in out if _similar(s_, t, values)), None)
            if twin:
                twin.setdefault('similar', []).append({'id': t['id'], 'title': f"{_names(t['gives_all'])} for {_names(t['receives_all'])}",
                                                       'me_adjusted': t['me']['adjusted_delta'],
                                                       'partner_adjusted': t['partner']['adjusted_delta']})
                folded += 1
                continue
            gs = [p['site_player_id'] for p in t['gives_all']]
            rs = [p['site_player_id'] for p in t['receives_all']]
            if any(gives_seen[g] >= cfg.per_player_limit for g in gs) or any(r in gets_seen for r in rs):
                continue
            gives_seen.update(gs)
            gets_seen.update(rs)
            t['display'] = _describe(t, cfg, ctx)
            t['alternative'] = False
            out.append(t)
            stage[t['package']]['shown'] += 1
        return out
    shown = pick('recommended', cfg.result_limit)
    ranked = [t for t in evaluated if t['tier'] == 'recommended']
    ranked.sort(key=lambda t: (-t['score'], t['id']))
    qualifying = len(shown)
    for t in evaluated:
        if t['tier'] == 'recommended':
            stage[t['package']][t['tier']] += 1
    best_by_type = {}
    for typ in cfg.package_types:
        top = next((t for t in ranked if t['package'] == typ), None)
        best_by_type[typ] = top and {'id': top['id'], 'title': f"{_names(top['gives_all'])} for {_names(top['receives_all'])}",
                                     'partner': top['partner']['team_name'], 'tier': top['tier'],
                                     'me_adjusted': top['me']['adjusted_delta'], 'me_effective': top['effective']['me'],
                                     'partner_adjusted': top['partner']['adjusted_delta'],
                                     'partner_effective': top['effective']['partner'],
                                     'value_label': top['value']['label'], 'label': top['label'],
                                     'score': round(top['score'], 3)}
    hold_side = {'team_id': team_id, 'team_name': inputs['teams'][team_id],
                 'record': ctx.base_record[team_id], 'seed': ctx.base_seed[team_id],
                 'win_pct': _win_pct(ctx.base_record[team_id]),
                 'strength_rank': {c: ctx.base_strength[team_id][c]['rank'] for c in CATEGORIES}}
    ranks = hold_side['strength_rank']
    hold_side['strongest'] = [LABELS[c] for c in sorted(CATEGORIES, key=lambda c: ranks[c] or 99)[:3]]
    hold_side['weakest'] = [LABELS[c] for c in sorted(CATEGORIES, key=lambda c: -(ranks[c] or 0))[:3]]
    if qualifying:
        top = shown[0]
        headline = (f"Best idea: {_names(top['gives_all'])} for {_names(top['receives_all'])} with "
                    f"{top['partner']['team_name']} ({top['display']['label'].lower()}; {_robust(top, cfg):+.1f} category "
                    f"wins for you, {_market_cost(top):+.0f} market points surrendered).")
    else:
        headline = 'HOLD: no realistic trade clears the gain and source checks.'
    balancing_gaps = sorted(set((['cant_cut_list_unverified'] if cant_cut_ids is None else []) + budget_gaps +
                                ['free_agent_status_is_a_dated_observation', 'partner_acquisitions_unverified'] +
                                (['il_eligibility_unverified'] if any(t['balancing'][s]['kind'] == 'il'
                                                                      for t in evaluated for s in ('me', 'partner')) else [])))
    return {
        'phase': '4D', 'team_id': team_id, 'team_name': inputs['teams'][team_id], 'hashtag_weight': weight,
        'as_of_utc': stamp, 'headline': headline, 'hold': hold_side,
        'cards': shown, 'qualifying_cards': qualifying, 'best_by_type': best_by_type,
        **({'finalists': finalists} if cfg.keep_finalists else {}),
        'market': market_summary(inputs, ctx, market, as_of),
        'options': {'partners': [{'team_id': t, 'team_name': inputs['teams'][t]} for t in ctx.teams if t != team_id],
                    'selected_partners': list(partners), 'protected_ids': sorted(cfg.protected_ids),
                    'package_types': list(cfg.package_types),
                    'roster': [{'site_player_id': p.site_player_id, 'name': p.name, 'slot': p.assigned_slot,
                                'value': values[p.site_player_id]['value']}
                               for p in sorted(inputs['rosters'][team_id], key=lambda p: -values[p.site_player_id]['value'])]},
        'counts': {'recommended': sum(t['tier'] == 'recommended' for t in evaluated),
                   'by_label': dict(Counter(t['label'] for t in evaluated if t.get('label')))},
        'timing': {'today': today.isoformat(), 'earliest_effective_day': eff_day.isoformat(),
                   'trade_deadline': rules['deadline'].isoformat(), 'review': rules['review'],
                   'reject_days': rules['reject_days'], 'assumed_acceptance_days': cfg.acceptance_days,
                   'remaining_weeks': [w.number for w in ctx.weeks]},
        'balancing': {'active_limit': ACTIVE_LIMIT, 'il_limit': IL_LIMIT, 'cant_cut_verified': cant_cut_ids is not None,
                      'il_move_statuses': sorted(il_rule), 'effective_week': budget_week, 'my_acquisitions_left': budget,
                      'free_agent_fills_tried': [ctx.site[p].name for p in fills],
                      'waiver_players_not_used': waiver_players, 'gaps': balancing_gaps},
        'verification': verification(inputs, evidence, yahoo_status, now, cfg.evidence_max_age_minutes),
        'assumptions': [
            f"Trades take effect from {eff_day.isoformat()}: {cfg.acceptance_days} day for the partner to accept plus the "
            f"{rules['reject_days']}-day {rules['review'].lower()} reject window. Earlier games count for the original teams.",
            'Every other team keeps its deterministic forecast; any matchup with the trade partner is recomputed with both new rosters.',
            f"Each team keeps at most {ACTIVE_LIMIT} active players. The side taking two players clears a spot with its best "
            f"legal drop among its {cfg.drop_options or 'all'} cheapest options (Can't Cut players excluded when the list is "
            "captured), or an IL move for an O/INJ player when an IL slot is free (IL eligibility unverified).",
            'An open spot is scored as left open. A free-agent fill is shown separately: it uses one acquisition in the '
            'effective week, relies on free-agent status at capture, never uses waiver players, and counts only partly in ranking.',
            'Trades themselves use no acquisitions. Keeper value, manager preferences, injury return dates and pending trades are not modelled.',
            'Category-win changes are deterministic projections, not probabilities. Market value and your competitive '
            'benefit are kept apart. Market value is how other managers are likely to see players: the mean of Yahoo '
            'preseason ADP and FantasyPros consensus rankings, each re-ranked among Yahoo\'s players and put on one '
            'auction-like curve; on a side with two players the second counts at half (the side that gets the best '
            'player is the one managers see as winning); any player a side must cut counts against that side. Paying '
            'the partner a market premium is allowed and ranked by what it buys; a package where the partner loses '
            'more than 10% by market value is never shown. Model value (z-scores over replacement) is shown only as an '
            'asset-sacrifice risk. None of this predicts acceptance.'],
        'diagnostics': {'partners': list(partners), 'my_tradeable': len(mine), 'pairs_screened': sum(v['feasible'] for v in stage.values()),
                        'plausible_after_screen': len(plausible), 'replayed': len(evaluated),
                        'drop_options_replayed': drops_replayed, 'source_checked': len(finalists),
                        'folded_equivalent': folded, 'dominated_variations': len(dominated), 'by_type': {k: dict(v) for k, v in stage.items()},
                        'skipped_players': dict(skipped), 'excluded': dict(excluded), 'exhaustive': cfg.shortlist is None,
                        'seconds': {'screen': round(screen_seconds, 2), 'replay': round(replay_seconds, 2),
                                    'balancing': round(balance_seconds, 2), 'sources': round(source_seconds, 2),
                                    'total': round(perf_counter() - started, 2)},
                        'engine': {'computed_days': ctx.engine.computed, 'cache_hits': ctx.engine.hits,
                                   'team_weeks_cached': len(ctx.cache)}},
        'all_evaluated': [{'id': t['id'], 'package': t['package'], 'partner': t['partner']['team_id'],
                           'gives': _names(t['gives_all']), 'receives': _names(t['receives_all']),
                           'me': t['me']['adjusted_delta'], 'me_points': t['me']['points_delta'],
                           'partner_adjusted': t['partner']['adjusted_delta'], 'me_effective': t['effective']['me'],
                           'partner_effective': t['effective']['partner'], 'gap': t['value']['gap'],
                           'model_gap': t['value']['model_gap'],
                           'tier': t['tier'], 'score': t['score'], 'label': t.get('label'),
                           'robust': _robust(t, cfg), 'market_cost': _market_cost(t), 'screen_me': t['screen']['me'],
                           'partner_surplus': t['value']['market']['partner_surplus'],
                           'near_term': t['me']['near_term_delta'], 'dominated_by': t.get('dominated_by'),
                           'balancing': {s_: {'kind': b['kind'], 'player': (b['player'] or {}).get('site_player_id'),
                                              'fill': (b.get('fill') or {}).get('used') and b['fill']['player']['site_player_id']
                                              or None}
                                         for s_, b in t['balancing'].items()}} for t in evaluated],
    }
