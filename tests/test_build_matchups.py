import unittest

from features.build_matchups import build_matchups


class FakeTeam:
    def __init__(self, team_id, team_name):
        self.team_id = team_id
        self.team_name = team_name


class FakeBox:
    def __init__(self, home_team, home_score, away_team, away_score):
        self.home_team = home_team
        self.home_score = home_score
        self.away_team = away_team
        self.away_score = away_score


class FakeLeague:
    def __init__(self, year, boxes_by_week):
        self.year = year
        self._boxes_by_week = boxes_by_week

    def box_scores(self, matchup_period):
        return self._boxes_by_week.get(matchup_period, [])


class BuildMatchupsTest(unittest.TestCase):
    def test_computes_winner_loser_and_margin(self):
        team_a = FakeTeam(1, "Team A")
        team_b = FakeTeam(2, "Team B")
        box = FakeBox(team_a, 120, team_b, 100)
        league = FakeLeague(2026, {1: [box]})

        result = build_matchups(league, start_week=1, end_week=1, season_year=2026)
        row = result.iloc[0]

        self.assertEqual(row["season_year"], 2026)
        self.assertEqual(row["week"], 1)
        self.assertEqual(row["winner"], "Team A")
        self.assertEqual(row["loser"], "Team B")
        self.assertEqual(row["winner_team_id"], 1)
        self.assertEqual(row["loser_team_id"], 2)
        self.assertEqual(row["margin"], 20)

    def test_away_win_sets_winner_to_away_team(self):
        team_a = FakeTeam(1, "Team A")
        team_b = FakeTeam(2, "Team B")
        box = FakeBox(team_a, 90, team_b, 110)
        league = FakeLeague(2026, {1: [box]})

        result = build_matchups(league, start_week=1, end_week=1, season_year=2026)
        row = result.iloc[0]

        self.assertEqual(row["winner"], "Team B")
        self.assertEqual(row["loser"], "Team A")
        self.assertEqual(row["winner_team_id"], 2)
        self.assertEqual(row["loser_team_id"], 1)
        self.assertEqual(row["margin"], 20)

    def test_tie_has_no_winner_team_id(self):
        team_a = FakeTeam(1, "Team A")
        team_b = FakeTeam(2, "Team B")
        box = FakeBox(team_a, 100, team_b, 100)
        league = FakeLeague(2026, {1: [box]})

        result = build_matchups(league, start_week=1, end_week=1, season_year=2026)
        row = result.iloc[0]

        self.assertEqual(row["winner"], "Tie")
        self.assertEqual(row["loser"], "Tie")
        self.assertIsNone(row["winner_team_id"])
        self.assertIsNone(row["loser_team_id"])
        self.assertEqual(row["margin"], 0)

    def test_defaults_season_year_from_league_when_not_provided(self):
        team_a = FakeTeam(1, "Team A")
        team_b = FakeTeam(2, "Team B")
        box = FakeBox(team_a, 100, team_b, 90)
        league = FakeLeague(2025, {1: [box]})

        result = build_matchups(league, start_week=1, end_week=1)

        self.assertEqual(result.iloc[0]["season_year"], 2025)

    def test_covers_multiple_weeks(self):
        team_a = FakeTeam(1, "Team A")
        team_b = FakeTeam(2, "Team B")
        boxes_by_week = {
            1: [FakeBox(team_a, 100, team_b, 90)],
            2: [FakeBox(team_a, 80, team_b, 95)],
        }
        league = FakeLeague(2026, boxes_by_week)

        result = build_matchups(league, start_week=1, end_week=2, season_year=2026)

        self.assertEqual(result["week"].tolist(), [1, 2])
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
