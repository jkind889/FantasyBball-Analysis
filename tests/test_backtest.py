import unittest

import numpy as np
import pandas as pd

from features.backtest import (
    biggest_misses,
    draft_scores,
    preseason_inputs,
    rank_metrics,
    ranking_benchmark,
    realized_value,
    run_preseason_backtest,
)


def _inputs(n=12):
    # Realized production descends with id; ESPN/prior mostly agree but swap
    # a couple of players so the ranking metrics have something to catch.
    rows = []
    for i in range(n):
        realized = 40 - i
        rows.append({
            "player_id": i, "player_name": f"P{i}", "position": "C",
            "pro_team": "X",
            "prior_avg_points": realized + (5 if i == 1 else 0),
            "current_avg_points": realized,
            "current_games_played": 70 if i != 3 else 5,
            "espn_proj_avg_points": realized - (8 if i == 0 else 0),
        })
    return pd.DataFrame(rows)


def _realized():
    return pd.DataFrame({
        "player_id": [1, 2, 3, 4],
        "realized_vorp_ros": [100.0, 80.0, 60.0, 40.0],
    })


class PreseasonInputsTest(unittest.TestCase):
    def test_strips_current_season(self):
        out = preseason_inputs(_inputs())
        self.assertTrue(out["current_avg_points"].isna().all())
        self.assertTrue((out["current_games_played"] == 0).all())


class RealizedValueTest(unittest.TestCase):
    def test_uses_actual_rate_and_games(self):
        out = realized_value(_inputs(), league_size=2, roster_slots={"C": 1})
        top = out.iloc[0]
        self.assertEqual(top["player_name"], "P0")
        self.assertEqual(top["games_played"], 70)
        self.assertAlmostEqual(top["realized_pts_per_game"], 40.0)
        # replacement = 2nd-best C (39) -> vorp 1/game * 70 games
        self.assertAlmostEqual(top["realized_vorp_ros"], 70.0)
        self.assertEqual(list(out["realized_rank"]), list(range(1, 13)))

    def test_min_games_filter(self):
        out = realized_value(_inputs(), league_size=2, min_games=10)
        self.assertNotIn(3, set(out["player_id"]))

    def test_never_played_counts_as_zero(self):
        df = _inputs()
        df.loc[df["player_id"] == 5, ["current_avg_points", "current_games_played"]] = [np.nan, 0]
        out = realized_value(df, league_size=2).set_index("player_id")
        self.assertEqual(out.loc[5, "games_played"], 0)
        self.assertEqual(out.loc[5, "realized_vorp_ros"], 0.0)


class RankMetricsTest(unittest.TestCase):
    def test_perfect_ranking(self):
        scores = pd.Series({1: 4.0, 2: 3.0, 3: 2.0, 4: 1.0})
        m = rank_metrics(scores, _realized(), ks=(2,))
        self.assertAlmostEqual(m["spearman"], 1.0)
        self.assertAlmostEqual(m["precision@2"], 1.0)
        self.assertAlmostEqual(m["value_captured@2"], 1.0)
        self.assertAlmostEqual(m["pairwise_win_rate"], 1.0)
        self.assertEqual(m["n_scored"], 4)

    def test_reversed_ranking(self):
        scores = pd.Series({1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0})
        m = rank_metrics(scores, _realized(), ks=(2,))
        self.assertAlmostEqual(m["spearman"], -1.0)
        self.assertAlmostEqual(m["precision@2"], 0.0)
        self.assertAlmostEqual(m["value_captured@2"], 100.0 / 180.0)
        self.assertAlmostEqual(m["pairwise_win_rate"], 0.0)

    def test_missing_scores_are_unranked(self):
        scores = pd.Series({1: 2.0, 2: 1.0})
        m = rank_metrics(scores, _realized(), ks=(2,))
        self.assertEqual(m["n_scored"], 2)
        self.assertEqual(m["n_universe"], 4)
        self.assertAlmostEqual(m["precision@2"], 1.0)


class RankingBenchmarkTest(unittest.TestCase):
    def test_one_row_per_candidate(self):
        cands = {"a": pd.Series({1: 4.0, 2: 3.0, 3: 2.0, 4: 1.0}),
                 "b": pd.Series({1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0})}
        out = ranking_benchmark(cands, _realized(), ks=(2,))
        self.assertEqual(list(out["candidate"]), ["a", "b"])
        self.assertIn("value_captured@2", out.columns)
        self.assertGreater(out.iloc[0]["spearman"], out.iloc[1]["spearman"])
        self.assertEqual(list(out["n_common"]), [4, 4])
        self.assertAlmostEqual(out.iloc[0]["spearman_common"], 1.0)


class DraftScoresTest(unittest.TestCase):
    def test_earlier_pick_scores_higher(self):
        draft = pd.DataFrame({
            "player_id": [10, 11, 12],
            "round_num": [1, 1, 2],
            "round_pick": [1, 2, 1],
        })
        s = draft_scores(draft)
        self.assertGreater(s[10], s[11])
        self.assertGreater(s[11], s[12])
        self.assertEqual(s[12], -3.0)


class RunPreseasonBacktestTest(unittest.TestCase):
    def test_end_to_end(self):
        draft = pd.DataFrame({"player_id": [0, 1], "round_num": [1, 1],
                              "round_pick": [1, 2]})
        result = run_preseason_backtest(
            _inputs(), league_size=2, draft_df=draft,
            roster_slots={"C": 1}, min_games_per_game=10,
        )
        summary = result["summary"]
        self.assertEqual(
            set(summary["candidate"]),
            {"engine_vorp", "engine_ppg", "espn_proj", "prior_ppg", "draft_order"},
        )
        self.assertEqual(
            set(summary["target"]),
            {"realized_vorp_ros", "realized_vorp_per_game"},
        )
        # Per-game target drops the 5-GP player from the universe.
        pg_rows = summary[summary["target"] == "realized_vorp_per_game"]
        self.assertEqual(pg_rows["n_universe"].iloc[0], 11)
        misses = result["misses"]
        self.assertIn("rank_delta", misses.columns)
        # P3 played 5 games and is filtered out of the misses table.
        self.assertNotIn(3, set(misses["player_id"]))
        self.assertIn("espn_proj_avg_points", misses.columns)


if __name__ == "__main__":
    unittest.main()
