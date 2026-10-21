"""Trade Lab configuration, package shapes and league trade rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
import os

UTC = timezone.utc
COUNTING = ('three_ptm', 'pts', 'reb', 'ast', 'stl', 'blk', 'turnovers')
SOURCE_WEIGHTS = (('hashtag', 'Hashtag', 1.0), ('leaguewinner', 'LeagueWinner', 0.0))
VALUE_FLOOR = 25.0   # value-over-replacement units; keeps fairness ratios finite near zero
PACKAGES = {'1-1': (1, 1), '2-1': (2, 1), '1-2': (1, 2), '2-2': (2, 2)}   # (players I send, players I receive)
PACKAGE_LABELS = {'1-1': '1-for-1', '2-1': '2-for-1', '1-2': '1-for-2', '2-2': '2-for-2'}
ACTIVE_LIMIT, IL_LIMIT = 14, 3        # league settings: 10 starting slots + 4 BN; 3 IL
IL_MOVE_STATUSES = frozenset({'O', 'INJ'})   # default IL-move rule when evidence supplies none; unverified
MARKET_DECAY, MARKET_FLOOR = 40.0, 10.0      # draft-slot value curve (auction-like) and fairness floor; heuristics


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
