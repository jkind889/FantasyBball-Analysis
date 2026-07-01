import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pandas as pd

from features.draft_analysis import (
    build_team_draft_summary,
    draft_analysis,
    value_status_for_vope_percentile,
)


PLAYER_COLUMNS = [
    "player_id",
    "player_name",
    "team_name",
    "position",
    "pro_team",
    "pos_rank",
    "total_points",
    "avg_points",
    "projected_total_points",
    "projected_avg_points",
    "games_played",
]


def make_player_row(player_id, name, total_points, games_played=60):
    avg_points = total_points / games_played if games_played else 0
    return {
        "player_id": player_id,
        "player_name": name,
        "team_name": "Team A",
        "position": "PG",
        "pro_team": "NBA",
        "pos_rank": player_id,
        "total_points": total_points,
        "avg_points": avg_points,
        "projected_total_points": total_points,
        "projected_avg_points": avg_points,
        "games_played": games_played,
    }


def make_draft_row(player_id, name, round_num, round_pick, fantasy_team="Team A"):
    return {
        "player_id": player_id,
        "player_name": name,
        "round_num": round_num,
        "round_pick": round_pick,
        "fantasy_team": fantasy_team,
    }


def make_player(
    player_id,
    name,
    avg_points,
    total_points,
    games_played,
    position="PG",
    year=2025,
):
    return SimpleNamespace(
        playerId=player_id,
        name=name,
        position=position,
        proTeam="NBA",
        posRank=1,
        avg_points=avg_points,
        total_points=total_points,
        projected_total_points=total_points,
        projected_avg_points=avg_points,
        year=year,
        stats={f"{year}_total": {"total": {"GP": games_played}}},
    )


class FakeLeague:
    def __init__(self, free_agents_by_position=None):
        self.free_agents_by_position = free_agents_by_position or {}
        self.free_agent_calls = []

    def free_agents(self, size=50, position=None):
        self.free_agent_calls.append((size, position))
        return self.free_agents_by_position.get(position, [])


