from __future__ import annotations
import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture

from copy import deepcopy
from datetime import date, datetime, timezone
import unittest

from app.phase1 import (CATEGORIES, RosterPlayer, _dated_team_totals, assign_lineup,
                        eligible, load_inputs, rank_league, resolve_projection)
from app.projections import Projection
from app.schedule import FantasyWeek, Game, Schedule


def projection(source: str, **values) -> Projection:
    defaults = dict(gp=82, mpg=30, fgm=0, fga=0, ftm=0, fta=0, three_ptm=0,
                    pts=0, reb=0, ast=0, stl=0, blk=0, turnovers=0)
    defaults.update(values)
    return Projection(source, source + " Stats", 2, {}, **defaults)


class LineupTests(unittest.TestCase):
    def test_multi_position_matching_beats_slot_greedy(self):
        roster = [
            RosterPlayer("1", "a", "Flexible guard", "PG", ("PG", "SG")),
            RosterPlayer("1", "b", "Point guard", "BN", ("PG",)),
            RosterPlayer("1", "c", "Weak guard", "BN", ("SG",)),
            RosterPlayer("1", "g", "Guard", "G", ("PG",)),
            RosterPlayer("1", "sf", "Wing", "SF", ("SF",)),
            RosterPlayer("1", "pf", "Forward", "PF", ("PF",)),
            RosterPlayer("1", "f", "Other forward", "F", ("SF",)),
            RosterPlayer("1", "c1", "Center 1", "C", ("C",)),
            RosterPlayer("1", "c2", "Center 2", "C", ("C",)),
            RosterPlayer("1", "u1", "Utility 1", "Util", ("C",)),
            RosterPlayer("1", "u2", "Utility 2", "Util", ("C",)),
            RosterPlayer("1", "inj", "Injured", "IL", ("C",)),
        ]
        scores = {p.site_player_id: 5.0 for p in roster if p.assigned_slot != "IL"}
        scores.update(a=10, b=9, c=1, inj=1000)
        result = assign_lineup(roster, scores)
        self.assertEqual(len(result), 10)
        self.assertTrue({"a", "b", "g"}.issubset(result))
        self.assertEqual(result["a"], "SG")
        self.assertNotIn("c", result)
        self.assertNotIn("inj", result)
        self.assertEqual(sorted(result.values()), sorted(("PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util")))
        for p in roster:
            if p.site_player_id in result:
                self.assertTrue(eligible(result[p.site_player_id], p.eligible_positions))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            assign_lineup(roster + [roster[0]], scores)

    def test_hand_checked_nine_cat_and_attempt_weighted_rates(self):
        positions = [("PG", "PG"), ("SG", "SG"), ("G", "PG"), ("SF", "SF"),
                     ("PF", "PF"), ("F", "SF"), ("C", "C"), ("C", "C"),
                     ("Util", "C"), ("Util", "C")]
        roster = [RosterPlayer("1", str(i), f"Player {i}", slot, (pos,), f"T{i}")
                  for i, (slot, pos) in enumerate(positions)]
        roster += [RosterPlayer("1", "bn", "Bench", "BN", ("C",), "T10"),
                   RosterPlayer("1", "il", "IL", "IL", ("C",), "T11")]
        projected = []
        for i, player in enumerate(roster):
            vals = {"gp": 82.0, "fgm": 1.0 if i == 0 else 45.0 if i == 1 else 0.0,
                    "fga": 1.0 if i == 0 else 100.0 if i == 1 else 0.0,
                    "ftm": 1.0 if i == 0 else 4.0 if i == 1 else 0.0,
                    "fta": 1.0 if i == 0 else 10.0 if i == 1 else 0.0,
                    "three_ptm": float(i + 1) if i < 10 else 0.0,
                    "pts": float(10 + i) if i < 10 else 0.0,
                    "reb": float(2 + i) if i < 10 else 0.0,
                    "ast": float(i) if i < 10 else 0.0,
                    "stl": 1.0 if i < 10 else 0.0,
                    "blk": 0.5 if i < 10 else 0.0,
                    "turnovers": 2.0 if i < 10 else 0.0}
            projected.append((player, {"values": vals, "coverage": "two_sources", "source_rows": {}, "disagreement": {}}))
        day = date(2026, 10, 20)
        tipoff = datetime(2026, 10, 20, 23, tzinfo=timezone.utc)
        games = { (p.nba_team, day): Game(day, tipoff, (p.nba_team, f"O{i}"), ("home", "away"), f"synthetic-{i}")
                  for i, p in enumerate(roster) }
        schedule = Schedule(tuple(games.values()), games, {}, {}, "synthetic", 0)
        totals, details, daily = _dated_team_totals(roster, projected, schedule,
                              FantasyWeek(1, day, day, "regular_season"),
                              datetime(2026, 10, 20, 18, tzinfo=timezone.utc))
        cat = totals["categories"]
        self.assertEqual(set(cat), set(CATEGORIES))
        self.assertAlmostEqual(cat["fg_pct"], 46/101)
        self.assertAlmostEqual(cat["ft_pct"], 5/11)
        self.assertEqual([cat[k] for k in ("three_ptm", "pts", "reb", "ast", "stl", "blk", "turnovers")],
                         [55, 145, 65, 45, 10, 5, 20])
        self.assertEqual(totals["shooting_volumes"], {"fgm": 46, "fga": 101, "ftm": 5, "fta": 11})
        self.assertEqual(len(daily[0]["modeled_starts"]), 10)
        self.assertEqual(details[-2]["expected_games"], 0)
        self.assertEqual(details[-1]["expected_games"], 0)


