import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from features.waiver_inputs import fetch_snapshot, matchup_mapping, normalize_game_logs, normalize_rules
from features.nba_schedule import build_nba_schedule


def raw_league():
    return {
        "scoringPeriodId": 10, "status": {"currentMatchupPeriod": 2, "finalScoringPeriod": 20},
        "settings": {
            "scoringSettings": {"scoringType": "H2H_POINTS"},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "12": 1},
                "lineupLocktimeType": "INDIVIDUAL_GAME", "rosterLocktimeType": "FIRSTGAME_SCORINGPERIOD",
                "moveLimit": -1, "positionLimits": {"0": 0, "1": -1, "5": 1}, "lineupSlotStatLimits": {}},
            "acquisitionSettings": {"acquisitionLimit": -1, "matchupAcquisitionLimit": 1.0,
                                    "matchupLimitPerScoringPeriod": True},
            # These values are WEEKS. They must not be used as scoring days.
            "scheduleSettings": {"matchupPeriods": {"1": [1], "2": [2], "3": [3]}},
        },
        "schedule": [{"matchupPeriodId": 1, "home": {"pointsByScoringPeriod": {str(i): 0 for i in range(1, 7)}}},
                     {"matchupPeriodId": 2, "home": {"pointsByScoringPeriod": {str(i): 0 for i in range(7, 14)}}},
                     {"matchupPeriodId": 3, "home": {"pointsByScoringPeriod": {str(i): 0 for i in range(14, 21)}}}],
        "teams": [{"id": 1, "roster": {"entries": []}, "transactionCounter": {"acquisitions": 8, "matchupAcquisitionTotals": {"2": 3}}}],
    }


def raw_player(pid, pg=30):
    stats = [{"id": "002026", "seasonId": 2026, "statSourceId": 0, "statSplitTypeId": 0,
              "scoringPeriodId": 0, "appliedAverage": pg, "appliedTotal": pg * 6, "stats": {"42": 6}}]
    for period in range(1, 7):
        stats.append({"id": f"05g{period}", "externalId": f"g{period}", "seasonId": 2026,
                      "scoringPeriodId": period, "statSourceId": 0, "statSplitTypeId": 5,
                      "appliedTotal": pg, "stats": {"42": 1, "40": 30}})
    return {"id": pid, "status": "FREEAGENT", "player": {"id": pid, "fullName": f"Player {pid}",
            "defaultPositionId": 1, "eligibleSlots": [0, 5, 11, 12], "proTeamId": 1,
            "injuryStatus": "ACTIVE", "droppable": True, "stats": stats}}


def pro_schedule():
    return {1: {str(p): [{"id": f"g{p}", "homeProTeamId": 1, "awayProTeamId": 2,
                          "date": int(pd.Timestamp(f"2026-01-{p:02}T23:00:00Z").timestamp() * 1000),
                          "statsOfficial": p < 10, "startTimeTBD": False, "validForLocking": True}]
                for p in range(1, 21)}}


class WaiverInputTests(unittest.TestCase):
    def test_actual_rules_daily_limit_and_position_sentinel(self):
        raw = raw_league()
        rules = normalize_rules(raw, raw["teams"][0], {}, daily_used=1)
        self.assertEqual(rules["starting_slots"], ["PG"])
        self.assertEqual(rules["roster_capacity"], 2)
        self.assertEqual(rules["position_limits"], {"C": 1})
        self.assertEqual(rules["daily_acquisition_limit"], 1)
        self.assertIsNone(rules["acquisitions_remaining"])
        self.assertEqual(rules["drop_lock"], "FIRSTGAME_SCORINGPERIOD")
        with self.assertRaisesRegex(ValueError, "Daily acquisition usage"):
            normalize_rules(raw, raw["teams"][0], {})

    def test_matchup_limit_not_lifetime_usage(self):
        raw = raw_league()
        raw["settings"]["acquisitionSettings"].update(matchupLimitPerScoringPeriod=False, matchupAcquisitionLimit=5)
        rules = normalize_rules(raw, raw["teams"][0], {})
        self.assertEqual(rules["acquisitions_remaining"], 2)
        self.assertIsNone(rules["daily_acquisition_limit"])

    def test_mapping_uses_daily_keys_and_official_schedule_status(self):
        mapping = matchup_mapping(raw_league(), {}, 2)
        self.assertEqual(mapping[2], list(range(7, 14)))
        games = build_nba_schedule(pro_schedule(), season_year=2026, matchup_ids=mapping)
        self.assertEqual(games.loc[games.scoring_period == 9, "fantasy_week"].iloc[0], 2)
        self.assertEqual(games.loc[games.scoring_period == 9, "status"].iloc[0], "FINAL")
        self.assertEqual(games.loc[games.scoring_period == 10, "status"].iloc[0], "SCHEDULED")

    def test_game_card_split_five_zero_and_current_unfinished(self):
        schedule = build_nba_schedule(pro_schedule(), season_year=2026)
        card = raw_player(1)
        card["player"]["stats"][1]["appliedTotal"] = 0
        # Legacy split duplicates the same actual game: prefer the per-game split.
        card["player"]["stats"].append(dict(card["player"]["stats"][1], statSplitTypeId=1))
        logs, coverage = normalize_game_logs([card], schedule, 2026, 10, "2026-01-10T20:00:00Z", "America/New_York")
        self.assertEqual(len(logs), 6)
        self.assertEqual(logs[0]["points"], 0)
        self.assertEqual(logs[0]["game_id"], "g1")
        self.assertTrue(coverage[1])
        card["player"]["stats"] = [dict(card["player"]["stats"][1], scoringPeriodId=10, externalId="g10")]
        logs, _ = normalize_game_logs([card], schedule, 2026, 10, "2026-01-10T23:30:00Z", "America/New_York")
        self.assertEqual(logs, [])

    def test_batched_integration_retains_availability_and_uses_real_player_parser(self):
        raw = raw_league()
        own, free = raw_player(1), raw_player(2, pg=34)
        own["status"] = "ONTEAM"
        raw["teams"][0]["roster"]["entries"] = [{"playerId": 1, "lineupSlotId": 0, "playerPoolEntry": own}]
        free.update(status="WAIVERS", waiverProcessDate=int(pd.Timestamp("2026-01-11T08:00:00Z").timestamp() * 1000))
        request = Mock()
        request.get_league.return_value = raw
        request.league_get.return_value = {"players": [free]}
        request.get_player_card.return_value = {"players": [own, free]}
        league = SimpleNamespace(espn_request=request, league_id=123, year=2026,
                                 finalScoringPeriod=20, pro_schedule=pro_schedule(), recent_activity=Mock(return_value=[]))
        result = fetch_snapshot(league, None, 1)
        self.assertEqual(len(result["players"]), 2)
        self.assertEqual(result["players"][1]["availability"], "WAIVERS")
        self.assertFalse(result["players"][0]["undroppable"])
        self.assertTrue(result["players"][1]["recent_logs_complete"])
        self.assertEqual(len(result["game_logs"]), 12)
        self.assertTrue(result["schedule_complete"])
        request.get_player_card.assert_called_once_with([1, 2], 20)

    def test_closed_season_stops_before_available_pool(self):
        raw = raw_league()
        raw["scoringPeriodId"] = 21
        request = Mock()
        request.get_league.return_value = raw
        league = SimpleNamespace(espn_request=request)
        with self.assertRaisesRegex(ValueError, "season has ended"):
            fetch_snapshot(league, None, 1)
        request.league_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
