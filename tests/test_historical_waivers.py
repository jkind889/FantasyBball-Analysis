from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pandas as pd

from features.historical_waivers import (
    build_historical_snapshot, evaluate_planned_lineups, historical_cutoff,
    write_historical_summary,
)
from features.nba_schedule import build_nba_schedule
from features.waiver_advisor import advise_waivers, write_advice
from features.waiver_inputs import matchup_mapping
from test_waiver_inputs import raw_league, raw_player, pro_schedule


def inputs():
    raw = raw_league()
    raw["teams"].append({"id": 2})
    raw["teams"][0]["name"] = "Historical team"
    own, candidate, other, late = [raw_player(i, pg=20 + i * 5) for i in range(1, 5)]
    own["player"]["stats"][0]["appliedAverage"] = 9999  # End-season aggregate must be ignored.
    later = {"id": "05g10", "externalId": "g10", "seasonId": 2026, "scoringPeriodId": 10,
             "statSourceId": 0, "statSplitTypeId": 5, "appliedTotal": 1000, "proTeamId": 2,
             "stats": {"42": 1, "40": 30}}
    own["player"]["stats"].append(dict(later, appliedTotal=5))
    candidate["player"]["stats"].append(later)
    late["player"]["stats"] = [later]
    for card in (own, candidate):
        for s in card["player"]["stats"]:
            if 0 < s["scoringPeriodId"] < 10:
                s["proTeamId"] = 1
    candidate["player"]["proTeamId"] = 2  # Later metadata trade, also ignored for team inference.
    rosters = {1: [{"playerId": 1, "lineupSlotId": 0, "playerPoolEntry": own}],
               2: [{"playerId": 3, "lineupSlotId": 0, "playerPoolEntry": other}]}
    schedule = build_nba_schedule(pro_schedule(), season_year=2026, matchup_ids=matchup_mapping(raw, {}, 2))
    schedule["status"] = "FINAL"
    return raw, schedule, rosters, [own, candidate, other, late]


def build(data):
    return build_historical_snapshot(*data, year=2026, league_id=123, team_id=1,
                                     cutoff="2026-01-10T05:00:00Z", period=10, week=2)


class HistoricalTests(unittest.TestCase):
    def test_baseline_and_recent_form_exclude_future_and_final_totals(self):
        data = inputs()
        snapshot, actuals = build(data)
        self.assertEqual({p["player_id"] for p in snapshot["players"]}, {1, 2})
        own = next(p for p in snapshot["players"] if p["rostered"])
        candidate = next(p for p in snapshot["players"] if not p["rostered"])
        self.assertEqual(own["proj_pts_per_game"], 25)
        self.assertEqual(candidate["proj_pts_per_game"], 30)
        self.assertEqual(candidate["pro_team"], "ATL")
        self.assertEqual(own["baseline_games"], 6)
        self.assertTrue(all(pd.Timestamp(g["completed_at"]) < pd.Timestamp(snapshot["as_of"]) for g in snapshot["game_logs"]))
        self.assertTrue(any(a["points"] == 1000 for a in actuals))
        changed = deepcopy(data)
        for card in changed[3]:
            for stat in card["player"]["stats"]:
                if stat["scoringPeriodId"] >= 10 or stat["scoringPeriodId"] == 0:
                    stat["appliedTotal"] = -99999
                    stat["appliedAverage"] = -99999
        another, _ = build(changed)
        self.assertEqual(snapshot["players"], another["players"])
        self.assertEqual(snapshot["game_logs"], another["game_logs"])

    def test_historical_future_games_playable_but_results_not_live_recommendations(self):
        snapshot, _ = build(inputs())
        self.assertTrue(all(g["status"] == "SCHEDULED" for g in snapshot["games"] if g["scoring_period"] >= 10))
        result = advise_waivers(snapshot)
        self.assertGreater(result["recommendations"].net_gain.max(), 0)
        self.assertFalse(result["recommendations"].recommended.any())
        self.assertEqual(set(result["recommendations"].analysis_mode), {"historical_demo"})

    def test_unknown_shared_actuals_cancel_but_missing_changed_games_do_not(self):
        rows = [{"action_id": action, "player_id": pid, "game_id": gid, "reason": "recommended start"}
                for action, pid, gid in [("hold", 1, "shared"), ("hold", 2, "old"),
                                          ("move", 1, "shared"), ("move", 3, "new")]]
        result = {"calendar": pd.DataFrame(rows)}
        actuals = [{"player_id": 2, "game_id": "old", "points": 20},
                   {"player_id": 3, "game_id": "new", "points": 30}]
        evaluation = evaluate_planned_lineups(result, actuals).set_index("action_id")
        self.assertEqual(evaluation.loc["move", "actual_gain_vs_hold"], 10)
        self.assertTrue(pd.isna(evaluation.loc["move", "actual_planned_points"]))
        missing = evaluate_planned_lineups(result, actuals[:1]).set_index("action_id")
        self.assertTrue(pd.isna(missing.loc["move", "actual_gain_vs_hold"]))

    def test_deduplication_and_missing_rosters(self):
        data = inputs()
        data[3][0]["player"]["stats"].append(deepcopy(data[3][0]["player"]["stats"][1]))
        snapshot, _ = build(data)
        self.assertEqual(snapshot["players"][0]["baseline_games"], 6)
        data[2].pop(2)
        with self.assertRaisesRegex(ValueError, "missing for some"):
            build(data)

    def test_offline_historical_replay_retains_label_and_separate_outputs(self):
        snapshot, actuals = build(inputs())
        result = advise_waivers(snapshot)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            output = Path(directory) / "replay"
            write_advice(snapshot, result, source)
            (source / "historical_actuals.json").write_text(json.dumps(actuals))
            write_historical_summary(result, actuals, source)
            root = Path(__file__).resolve().parents[1]
            run = subprocess.run([sys.executable, str(root / "main.py"), "--mode", "waivers", "--snapshot",
                str(source / "waiver_snapshot.json"), "--output-dir", str(output)], cwd=directory,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Historical team", (output / "historical_demo.md").read_text())
            board = pd.read_csv(output / "waiver_recommendations.csv")
            self.assertFalse(board.recommended.any())
            self.assertFalse((Path(directory) / "reports" / "waiver_recommendations.csv").exists())

    def test_date_must_be_a_completed_local_day(self):
        self.assertEqual(historical_cutoff("2026-01-08").isoformat(), "2026-01-08T05:00:00+00:00")
        for date in ("2026-01-08T12:00:00", "2026-01-08T00:00:00Z", "2099-01-01"):
            with self.assertRaises(ValueError):
                historical_cutoff(date)


if __name__ == "__main__":
    unittest.main()
