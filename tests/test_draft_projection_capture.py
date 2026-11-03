"""Offline capture tests with raw synthetic ESPN cards and a disposable database."""
from copy import deepcopy
from contextlib import redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import pandas as pd

from cli import parse_args
from features.draft_projection_capture import (
    CaptureCommitUncertainError, CaptureExportError, capture_draft_projections,
    run_from_environment,
)


YEAR = 2026
LEAGUE = 7
PICKS = [(1, 1, 11, 1, 1), (2, 2, 22, 1, 2), (3, 1, 33, 1, 3)]
ROOT = Path(__file__).resolve().parents[1]


class Cursor:
    def __init__(self, parent):
        self.parent = parent
        self.raw = parent.raw.cursor()

    def execute(self, sql, params=()):
        if "INSERT INTO provider_projection_snapshots" in sql:
            self.parent.inserts += 1
            if self.parent.fail_second_insert and self.parent.inserts == 2:
                raise sqlite3.OperationalError("synthetic insert failure")
        return self.raw.execute(sql.replace("%s", "?"), params)

    def fetchall(self):
        return self.raw.fetchall()

    def close(self):
        self.raw.close()


class Connection:
    def __init__(self):
        self.raw = sqlite3.connect(":memory:")
        self.raw.execute("CREATE TABLE draft_picks (season_year INT, overall_pick INT, team_id INT, player_id INT, round_num INT, round_pick INT)")
        self.raw.executemany("INSERT INTO draft_picks VALUES (?, ?, ?, ?, ?, ?)", [(YEAR, *p) for p in PICKS])
        self.raw.execute("""CREATE TABLE provider_projection_snapshots (
            snapshot_id TEXT PRIMARY KEY, league_id INT, player_id INT, season_year INT,
            horizon TEXT, source TEXT, observed_at TEXT, projected_total_points REAL, projected_avg_points REAL)""")
        self.raw.commit()
        self.inserts = self.commits = self.rollbacks = self.cursor_calls = 0
        self.fail_second_insert = self.fail_commit = False

    def cursor(self):
        self.cursor_calls += 1
        return Cursor(self)

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("synthetic failed acknowledgment")
        self.raw.commit()
        self.commits += 1

    def rollback(self):
        self.raw.rollback()
        self.rollbacks += 1

    def close(self):
        self.raw.close()


class Request:
    def __init__(self, fields=None):
        self.data = {"id": LEAGUE, "seasonId": YEAR, "status": {"finalScoringPeriod": 174},
                     "draftDetail": {"picks": [dict(zip(("overallPickNumber", "teamId", "playerId", "roundId", "roundPickNumber"), p)) for p in PICKS]}}
        self.fields = fields if fields is not None else {11: {"appliedTotal": 0, "appliedAverage": 0}, 22: {"appliedAverage": 20}, 33: {}}
        self.calls = self.card_calls = 0

    def league_get(self, **kwargs):
        self.calls += 1
        return deepcopy(self.data)

    def get_player_card(self, ids, period):
        self.card_calls += 1
        return {"players": [{"player": {"id": pid, "stats": [{"seasonId": YEAR, "statSourceId": 1,
                                "statSplitTypeId": 0, **self.fields.get(pid, {})}]}} for pid in ids]}


