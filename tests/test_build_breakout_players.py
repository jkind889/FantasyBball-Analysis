import unittest
from unittest.mock import patch
from types import SimpleNamespace

import pandas as pd

from cache import BreakoutPlayerCache
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


class FakePreviousPlayerCache:
    def __init__(self, previous_rows):
        self.previous_rows = {
            row["player_id"]: row
            for row in previous_rows
        }
        self.lookups = []

    def find_previous_player(self, current_player_row):
        self.lookups.append(current_player_row["player_id"])
        previous_row = self.previous_rows.get(current_player_row["player_id"])
        if previous_row is None:
            return None, "missing"
        return previous_row, "database"


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

    def test_uses_previous_player_cache_rows_when_provided(self):
        current = make_player(1, "Database Jump", 32, 1920, 60)
        current_league = FakeLeague(2025, teams=[make_team(current)])
        previous_league = FakeLeague(2024)
        previous_cache = FakePreviousPlayerCache([
            {
                "player_id": 1,
                "player_name": "Database Jump",
                "position": "PG",
                "pro_team": "NBA",
                "avg_points": 14,
                "total_points": 840,
                "games_played": 60,
            }
        ])

        result = build_breakout_players(
            current_league,
            previous_league,
            previous_player_cache=previous_cache,
        )

        self.assertEqual(result["player_id"].tolist(), [1])
        self.assertEqual(result["source_previous_season"].tolist(), ["database"])
        self.assertEqual(result["previous_avg_points"].tolist(), [14])
        self.assertEqual(result["avg_points_jump"].tolist(), [18])
        self.assertEqual(previous_cache.lookups, [1])
        self.assertEqual(previous_league.free_agent_calls, [])

    def test_breakout_player_cache_falls_back_to_previous_free_agents_once_per_position(self):
        previous_free_agent = make_player(
            2,
            "Free Agent Jump",
            10,
            600,
            60,
            position="SG",
            year=2024,
        )
        previous_league = FakeLeague(
            2024,
            free_agents_by_position={"SG": [previous_free_agent]},
        )
        empty_previous_rows = pd.DataFrame(
            columns=[
                "player_id",
                "player_name",
                "position",
                "pro_team",
                "avg_points",
                "total_points",
                "games_played",
            ]
        )

        with patch("cache._fetch_previous_rostered_players", return_value=empty_previous_rows):
            previous_cache = BreakoutPlayerCache(
                None,
                2024,
                previous_league=previous_league,
            )

        current_row = {
            "player_id": 2,
            "player_name": "Free Agent Jump",
            "position": "SG",
        }

        first_match, first_source = previous_cache.find_previous_player(current_row)
        second_match, second_source = previous_cache.find_previous_player(current_row)

        self.assertEqual(first_source, "free_agent")
        self.assertEqual(second_source, "free_agent")
        self.assertEqual(first_match["player_id"], 2)
        self.assertEqual(second_match["avg_points"], 10)
        self.assertEqual(previous_league.free_agent_calls, [(500, "SG")])


if __name__ == "__main__":
    unittest.main()
