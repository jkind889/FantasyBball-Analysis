import unittest
from types import SimpleNamespace

import pandas as pd

from features.draft_analysis import draft_analysis


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
    def test_fetches_missing_player_before_ranking(self):
        players_df = pd.DataFrame([
            {
                "player_id": 1,
                "player_name": "Roster Player",
                "team_name": "Team A",
                "position": "PG",
                "pro_team": "NBA",
                "pos_rank": 2,
                "total_points": 1200,
                "avg_points": 20,
                "projected_total_points": 1200,
                "projected_avg_points": 20,
                "games_played": 60,
            }
        ])
        draft_df = pd.DataFrame([
            {
                "player_id": 1,
                "player_name": "Roster Player",
                "round_num": 1,
                "round_pick": 1,
                "fantasy_team": "Team A",
            },
            {
                "player_id": 2,
                "player_name": "Free Agent Hit",
                "round_num": 1,
                "round_pick": 2,
                "fantasy_team": "Team B",
            },
        ])
        league = FakeLeague({
            None: [make_player(2, "Free Agent Hit", 30, 1800, 60)],
        })

        result = draft_analysis(players_df, draft_df, league=league)

        self.assertEqual(result["player_name"].tolist(), ["Free Agent Hit", "Roster Player"])
        self.assertEqual(result["actual_pg_rank"].tolist(), [1, 2])
        self.assertEqual(result["draft_value_score"].tolist(), [1, -1])
        self.assertEqual(result["value_status"].tolist(), ["Fair", "Fair"])

    def test_fetches_each_missing_position_once(self):
        players_df = pd.DataFrame(columns=[
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
        ])
        draft_df = pd.DataFrame([
            {
                "player_id": 1,
                "player_name": "Missing One",
                "round_num": 1,
                "round_pick": 1,
                "fantasy_team": "Team A",
            },
            {
                "player_id": 2,
                "player_name": "Missing Two",
                "round_num": 1,
                "round_pick": 2,
                "fantasy_team": "Team B",
            },
        ])
        league = FakeLeague({
            None: [
                make_player(1, "Missing One", 30, 1800, 60),
                make_player(2, "Missing Two", 25, 1500, 60),
            ],
        })

        draft_analysis(players_df, draft_df, league=league)

        self.assertEqual(league.free_agent_calls, [(500, None)])

    def test_unfound_missing_player_is_preserved_with_zero_stats(self):
        players_df = pd.DataFrame(columns=[
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
        ])
        draft_df = pd.DataFrame([
            {
                "player_id": 1,
                "player_name": "Not Found",
                "round_num": 1,
                "round_pick": 1,
                "fantasy_team": "Team A",
            },
        ])

        result = draft_analysis(players_df, draft_df, league=FakeLeague())

        self.assertEqual(result.loc[0, "player_name"], "Not Found")
        self.assertEqual(result.loc[0, "avg_points"], 0)
        self.assertEqual(result.loc[0, "games_played"], 0)
        self.assertTrue(pd.isna(result.loc[0, "actual_pg_rank"]))
        self.assertTrue(pd.isna(result.loc[0, "draft_value_score"]))
        self.assertEqual(result.loc[0, "value_status"], "Missing Stats")

    def test_low_games_played_fetched_player_is_not_scored(self):
        players_df = pd.DataFrame(columns=[
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
        ])
        draft_df = pd.DataFrame([
            {
                "player_id": 1,
                "player_name": "Low GP",
                "round_num": 1,
                "round_pick": 1,
                "fantasy_team": "Team A",
            },
        ])
        league = FakeLeague({
            None: [make_player(1, "Low GP", 30, 300, 10)],
        })

        result = draft_analysis(players_df, draft_df, league=league)

        self.assertEqual(result.loc[0, "avg_points"], 30)
        self.assertEqual(result.loc[0, "games_played"], 10)
        self.assertTrue(pd.isna(result.loc[0, "actual_pg_rank"]))
        self.assertTrue(pd.isna(result.loc[0, "draft_value_score"]))
        self.assertEqual(result.loc[0, "value_status"], "Insufficient GP")


if __name__ == "__main__":
    unittest.main()