class DraftProjectionCaptureTests(unittest.TestCase):
    def setUp(self):
        self.conn = Connection()
        self.addCleanup(self.conn.raw.close)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "capture"
        original_read_sql = pd.read_sql
        read_patch = patch("features.provider_projections.pd.read_sql", side_effect=lambda sql, conn, params: original_read_sql(sql.replace("%s", "?"), conn.raw, params=params))
        read_patch.start()
        self.addCleanup(read_patch.stop)

    def count(self):
        return self.conn.raw.execute("SELECT COUNT(*) FROM provider_projection_snapshots").fetchone()[0]

    def old(self, player=22, total=100, average=10):
        self.conn.raw.execute("INSERT INTO provider_projection_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (f"old-{player}", LEAGUE, player, YEAR, "full_season", "espn_player_card", "2025-10-01 00:00:00", total, average))
        self.conn.raw.commit()

    def test_capture_persists_zero_partial_and_missing_without_dropping_picks(self):
        summary = capture_draft_projections(self.conn, Request(), LEAGUE, YEAR, self.out)
        report = pd.read_csv(self.out / "draft_pick_projections.csv").set_index("player_id")
        self.assertEqual(summary["saved_observations"], 2)
        self.assertEqual(self.count(), 2)
        self.assertEqual(len(report), 3)
        self.assertEqual(report.loc[11, "projected_total_points"], 0)
        self.assertEqual(report.loc[11, "observation_selection"], "current_capture")
        self.assertEqual(report.loc[22, "projection_status"], "average_only")
        self.assertTrue(pd.isna(report.loc[22, "projected_total_points"]))
        self.assertEqual(report.loc[33, "projection_status"], "missing")
        self.assertTrue((report["pre_draft_eligibility"] == "not_verified_pre_draft").all())
        self.assertEqual(self.conn.commits, 1)

    def test_new_average_does_not_replace_prior_total_or_mix_metadata(self):
        self.old()
        summary = capture_draft_projections(self.conn, Request({22: {"appliedAverage": 99}}), LEAGUE, YEAR, self.out)
        report = pd.read_csv(self.out / "draft_pick_projections.csv").set_index("player_id")
        self.assertEqual(summary["retained_observations"], 1)
        self.assertEqual(report.loc[22, "projected_total_points"], 100)
        self.assertEqual(report.loc[22, "projected_avg_points"], 10)
        self.assertEqual(report.loc[22, "projection_observed_at"], "2025-10-01 00:00:00")
        self.assertEqual(report.loc[22, "observation_selection"], "retained_capture")

    def test_provider_omission_retains_old_zero_and_creates_no_fake_observation(self):
        self.old(player=11, total=0, average=0)
        summary = capture_draft_projections(self.conn, Request({}), LEAGUE, YEAR, self.out)
        self.assertEqual(summary["saved_observations"], 0)
        self.assertEqual(self.count(), 1)
        report = pd.read_csv(self.out / "draft_pick_projections.csv").set_index("player_id")
        self.assertEqual(report.loc[11, "projected_total_points"], 0)
        self.assertEqual(report.loc[11, "observation_selection"], "retained_capture")

    def test_exact_pick_identity_rejected_even_when_player_set_matches(self):
        for key in ("teamId", "roundId", "roundPickNumber", "overallPickNumber"):
            with self.subTest(key=key):
                request = Request()
                request.data["draftDetail"]["picks"][0][key] = 99
                with self.assertRaises(ValueError):
                    capture_draft_projections(self.conn, request, LEAGUE, YEAR, self.out)
                self.assertEqual(request.card_calls, 0)
                self.assertEqual(self.count(), 0)
                self.assertFalse(self.out.exists())

    def test_wrong_source_league_or_season_rejected_before_storage(self):
        for key in ("id", "seasonId"):
            with self.subTest(key=key):
                request = Request()
                request.data[key] += 1
                with self.assertRaises(ValueError):
                    capture_draft_projections(self.conn, request, LEAGUE, YEAR, self.out)
                self.assertEqual(self.count(), 0)

    def test_duplicate_source_or_stored_picks_rejected(self):
        request = Request()
        request.data["draftDetail"]["picks"].append(deepcopy(request.data["draftDetail"]["picks"][0]))
        with self.assertRaises(ValueError):
            capture_draft_projections(self.conn, request, LEAGUE, YEAR, self.out)
        self.conn.raw.execute("INSERT INTO draft_picks VALUES (?, ?, ?, ?, ?, ?)", (YEAR, *PICKS[0]))
        self.conn.raw.commit()
        with self.assertRaises(ValueError):
            capture_draft_projections(self.conn, Request(), LEAGUE, YEAR, self.out)
        self.assertEqual(self.count(), 0)

    def test_missing_schema_never_auto_migrates(self):
        self.conn.raw.execute("DROP TABLE provider_projection_snapshots")
        self.conn.raw.commit()
        request = Request()
        with self.assertRaisesRegex(ValueError, "No automatic migration"):
            capture_draft_projections(self.conn, request, LEAGUE, YEAR, self.out)
        self.assertEqual(request.calls, 0)
        self.assertEqual(self.conn.raw.execute("SELECT name FROM sqlite_master WHERE name='provider_projection_snapshots'").fetchall(), [])

    def test_insert_failure_rolls_back_all_new_rows(self):
        self.conn.fail_second_insert = True
        with self.assertRaises(sqlite3.OperationalError):
            capture_draft_projections(self.conn, Request(), LEAGUE, YEAR, self.out)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.conn.rollbacks, 1)
        self.assertFalse(self.out.exists())

    def test_export_preparation_failure_rolls_back_before_commit(self):
        with patch.object(pd.DataFrame, "to_csv", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                capture_draft_projections(self.conn, Request(), LEAGUE, YEAR, self.out)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.conn.commits, 0)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_export_failure_after_commit_reports_successful_storage(self):
        with patch.object(Path, "rename", side_effect=OSError("synthetic rename failure")):
            with self.assertRaisesRegex(CaptureExportError, "committed 2 observations"):
                capture_draft_projections(self.conn, Request(), LEAGUE, YEAR, self.out)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.rollbacks, 0)
        self.assertFalse(self.out.exists())

    def test_failed_commit_acknowledgment_is_explicitly_uncertain(self):
        self.conn.fail_commit = True
        with self.assertRaisesRegex(CaptureCommitUncertainError, "outcome is uncertain"):
            capture_draft_projections(self.conn, Request(), LEAGUE, YEAR, self.out)
        self.assertFalse(self.out.exists())

    def test_existing_destination_preserved_before_any_storage_query(self):
        self.out.mkdir()
        existing = self.out / "keep.txt"
        existing.write_text("keep")
        with self.assertRaisesRegex(ValueError, "already exists"):
            capture_draft_projections(self.conn, Request(), LEAGUE, YEAR, self.out)
        self.assertEqual(existing.read_text(), "keep")
        self.assertEqual(self.conn.cursor_calls, 0)

    def test_cleanup_failure_cannot_hide_uncertain_commit(self):
        self.conn.fail_commit = True
        with patch.object(Cursor, "close", side_effect=RuntimeError("synthetic cursor close failure")), \
                patch("features.draft_projection_capture.shutil.rmtree", side_effect=OSError("synthetic temp cleanup failure")):
            with self.assertRaises(CaptureCommitUncertainError):
                capture_draft_projections(self.conn, Request(), LEAGUE, YEAR, self.out)

    def test_connection_close_failure_preserves_success_and_uncertain_outcomes(self):
        fake_db = SimpleNamespace(get_connection=lambda: self.conn)
        with patch.dict(sys.modules, {"database": fake_db}), \
                patch.dict(os.environ, {"ESPN_LEAGUE_ID": str(LEAGUE), "ESPN_S2": "synthetic", "ESPN_SWID": "synthetic"}, clear=True), \
                patch("espn_api.requests.espn_requests.EspnFantasyRequests", return_value=Request()), \
                patch.object(self.conn, "close", side_effect=RuntimeError("synthetic connection close failure")):
            result = run_from_environment(YEAR, self.out)
            self.assertEqual(result["database_commit"], "completed")
            self.assertIn("Connection cleanup failed", result["cleanup_warnings"][0])
            self.assertTrue((self.out / "draft_pick_projections.csv").is_file())
            self.conn.fail_commit = True
            with self.assertRaises(CaptureCommitUncertainError):
                run_from_environment(YEAR, self.out.with_name("another-capture"))


