import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from features.roster_points_comparison import roster_points_comparison


class RosterPointsComparisonTest(unittest.TestCase):
    def test_sums_draft_and_final_rosters_and_calculates_difference(self):
        players_df = pd.DataFrame([
            {"player_name": "A Final 1", "team_name": "Team A", "total_points": 900},
            {"player_name": "A Final 2", "team_name": "Team A", "total_points": 700},
            {"player_name": "B Final", "team_name": "Team B", "total_points": 800},
        ])
        draft_analysis_df = pd.DataFrame([
            {"player_name": "A Draft 1", "fantasy_team": "Team A", "total_points": 1000},
            {"player_name": "A Draft 2", "fantasy_team": "Team A", "total_points": 400},
            {"player_name": "B Draft", "fantasy_team": "Team B", "total_points": 1200},
        ])

        result = roster_points_comparison(players_df, draft_analysis_df)
        by_team = result.set_index("fantasy_team")

        self.assertEqual(by_team.loc["Team A", "draft_roster_total_points"], 1400)
        self.assertEqual(by_team.loc["Team A", "final_roster_total_points"], 1600)
        self.assertEqual(by_team.loc["Team A", "point_difference"], 200)
        self.assertEqual(by_team.loc["Team B", "point_difference"], -400)
        self.assertEqual(result["fantasy_team"].tolist(), ["Team A", "Team B"])

    def test_keeps_teams_present_in_only_one_roster_snapshot(self):
        players_df = pd.DataFrame([
            {"team_name": "Final Only", "total_points": 500},
        ])
        draft_analysis_df = pd.DataFrame([
            {"fantasy_team": "Draft Only", "total_points": 300},
        ])

        result = roster_points_comparison(players_df, draft_analysis_df).set_index(
            "fantasy_team"
        )

        self.assertEqual(result.loc["Final Only", "draft_roster_total_points"], 0)
        self.assertEqual(result.loc["Final Only", "point_difference"], 500)
        self.assertEqual(result.loc["Draft Only", "final_roster_total_points"], 0)
        self.assertEqual(result.loc["Draft Only", "point_difference"], -300)

    def test_writes_csv(self):
        players_df = pd.DataFrame([
            {"team_name": "Team A", "total_points": 500},
        ])
        draft_analysis_df = pd.DataFrame([
            {"fantasy_team": "Team A", "total_points": 300},
        ])

        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = roster_points_comparison(
                players_df,
                draft_analysis_df,
                output_dir=output_dir,
            )
            written = pd.read_csv(output_dir / "roster_points_comparison.csv")

        self.assertEqual(written.columns.tolist(), result.columns.tolist())
        self.assertEqual(written.loc[0, "point_difference"], 200)


if __name__ == "__main__":
    unittest.main()
