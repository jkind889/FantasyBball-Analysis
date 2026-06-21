import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pandas as pd

from features.projection_analysis import (
    projection_analysis,
    projection_status_for_percentile,
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


def make_player_row(
    player_id,
    name,
    actual,
    projected,
    team="Final Team",
    games_played=60,
):
    return {
        "player_id": player_id,
        "player_name": name,
        "team_name": team,
        "position": "PG",
        "pro_team": "NBA",
        "pos_rank": player_id,
        "total_points": actual,
        "avg_points": actual / games_played if games_played else 0,
        "projected_total_points": projected,
        "projected_avg_points": projected / games_played if games_played else 0,
        "games_played": games_played,
    }


def make_draft_row(player_id, name, team="Draft Team"):
    return {
        "player_id": player_id,
        "player_name": name,
        "round_num": 1,
        "round_pick": player_id,
        "fantasy_team": team,
    }


def make_espn_player(player_id, name, actual, projected, games_played=60):
    year = 2025
    return SimpleNamespace(
        playerId=player_id,
        name=name,
        position="PG",
        proTeam="NBA",
        posRank=1,
        total_points=actual,
        avg_points=actual / games_played if games_played else 0,
        projected_total_points=projected,
        projected_avg_points=projected / games_played if games_played else 0,
        year=year,
        stats={f"{year}_total": {"total": {"GP": games_played}}},
    )


class FakeLeague:
    def __init__(self, free_agents=None):
        self._free_agents = free_agents or []

    def free_agents(self, size=50, position=None):
        return self._free_agents


class ProjectionAnalysisTest(unittest.TestCase):
    def test_builds_deduplicated_union_with_membership_and_teams(self):
        players_df = pd.DataFrame([
            make_player_row(1, "Both Player", 1200, 1000, team="Final A"),
            make_player_row(2, "Final Player", 900, 1000, team="Final B"),
        ])
        draft_df = pd.DataFrame([
            make_draft_row(1, "Both Player", team="Draft A"),
            make_draft_row(3, "Draft Player", team="Draft C"),
        ])

        result = projection_analysis(
            players_df,
            draft_df,
            league=FakeLeague([make_espn_player(3, "Draft Player", 700, 500)]),
        ).set_index("player_name")

        self.assertEqual(len(result), 3)
        self.assertEqual(result.loc["Both Player", "roster_source"], "Both")
        self.assertEqual(result.loc["Both Player", "draft_team"], "Draft A")
        self.assertEqual(result.loc["Both Player", "final_team"], "Final A")
        self.assertEqual(result.loc["Final Player", "roster_source"], "Final")
        self.assertEqual(result.loc["Draft Player", "roster_source"], "Draft")
        self.assertEqual(result.loc["Draft Player", "projection_difference"], 200)

    def test_scores_all_resolved_players_and_sorts_by_difference(self):
        players_df = pd.DataFrame([
            make_player_row(1, "Exact", 1000, 1000, games_played=0),
            make_player_row(2, "Above", 1300, 1000, games_played=1),
            make_player_row(3, "Below", 600, 1000, games_played=60),
        ])
        draft_df = pd.DataFrame(columns=[
            "player_id", "player_name", "round_num", "round_pick", "fantasy_team"
        ])

        result = projection_analysis(players_df, draft_df)

        self.assertEqual(result["player_name"].tolist(), ["Above", "Exact", "Below"])
        self.assertEqual(result["projection_difference"].tolist(), [300, 0, -400])
        self.assertEqual(result["projection_percentile"].tolist(), [100, 50, 0])

    def test_zero_projection_is_ranked_but_percentage_is_blank(self):
        players_df = pd.DataFrame([make_player_row(1, "Zero Projection", 500, 0)])
        draft_df = pd.DataFrame([make_draft_row(1, "Zero Projection")])

        result = projection_analysis(players_df, draft_df)

        self.assertEqual(result.loc[0, "projection_difference"], 500)
        self.assertEqual(result.loc[0, "projection_percentile"], 50)
        self.assertTrue(pd.isna(result.loc[0, "percent_above_projection"]))
        self.assertEqual(result.loc[0, "projection_status"], "Near Projection")

    def test_tied_differences_receive_average_percentile(self):
        players_df = pd.DataFrame([
            make_player_row(1, "One", 1100, 1000),
            make_player_row(2, "Two", 600, 500),
        ])
        draft_df = pd.DataFrame(columns=[
            "player_id", "player_name", "round_num", "round_pick", "fantasy_team"
        ])

        result = projection_analysis(players_df, draft_df)

        self.assertEqual(result["projection_percentile"].tolist(), [50, 50])
        self.assertEqual(result["projection_status"].tolist(), ["Near Projection"] * 2)

    def test_unresolved_draft_player_is_preserved_below_scored_players(self):
        players_df = pd.DataFrame([
            make_player_row(1, "Resolved", 1200, 1000),
        ])
        draft_df = pd.DataFrame([
            make_draft_row(2, "Missing"),
        ])

        result = projection_analysis(players_df, draft_df, league=FakeLeague())

        self.assertEqual(result["player_name"].tolist(), ["Resolved", "Missing"])
        self.assertEqual(result.loc[1, "projection_status"], "Missing Stats")
        self.assertTrue(pd.isna(result.loc[1, "projection_difference"]))

    def test_projection_status_boundaries(self):
        cases = [
            (90, "Far Above Projection"),
            (75, "Above Projection"),
            (25.01, "Near Projection"),
            (25, "Below Projection"),
            (10.01, "Below Projection"),
            (10, "Far Below Projection"),
        ]

        for percentile, expected in cases:
            with self.subTest(percentile=percentile):
                self.assertEqual(projection_status_for_percentile(percentile), expected)

    def test_writes_sibling_csv(self):
        players_df = pd.DataFrame([make_player_row(1, "Player", 1200, 1000)])
        draft_df = pd.DataFrame([make_draft_row(1, "Player")])

        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = projection_analysis(
                players_df,
                draft_df,
                output_dir=output_dir,
            )
            written = pd.read_csv(output_dir / "projection_analysis.csv")

        self.assertEqual(written.columns.tolist(), result.columns.tolist())
        self.assertEqual(written.loc[0, "projection_difference"], 200)


if __name__ == "__main__":
    unittest.main()
