import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from features.manager_power_rankings import manager_power_rankings


def _matchup(week, h_id, h_name, h_score, a_id, a_name, a_score, season_year=2026):
    if h_score > a_score:
        winner_team_id, loser_team_id = h_id, a_id
    elif a_score > h_score:
        winner_team_id, loser_team_id = a_id, h_id
    else:
        winner_team_id, loser_team_id = None, None
    return {
        "season_year": season_year,
        "week": week,
        "home_team_id": h_id,
        "home_team": h_name,
        "home_score": h_score,
        "away_team_id": a_id,
        "away_team": a_name,
        "away_score": a_score,
        "winner": h_name if winner_team_id == h_id else (a_name if winner_team_id == a_id else "Tie"),
        "loser": a_name if winner_team_id == h_id else (h_name if winner_team_id == a_id else "Tie"),
        "winner_team_id": winner_team_id,
        "loser_team_id": loser_team_id,
        "margin": abs(h_score - a_score),
    }


def _two_weeks():
    rows = []
    for week in (1, 2):
        rows.append(_matchup(week, 1, "T1", 100, 4, "T4", 70))
        rows.append(_matchup(week, 2, "T2", 90, 3, "T3", 80))
    return pd.DataFrame(rows)


class ManagerPowerRankingsTest(unittest.TestCase):
    def test_all_play_win_pct_and_ranking_order(self):
        result = manager_power_rankings(_two_weeks())

        by_team = result.set_index("team_name")
        self.assertEqual(by_team.loc["T1", "all_play_win_pct"], 1.0)
        self.assertAlmostEqual(by_team.loc["T2", "all_play_win_pct"], round(2 / 3, 4))
        self.assertAlmostEqual(by_team.loc["T3", "all_play_win_pct"], round(1 / 3, 4))
        self.assertEqual(by_team.loc["T4", "all_play_win_pct"], 0.0)

        self.assertEqual(result.sort_values("rank")["team_name"].tolist(),
                         ["T1", "T2", "T3", "T4"])

    def test_luck_is_actual_minus_all_play(self):
        # T4 loses both games but we flip week-2 result via scores
        rows = [
            _matchup(1, 1, "T1", 100, 4, "T4", 70),
            _matchup(2, 1, "T1", 60, 4, "T4", 70),   # T4 beats top scorer -> lucky-ish
            _matchup(1, 2, "T2", 90, 3, "T3", 80),
            _matchup(2, 2, "T2", 90, 3, "T3", 80),
        ]
        result = manager_power_rankings(pd.DataFrame(rows)).set_index("team_name")
        self.assertAlmostEqual(
            result.loc["T4", "luck"],
            round(result.loc["T4", "actual_win_pct"] - result.loc["T4", "all_play_win_pct"], 4),
        )

    def test_efficiency_merge_influences_score(self):
        eff = pd.DataFrame([
            {"season_year": 2026, "team_id": 1, "avg_weekly_efficiency": 0.99},
            {"season_year": 2026, "team_id": 2, "avg_weekly_efficiency": 0.5},
            {"season_year": 2026, "team_id": 3, "avg_weekly_efficiency": 0.5},
            {"season_year": 2026, "team_id": 4, "avg_weekly_efficiency": 0.5},
        ])
        result = manager_power_rankings(_two_weeks(), lineup_efficiency_df=eff)
        self.assertEqual(result.loc[result["team_id"] == 1, "avg_weekly_efficiency"].iloc[0], 0.99)
        self.assertEqual(result.sort_values("rank")["team_name"].iloc[0], "T1")

    def test_runs_without_efficiency(self):
        result = manager_power_rankings(_two_weeks(), lineup_efficiency_df=None)
        self.assertTrue(result["avg_weekly_efficiency"].isna().all())
        self.assertEqual(sorted(result["rank"].tolist()), [1, 2, 3, 4])

    def test_writes_csv(self):
        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = manager_power_rankings(_two_weeks(), output_dir=output_dir)
            written = pd.read_csv(output_dir / "manager_power_rankings.csv")
        self.assertEqual(written.columns.tolist(), result.columns.tolist())

    def test_empty_returns_empty(self):
        result = manager_power_rankings(pd.DataFrame())
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
