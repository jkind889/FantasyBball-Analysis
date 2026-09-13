import copy
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pandas as pd

from features.recent_form import streaming_projection
from features.waiver_advisor import advise_waivers, write_advice
from features.usable_games import optimize_lineup, prepare_schedule


def example_snapshot():
    """Equal schedules: hot FA beats a season-long 30 PPG player in a slump."""
    snapshot = {
        "schema_version": 1, "league_id": 123, "season_year": 2026,
        "team_id": 1, "week": 12, "as_of": "2026-01-15T12:00:00-05:00",
        "rules": {"scoring_type": "H2H_POINTS", "starting_slots": ["PG"],
                  "roster_capacity": 1, "timezone": "America/New_York",
                  "lineup_lock": "INDIVIDUAL_GAME", "drop_lock": "INDIVIDUAL_GAME",
                  "acquisition_timing": "SAME_DAY", "games_cap": None,
                  "lineup_move_limit": None, "acquisitions_remaining": 2,
                  "position_limits": {}, "ir_legal": True},
        "schedule_complete": True, "pool_complete": True,
        "players": [], "games": [], "game_logs": [],
    }
    for pid, name, baseline, season, recent in [(1, "Cold incumbent", 30, 30, 20), (2, "Hot free agent", 25, 24, 34), (3, "Steady option", 28, 28, 28)]:
        snapshot["players"].append({
            "player_id": pid, "player_name": name, "position": "PG", "pro_team": "ATL",
            "eligible_slots": ["PG", "G", "UT"], "injury_status": "ACTIVE",
            "lineup_slot": "PG" if pid == 1 else None, "rostered": pid == 1,
            "availability": "ONTEAM" if pid == 1 else "FREEAGENT",
            "available_at": snapshot["as_of"], "undroppable": False,
            "proj_pts_per_game": baseline, "season_avg_points": season,
            "vorp_ros": baseline * 20, "recent_logs_complete": True})
        for day in range(7, 13):
            snapshot["game_logs"].append({"player_id": pid, "game_id": f"{pid}-{day}",
                "completed_at": f"2026-01-{day:02}T23:00:00-05:00",
                "points": recent, "minutes": 30, "played": True, "completed": True})
    for day in (15, 16, 18):
        snapshot["games"].append({"game_id": f"g{day}", "pro_team": "ATL", "opponent": "BOS",
            "fantasy_week": 12, "scoring_period": day,
            "tipoff": f"2026-01-{day}T19:00:00-05:00", "status": "SCHEDULED"})
    return snapshot


