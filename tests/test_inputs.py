from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from fantasy_dashboard.cli import run
from fantasy_dashboard.inputs import LEAGUE_SETTINGS, REVIEWED_MATCHES, ROOT, WORKBOOK
from fantasy_dashboard.sources.blending import attempt_weighted_pct, blend, shooting_pct
from fantasy_dashboard.sources.matching import match_sources, normalize_name, select_unique
from fantasy_dashboard.sources.projections import Projection, import_workbook, parse_row
from fantasy_dashboard.sources.reviewed_matches import apply_reviewed_matches
from fantasy_dashboard.sources.settings import read_local_settings
from fantasy_dashboard.yahoo.player_capture import BrowserPlayer, is_waiver_label, load_player_capture, match_browser_players
from fantasy_dashboard.yahoo.refresh import _save, lineup_date
from fantasy_dashboard.yahoo.roster_capture import (
    BrowserRosterPlayer,
    fnv32,
    load_browser_capture,
    match_browser_rosters,
)

import pinned_capture  # noqa: F401  (pins the archived capture)
from support import PINNED_REPORTS


class ProjectionTests(unittest.TestCase):
    def test_workbook_inventory_and_lineage(self):
        batch = import_workbook(ROOT / WORKBOOK)
        self.assertEqual(batch.file_sha256, "12837d8a2159d2fadeadaaa8d49498730d4a487b046e9d1fbe450eef04bdce34")
        self.assertEqual(batch.row_counts, {"LeagueWinner Stats": 527, "Hashtag Stats": 400})
        self.assertEqual(len(batch.records), 927)
        self.assertEqual(len(batch.file_sha256), 64)
        lw, hashtag = batch.records[0], batch.records[527]
        self.assertEqual(lw.raw["Player"], "Nikola Jokić")
        self.assertEqual(lw.row, 2)
        self.assertEqual((lw.fgm, lw.fga, lw.ftm, lw.fta), (10.2, 18, 5.5, 6.7))
        self.assertAlmostEqual(lw.fg_pct_reported, 0.566)
        self.assertEqual(hashtag.raw["FG%"], "0.573 (10.5/18.3)")
        self.assertEqual((hashtag.fgm, hashtag.fga, hashtag.ftm, hashtag.fta), (10.5, 18.3, 5.6, 6.8))
        self.assertEqual(hashtag.reb, hashtag.raw["TREB"])
        self.assertEqual(hashtag.three_ptm, hashtag.raw["3PM"])
        self.assertEqual(hashtag.turnovers, hashtag.raw["TO"])
        self.assertEqual(sum(r.fta == 0 for r in batch.records), 0)
        self.assertEqual(sum(r.gp == 0 for r in batch.records), 4)

    def test_invalid_shooting_and_missing_fields_are_reported(self):
        raw = {"R#": 1, "PLAYER": "Example", "POS": None, "TEAM": None, "GP": 10, "MPG": 20,
               "FG%": "0.500 (2.0/1.0)", "FT%": "bad", "3pm": 1, "PTS": 2,
               "TREB": 3, "AST": 4, "STL": 1, "BLK": 0, "TO": 1, "TOTAL": 0}
        row = parse_row("Hashtag Stats", 9, raw)
        self.assertEqual(row.raw, raw)
        self.assertIn("invalid_shooting_volume", [i.code for i in row.issues])
        self.assertIn("invalid_shooting_format", [i.code for i in row.issues])
        self.assertEqual(sum(i.code == "missing_value" for i in row.issues), 2)

    def test_rounding_aware_small_volume(self):
        raw = {"R#": 1, "PLAYER": "Example", "POS": "C", "TEAM": "TOR", "GP": 10, "MPG": 20,
               "FG%": "0.500 (0.1/0.2)", "FT%": "0.750 (0.1/0.2)", "3pm": 0, "PTS": 2,
               "TREB": 3, "AST": 4, "STL": 1, "BLK": 0, "TO": 1, "TOTAL": 0}
        row = parse_row("Hashtag Stats", 9, raw)
        self.assertNotIn("rate_volume_discrepancy", [i.code for i in row.issues])
        raw["FG%"] = "0.900 (1.0/5.0)"
        self.assertIn("rate_volume_discrepancy", [i.code for i in parse_row("Hashtag Stats", 9, raw).issues])
        raw["FG%"] = "0.500 (0.0/0.0)"
        self.assertIn("rate_without_attempts", [i.code for i in parse_row("Hashtag Stats", 9, raw).issues])

    def test_duplicates_and_source_review(self):
        batch = import_workbook(ROOT / WORKBOOK)
        chosen, duplicate = select_unique(batch.records)
        self.assertEqual(len(chosen), 927)
        self.assertEqual(duplicate, [])
        report = match_sources(batch.records)
        self.assertEqual(report["shared_unique_names"], 394)
        self.assertEqual((len(report["leaguewinner_only"]), len(report["hashtag_only"])), (133, 6))
        self.assertTrue(any(r["reason"] == "team_conflict" for r in report["review_queue"]))