class DraftAnalysisTest(unittest.TestCase):
    def test_assigns_five_pick_buckets_and_scores_inclusive_total_points(self):
        totals = [100, 200, 300, 400, 500, 600]
        players_df = pd.DataFrame([
            make_player_row(index, f"Player {index}", total)
            for index, total in enumerate(totals, start=1)
        ])
        draft_df = pd.DataFrame([
            make_draft_row(index, f"Player {index}", 1, index)
            for index in range(1, 7)
        ])

        result = draft_analysis(players_df, draft_df).set_index("overall_pick")

        self.assertEqual(result.loc[1, "draft_bucket"], "1-5")
        self.assertEqual(result.loc[5, "draft_bucket"], "1-5")
        self.assertEqual(result.loc[6, "draft_bucket"], "6-10")
        self.assertEqual(result.loc[1, "expected_total_points"], 300)
        self.assertEqual(result.loc[1, "vope_score"], -200)
        self.assertAlmostEqual(result.loc[1, "percent_above_expected"], -200 / 3)
        self.assertEqual(result.loc[5, "vope_score"], 200)
        self.assertEqual(result.loc[6, "expected_total_points"], 600)
        self.assertEqual(result.loc[6, "vope_score"], 0)
        self.assertEqual(result.loc[6, "percent_above_expected"], 0)

    def test_bucket_boundaries_continue_across_draft_rounds(self):
        players_df = pd.DataFrame([
            make_player_row(index, f"Player {index}", 1000 + index)
            for index in range(1, 12)
        ])
        draft_df = pd.DataFrame([
            make_draft_row(
                index,
                f"Player {index}",
                ((index - 1) // 10) + 1,
                ((index - 1) % 10) + 1,
            )
            for index in range(1, 12)
        ])

        result = draft_analysis(players_df, draft_df).set_index("overall_pick")

        self.assertEqual(result.loc[5, "draft_bucket"], "1-5")
        self.assertEqual(result.loc[6, "draft_bucket"], "6-10")
        self.assertEqual(result.loc[10, "draft_bucket"], "6-10")
        self.assertEqual(result.loc[11, "draft_bucket"], "11-15")

    def test_value_status_uses_vope_percentile_boundaries(self):
        examples = [
            (100, "Elite Steal"),
            (90, "Elite Steal"),
            (89.99, "Steal"),
            (75, "Steal"),
            (74.99, "Fair"),
            (25.01, "Fair"),
            (25, "Bust"),
            (10.01, "Bust"),
            (10, "Major Bust"),
            (1, "Major Bust"),
        ]

        for percentile, expected_status in examples:
            with self.subTest(percentile=percentile):
                self.assertEqual(
                    value_status_for_vope_percentile(percentile),
                    expected_status,
                )

    def test_tied_vope_scores_receive_average_percentile(self):
        players_df = pd.DataFrame([
            make_player_row(index, f"Player {index}", 1000)
            for index in range(1, 11)
        ])
        draft_df = pd.DataFrame([
            make_draft_row(index, f"Player {index}", 1, index)
            for index in range(1, 11)
        ])

        result = draft_analysis(players_df, draft_df)

        self.assertEqual(result["vope_score"].tolist(), [0] * 10)
        self.assertEqual(result["vope_percentile"].tolist(), [50] * 10)
        self.assertEqual(result["value_status"].tolist(), ["Fair"] * 10)

    def test_twenty_one_games_is_eligible_and_twenty_is_not(self):
        players_df = pd.DataFrame([
            make_player_row(1, "Injured Qualifier", 840, games_played=21),
            make_player_row(2, "Healthy Peer", 1200, games_played=60),
            make_player_row(3, "Below Minimum", 900, games_played=20),
        ])
        draft_df = pd.DataFrame([
            make_draft_row(1, "Injured Qualifier", 1, 1),
            make_draft_row(2, "Healthy Peer", 1, 2),
            make_draft_row(3, "Below Minimum", 1, 3),
        ])

        result = draft_analysis(players_df, draft_df).set_index("player_name")

        self.assertEqual(result.loc["Injured Qualifier", "expected_total_points"], 1020)
        self.assertEqual(result.loc["Injured Qualifier", "vope_score"], -180)
        self.assertAlmostEqual(
            result.loc["Injured Qualifier", "percent_above_expected"],
            -180 / 1020 * 100,
        )
        self.assertTrue(pd.isna(result.loc["Below Minimum", "vope_score"]))
        self.assertTrue(pd.isna(result.loc["Below Minimum", "vope_percentile"]))
        self.assertEqual(result.loc["Below Minimum", "value_status"], "Insufficient GP")

    def test_zero_expected_points_leaves_percentage_unavailable(self):
        players_df = pd.DataFrame([make_player_row(1, "Zero", 0)])
        draft_df = pd.DataFrame([make_draft_row(1, "Zero", 1, 1)])

        result = draft_analysis(players_df, draft_df)

        self.assertEqual(result.loc[0, "expected_total_points"], 0)
        self.assertEqual(result.loc[0, "vope_score"], 0)
        self.assertTrue(pd.isna(result.loc[0, "percent_above_expected"]))

    def test_fetches_missing_player_before_scoring(self):
        players_df = pd.DataFrame([make_player_row(1, "Roster Player", 1200)])
        draft_df = pd.DataFrame([
            make_draft_row(1, "Roster Player", 1, 1),
            make_draft_row(2, "Free Agent Hit", 1, 2, fantasy_team="Team B"),
        ])
        league = FakeLeague({
            None: [make_player(2, "Free Agent Hit", 30, 1800, 60)],
        })

        result = draft_analysis(players_df, draft_df, league=league).set_index("player_name")

        self.assertEqual(result.loc["Free Agent Hit", "actual_total_points_rank"], 1)
        self.assertEqual(result.loc["Free Agent Hit", "vope_score"], 300)
        self.assertEqual(result.loc["Roster Player", "vope_score"], -300)
        self.assertEqual(league.free_agent_calls, [(500, None)])

    def test_unfound_missing_player_is_preserved_with_zero_stats(self):
        players_df = pd.DataFrame(columns=PLAYER_COLUMNS)
        draft_df = pd.DataFrame([make_draft_row(1, "Not Found", 1, 1)])

        result = draft_analysis(players_df, draft_df, league=FakeLeague())

        self.assertEqual(result.loc[0, "player_name"], "Not Found")
        self.assertEqual(result.loc[0, "total_points"], 0)
        self.assertEqual(result.loc[0, "games_played"], 0)
        self.assertTrue(pd.isna(result.loc[0, "vope_score"]))
        self.assertTrue(pd.isna(result.loc[0, "vope_percentile"]))
        self.assertEqual(result.loc[0, "value_status"], "Missing Stats")

    def test_builds_team_summary_from_plain_vope_scores(self):
        draft_analysis_df = pd.DataFrame([
            {"player_name": "A Hit", "fantasy_team": "Team A", "vope_score": 10},
            {"player_name": "A Missing", "fantasy_team": "Team A", "vope_score": pd.NA},
            {"player_name": "A Miss", "fantasy_team": "Team A", "vope_score": -4},
            {"player_name": "B Hit", "fantasy_team": "Team B", "vope_score": 6},
        ])

        result = build_team_draft_summary(draft_analysis_df)
        by_team = result.set_index("fantasy_team")

        self.assertEqual(by_team.loc["Team A", "total_picks"], 3)
        self.assertEqual(by_team.loc["Team A", "scored_picks"], 2)
        self.assertEqual(by_team.loc["Team A", "unscored_picks"], 1)
        self.assertEqual(by_team.loc["Team A", "total_vope_score"], 6)
        self.assertEqual(by_team.loc["Team A", "average_vope_score_per_pick"], 3)
        self.assertEqual(result["fantasy_team"].tolist(), ["Team B", "Team A"])

    def test_writes_player_and_team_reports(self):
        players_df = pd.DataFrame([
            make_player_row(1, "Team A Hit", 1800),
            make_player_row(2, "Team B Miss", 1200),
        ])
        draft_df = pd.DataFrame([
            make_draft_row(1, "Team A Hit", 1, 1, fantasy_team="Team A"),
            make_draft_row(2, "Team B Miss", 1, 2, fantasy_team="Team B"),
        ])

        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = draft_analysis(players_df, draft_df, output_dir=output_dir)
            player_report = pd.read_csv(output_dir / "draft_analysis.csv")
            team_summary = pd.read_csv(output_dir / "draft_analysis_by_team.csv")

        self.assertIn("draft_bucket", result.columns)
        self.assertIn("percent_above_expected", player_report.columns)
        self.assertNotIn("weighted_vope_score", player_report.columns)
        self.assertIn("total_vope_score", team_summary.columns)


if __name__ == "__main__":
    unittest.main()
