import unittest
from types import SimpleNamespace

from features.build_breakout_players import build_breakout_players


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
        avg_points=avg_points,
        total_points=total_points,
        year=year,
        stats={f"{year}_total": {"total": {"GP": games_played}}},
    )


def make_team(*players):
    return SimpleNamespace(roster=list(players))


class FakeLeague:
    def __init__(self, year, teams=None, free_agents_by_position=None, free_agents=None):
        self.year = year
        self.teams = teams or []
        self.free_agents_by_position = free_agents_by_position or {}
        self.free_agents_list = free_agents or []
        self.free_agent_calls = []

    def free_agents(self, size=50, position=None):
        self.free_agent_calls.append((size, position))
        if position is None:
            return self.free_agents_list
        return self.free_agents_by_position.get(position, [])


class BuildBreakoutPlayersTest(unittest.TestCase):
    def test_ranks_roster_and_free_agent_previous_matches(self):
        current_rostered = make_player(1, "Roster Jump", 35, 2100, 60)
        current_free_agent = make_player(2, "Free Agent Jump", 28, 1680, 60, position="SG")
        previous_rostered = make_player(1, "Roster Jump", 20, 1200, 60)
        previous_free_agent = make_player(2, "Free Agent Jump", 10, 600, 60, position="SG")

        current_league = FakeLeague(
            2025,
            teams=[make_team(current_rostered)],
            free_agents=[current_free_agent],
        )
        previous_league = FakeLeague(
            2024,
            teams=[make_team(previous_rostered)],
            free_agents_by_position={"SG": [previous_free_agent]},
        )

        result = build_breakout_players(current_league, previous_league)

        self.assertEqual(result["player_id"].tolist(), [2, 1])
        self.assertEqual(result["source_previous_season"].tolist(), ["free_agent", "roster"])
        self.assertEqual(result["avg_points_jump"].tolist(), [18, 15])
        self.assertEqual(previous_league.free_agent_calls, [(500, "SG")])

    def test_excludes_missing_and_low_games_played_players(self):
        missing_previous = make_player(1, "Missing Previous", 30, 1800, 60)
        low_current_gp = make_player(2, "Low Current GP", 30, 900, 30)
        low_previous_gp_current = make_player(3, "Low Previous GP", 30, 1800, 60)
        low_previous_gp_previous = make_player(3, "Low Previous GP", 10, 100, 10)

        current_league = FakeLeague(
            2025,
            teams=[
                make_team(
                    missing_previous,
                    low_current_gp,
                    low_previous_gp_current,
                )
            ],
        )
        previous_league = FakeLeague(
            2024,
            teams=[make_team(low_previous_gp_previous)],
        )

        result = build_breakout_players(current_league, previous_league)

        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [
            "player_id",
            "player_name",
            "position",
            "pro_team",
            "current_avg_points",
            "previous_avg_points",
            "avg_points_jump",
            "current_games_played",
            "previous_games_played",
            "current_total_points",
            "previous_total_points",
            "source_previous_season",
        ])

    def test_deduplicates_current_roster_and_free_agent_pool(self):
        duplicate_rostered = make_player(1, "Duplicate Player", 25, 1500, 60)
        duplicate_free_agent = make_player(1, "Duplicate Player", 25, 1500, 60)
        previous = make_player(1, "Duplicate Player", 15, 900, 60)

        current_league = FakeLeague(
            2025,
            teams=[make_team(duplicate_rostered)],
            free_agents=[duplicate_free_agent],
        )
        previous_league = FakeLeague(2024, teams=[make_team(previous)])

        result = build_breakout_players(current_league, previous_league)

        self.assertEqual(result["player_id"].tolist(), [1])


if __name__ == "__main__":
    unittest.main()