class MathTests(unittest.TestCase):
    def test_attempt_weighted_not_average_of_rates(self):
        self.assertAlmostEqual(attempt_weighted_pct([(1, 1), (45, 100)]), 46 / 101)
        self.assertNotAlmostEqual(46 / 101, (1 + 0.45) / 2)
        self.assertIsNone(shooting_pct(0, 0))
        with self.assertRaises(ValueError):
            shooting_pct(2, 1)
        with self.assertRaises(ValueError):
            attempt_weighted_pct([(2, 1), (1, 2)])

    def test_blend_uses_each_sources_own_attempts(self):
        h = Projection("Hashtag", "Hashtag Stats", 2, {}, fgm=9, fga=20, ftm=2, fta=3, pts=20)
        lw = Projection("LeagueWinner", "LeagueWinner Stats", 2, {}, fgm=1, fga=2, ftm=8, fta=10, pts=10)
        combined = blend(h, lw, 0.25)
        self.assertAlmostEqual(combined.fg_pct, (0.25 * 9 + 0.75 * 1) / (0.25 * 20 + 0.75 * 2))
        self.assertAlmostEqual(combined.ft_pct, (0.25 * 2 + 0.75 * 8) / (0.25 * 3 + 0.75 * 10))
        self.assertEqual(combined.coverage, "two_sources")
        self.assertEqual(blend(h, lw, 1).values["fga"], 20)
        self.assertEqual(blend(h, lw, 0).values["fga"], 2)
        self.assertEqual(blend(h, None).values["fga"], 20)
        self.assertEqual(blend(h, None).coverage, "one_source")
        h.fga = None
        self.assertIsNone(blend(h, lw).values["fga"])
        self.assertIsNone(blend(h, lw).fg_pct)


class MatchingTests(unittest.TestCase):
    def test_name_normalization(self):
        self.assertEqual(normalize_name("Bobby Portis Jr."), normalize_name("Bobby Pórtis"))


