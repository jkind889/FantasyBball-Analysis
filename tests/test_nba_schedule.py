import unittest
from datetime import date, datetime

import pandas as pd

from features.nba_schedule import (
    attach_games_remaining,
    build_nba_schedule,
    games_per_week,
    games_remaining,
)

# ESPN pro-team ids: 1 = ATL, 2 = BOS, 4 = CHI (see PRO_TEAM_MAP).


def _ms(y, m, d):
    return int(datetime(y, m, d).timestamp() * 1000)


def _game(home, away, y, m, d):
    return {"homeProTeamId": home, "awayProTeamId": away, "date": _ms(y, m, d)}


def _sample_pro_schedule():
    # ATL: 3 games (Oct 20 vs BOS home, Oct 22 @ CHI, Oct 27 vs CHI home)
    # BOS: 1 game (Oct 20 @ ATL)
    # CHI: 2 games
    return {
        0: {},  # free agents, ignored
        1: {
            "1": [_game(1, 2, 2025, 10, 20)],
            "3": [_game(4, 1, 2025, 10, 22)],
            "8": [_game(1, 4, 2025, 10, 27)],
        },
        2: {"1": [_game(1, 2, 2025, 10, 20)]},
        4: {
            "3": [_game(4, 1, 2025, 10, 22)],
            "8": [_game(1, 4, 2025, 10, 27)],
        },
    }


class BuildNbaScheduleTest(unittest.TestCase):
    def test_flattens_to_one_row_per_team_per_game(self):
        df = build_nba_schedule(_sample_pro_schedule(), season_year=2026)
        self.assertEqual(set(df["pro_team"]), {"ATL", "BOS", "CHI"})
        self.assertEqual(len(df[df["pro_team"] == "ATL"]), 3)
        self.assertEqual(len(df[df["pro_team"] == "BOS"]), 1)

    def test_home_away_and_opponent(self):
        df = build_nba_schedule(_sample_pro_schedule(), season_year=2026)
        atl_first = df[(df["pro_team"] == "ATL")].sort_values("game_date").iloc[0]
        self.assertEqual(atl_first["opponent"], "BOS")
        self.assertEqual(atl_first["is_home"], 1)
        chi_row = df[(df["pro_team"] == "CHI") & (df["opponent"] == "ATL")].iloc[0]
        self.assertEqual(chi_row["is_home"], 1)

    def test_fantasy_week_from_matchup_ids(self):
        df = build_nba_schedule(
            _sample_pro_schedule(),
            season_year=2026,
            matchup_ids={1: ["1", "2", "3"], 2: ["8", "9"]},
        )
        weeks = dict(zip(df["scoring_period"], df["fantasy_week"]))
        self.assertEqual(weeks[1], 1)
        self.assertEqual(weeks[3], 1)
        self.assertEqual(weeks[8], 2)

    def test_fantasy_week_fallback_buckets_by_seven(self):
        df = build_nba_schedule(_sample_pro_schedule(), season_year=2026)
        weeks = dict(zip(df["scoring_period"], df["fantasy_week"]))
        self.assertEqual(weeks[1], 1)   # (1-1)//7 + 1
        self.assertEqual(weeks[8], 2)   # (8-1)//7 + 1

    def test_empty_source(self):
        self.assertTrue(build_nba_schedule({}, season_year=2026).empty)


class AggregatesTest(unittest.TestCase):
    def setUp(self):
        self.df = build_nba_schedule(_sample_pro_schedule(), season_year=2026)

    def test_games_remaining_as_of_date(self):
        remaining = games_remaining(self.df, as_of=date(2025, 10, 23))
        self.assertEqual(remaining["ATL"], 1)   # only Oct 27 left
        self.assertNotIn("BOS", remaining)      # Oct 20 already played
        self.assertEqual(remaining["CHI"], 1)

    def test_games_per_week(self):
        wide = games_per_week(self.df)
        self.assertEqual(wide.loc["ATL", 1], 2)
        self.assertEqual(wide.loc["ATL", 2], 1)

    def test_attach_games_remaining_maps_by_pro_team(self):
        players = pd.DataFrame([
            {"player_id": 1, "pro_team": "ATL"},
            {"player_id": 2, "pro_team": "BOS"},
            {"player_id": 3, "pro_team": "XXX"},
        ])
        out = attach_games_remaining(players, self.df, as_of=date(2025, 10, 23))
        self.assertEqual(out.loc[out["player_id"] == 1, "games_remaining"].iloc[0], 1)
        self.assertTrue(
            pd.isna(out.loc[out["player_id"] == 3, "games_remaining"].iloc[0])
        )


if __name__ == "__main__":
    unittest.main()
