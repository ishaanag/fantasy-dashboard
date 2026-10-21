from __future__ import annotations
import os
os.environ.setdefault('FANTASY_REPORTS_DIR', '/Users/ishaanag/Documents/work/projects/fantasy-basketball-2026/fantasy-dashboard/data/reports/archive-2026-10-20T1411Z')  # pinned capture

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import csv
import tempfile
import unittest

from app.phase1 import RosterPlayer, _dated_team_totals, assign_lineup, load_inputs, rank_league
from app.schedule import (CUP_FINAL_DATE, FantasyWeek, Game, Schedule, cup_window_provisional,
                          load_fantasy_weeks, load_schedule, week_for_day)


ROOT = Path(__file__).resolve().parents[2]


class SourceScheduleTests(unittest.TestCase):
    def test_real_schedule_and_yahoo_calendar(self):
        s = load_schedule(ROOT / "nba_2026_27_team_schedule.csv")
        weeks = load_fantasy_weeks(ROOT / "YAHOO_FANTASY_CALENDAR_AND_LINEUP_LOCKS_2026-27.md")
        self.assertEqual((len(s.games), len(s.team_dates)), (1200, 2400))
        self.assertEqual(set(s.team_counts.values()), {80})
        self.assertEqual(set(s.unscheduled_by_team.values()), {2})
        self.assertEqual(s.metadata()["unscheduled_games_league_wide"], 30)
        self.assertEqual(len(weeks), 21)
        opener = s.team_dates[("OKC", date(2026, 10, 20))]
        self.assertEqual(opener.tipoff_utc.date(), date(2026, 10, 21))
        self.assertEqual(opener.tipoff_et.date(), date(2026, 10, 20))
        self.assertEqual(week_for_day(weeks, opener.date).number, 1)
        self.assertEqual([(w.number, w.days) for w in weeks if w.days == 14], [(7, 14), (17, 14)])
        cases = {"2026-10-20": 1, "2026-10-25": 1, "2026-10-26": 2,
                 "2026-12-06": 7, "2026-12-07": 7, "2026-12-13": 7,
                 "2026-12-14": 8, "2027-02-21": 17, "2027-02-22": 17,
                 "2027-02-28": 17, "2027-03-01": 18, "2027-03-07": 18,
                 "2027-03-08": 19, "2027-03-21": 20, "2027-03-22": 21,
                 "2027-03-28": 21}
        for day, expected in cases.items():
            self.assertEqual(week_for_day(weeks, date.fromisoformat(day)).number, expected)
        self.assertIsNone(week_for_day(weeks, date(2027, 3, 29)))
        self.assertTrue(cup_window_provisional(weeks[6], s))
        self.assertFalse(cup_window_provisional(weeks[16], s))
        self.assertTrue(all(w.max_adds == 4 for w in weeks))

    def test_complete_updated_snapshot_is_idempotent_and_excludes_cup_final(self):
        with (ROOT / "nba_2026_27_team_schedule.csv").open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows, fields = list(reader), reader.fieldnames
        teams = sorted({r["team_abbr"] for r in rows})
        # Synthetic official-addition shape: every team gets one new game on each
        # of two dates. This tests ingestion only; these are not real fixtures.
        for day, number_base in ((date(2026, 12, 4), 2000), (date(2026, 12, 8), 2100)):
            for index in range(0, len(teams), 2):
                a, b = teams[index:index+2]
                et = day.isoformat() + "T19:00-05:00"
                utc = (day + timedelta(days=1)).isoformat() + "T00:00Z"
                for team, opp, venue in ((a, b, "home"), (b, a, "away")):
                    rows.append({**{f: "" for f in fields}, "team_abbr": team,
                                 "team": team, "game_date": day.isoformat(), "day_of_week": day.strftime("%A"),
                                 "tipoff_time_et": "19:00", "tipoff_at_et": et, "tipoff_at_utc": utc,
                                 "opponent_abbr": opp, "opponent": opp, "home_away": venue,
                                 "source_game_number": str(number_base + index // 2)})
        # A Cup final row pair must never raise team totals beyond 82.
        for team, opp, venue in ((teams[0], teams[1], "home"), (teams[1], teams[0], "away")):
            rows.append({**{f: "" for f in fields}, "team_abbr": team,
                         "game_date": CUP_FINAL_DATE.isoformat(), "tipoff_at_et": "2026-12-11T19:00-05:00",
                         "tipoff_at_utc": "2026-12-12T00:00Z", "opponent_abbr": opp,
                         "home_away": venue, "source_game_number": "9999"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "schedule.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            first, second = load_schedule(path), load_schedule(path)
            self.assertEqual(first, second)
            self.assertEqual((len(first.games), len(first.team_dates)), (1230, 2460))
            self.assertEqual(set(first.team_counts.values()), {82})
            self.assertEqual(set(first.unscheduled_by_team.values()), {0})
            self.assertEqual(first.excluded_cup_championship_rows, 2)
            self.assertFalse(cup_window_provisional(load_fantasy_weeks(ROOT / "YAHOO_FANTASY_CALENDAR_AND_LINEUP_LOCKS_2026-27.md")[6], first))

    def test_duplicate_and_broken_fixture_rejected(self):
        with (ROOT / "nba_2026_27_team_schedule.csv").open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows, fields = list(reader), reader.fieldnames
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "schedule.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows + [rows[0]])
            with self.assertRaisesRegex(ValueError, "reciprocal"):
                load_schedule(path)


class DailyLineupTests(unittest.TestCase):
    def test_tipoff_boundary_bench_start_il_exclusion_and_weighted_rates(self):
        day = date(2026, 10, 20)
        t7 = datetime(2026, 10, 20, 19, tzinfo=timezone.utc)
        t10 = datetime(2026, 10, 20, 22, tzinfo=timezone.utc)
        ga = Game(day, t7, ("AAA", "XXX"), ("home", "away"), "synthetic-a")
        gb = Game(day, t10, ("BBB", "YYY"), ("home", "away"), "synthetic-b")
        gc = Game(day, t10, ("CCC", "ZZZ"), ("home", "away"), "synthetic-c")
        schedule = Schedule((ga, gb, gc), {("AAA", day): ga, ("BBB", day): gb, ("CCC", day): gc},
                            {"AAA": 1, "BBB": 1, "CCC": 1}, {"AAA": 81, "BBB": 81, "CCC": 81}, "synthetic", 0)
        roster = [RosterPlayer("1", "a", "Early", "PG", ("PG",), "AAA"),
                  RosterPlayer("1", "b", "Late bench", "BN", ("SG",), "BBB"),
                  RosterPlayer("1", "c", "Injured", "IL", ("C",), "CCC")]
        def p(fgm, fga, ftm, fta, pts):
            return {"values": {"gp": 82.0, "fgm": fgm, "fga": fga, "ftm": ftm, "fta": fta,
                               "three_ptm": 1.0, "pts": pts, "reb": 2.0, "ast": 1.0,
                               "stl": 1.0, "blk": 0.0, "turnovers": 1.0},
                    "coverage": "two_sources", "source_rows": {}, "disagreement": {}}
        projected = list(zip(roster, [p(1, 1, 1, 1, 10), p(45, 100, 4, 10, 20), p(100, 100, 100, 100, 100)]))
        week = FantasyWeek(1, day, day, "regular_season")
        before, detail, days = _dated_team_totals(roster, projected, schedule, week, datetime(2026, 10, 20, 18, tzinfo=timezone.utc))
        self.assertAlmostEqual(before["categories"]["fg_pct"], 46/101)
        self.assertAlmostEqual(before["categories"]["ft_pct"], 5/11)
        self.assertEqual(before["categories"]["pts"], 30)
        self.assertEqual([d["modeled_start_opportunities"] for d in detail], [1, 1, 0])
        self.assertEqual(days[0]["scheduled_eligible_players"], 2)
        after, detail, _ = _dated_team_totals(roster, projected, schedule, week, datetime(2026, 10, 20, 20, tzinfo=timezone.utc))
        self.assertEqual(after["categories"]["pts"], 20)
        self.assertEqual(detail[0]["modeled_start_opportunities"], 0)
        self.assertEqual(detail[1]["modeled_start_opportunities"], 1)
        after_all, _, _ = _dated_team_totals(roster, projected, schedule, week, t10)
        self.assertEqual(after_all["categories"]["pts"], 0)
        self.assertIsNone(after_all["categories"]["fg_pct"])

    def test_locked_active_slot_cannot_be_reused_after_tipoff(self):
        late_guard = RosterPlayer("1", "late", "Late PG", "BN", ("PG",), "BBB")
        score = {"late": 10.0}
        self.assertIn("late", assign_lineup([late_guard], score, require_full=False))
        self.assertEqual(assign_lineup([late_guard], score, require_full=False,
                                       blocked_slots=("PG", "G", "Util", "Util")), {})
        with self.assertRaisesRegex(ValueError, "Invalid locked"):
            assign_lineup([late_guard], score, require_full=False, blocked_slots=("C", "C", "C"))

    def test_missing_same_day_snapshot_refuses_intraday_retroactive_lineup(self):
        inputs = load_inputs()
        first_week_two_game = next(g for g in inputs["schedule"].games
                                  if date(2026, 10, 26) <= g.date <= date(2026, 11, 1))
        with self.assertRaisesRegex(ValueError, "No same-day Yahoo lineup snapshot"):
            rank_league(inputs, week_number=2, as_of=first_week_two_game.tipoff_utc + timedelta(minutes=1))

    def test_real_saved_rosters_all_21_weeks_and_source_weights(self):
        inputs = load_inputs()
        results = [rank_league(inputs, week_number=i) for i in range(1, 22)]
        self.assertTrue(all(len(r["teams"]) == 10 for r in results))
        self.assertEqual(results[0], rank_league(inputs, week_number=1))
        self.assertTrue(results[6]["schedule"]["selected_week_provisional_cup"])
        self.assertFalse(results[16]["schedule"]["selected_week_provisional_cup"])
        self.assertGreater(sum(t["quality"]["scheduled_start_opportunities"] for t in results[16]["teams"]),
                           sum(t["quality"]["scheduled_start_opportunities"] for t in results[0]["teams"]))
        for r in results:
            for team in r["teams"]:
                for day in team["days"]:
                    starts = day["modeled_starts"]
                    self.assertLessEqual(len(starts), 10)
                    self.assertEqual(len({s["site_player_id"] for s in starts}), len(starts))
                    slot_counts = Counter(s["slot"] for s in starts)
                    self.assertLessEqual(slot_counts["C"], 2)
                    self.assertLessEqual(slot_counts["Util"], 2)
                    self.assertTrue(all(n <= 1 for slot, n in slot_counts.items() if slot not in {"C", "Util"}))
        h = rank_league(inputs, 1, week_number=1)
        lw = rank_league(inputs, 0, week_number=1)
        blend = results[0]
        by = lambda r: {t["team_id"]: t for t in r["teams"]}
        self.assertTrue(any(by(h)[k]["totals"] != by(lw)[k]["totals"] for k in inputs["teams"]))
        self.assertTrue(any(by(blend)[k]["totals"] != by(h)[k]["totals"] for k in inputs["teams"]))
        regular = rank_league(inputs)
        all_weeks = rank_league(inputs, week_number="all")
        self.assertEqual(regular["week"]["included_weeks"], list(range(1, 19)))
        self.assertEqual(all_weeks["week"]["included_weeks"], list(range(1, 22)))
        self.assertEqual(all_weeks["week"]["playoff_participation"], "hypothetical_for_all_ten_teams")
        self.assertTrue(regular["schedule"]["selected_week_provisional_cup"])
        self.assertTrue(all_weeks["schedule"]["selected_week_provisional_cup"])
        for report, pieces in ((regular, results[:18]), (all_weeks, results)):
            combined = by(report)
            weekly = [by(piece) for piece in pieces]
            for team_id in inputs["teams"]:
                team = combined[team_id]
                self.assertEqual(team["quality"]["scheduled_start_opportunities"],
                                 sum(piece[team_id]["quality"]["scheduled_start_opportunities"] for piece in weekly))
                for stat in ("fgm", "fga", "ftm", "fta"):
                    self.assertAlmostEqual(team["totals"]["shooting_volumes"][stat],
                                           sum(piece[team_id]["totals"]["shooting_volumes"][stat] for piece in weekly), places=8)
                for stat in ("three_ptm", "pts", "reb", "ast", "stl", "blk", "turnovers"):
                    self.assertAlmostEqual(team["totals"]["categories"][stat],
                                           sum(piece[team_id]["totals"]["categories"][stat] for piece in weekly), places=8)
                shots = team["totals"]["shooting_volumes"]
                self.assertAlmostEqual(team["totals"]["categories"]["fg_pct"], shots["fgm"] / shots["fga"])
                self.assertAlmostEqual(team["totals"]["categories"]["ft_pct"], shots["ftm"] / shots["fta"])


if __name__ == "__main__":
    unittest.main()
