import sqlite3
import unittest
import os
import runpy
import sys
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from espn_api.basketball.player import Player

from cache import espn_player_to_row
from features.draft_analysis import fill_missing_draft_analysis_data, merge_missing_draft_analysis_data
from features.projection_analysis import projection_analysis
from features.projection_inputs import build_projection_inputs
from features.projections import project_rest_of_season
from features.provider_projections import (
    PROJECTION_COLUMNS, attach_projections, empty_projection, fetch_projections,
    load_projection_snapshots, player_projection, raw_projection, require_projection_schema,
    save_projection_snapshots,
)

YEAR = 2026
OBSERVED = "2026-10-01 00:00:00"


def card(pid=1, **fields):
    return {"player": {
        "id": pid, "fullName": f"Player {pid}", "defaultPositionId": 1,
        "eligibleSlots": [0], "proTeamId": 1,
        "stats": [{"id": f"10{YEAR}", "seasonId": YEAR, "statSourceId": 1,
                   "statSplitTypeId": 0, "scoringPeriodId": 0, **fields}],
    }}


def frame(pid=1, **fields):
    return pd.DataFrame([{"player_id": pid, **raw_projection(card(pid, **fields)["player"], YEAR, OBSERVED)}])


def player_frame(pid=1, actual=100):
    return pd.DataFrame([{
        "player_id": pid, "player_name": f"Player {pid}", "team_name": "Team",
        "position": "PG", "pro_team": "ATL", "games_played": 10,
        "total_points": actual, "avg_points": 10, "pos_rank": None,
        "season_year": YEAR,
    }])


def empty_draft():
    return pd.DataFrame(columns=["player_id", "player_name", "fantasy_team"])


