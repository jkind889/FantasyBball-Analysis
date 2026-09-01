import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from features.lineup_efficiency import lineup_efficiency


def _p(week, team_id, player_id, points, lineup_slot, eligible_slots,
       injury_status="ACTIVE", injured=False, team_name="Team A"):
    return {
        "week": week,
        "fantasy_team_id": team_id,
        "fantasy_team": team_name,
        "player_id": player_id,
        "player_name": f"Player {player_id}",
        "points": points,
        "lineup_slot": lineup_slot,
        "eligible_slots": eligible_slots,
        "was_started": lineup_slot not in ("BE", "IR"),
        "injury_status": injury_status,
        "injured": injured,
    }


class LineupEfficiencyTest(unittest.TestCase):
    def test_perfect_lineup_is_full_efficiency(self):
        df = pd.DataFrame([
            _p(1, 1, "a", 20, "PG", ["PG"]),
            _p(1, 1, "b", 15, "UT", ["UT"]),
            _p(1, 1, "c", 5, "BE", ["PG", "UT"]),
        ])

        result = lineup_efficiency(df, season_year=2026)
        row = result.iloc[0]

        self.assertEqual(row["avg_weekly_efficiency"], 1.0)
        self.assertEqual(row["total_points_left_on_bench"], 0.0)
        self.assertEqual(row["management_misses"], 0)

    def test_eligible_bench_player_outscoring_starter_lowers_efficiency(self):
        df = pd.DataFrame([
            _p(1, 1, "a", 10, "PG", ["PG", "G", "UT"]),
            _p(1, 1, "b", 20, "UT", ["SF", "F", "UT"]),
            _p(1, 1, "c", 30, "BE", ["PG", "G", "UT"]),
        ])

        result = lineup_efficiency(df, season_year=2026)
        row = result.iloc[0]

        # optimal: C->PG (30) + B->UT (20) = 50; actual = 30
        self.assertAlmostEqual(row["total_points_left_on_bench"], 20.0)
        self.assertAlmostEqual(row["avg_weekly_efficiency"], round(30 / 50, 4))
        self.assertEqual(row["management_misses"], 1)

    def test_known_out_bench_player_is_excluded_from_optimal(self):
        df = pd.DataFrame([
            _p(1, 1, "a", 10, "PG", ["PG", "G", "UT"]),
            _p(1, 1, "b", 20, "UT", ["SF", "F", "UT"]),
            _p(1, 1, "c", 30, "BE", ["PG", "G", "UT"], injury_status="OUT"),
        ])

        result = lineup_efficiency(df, season_year=2026)
        row = result.iloc[0]

        self.assertEqual(row["avg_weekly_efficiency"], 1.0)
        self.assertEqual(row["total_points_left_on_bench"], 0.0)
        self.assertEqual(row["management_misses"], 0)

    def test_ineligible_bench_player_does_not_count(self):
        df = pd.DataFrame([
            _p(1, 1, "a", 10, "PG", ["PG"]),
            _p(1, 1, "b", 20, "C", ["C"]),
            _p(1, 1, "c", 40, "BE", ["SF"]),  # no SF slot in this lineup -> unusable
        ])

        result = lineup_efficiency(df, season_year=2026)
        row = result.iloc[0]

        self.assertEqual(row["management_misses"], 0)
        self.assertEqual(row["total_points_left_on_bench"], 0.0)

    def test_writes_both_csvs(self):
        df = pd.DataFrame([
            _p(1, 1, "a", 10, "PG", ["PG", "UT"]),
            _p(1, 1, "c", 30, "BE", ["PG", "UT"]),
        ])

        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = lineup_efficiency(df, season_year=2026, output_dir=output_dir)
            season = pd.read_csv(output_dir / "lineup_efficiency.csv")
            weekly = pd.read_csv(output_dir / "lineup_efficiency_weekly.csv")

        self.assertEqual(season.columns.tolist(), result.columns.tolist())
        self.assertEqual(len(weekly), 1)

    def test_empty_input_returns_empty(self):
        result = lineup_efficiency(pd.DataFrame())
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
