import unittest

import pandas as pd

from features.decision_modes import draft_board, recommend_lineup, waiver_board


def _value_row(pid, name, pos, pg, games=40, repl=10.0):
    return {
        "rank": 0, "player_id": pid, "player_name": name, "position": pos,
        "eligible_positions": pos, "proj_pts_per_game": pg,
        "games_remaining": games, "ros_total": pg * games,
        "replacement_pts_per_game": repl, "vorp_per_game": pg - repl,
        "vorp_ros": (pg - repl) * games,
    }


def _inputs_row(pid, pro_team, espn=None):
    return {
        "player_id": pid, "player_name": f"p{pid}", "position": "PG",
        "pro_team": pro_team, "prior_avg_points": None,
        "current_avg_points": None, "current_games_played": 0,
        "espn_proj_avg_points": espn,
    }


def _schedule(rows):
    # rows: (pro_team, fantasy_week, day)
    return pd.DataFrame(
        [
            {"season_year": 2026, "pro_team": t, "opponent": "XXX",
             "is_home": 1, "game_date": f"2026-01-{d:02d}",
             "scoring_period": w, "fantasy_week": w}
            for (t, w, d) in rows
        ]
    )


class DraftBoardTest(unittest.TestCase):
    def test_ranks_by_vorp_ros_and_attaches_espn_columns(self):
        value_df = pd.DataFrame([
            _value_row(1, "Star", "PG", 40),
            _value_row(2, "Scrub", "SG", 12),
        ])
        inputs_df = pd.DataFrame([
            _inputs_row(1, "ATL", espn=38.0),
            _inputs_row(2, "BOS", espn=11.0),
        ])
        board = draft_board(value_df, inputs_df)
        self.assertEqual(list(board["player_name"]), ["Star", "Scrub"])
        self.assertEqual(list(board["rank"]), [1, 2])
        self.assertEqual(board.iloc[0]["espn_proj_avg_points"], 38.0)
        self.assertEqual(board.iloc[0]["pro_team"], "ATL")


class WaiverBoardTest(unittest.TestCase):
    def setUp(self):
        self.value_df = pd.DataFrame([
            _value_row(1, "Rostered", "PG", 40),
            _value_row(2, "FA-good", "PG", 25),
            _value_row(3, "FA-streamer", "PG", 18),
        ])
        self.inputs_df = pd.DataFrame([
            _inputs_row(1, "ATL"), _inputs_row(2, "BOS"), _inputs_row(3, "CHI"),
        ])
        # week 3: BOS plays 2, CHI plays 4
        self.schedule_df = _schedule([
            ("BOS", 3, 1), ("BOS", 3, 3),
            ("CHI", 3, 1), ("CHI", 3, 3), ("CHI", 3, 5), ("CHI", 3, 7),
        ])

    def test_only_free_agents_and_next_week_value(self):
        board = waiver_board(
            self.value_df, self.inputs_df, self.schedule_df,
            free_agent_ids={2, 3}, week=3,
        )
        self.assertEqual(set(board["player_name"]), {"FA-good", "FA-streamer"})
        streamer = board[board["player_name"] == "FA-streamer"].iloc[0]
        self.assertEqual(streamer["next_week_games"], 4)
        self.assertAlmostEqual(streamer["next_week_value"], 18 * 4)
        # ROS rank still favours the better player
        self.assertEqual(
            board.sort_values("ros_rank").iloc[0]["player_name"], "FA-good"
        )

    def test_missing_week_gives_zero_next_week_value(self):
        board = waiver_board(
            self.value_df, self.inputs_df, self.schedule_df,
            free_agent_ids={2}, week=99,
        )
        self.assertEqual(board.iloc[0]["next_week_games"], 0)
        self.assertEqual(board.iloc[0]["next_week_value"], 0)


class RecommendLineupTest(unittest.TestCase):
    def test_fills_best_slots_and_benches_the_rest(self):
        roster = [
            {"player_id": 1, "player_name": "PG-A", "position": "PG",
             "pro_team": "ATL", "eligible_slots": ["PG", "G", "UTIL"],
             "injury_status": "ACTIVE"},
            {"player_id": 2, "player_name": "PG-B", "position": "PG",
             "pro_team": "ATL", "eligible_slots": ["PG", "G", "UTIL"],
             "injury_status": "ACTIVE"},
            {"player_id": 3, "player_name": "OUT-guy", "position": "C",
             "pro_team": "ATL", "eligible_slots": ["C", "UTIL"],
             "injury_status": "OUT"},
        ]
        projections_df = pd.DataFrame([
            {"player_id": 1, "proj_pts_per_game": 30.0},
            {"player_id": 2, "proj_pts_per_game": 20.0},
            {"player_id": 3, "proj_pts_per_game": 40.0},
        ])
        schedule_df = _schedule([("ATL", 1, 1), ("ATL", 1, 3), ("ATL", 1, 5)])
        out = recommend_lineup(roster, projections_df, schedule_df, week=1)

        row1 = out[out["player_name"] == "PG-A"].iloc[0]
        self.assertEqual(row1["games_this_week"], 3)
        self.assertAlmostEqual(row1["proj_points_week"], 90.0)
        self.assertNotEqual(row1["recommended_slot"], "BENCH")
        # OUT player is never started even though his projection is highest
        out_row = out[out["player_name"] == "OUT-guy"].iloc[0]
        self.assertEqual(out_row["recommended_slot"], "BENCH")

    def test_empty_roster(self):
        out = recommend_lineup([], pd.DataFrame(), pd.DataFrame(), week=1)
        self.assertEqual(len(out), 0)
        self.assertIn("recommended_slot", out.columns)


if __name__ == "__main__":
    unittest.main()