class ReportAndBrowserTests(unittest.TestCase):
    def test_cli_reports_account_for_every_row(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run(ROOT, Path(directory))
            records = json.loads((Path(directory) / "projections.json").read_text())["records"]
            quality = json.loads((Path(directory) / "data-quality.json").read_text())
            matching = json.loads((Path(directory) / "player-matching.json").read_text())
            self.assertEqual(len(records), 927)
            self.assertEqual(sum(quality["sheet_row_counts"].values()), len(records))
            self.assertEqual(matching["workbook_sha256"], quality["workbook_sha256"])
            self.assertEqual(summary["duplicate_groups"], 0)
            self.assertEqual(matching["source_matching"]["status"], "provisional_source_names")
            self.assertNotIn("yahoo_matching", matching)
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
            self.assertEqual((Path(directory) / "player-matching.json").stat().st_mode & 0o777, 0o600)

    def test_browser_capture_validates_rosters_and_all_settings(self):
        local = read_local_settings(ROOT / LEAGUE_SETTINGS)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            roster_text = "\n".join(f"{team}\tPG\t{team}\tPlayer {team}\tATL - PG" for team in range(1, 11))
            (folder / "rosters.tsv").write_text(roster_text + "\n", encoding="utf-8")
            (folder / "settings.json").write_text(json.dumps({
                "source_url": "https://basketball.fantasysports.yahoo.com/nba/92103/settings",
                "rows": [[f"{key}:", value] for key, value in local.items()],
            }), encoding="utf-8")
            capture = {
                "captured_at_utc": "2026-10-20T00:00:00Z", "league_id": "92103",
                "league_name": "Big Baller League", "my_team_id": "4",
                "teams": [{"team_id": str(team), "name": f"Team {team}"} for team in range(1, 11)],
                "rosters": {
                    "source_url": "https://basketball.fantasysports.yahoo.com/nba/92103/startingrosters",
                    "lineup_date": "2026-10-20", "file": "rosters.tsv", "fnv32": fnv32(roster_text),
                },
                "settings": {"file": "settings.json"}, "standings": {"status": "preseason"},
            }
            path = folder / "capture.json"
            path.write_text(json.dumps(capture), encoding="utf-8")
            report, players = load_browser_capture(path, local)
            self.assertEqual((report["team_count"], report["rostered_player_count"]), (10, 10))
            self.assertEqual(report["settings_summary"]["matched"], len(local))
            self.assertEqual(report["settings_summary"]["different"], 0)
            self.assertEqual(players[3].fantasy_team_id, "4")
            (folder / "rosters.tsv").write_text(roster_text.replace("Player 1", "Player X") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                load_browser_capture(path, local)

    def test_browser_matching_keeps_site_ids_provisional(self):
        records = [
            Projection("Hashtag", "Hashtag Stats", 2, {}, name="Player A", team="ATL", position="PG"),
            Projection("LeagueWinner", "LeagueWinner Stats", 3, {}, name="Player B", team="BOS", position="C"),
        ]
        players = [
            BrowserRosterPlayer("1", "PG", "101", "Player A", "ATL", ("PG",)),
            BrowserRosterPlayer("2", "C", "102", "Player B", "NYK", ("C",)),
        ]
        report = match_browser_rosters(records, players)
        self.assertEqual(report["status"], "provisional_browser_site_ids")
        self.assertEqual(report["candidate_links"][0]["site_player_id"], "101")
        self.assertEqual(report["manual_review"][0]["reason"], "team_conflict")

    def test_full_player_capture_reconciles_all_pages_and_rosters(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            teams = [{"team_id": str(i), "name": f"Team {i}"} for i in range(1, 11)]
            roster = {"teams": teams, "lineup_date": "2026-10-20",
                      "text": "\n".join(f"{i}\tPG\t{i}\tPlayer {i}\tATL - PG" for i in range(1, 11))}
            rows = []
            for i in range(1, 28):
                page, row = divmod(i - 1, 25)
                rows.append(f"{page + 1}\t{row + 1}\t{i}\tPlayer {i}\tATL - PG\t{f'Team {i}' if i <= 10 else 'FA'}")
            path = _save(folder, roster, "\n".join(rows), 2, "92103")
            report, players = load_player_capture(path)
            self.assertEqual((report["page_count"], report["player_count"], report["rostered_count"]), (2, 27, 10))
            self.assertEqual(report["free_agent_count"], 17)
            self.assertEqual(report["roster_reconciliation"]["conflicts"], [])
            rows[0] = rows[0].replace("Team 1", "FA")
            path = _save(folder, roster, "\n".join(rows), 2, "92103")
            report, _ = load_player_capture(path)
            self.assertEqual(report["status"], "reconciliation_conflict")
            self.assertEqual(report["roster_reconciliation"]["conflicts"][0]["reason"], "roster_player_mismatch")
            rows[0] = rows[0].replace("FA", "Team 1")
            path = _save(folder, roster, "\n".join(rows), 3, "92103")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                load_player_capture(path)

    def test_full_player_matching_flags_collisions_and_conflicts(self):
        players = [BrowserPlayer(1, 1, "10", "Player A", "ATL", ("PG",), "FA", None),
                   BrowserPlayer(1, 2, "11", "Player A Jr.", "ATL", ("PG",), "W", None),
                   BrowserPlayer(1, 3, "12", "Player B", "BOS", ("C",), "Team 1", "1")]
        records = [Projection("Hashtag", "Hashtag Stats", 2, {}, name="Player A", team="ATL", position="PG"),
                   Projection("LeagueWinner", "LeagueWinner Stats", 3, {}, name="Player B", team="ATL", position="C")]
        report = match_browser_players(records, players)
        self.assertEqual(report["candidate_links"], [])
        self.assertEqual({item["reason"] for item in report["manual_review"]}, {"name_collision", "team_conflict"})
        records[1].team = "BOS"
        report = match_browser_players(records, players)
        self.assertEqual(report["candidate_links"][0]["site_player_id"], "12")

    def test_approved_review_resolves_all_current_flags(self):
        records = import_workbook(ROOT / WORKBOOK).records
        _, players = load_player_capture(PINNED_REPORTS / "yahoo-browser-players-capture.json")
        raw = match_browser_players(records, players)
        approved = apply_reviewed_matches(raw, records, players,
                                          ROOT / REVIEWED_MATCHES)
        self.assertEqual((len(raw["manual_review"]), approved["reviewed_count"]), (35, 35))
        self.assertEqual((len(approved["candidate_links"]), len(approved["manual_review"])), (927, 0))
        self.assertTrue(all(link["site_eligible_positions"] for link in approved["candidate_links"]))
        self.assertEqual(approved["unmatched_projections"], [])
        self.assertEqual(approved["unmatched_rostered_browser_players"], [])
        grant = next(link for link in approved["candidate_links"] if link["name"] == "Grant Nelson")
        self.assertEqual((grant["site_player_id"], grant["site_eligible_positions"]), ("10561", ("PF",)))
        self.assertEqual(grant["decision_origin"], "user_instruction")

    def test_cli_applies_reviewed_site_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run(ROOT, Path(directory),
                          browser_players_capture=PINNED_REPORTS / "yahoo-browser-players-capture.json")
            matching = json.loads((Path(directory) / "player-matching.json").read_text())
        self.assertEqual(summary["reviewed_matches"], 35)
        self.assertEqual(len(matching["browser_player_matching"]["candidate_links"]), 927)
        self.assertEqual(matching["browser_player_matching"]["manual_review"], [])

    def test_reviewed_match_rejects_stale_site_id(self):
        records = import_workbook(ROOT / WORKBOOK).records
        _, players = load_player_capture(PINNED_REPORTS / "yahoo-browser-players-capture.json")
        raw = match_browser_players(records, players)
        source = (ROOT / REVIEWED_MATCHES).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.csv"
            invalid.write_text(source.replace("Grant Nelson,10561", "Grant Nelson,999999"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "absent from the captured player list"):
                apply_reviewed_matches(raw, records, players, invalid)
            output = Path(directory) / "reports"
            with self.assertRaisesRegex(ValueError, "absent from the captured player list"):
                run(ROOT, output,
                    browser_players_capture=PINNED_REPORTS / "yahoo-browser-players-capture.json",
                    matching_decisions=invalid)
            self.assertFalse(output.exists())

    def test_roster_date_resolution(self):
        self.assertEqual(lineup_date("Starting Rosters\nTue, Oct 20", date(2026, 10, 19)), "2026-10-20")
        with self.assertRaises(ValueError):
            lineup_date("Starting Rosters\nTue, Oct 20", date(2027, 8, 1))

    def test_waiver_label_with_clear_date(self):
        self.assertTrue(all(map(is_waiver_label, ["W", "W (Oct 24)", "W (Nov 3)"])))
        self.assertFalse(any(map(is_waiver_label, ["FA", "W (soon)", "Whole Team is Consistent"])))


if __name__ == "__main__":
    unittest.main()
