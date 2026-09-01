import unittest

import pandas as pd

from features.player_value import compute_player_value, parse_positions


def _proj(pid, name, pos, pg, games=40):
    return {"player_id": pid, "player_name": name, "position": pos,
            "proj_pts_per_game": pg, "games_remaining": games,
            "ros_total": pg * games}


class ParsePositionsTest(unittest.TestCase):
    def test_splits_slash_and_filters_unknown(self):
        self.assertEqual(parse_positions("PG/SG"), ["PG", "SG"])
        self.assertEqual(parse_positions("G/F"), [])
        self.assertEqual(parse_positions(None), [])


class ComputePlayerValueTest(unittest.TestCase):
    def test_empty_input(self):
        result = compute_player_value(pd.DataFrame())
        self.assertEqual(len(result), 0)
        self.assertIn("vorp_ros", result.columns)

    def test_ranks_by_vorp_and_sets_replacement_line(self):
        # 12 centers, descending rate; small league so the replacement line
        # sits high in the pool.
        rows = [_proj(i, f"C{i}", "C", 40 - i) for i in range(12)]
        result = compute_player_value(
            pd.DataFrame(rows), roster_slots={"C": 1}, league_size=4
        )
        self.assertEqual(result.iloc[0]["player_name"], "C0")
        self.assertEqual(list(result["rank"]), list(range(1, 13)))
        # league_size 4, one C slot -> replacement is the 4th-best center (37).
        self.assertAlmostEqual(result.iloc[0]["replacement_pts_per_game"], 37.0)
        self.assertAlmostEqual(result.iloc[0]["vorp_per_game"], 3.0)
        self.assertAlmostEqual(result.iloc[0]["vorp_ros"], 3.0 * 40)

    def test_multi_position_player_uses_best_line(self):
        rows = [_proj(i, f"P{i}", "PG", 30 - i) for i in range(6)]
        rows += [_proj(100 + i, f"C{i}", "C", 20 - i) for i in range(6)]
        rows.append(_proj(999, "Flex", "PG/C", 15))
        result = compute_player_value(
            pd.DataFrame(rows), roster_slots={"PG": 1, "C": 1}, league_size=3
        )
        flex = result[result["player_name"] == "Flex"].iloc[0]
        pg_line = result[result["player_name"] == "P2"].iloc[0][
            "replacement_pts_per_game"
        ]
        # Flex is eligible at PG and C; it should be measured against the more
        # favourable (higher) of the two replacement lines -> the PG line.
        self.assertAlmostEqual(flex["replacement_pts_per_game"], pg_line)


if __name__ == "__main__":
    unittest.main()