class RecentFormTests(unittest.TestCase):
    def setUp(self):
        self.s = example_snapshot()
        self.player = self.s["players"][1]

    def form(self, **kwargs):
        return streaming_projection(self.player, self.s["game_logs"], self.s["as_of"], **kwargs)

    def test_sustained_streak_and_season_projection_unchanged(self):
        before = copy.deepcopy(self.player)
        form = self.form()
        self.assertAlmostEqual(form["streaming_ppg"], 30.4)
        self.assertEqual(form["games_above_threshold"], 6)
        self.assertEqual(before, self.player)

    def test_outlier_mean_is_tempered(self):
        for log in self.s["game_logs"]:
            if log["player_id"] == 2:
                log["points"] = 100 if log["game_id"] == "2-12" else 20
        form = self.form()
        self.assertEqual(form["recent_median"], 20)
        self.assertLess(form["streaming_ppg"], .6 * form["recent_mean"] + .4 * 25)

    def test_future_dnp_duplicates_and_played_zero(self):
        logs = [log for log in self.s["game_logs"] if log["player_id"] == 2]
        logs[0]["points"] = 0
        self.s["game_logs"] += [dict(logs[0]), dict(logs[0], game_id="future", points=500,
                              completed_at="2026-01-20T23:00:00Z"),
                              dict(logs[0], game_id="dnp", points=0, played=False)]
        form = self.form()
        self.assertEqual(form["recent_games"], 6)
        self.assertAlmostEqual(form["recent_mean"], 34 * 5 / 6)

    def test_small_samples_and_stale_fallback(self):
        self.s["game_logs"] = [x for x in self.s["game_logs"] if x["game_id"] in {"2-10", "2-11", "2-12"}]
        self.assertAlmostEqual(self.form()["recent_weight"], .3)
        self.assertAlmostEqual(self.form(window=5)["recent_weight"], .36)
        self.s["as_of"] = "2026-03-01T00:00:00Z"
        self.assertEqual(self.form()["streaming_ppg"], 25)
        self.assertIn("three", self.form()["fallback_reason"])

    def test_missing_conflicting_logs_and_missing_baseline(self):
        self.player["recent_logs_complete"] = False
        self.assertEqual(self.form()["streaming_ppg"], 25)
        self.player["recent_logs_complete"] = True
        self.s["game_logs"].append(dict(self.s["game_logs"][6], points=999))
        self.assertEqual(self.form()["streaming_ppg"], 25)
        self.player["proj_pts_per_game"] = None
        self.assertIsNone(self.form()["streaming_ppg"])


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        self.s = example_snapshot()

    def test_hot_player_and_calendar_reconcile(self):
        result = advise_waivers(self.s)
        best = result["recommendations"].iloc[0]
        self.assertEqual(best["add_id"], 2)
        self.assertTrue(best["recommended"])
        self.assertAlmostEqual(best["net_gain"], 19.2)
        totals = result["calendar"].groupby("action_id")["projected_points"].sum()
        for r in result["recommendations"].itertuples():
            self.assertAlmostEqual(totals[r.action_id], r.candidate_points)
        self.assertEqual(best["add_recent_mean"], 34)

    def test_protected_undroppable_and_limit_hold(self):
        for kind in ("protect", "undroppable", "limit", "unknown_drop"):
            with self.subTest(kind=kind):
                s = copy.deepcopy(self.s)
                if kind == "undroppable":
                    s["players"][0]["undroppable"] = True
                elif kind == "unknown_drop":
                    s["players"][0]["undroppable"] = None
                elif kind == "limit":
                    s["rules"]["acquisitions_remaining"] = 0
                result = advise_waivers(s, protected_ids=[1] if kind == "protect" else [])
                self.assertEqual(list(result["recommendations"]["action"]), ["HOLD"])
                self.assertTrue(result["recommendations"].iloc[0]["recommended"])

    def test_add_only_empty_roster_and_negative_hold(self):
        self.s["players"] = self.s["players"][1:]
        best = advise_waivers(self.s)["recommendations"].iloc[0]
        self.assertEqual(best["action"], "ADD")
        self.assertAlmostEqual(best["net_gain"], 91.2)
        for p in self.s["players"]:
            p["proj_pts_per_game"] = -10
            p["recent_logs_complete"] = False
        self.assertEqual(advise_waivers(self.s)["recommendations"].iloc[0]["action"], "HOLD")

    def test_midweek_lock_and_delayed_claim(self):
        self.s["as_of"] = "2026-01-15T20:00:00-05:00"
        result = advise_waivers(self.s)
        best = result["recommendations"].iloc[0]
        self.assertAlmostEqual(best["baseline_points"], 48)
        self.assertAlmostEqual(best["net_gain"], 12.8)
        self.assertEqual(best["effective_at"], "2026-01-16T05:00:00+00:00")
        self.s["players"][1].update(availability="WAIVERS", available_at="2026-01-17T08:00:00Z")
        board = advise_waivers(self.s)["recommendations"]
        claim = board[board.add_id == 2].iloc[0]
        self.assertEqual(claim["status"], "conditional")
        self.assertFalse(claim["recommended"])
        self.assertAlmostEqual(claim["net_gain"], 6.4)

    def test_next_day_and_unknown_availability(self):
        self.s["rules"]["acquisition_timing"] = "NEXT_DAY"
        best = advise_waivers(self.s)["recommendations"].iloc[0]
        self.assertAlmostEqual(best["net_gain"], 12.8)
        self.s["players"][1]["available_at"] = None
        self.assertIn("unknown acquisition", advise_waivers(self.s)["rejected"].iloc[0]["reason"])

    def test_daily_budget_resets_and_first_game_roster_lock(self):
        self.s["rules"].update(daily_acquisition_limit=1, daily_acquisitions_used=1)
        best = advise_waivers(self.s)["recommendations"].iloc[0]
        self.assertAlmostEqual(best["net_gain"], 12.8)
        self.s["rules"].update(daily_acquisitions_used=0, drop_lock="FIRSTGAME_SCORINGPERIOD")
        # A different NBA team's early game locks all roster changes for today.
        self.s["games"].append(dict(self.s["games"][0], game_id="early", pro_team="BOS", tipoff="2026-01-15T11:00:00-05:00"))
        best = advise_waivers(self.s)["recommendations"].iloc[0]
        self.assertAlmostEqual(best["net_gain"], 12.8)

    def test_incomplete_pool_and_unknown_tipoff_are_partial(self):
        self.s["pool_complete"] = False
        self.assertFalse(advise_waivers(self.s)["recommendations"].recommended.any())
        self.s["pool_complete"] = True
        self.s["games"][0]["tipoff"] = None
        result = advise_waivers(self.s)
        self.assertFalse(result["recommendations"].recommended.any())
        self.assertIn("Unknown tipoff", result["recommendations"].iloc[0]["limitations"])

    def test_injury_and_timezone_boundaries(self):
        self.s["players"][1]["injury_status"] = "OUT"
        self.assertEqual(advise_waivers(self.s)["recommendations"].iloc[0]["add_id"], 3)
        self.s["games"][0]["tipoff"] = "2026-01-16T01:00:00Z"
        calendar = advise_waivers(self.s)["calendar"]
        self.assertEqual(set(calendar[calendar.game_id == "g15"]["date"]), {"2026-01-15"})

    def test_missing_data_partial_never_definitive(self):
        self.s["players"][0]["recent_logs_complete"] = False
        board = advise_waivers(self.s)["recommendations"]
        self.assertTrue((board.status == "partial").all())
        self.assertFalse(board.recommended.any())
        self.s["players"][0]["pro_team"] = "UNKNOWN"
        result = advise_waivers(self.s)
        self.assertIn("unknown schedule", set(result["calendar"]["reason"]))

    def test_full_lineup_and_position_limits(self):
        self.s["rules"]["roster_capacity"] = 2
        self.s["players"][0]["proj_pts_per_game"] = 100
        for log in self.s["game_logs"]:
            if log["player_id"] == 1:
                log["points"] = 100
        result = advise_waivers(self.s)
        self.assertEqual(result["recommendations"].iloc[0]["action"], "HOLD")
        add_only = result["recommendations"].query("action == 'ADD'")
        self.assertTrue((add_only.net_gain == 0).all())
        self.s["rules"]["position_limits"] = {"PG": 1}
        self.assertIn("Roster or position limit", set(advise_waivers(self.s)["rejected"]["reason"]))

    def test_unsupported_rules_and_protect_typo(self):
        for key, val in [("lineup_lock", "WEEKLY"), ("games_cap", 40), ("ir_legal", False)]:
            s = copy.deepcopy(self.s)
            s["rules"][key] = val
            with self.assertRaises(ValueError):
                advise_waivers(s)
        with self.assertRaises(ValueError):
            advise_waivers(self.s, protected_ids=[999])

    def test_optimizer_matches_exhaustive_and_locked_slot(self):
        self.s["rules"]["starting_slots"] = ["PG", "UT"]
        self.s["rules"]["roster_capacity"] = 3
        for p in self.s["players"]:
            p["rostered"] = True
        self.s["players"][0]["eligible_slots"] = ["PG", "UT"]
        self.s["players"][1]["eligible_slots"] = ["PG"]
        self.s["players"][2]["eligible_slots"] = ["UT"]
        forms = {p["player_id"]: streaming_projection(p, self.s["game_logs"], self.s["as_of"])
                 for p in self.s["players"]}
        best = 0
        for assignment in itertools.permutations(self.s["players"], 2):
            if all(slot in p["eligible_slots"] for slot, p in zip(["PG", "UT"], assignment)):
                best = max(best, sum(forms[p["player_id"]]["streaming_ppg"] for p in assignment))
        result = optimize_lineup(self.s["players"], prepare_schedule(self.s), self.s["rules"], forms)
        self.assertAlmostEqual(result["points"], best * 3)
        # Only one team has already tipped off; its starter reserves UT today.
        self.s["as_of"] = "2026-01-15T20:00:00-05:00"
        self.s["players"][0].update(lineup_slot="UT")
        for p in self.s["players"][1:]:
            p.update(pro_team="BOS", lineup_slot="BE")
        self.s["games"].append(dict(self.s["games"][0], game_id="late", pro_team="BOS", tipoff="2026-01-15T22:00:00-05:00"))
        result = optimize_lineup(self.s["players"], prepare_schedule(self.s), self.s["rules"], forms)
        today = [r for r in result["rows"] if r["date"] == "2026-01-15"]
        self.assertEqual(next(r for r in today if r["player_id"] == 1)["slot"], "UT")
        self.assertEqual(next(r for r in today if r["player_id"] == 3)["slot"], "BENCH")

    def test_offline_cli_and_outputs(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(self.s))
            command = [sys.executable, str(root / "main.py"), "--mode", "waivers", "--snapshot", str(path)]
            run = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Hot free agent", run.stdout)
            reports = Path(directory) / "reports"
            self.assertEqual(json.loads((reports / "waiver_snapshot.json").read_text()), dict(self.s, recent_window=6))
            self.assertEqual(len(list(reports.glob("*.csv"))), 4)
            self.assertNotIn("espn_s2", (reports / "waiver_snapshot.json").read_text())

    def test_replay_preserves_window_protection_and_source(self):
        original = copy.deepcopy(self.s)
        result = advise_waivers(self.s, window=5, protected_ids=[1])
        self.assertEqual(self.s, original)
        with tempfile.TemporaryDirectory() as directory:
            write_advice(self.s, result, directory)
            saved = json.loads((Path(directory) / "waiver_snapshot.json").read_text())
        replay = advise_waivers(saved)
        pd.testing.assert_frame_equal(result["recommendations"], replay["recommendations"])
        self.assertEqual(saved["recent_window"], 5)
        self.assertTrue(saved["players"][0]["protected"])


if __name__ == "__main__":
    unittest.main()