class DraftCaptureCliTests(unittest.TestCase):
    def test_flags_require_explicit_mode_year_and_new_destination(self):
        good = ["--mode", "draft", "--capture-draft-projections", "--season", "2026", "--output-dir", "new-directory"]
        args = parse_args(good)[1]
        self.assertTrue(args.capture_draft_projections)
        self.assertEqual(args.season, 2026)
        invalid = [["--capture-draft-projections"], ["--mode", "draft", "--capture-draft-projections"],
                   ["--mode", "draft", "--season", "2026"], good + ["--team", "1"],
                   good + ["--week", "1"], good + ["--claude-review"], good + ["--protect", "1"],
                   ["--mode", "waivers", "--capture-draft-projections", "--season", "2026", "--output-dir", "out"]]
        for argv in invalid:
            with self.subTest(argv=argv), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                parse_args(argv)
            self.assertEqual(error.exception.code, 2)

    def test_capture_dispatch_uses_explicit_year_and_never_enters_general_pipeline(self):
        script = '''
import json, runpy, sys
from pathlib import Path
from unittest.mock import patch
import os
root = Path(sys.argv[1]); sys.argv = [str(root / "main.py"), "--mode", "draft", "--capture-draft-projections", "--season", "2026", "--output-dir", "new-output"]
def forbidden(*args, **kwargs): raise AssertionError("General pipeline or live I/O entered")
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"ESPN_YEAR":"2027", "ESPN_TEAM_ID":"not-an-integer"}), patch("espn_api.basketball.League", side_effect=forbidden), patch("database.get_connection", side_effect=forbidden), patch("features.draft_projection_capture.run_from_environment", return_value={"season_year":2026,"saved_observations":0}) as capture:
    try: runpy.run_path(str(root / "main.py"), run_name="__main__")
    except SystemExit as error: assert error.code == 0
    capture.assert_called_once_with(2026, Path("new-output"))
'''
        with TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]), PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT)], cwd=directory,
                                    env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not verified pre-draft", result.stdout)


if __name__ == "__main__":
    unittest.main()