class ProviderProjectionTest(unittest.TestCase):
    def test_real_library_defaults_are_not_forecasts(self):
        raw = card()
        raw["player"]["stats"] = []
        player = Player(raw, YEAR)
        self.assertEqual(player.projected_total_points, 0)
        self.assertEqual(player.projected_avg_points, 0)
        row = espn_player_to_row(player, YEAR)
        self.assertIsNone(row["projected_total_points"])
        self.assertIsNone(row["projected_avg_points"])
        self.assertIsNone(row["projection_source"])

    def test_omitted_fields_in_existing_split_stay_missing(self):
        for fields in ({}, {"appliedAverage": 12}, {"appliedTotal": None}):
            with self.subTest(fields=fields):
                raw = card(**fields)
                player = Player(raw, YEAR) if fields.get("appliedTotal", 0) is not None else None
                result = raw_projection(raw["player"], YEAR, OBSERVED)
                self.assertIsNone(result["projected_total_points"])
                if player:
                    self.assertIsNone(espn_player_to_row(player, YEAR)["projected_total_points"])

    def test_explicit_zero_and_negative_values_survive_raw_adapter(self):
        for value in (0, -10, 1400):
            with self.subTest(value=value):
                raw = card(appliedTotal=value, appliedAverage=value)
                result = raw_projection(raw["player"], YEAR, OBSERVED)
                self.assertEqual(result["projected_total_points"], value)
                self.assertEqual(result["projection_season"], YEAR)
                self.assertEqual(result["projection_horizon"], "full_season")
                player = Player(raw, YEAR)
                player.provider_projection = result
                self.assertEqual(espn_player_to_row(player, YEAR)["projected_total_points"], value)

    def test_rejects_wrong_season_source_horizon_and_malformed_values(self):
        for fields in ({"seasonId": YEAR - 1}, {"statSourceId": 0},
                       {"statSplitTypeId": 1}, {"appliedTotal": float("inf")},
                       {"appliedTotal": "bad"}, {"appliedTotal": True}):
            with self.subTest(fields=fields):
                result = raw_projection(card(**{"appliedTotal": 100, **fields})["player"], YEAR, OBSERVED)
                self.assertIsNone(result["projected_total_points"])
        raw = card(appliedTotal=100)["player"]
        raw["stats"].append({**raw["stats"][0], "appliedTotal": 200})
        self.assertEqual(raw_projection(raw, YEAR, OBSERVED), empty_projection())
        for malformed in (None, [], {"stats": None}, {"stats": [None, "invalid"]}):
            self.assertEqual(raw_projection(malformed, YEAR, OBSERVED), empty_projection())

    def test_no_other_year_or_bare_attribute_fallback(self):
        p = Player(card(appliedTotal=100, appliedAverage=20), YEAR)
        self.assertEqual(espn_player_to_row(p, YEAR)["projected_total_points"], 100)
        self.assertIsNone(espn_player_to_row(p, YEAR - 1)["projected_total_points"])
        p.stats = {}
        self.assertIsNone(espn_player_to_row(p, YEAR)["projected_total_points"])

    def test_fetch_batches_requested_ids_and_keeps_explicit_zero(self):
        request = Mock()
        request.get_player_card.side_effect = [
            {"players": [card(1, appliedTotal=0), card(99, appliedTotal=999)]},
            {"players": [card(2, appliedTotal=100)]},
        ]
        league = SimpleNamespace(year=YEAR, espn_request=request, finalScoringPeriod=180)
        rows = fetch_projections(league, [1, 2, 1], batch_size=1)
        self.assertEqual(rows.player_id.tolist(), [1, 2])
        self.assertEqual(rows.projected_total_points.tolist(), [0, 100])
        self.assertEqual(request.get_player_card.call_count, 2)

    def test_attached_forecasts_require_compatible_complete_provenance(self):
        valid = raw_projection(card(appliedTotal=0, appliedAverage=0)["player"], YEAR, OBSERVED)
        for changes in ({"projection_horizon": "rest_of_season"}, {"projection_season": YEAR - 1},
                        {"projection_source": " "}, {"projection_source": None},
                        {"projection_observed_at": "invalid"}, {"projection_observed_at": []}):
            with self.subTest(changes=changes):
                player = SimpleNamespace(provider_projection={**valid, **changes},
                                         stats={f"{YEAR}_projected": {"applied_avg": 999}})
                self.assertEqual(player_projection(player, YEAR), empty_projection())
        self.assertEqual(player_projection(SimpleNamespace(provider_projection=valid), YEAR), valid)

    def test_malformed_normalized_metadata_stays_unavailable(self):
        for player in (SimpleNamespace(provider_projection="invalid"),
                       SimpleNamespace(stats=[]), SimpleNamespace(stats=None),
                       SimpleNamespace(stats={f"{YEAR}_projected": []})):
            with self.subTest(player=player):
                self.assertEqual(player_projection(player, YEAR), empty_projection())

    def test_provider_failure_is_missing_not_zero_and_does_not_log_secrets(self):
        request = Mock()
        request.get_player_card.side_effect = OSError("secret-cookie")
        league = SimpleNamespace(year=YEAR, espn_request=request, finalScoringPeriod=180)
        with self.assertWarnsRegex(UserWarning, "OSError") as warning:
            result = fetch_projections(league, [1])
        self.assertNotIn("secret-cookie", str(warning.warning))
        self.assertTrue(result.empty)

    def test_draft_fill_does_not_zero_forecasts(self):
        result = fill_missing_draft_analysis_data(pd.DataFrame([empty_projection()]))
        self.assertTrue(result.projected_total_points.isna().all())
        self.assertTrue(result.projected_avg_points.isna().all())

    def test_recovery_does_not_mix_projection_observations(self):
        original = attach_projections(player_frame(), frame(appliedTotal=0))
        recovery = attach_projections(player_frame(), frame(appliedTotal=500, appliedAverage=50))
        result = merge_missing_draft_analysis_data(original, recovery)
        self.assertEqual(result.loc[0, "projected_total_points"], 0)
        self.assertTrue(pd.isna(result.loc[0, "projected_avg_points"]))

    def test_recovery_cannot_assign_provenance_to_legacy_values(self):
        original = player_frame()
        original["projected_total_points"] = 0
        recovery = attach_projections(player_frame(), frame(appliedTotal=500, appliedAverage=50))
        result = merge_missing_draft_analysis_data(original, recovery)
        self.assertEqual(result.loc[0, "projected_total_points"], 0)
        self.assertTrue(pd.isna(result.loc[0, "projection_source"]))

    def test_predictive_input_retains_verified_zero(self):
        p = Player(card(appliedTotal=0, appliedAverage=0), YEAR)
        p.provider_projection = raw_projection(card(appliedTotal=0, appliedAverage=0)["player"], YEAR, OBSERVED)
        league = SimpleNamespace(year=YEAR, teams=[SimpleNamespace(roster=[p])])
        inputs = build_projection_inputs(league)
        result = project_rest_of_season(inputs)
        self.assertEqual(result.loc[0, "proj_pts_per_game"], 0)
        del p.provider_projection
        inputs = build_projection_inputs(league)
        self.assertTrue(pd.isna(inputs.loc[0, "espn_proj_avg_points"]))


