import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from features.biggest_upset import biggest_upset


def _matchup(season_year, week, home_team_id, home_team, home_score,
             away_team_id, away_team, away_score):
    if home_score > away_score:
        winner, loser = home_team, away_team
        winner_team_id, loser_team_id = home_team_id, away_team_id
    elif away_score > home_score:
        winner, loser = away_team, home_team
        winner_team_id, loser_team_id = away_team_id, home_team_id
    else:
        winner, loser = "Tie", "Tie"
        winner_team_id, loser_team_id = None, None

    return {
        "season_year": season_year,
        "week": week,
        "home_team_id": home_team_id,
        "home_team": home_team,
        "home_score": home_score,
        "away_team_id": away_team_id,
        "away_team": away_team,
        "away_score": away_score,
        "winner": winner,
        "loser": loser,
        "winner_team_id": winner_team_id,
        "loser_team_id": loser_team_id,
        "margin": abs(home_score - away_score),
    }


class BiggestUpsetTest(unittest.TestCase):
    def test_lower_average_team_winning_is_flagged_as_upset(self):
        # Team A averages low through week 2 (80, 80), Team B averages high (140, 140).
        # In week 3, A (entering avg 80) beats B (entering avg 140) => upset.
        matchups_df = pd.DataFrame([
            _matchup(2026, 1, 1, "Team A", 80, 2, "Team B", 140),
            _matchup(2026, 2, 1, "Team A", 80, 2, "Team B", 140),
            _matchup(2026, 3, 1, "Team A", 100, 2, "Team B", 90),
        ])

        result = biggest_upset(matchups_df)

        self.assertEqual(len(result), 1)
        row = result.iloc[0]
        self.assertEqual(row["week"], 3)
        self.assertEqual(row["winner"], "Team A")
        self.assertEqual(row["loser"], "Team B")
        self.assertEqual(row["winner_season_avg_entering_week"], 80)
        self.assertEqual(row["loser_season_avg_entering_week"], 140)
        self.assertEqual(row["avg_gap"], 60)

    def test_higher_average_team_winning_is_not_an_upset(self):
        matchups_df = pd.DataFrame([
            _matchup(2026, 1, 1, "Team A", 140, 2, "Team B", 80),
            _matchup(2026, 2, 1, "Team A", 140, 2, "Team B", 80),
            _matchup(2026, 3, 1, "Team A", 100, 2, "Team B", 90),
        ])

        result = biggest_upset(matchups_df)

        self.assertTrue(result.empty)

    def test_first_matchup_of_season_has_no_prior_average_and_is_excluded(self):
        matchups_df = pd.DataFrame([
            _matchup(2026, 1, 1, "Team A", 80, 2, "Team B", 140),
        ])

        result = biggest_upset(matchups_df)

        self.assertTrue(result.empty)

    def test_ties_are_excluded(self):
        matchups_df = pd.DataFrame([
            _matchup(2026, 1, 1, "Team A", 80, 2, "Team B", 140),
            _matchup(2026, 2, 1, "Team A", 80, 2, "Team B", 140),
            _matchup(2026, 3, 1, "Team A", 100, 2, "Team B", 100),
        ])

        result = biggest_upset(matchups_df)

        self.assertTrue(result.empty)

    def test_multiple_upsets_are_sorted_by_avg_gap_descending(self):
        matchups_df = pd.DataFrame([
            # Team A vs Team B: small gap upset
            _matchup(2026, 1, 1, "Team A", 90, 2, "Team B", 100),
            _matchup(2026, 2, 1, "Team A", 90, 2, "Team B", 100),
            _matchup(2026, 3, 1, "Team A", 95, 2, "Team B", 90),  # gap 10
            # Team C vs Team D: big gap upset
            _matchup(2026, 1, 3, "Team C", 60, 4, "Team D", 150),
            _matchup(2026, 2, 3, "Team C", 60, 4, "Team D", 150),
            _matchup(2026, 3, 3, "Team C", 100, 4, "Team D", 90),  # gap 90
        ])

        result = biggest_upset(matchups_df)

        self.assertEqual(len(result), 2)
        self.assertEqual(result.iloc[0]["winner"], "Team C")
        self.assertEqual(result.iloc[0]["avg_gap"], 90)
        self.assertEqual(result.iloc[1]["winner"], "Team A")
        self.assertEqual(result.iloc[1]["avg_gap"], 10)

    def test_writes_csv(self):
        matchups_df = pd.DataFrame([
            _matchup(2026, 1, 1, "Team A", 80, 2, "Team B", 140),
            _matchup(2026, 2, 1, "Team A", 80, 2, "Team B", 140),
            _matchup(2026, 3, 1, "Team A", 100, 2, "Team B", 90),
        ])

        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = biggest_upset(matchups_df, output_dir=output_dir)
            written = pd.read_csv(output_dir / "biggest_upsets.csv")

        self.assertEqual(written.columns.tolist(), result.columns.tolist())
        self.assertEqual(written.loc[0, "winner"], "Team A")

    def test_empty_matchups_returns_empty_dataframe(self):
        matchups_df = pd.DataFrame(columns=[
            "season_year", "week", "home_team_id", "home_team", "home_score",
            "away_team_id", "away_team", "away_score", "winner", "loser",
            "winner_team_id", "loser_team_id", "margin",
        ])

        result = biggest_upset(matchups_df)

        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
