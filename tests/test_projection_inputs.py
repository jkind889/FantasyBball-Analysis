import unittest

from features.projection_inputs import build_projection_inputs


class FakePlayer:
    def __init__(self, player_id, name, position, pro_team, avg_points,
                 projected_avg_points=None, games_played=0, year=2026):
        self.playerId = player_id
        self.name = name
        self.position = position
        self.proTeam = pro_team
        self.avg_points = avg_points
        self.projected_avg_points = projected_avg_points
        self.stats = {f"{year}_total": {"total": {"GP": games_played}}}


class FakeTeam:
    def __init__(self, roster):
        self.roster = roster


class FakeLeague:
    def __init__(self, year, teams):
        self.year = year
        self.teams = teams


class BuildProjectionInputsTest(unittest.TestCase):
    def test_blends_current_prior_and_espn_columns(self):
        current = FakeLeague(2026, [FakeTeam([
            FakePlayer(1, "Alice", "PG", "ATL", 42.0,
                       projected_avg_points=40.0, games_played=12),
            FakePlayer(2, "Bob", "C", "BOS", 25.0,
                       projected_avg_points=28.0, games_played=0),
        ])])
        previous = FakeLeague(2025, [FakeTeam([
            FakePlayer(1, "Alice", "PG", "ATL", 30.0, year=2025),
        ])])

        df = build_projection_inputs(current, previous).set_index("player_id")

        self.assertEqual(df.loc[1, "prior_avg_points"], 30.0)
        self.assertEqual(df.loc[1, "current_avg_points"], 42.0)
        self.assertEqual(df.loc[1, "current_games_played"], 12)
        self.assertEqual(df.loc[1, "espn_proj_avg_points"], 40.0)
        # No prior-year row for Bob -> prior stays null.
        self.assertTrue(df.loc[2, "prior_avg_points"] is None
                        or df.loc[2, "prior_avg_points"] != df.loc[2, "prior_avg_points"])

    def test_dedupes_players_on_multiple_rosters(self):
        shared = FakePlayer(7, "Dup", "SF", "CHI", 10.0)
        current = FakeLeague(2026, [FakeTeam([shared]), FakeTeam([shared])])
        df = build_projection_inputs(current)
        self.assertEqual(len(df), 1)

    def test_works_without_previous_league(self):
        current = FakeLeague(2026, [FakeTeam([
            FakePlayer(1, "Solo", "PG", "ATL", 20.0, games_played=5),
        ])])
        df = build_projection_inputs(current)
        self.assertEqual(list(df["player_id"]), [1])
        self.assertEqual(df.loc[0, "current_games_played"], 5)


if __name__ == "__main__":
    unittest.main()
