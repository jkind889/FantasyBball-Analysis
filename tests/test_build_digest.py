import unittest

import pandas as pd

from features.build_digest import build_digest


class BuildDigestTest(unittest.TestCase):
    def _matchups_df(self):
        return pd.DataFrame([
            {
                "week": 1,
                "home_team": "Team A",
                "home_score": 100.0,
                "away_team": "Team B",
                "away_score": 95.0,
                "winner": "Team A",
                "loser": "Team B",
                "margin": 5.0,
            },
            {
                "week": 1,
                "home_team": "Team C",
                "home_score": 130.0,
                "away_team": "Team D",
                "away_score": 80.0,
                "winner": "Team C",
                "loser": "Team D",
                "margin": 50.0,
            },
            {
                "week": 2,
                "home_team": "Team A",
                "home_score": 90.0,
                "away_team": "Team C",
                "away_score": 91.0,
                "winner": "Team C",
                "loser": "Team A",
                "margin": 1.0,
            },
        ])

    def _breakout_players_df(self):
        return pd.DataFrame([
            {"player_name": "Player X", "avg_points_jump": 3.0},
            {"player_name": "Player Y", "avg_points_jump": 8.5},
        ])

    def _draft_analysis_df(self):
        return pd.DataFrame([
            {"player_name": "Player X", "fantasy_team": "Team A", "vope_score": 120.0},
            {"player_name": "Player Z", "fantasy_team": "Team B", "vope_score": pd.NA},
            {"player_name": "Player Y", "fantasy_team": "Team C", "vope_score": 45.0},
        ])

    def test_identifies_closest_and_blowout_for_given_week(self):
        subject, body = build_digest(
            self._matchups_df(),
            self._breakout_players_df(),
            self._draft_analysis_df(),
            season_year=2026,
            week=1,
        )

        self.assertIn("Week 1", subject)
        self.assertIn("Closest matchup: Team A 100.0 - 95.0 Team B (margin 5.0)", body)
        self.assertIn("Biggest blowout: Team C beat Team D by 50.0", body)

    def test_defaults_to_latest_week_when_not_specified(self):
        subject, body = build_digest(
            self._matchups_df(),
            self._breakout_players_df(),
            self._draft_analysis_df(),
            season_year=2026,
        )

        self.assertIn("Week 2", subject)
        self.assertIn("Closest matchup: Team A 90.0 - 91.0 Team C (margin 1.0)", body)

    def test_reports_top_riser_and_best_value_pick(self):
        _, body = build_digest(
            self._matchups_df(),
            self._breakout_players_df(),
            self._draft_analysis_df(),
            season_year=2026,
            week=1,
        )

        self.assertIn("Top riser: Player Y (+8.5 avg points vs. last season)", body)
        self.assertIn(
            "Best draft value so far: Player X (Team A), vope score 120.0", body
        )

    def test_handles_empty_inputs_without_error(self):
        subject, body = build_digest(
            pd.DataFrame(columns=["week", "home_team", "home_score", "away_team", "away_score", "winner", "loser", "margin"]),
            pd.DataFrame(columns=["player_name", "avg_points_jump"]),
            pd.DataFrame(columns=["player_name", "fantasy_team", "vope_score"]),
            season_year=2026,
        )

        self.assertIn("Fantasy Basketball Weekly Digest", body)


if __name__ == "__main__":
    unittest.main()