class SQLiteCursor:
    """Execute production DML against a disposable database, translating binds."""
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, sql, params=()):
        return self.cursor.execute(sql.replace("%s", "?"), params)

    def fetchall(self):
        return self.cursor.fetchall()


class PersistenceAndReportTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("""CREATE TABLE provider_projection_snapshots (
            snapshot_id TEXT PRIMARY KEY, league_id INTEGER, player_id INTEGER,
            season_year INTEGER, horizon TEXT, source TEXT, observed_at TEXT,
            projected_total_points REAL, projected_avg_points REAL)""")
        self.cursor = SQLiteCursor(self.conn.cursor())
        original_read_sql = pd.read_sql
        self.read_patch = patch("features.provider_projections.pd.read_sql",
                                side_effect=lambda sql, conn, params: original_read_sql(sql.replace("%s", "?"), conn, params=params))
        self.read_patch.start()
        self.addCleanup(self.read_patch.stop)

    def save(self, rows, league_id=7):
        save_projection_snapshots(self.cursor, rows, league_id)
        self.conn.commit()

    def load(self):
        return load_projection_snapshots(self.conn, 7, YEAR)

    def test_roundtrip_zero_partial_valid_missing_and_league_season_scope(self):
        require_projection_schema(self.cursor)
        rows = pd.concat([frame(1, appliedTotal=0, appliedAverage=0),
                          frame(2, appliedTotal=1000, appliedAverage=20),
                          frame(3, appliedAverage=30), frame(4)], ignore_index=True)
        self.save(rows)
        self.save(frame(1, appliedTotal=9999), league_id=8)
        other_year = frame(1, appliedTotal=8888)
        other_year["projection_season"] = YEAR - 1
        self.save(other_year)
        loaded = self.load().set_index("player_id")
        self.assertEqual(set(loaded.index), {1, 2, 3})
        self.assertEqual(loaded.loc[1, "projected_total_points"], 0)
        self.assertEqual(loaded.loc[2, "projected_total_points"], 1000)
        self.assertTrue(pd.isna(loaded.loc[3, "projected_total_points"]))
        self.assertEqual(loaded.loc[3, "projected_avg_points"], 30)
        self.assertEqual(loaded.loc[2, "projection_observed_at"], OBSERVED)

    def test_missing_or_partial_later_fetch_does_not_destroy_valid_snapshot(self):
        self.save(frame(appliedTotal=1000, appliedAverage=20))
        self.save(frame())
        partial = frame(appliedAverage=40)
        partial["projection_observed_at"] = "2026-10-03 00:00:00"
        self.save(partial)
        self.assertEqual(self.load().iloc[0].projected_total_points, 1000)
        newer = frame(appliedTotal=0, appliedAverage=0)
        newer["projection_observed_at"] = "2026-10-02 00:00:00"
        self.save(newer)
        self.assertEqual(self.load().iloc[0].projected_total_points, 0)
        self.assertEqual(self.load().iloc[0].projected_avg_points, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM provider_projection_snapshots").fetchone()[0], 3)

    def test_ingestion_through_storage_query_report_and_csv(self):
        rows = pd.concat([frame(1, appliedTotal=0), frame(2), frame(3, appliedTotal=500)])
        self.save(rows)
        players = pd.concat([player_frame(1), player_frame(2), player_frame(3, actual=400)], ignore_index=True)
        players = attach_projections(players, self.load())
        with TemporaryDirectory() as tmp:
            report = projection_analysis(players, empty_draft(), output_dir=Path(tmp))
            csv = pd.read_csv(Path(tmp) / "projection_analysis.csv").set_index("player_name")
        self.assertEqual(len(report), 3)
        self.assertEqual(csv.loc["Player 1", "projection_difference"], 100)
        self.assertTrue(pd.isna(csv.loc["Player 1", "percent_above_projection"]))
        self.assertEqual(csv.loc["Player 1", "projection_status"], "Above Projection")
        self.assertEqual(csv.loc["Player 3", "projection_difference"], -100)
        self.assertEqual(csv.loc["Player 3", "projection_status"], "Below Projection")
        self.assertEqual(csv.loc["Player 2", "projection_status"], "Missing Projection")
        for col in ("projected_total_points", "projection_difference", "projection_percentile", "percent_above_projection"):
            self.assertTrue(pd.isna(csv.loc["Player 2", col]))

    def test_draft_only_player_uses_stored_forecast_with_recovered_actuals(self):
        self.save(frame(appliedTotal=80))
        draft = pd.DataFrame([{"player_id": 1, "player_name": "Player 1", "fantasy_team": "Old Team"}])
        p = Player(card(appliedTotal=9999), YEAR)
        p.total_points = 100
        p.avg_points = 10
        p.stats[f"{YEAR}_total"] = {"total": {"GP": 10}}
        league = SimpleNamespace(year=YEAR, free_agents=Mock(return_value=[p]))
        report = projection_analysis(attach_projections(player_frame().iloc[:0], self.load()), draft,
                                     league=league, provider_projections=self.load())
        self.assertEqual(report.loc[0, "roster_source"], "Draft")
        self.assertEqual(report.loc[0, "projection_difference"], 20)

    def test_report_rejects_mismatched_unverified_or_nonfinite_projection(self):
        for updates, status in (({"projection_season": YEAR - 1}, "Incompatible Projection"),
                                ({"projection_horizon": "rest_of_season"}, "Incompatible Projection"),
                                ({"projection_observed_at": None}, "Unverified Projection"),
                                ({"projected_total_points": float("inf")}, "Missing Projection")):
            with self.subTest(updates=updates):
                rows = frame(appliedTotal=50)
                for key, value in updates.items():
                    rows[key] = value
                report = projection_analysis(attach_projections(player_frame(), rows), empty_draft())
                self.assertEqual(report.loc[0, "projection_status"], status)
                self.assertTrue(pd.isna(report.loc[0, "projection_difference"]))

    def test_empty_report_and_all_negative_cohort(self):
        empty = projection_analysis(attach_projections(player_frame().iloc[:0], frame().iloc[:0]), empty_draft())
        self.assertTrue(empty.empty)
        players = pd.concat([player_frame(1, actual=0), player_frame(2, actual=5)])
        rows = pd.concat([frame(1, appliedTotal=10), frame(2, appliedTotal=10)])
        report = projection_analysis(attach_projections(players, rows), empty_draft())
        self.assertEqual(report.projection_status.tolist(), ["Below Projection", "Below Projection"])

    def test_actual_zero_missing_actual_and_average_only_forecast(self):
        for actual, fields, status, difference in (
            (0, {"appliedTotal": 0}, "At Projection", 0),
            (None, {"appliedTotal": 0}, "Missing Stats", None),
            (100, {"appliedAverage": 0}, "Missing Projection", None),
        ):
            with self.subTest(actual=actual, fields=fields):
                players = attach_projections(player_frame(actual=actual), frame(**fields))
                report = projection_analysis(players, empty_draft())
                self.assertEqual(report.loc[0, "projection_status"], status)
                if difference is None:
                    self.assertTrue(pd.isna(report.loc[0, "projection_difference"]))
                else:
                    self.assertEqual(report.loc[0, "projection_difference"], difference)

    def test_missing_schema_preflight_fails_without_creating_tables(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            require_projection_schema(SQLiteCursor(conn.cursor()))
        self.assertEqual(conn.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_weekly_entry_point_wires_raw_capture_storage_query_and_reports(self):
        """Run the real CLI with all external effects isolated, not live accounts."""
        raw_cards = [card(1, appliedTotal=0, appliedAverage=0), card(2)]
        players = [Player(raw, YEAR) for raw in raw_cards]
        for player in players:
            player.total_points = 100
            player.avg_points = 10
            player.stats[f"{YEAR}_total"] = {"total": {"GP": 10}}
        team = SimpleNamespace(team_id=1, team_name="Team", owners=[], roster=players)
        picks = [SimpleNamespace(playerId=p.playerId, playerName=p.name, team=team,
                                 round_num=1, round_pick=i + 1) for i, p in enumerate(players)]
        request = Mock()
        request.get_player_card.return_value = {"players": raw_cards}
        league = SimpleNamespace(year=YEAR, league_id=7, teams=[team], draft=picks,
                                 currentMatchupPeriod=1, finalScoringPeriod=180,
                                 espn_request=request, free_agents=Mock(return_value=[]))
        previous = SimpleNamespace(year=YEAR - 1, teams=[])
        sql_players = pd.concat([player_frame(1), player_frame(2)], ignore_index=True)
        sql_players["projected_total_points"] = None
        sql_players["projected_avg_points"] = None
        sql_draft = pd.DataFrame([{"player_id": p.playerId, "player_name": p.name,
                                  "round_num": 1, "round_pick": i + 1,
                                  "fantasy_team": "Team"} for i, p in enumerate(players)])
        read_snapshot = pd.read_sql

        def read(sql, conn, params):
            if "provider_projection_snapshots" in sql:
                return read_snapshot(sql, self.conn, params=params)
            return sql_players.copy() if "FROM final_rosters" in sql else sql_draft.copy()

        # Only snapshot SQL reaches the disposable database. Existing unrelated
        # weekly INSERTs are mocked, and assertions inspect the captured forecast.
        cursor = Mock()
        cursor.execute.side_effect = lambda sql, params=(): (
            self.cursor.execute(sql, params) if "provider_projection_snapshots" in sql else None
        )
        cursor.fetchall.side_effect = self.cursor.fetchall
        conn = Mock()
        conn.cursor.return_value = cursor
        conn.commit.side_effect = self.conn.commit
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        old_cwd = Path.cwd()
        with TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(sys, "path", [str(main_path.parent), *sys.path]))
            stack.callback(os.chdir, old_cwd)
            os.chdir(tmp)
            stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
            stack.enter_context(patch.dict(os.environ, {
                "ESPN_YEAR": str(YEAR), "ESPN_LEAGUE_ID": "7",
                "ESPN_S2": "synthetic", "ESPN_SWID": "synthetic",
            }, clear=True))
            stack.enter_context(patch("sys.argv", [str(main_path)]))
            stack.enter_context(patch("espn_api.basketball.League", side_effect=[league, previous]))
            stack.enter_context(patch("database.get_connection", return_value=conn))
            stack.enter_context(patch("pandas.read_sql", side_effect=read))
            for module, function in (
                ("build_breakout_players", "build_breakout_players"),
                ("build_matchups", "build_matchups"), ("biggest_upset", "biggest_upset"),
                ("build_players_weekly", "build_players_weekly"),
                ("lineup_efficiency", "lineup_efficiency"),
                ("manager_power_rankings", "manager_power_rankings"),
                ("nba_schedule", "build_nba_schedule"),
            ):
                stack.enter_context(patch(f"features.{module}.{function}", return_value=pd.DataFrame()))
            stack.enter_context(patch("features.predictive_engine.run_projection_pipeline",
                                      return_value={"projections": pd.DataFrame(), "value": pd.DataFrame()}))
            email = stack.enter_context(patch("alerts.send_email.send_digest_email"))
            with redirect_stdout(StringIO()):
                runpy.run_path(str(main_path), run_name="__main__")
            report = pd.read_csv(Path(tmp) / "reports/projection_analysis.csv").set_index("player_name")
            output = pd.read_csv(Path(tmp) / "data/players.csv").set_index("player_id")
            email.assert_not_called()
        self.assertEqual(output.loc[1, "projected_total_points"], 0)
        self.assertTrue(pd.isna(output.loc[2, "projected_total_points"]))
        self.assertEqual(report.loc["Player 1", "projection_status"], "Above Projection")
        self.assertEqual(report.loc["Player 2", "projection_status"], "Missing Projection")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM provider_projection_snapshots").fetchone()[0], 1)
        request.get_player_card.assert_called_once_with([1, 2], 180)


if __name__ == "__main__":
    unittest.main()
