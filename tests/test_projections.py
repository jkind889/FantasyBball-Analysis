import unittest

import numpy as np
import pandas as pd

from features.projections import (
    MAX_CURRENT_WEIGHT,
    STABILIZE_GP,
    project_rest_of_season,
)


class ProjectRestOfSeasonTest(unittest.TestCase):
    def test_empty_input_returns_empty_frame(self):
        result = project_rest_of_season(pd.DataFrame())
        self.assertEqual(len(result), 0)
        self.assertIn("ros_total", result.columns)

    def test_preseason_uses_prior_and_espn_average(self):
        players = pd.DataFrame([
            {"player_id": 1, "player_name": "A", "position": "PG",
             "prior_avg_points": 30.0, "espn_proj_avg_points": 40.0,
             "current_games_played": 0, "games_remaining": 82},
        ])
        row = project_rest_of_season(players).iloc[0]
        self.assertEqual(row["basis"], "preseason")
        self.assertEqual(row["current_weight"], 0.0)
        self.assertAlmostEqual(row["proj_pts_per_game"], 35.0)
        self.assertEqual(row["ros_total"], 35.0 * 82)

    def test_full_data_caps_current_weight_and_blends(self):
        players = pd.DataFrame([
            {"player_id": 1, "player_name": "A", "position": "C",
             "prior_avg_points": 20.0, "espn_proj_avg_points": 20.0,
             "current_avg_points": 40.0, "current_games_played": STABILIZE_GP + 10,
             "games_remaining": 40},
        ])
        row = project_rest_of_season(players).iloc[0]
        self.assertEqual(row["current_weight"], MAX_CURRENT_WEIGHT)
        expected = MAX_CURRENT_WEIGHT * 40.0 + (1 - MAX_CURRENT_WEIGHT) * 20.0
        self.assertAlmostEqual(row["proj_pts_per_game"], round(expected, 3))
        self.assertEqual(row["basis"], "current")

    def test_partial_season_weight_scales_with_games(self):
        gp = 10
        players = pd.DataFrame([
            {"player_id": 1, "player_name": "A", "position": "SF",
             "prior_avg_points": 10.0, "espn_proj_avg_points": 10.0,
             "current_avg_points": 30.0, "current_games_played": gp,
             "games_remaining": 20},
        ])
        row = project_rest_of_season(players).iloc[0]
        self.assertAlmostEqual(
            row["current_weight"], (gp / STABILIZE_GP) * MAX_CURRENT_WEIGHT
        )
        self.assertEqual(row["basis"], "blended")

    def test_missing_games_remaining_falls_back_to_default(self):
        players = pd.DataFrame([
            {"player_id": 1, "player_name": "A", "position": "PG",
             "prior_avg_points": 25.0},
        ])
        row = project_rest_of_season(players, default_games_remaining=50).iloc[0]
        self.assertEqual(row["games_remaining"], 50)

    def test_only_current_data_available(self):
        players = pd.DataFrame([
            {"player_id": 1, "player_name": "A", "position": "PG",
             "current_avg_points": 33.0, "current_games_played": 5,
             "games_remaining": 10},
        ])
        row = project_rest_of_season(players).iloc[0]
        self.assertEqual(row["basis"], "current")
        self.assertAlmostEqual(row["proj_pts_per_game"], 33.0)

    def test_no_data_yields_nan_projection(self):
        players = pd.DataFrame([
            {"player_id": 1, "player_name": "A", "position": "PG"},
        ])
        row = project_rest_of_season(players).iloc[0]
        self.assertTrue(np.isnan(row["proj_pts_per_game"]))


if __name__ == "__main__":
    unittest.main()