class ProjectionAndIntegrationTests(unittest.TestCase):
    def test_source_extremes_and_blend(self):
        h = projection("Hashtag", fgm=9, fga=20, ftm=2, fta=3, pts=20)
        lw = projection("LeagueWinner", fgm=1, fga=2, ftm=8, fta=10, pts=10)
        sources = {"Hashtag": h, "LeagueWinner": lw}
        for weight, fgm, fga, ftm, fta, pts in [(1, 9, 20, 2, 3, 20), (0, 1, 2, 8, 10, 10),
                                                  (.5, 5, 11, 5, 6.5, 15)]:
            result = resolve_projection(sources, weight)
            v = result["values"]
            self.assertEqual(tuple(v[k] for k in ("fgm", "fga", "ftm", "fta", "pts")),
                             (fgm, fga, ftm, fta, pts))
            self.assertAlmostEqual(v["fg_pct"], fgm/fga)
            self.assertAlmostEqual(v["ft_pct"], ftm/fta)
        with self.assertRaisesRegex(ValueError, "Selected source"):
            resolve_projection({"Hashtag": h}, 0)
        self.assertEqual(resolve_projection({"Hashtag": h}, .5)["coverage"], "one_source")

    def test_real_capture_has_ten_deterministic_teams_and_source_switches(self):
        inputs = load_inputs()
        blended = rank_league(inputs, week_number=1)
        again = rank_league(inputs, week_number=1)
        self.assertEqual(blended, again)
        reordered = deepcopy(inputs)
        for roster in reordered["rosters"].values():
            roster.reverse()
        self.assertEqual(blended, rank_league(reordered, week_number=1))
        self.assertEqual(len(blended["teams"]), 10)
        self.assertEqual({t["team_id"] for t in blended["teams"]}, set(inputs["teams"]))
        self.assertEqual(blended["schedule"]["status"], "dated_fixtures_with_cup_gap")
        self.assertEqual(blended["week"]["week"], 1)
        self.assertGreater(sum(t["quality"]["scheduled_start_opportunities"] for t in blended["teams"]), 0)
        for t in blended["teams"]:
            self.assertEqual(len({d["site_player_id"] for d in t["lineup"]}), len(t["lineup"]))
            self.assertEqual(sum(d["modeled_start_opportunities"] for d in t["lineup"]), t["quality"]["scheduled_start_opportunities"])
            self.assertTrue(all(d["modeled_start_opportunities"] == 0 for d in t["lineup"] if d["captured_slot"] == "IL"))
        h = rank_league(inputs, 1, week_number=1)
        lw = rank_league(inputs, 0, week_number=1)
        by_id = lambda report: {t["team_id"]: t for t in report["teams"]}
        self.assertTrue(any(by_id(h)[k]["totals"] != by_id(lw)[k]["totals"] for k in inputs["teams"]))
        self.assertEqual(h["projection_model"], "Hashtag-only")
        self.assertEqual(lw["projection_model"], "LeagueWinner-only")
        self.assertEqual(blended["projection_model"], "Blended")

    def test_unmatched_or_duplicate_cannot_rank_silently(self):
        inputs = load_inputs()
        target = next(iter(inputs["rosters"]))
        player = inputs["rosters"][target][0]
        missing = deepcopy(inputs)
        del missing["projections"][player.site_player_id]
        with self.assertRaises((KeyError, ValueError)):
            rank_league(missing, week_number=1)
        duplicated = deepcopy(inputs)
        duplicated["rosters"][target].append(player)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            rank_league(duplicated, week_number=1)
        bad = deepcopy(inputs)
        bad["projections"][player.site_player_id]["Hashtag"].pts = None
        with self.assertRaisesRegex(ValueError, "missing required inputs"):
            rank_league(bad, week_number=1)


if __name__ == "__main__":
    unittest.main()
