import unittest
from types import SimpleNamespace

import pandas as pd

from features.draft_analysis import draft_analysis


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


def make_player_row(player_id, name, avg_points, games_played=60):
    return {
        "player_id": player_id,
        "player_name": name,
        "team_name": "Team A",
        "position": "PG",
        "pro_team": "NBA",
        "pos_rank": player_id,
        "total_points": avg_points * games_played,
        "avg_points": avg_points,
        "projected_total_points": avg_points * games_played,
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
    def test_fetches_missing_player_before_ranking(self):
        players_df = pd.DataFrame([
            make_player_row(1, "Roster Player", 20),
        ])
        draft_df = pd.DataFrame([
            make_draft_row(1, "Roster Player", 1, 1),
            make_draft_row(2, "Free Agent Hit", 1, 2, fantasy_team="Team B"),
        ])
        league = FakeLeague({
            None: [make_player(2, "Free Agent Hit", 30, 1800, 60)],
        })

        result = draft_analysis(players_df, draft_df, league=league)

        self.assertEqual(result["player_name"].tolist(), ["Free Agent Hit", "Roster Player"])
        self.assertEqual(result["actual_pg_rank"].tolist(), [1, 2])
        self.assertEqual(result["vope_score"].tolist(), [10, -10])
        self.assertEqual(result["weighted_vope_score"].tolist(), [10, -20])
        self.assertEqual(result["value_status"].tolist(), ["Steal", "Reach"])
        self.assertNotIn("draft_value_score", result.columns)

    def test_scores_against_same_round_expectation(self):
        players_df = pd.DataFrame([
            make_player_row(1, "Round Star", 40),
            make_player_row(2, "Round Plus", 30),
            make_player_row(3, "Round Minus", 20),
            make_player_row(4, "Round Bust", 10),
        ])
        draft_df = pd.DataFrame([
            make_draft_row(1, "Round Star", 1, 1),
            make_draft_row(2, "Round Plus", 1, 2),
            make_draft_row(3, "Round Minus", 1, 3),
            make_draft_row(4, "Round Bust", 1, 4),
        ])

        result = draft_analysis(players_df, draft_df)
        by_player = result.set_index("player_name")

        self.assertAlmostEqual(by_player.loc["Round Star", "expected_avg_points"], 20)
        self.assertAlmostEqual(by_player.loc["Round Star", "vope_score"], 20)
        self.assertAlmostEqual(by_player.loc["Round Star", "roi"], 1)
        self.assertAlmostEqual(by_player.loc["Round Bust", "expected_avg_points"], 30)
        self.assertAlmostEqual(by_player.loc["Round Bust", "vope_score"], -20)
        self.assertAlmostEqual(by_player.loc["Round Bust", "roi"], -2 / 3)

    def test_early_pick_miss_is_weighted_more_than_late_pick_miss(self):
        players_df = pd.DataFrame([
            make_player_row(1, "Early Miss", 10),
            make_player_row(2, "Early Peer", 20),
            make_player_row(3, "Late Miss", 10),
            make_player_row(4, "Late Peer", 20),
        ])
        draft_df = pd.DataFrame([
            make_draft_row(1, "Early Miss", 1, 1),
            make_draft_row(2, "Early Peer", 1, 2),
            make_draft_row(3, "Late Miss", 2, 1),
            make_draft_row(4, "Late Peer", 2, 2),
        ])

        result = draft_analysis(players_df, draft_df)
        by_player = result.set_index("player_name")

        self.assertEqual(by_player.loc["Early Miss", "vope_score"], -10)
        self.assertEqual(by_player.loc["Late Miss", "vope_score"], -10)
        self.assertLess(
            by_player.loc["Early Miss", "weighted_vope_score"],
            by_player.loc["Late Miss", "weighted_vope_score"],
        )

    def test_fetches_each_missing_position_once(self):
        players_df = pd.DataFrame(columns=PLAYER_COLUMNS)
        draft_df = pd.DataFrame([
            make_draft_row(1, "Missing One", 1, 1),
            make_draft_row(2, "Missing Two", 1, 2, fantasy_team="Team B"),
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
        players_df = pd.DataFrame(columns=PLAYER_COLUMNS)
        draft_df = pd.DataFrame([
            make_draft_row(1, "Not Found", 1, 1),
        ])

        result = draft_analysis(players_df, draft_df, league=FakeLeague())

        self.assertEqual(result.loc[0, "player_name"], "Not Found")
        self.assertEqual(result.loc[0, "avg_points"], 0)
        self.assertEqual(result.loc[0, "games_played"], 0)
        self.assertTrue(pd.isna(result.loc[0, "actual_pg_rank"]))
        self.assertTrue(pd.isna(result.loc[0, "weighted_vope_score"]))
        self.assertEqual(result.loc[0, "value_status"], "Missing Stats")

    def test_low_games_played_fetched_player_is_not_scored(self):
        players_df = pd.DataFrame(columns=PLAYER_COLUMNS)
        draft_df = pd.DataFrame([
            make_draft_row(1, "Low GP", 1, 1),
        ])
        league = FakeLeague({
            None: [make_player(1, "Low GP", 30, 300, 10)],
        })

        result = draft_analysis(players_df, draft_df, league=league)

        self.assertEqual(result.loc[0, "avg_points"], 30)
        self.assertEqual(result.loc[0, "games_played"], 10)
        self.assertTrue(pd.isna(result.loc[0, "actual_pg_rank"]))
        self.assertTrue(pd.isna(result.loc[0, "weighted_vope_score"]))
        self.assertEqual(result.loc[0, "value_status"], "Insufficient GP")


if __name__ == "__main__":
    unittest.main()
