import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from features.waiver_inputs import (fetch_snapshot, infer_matchup_periods, injury_details, matchup_mapping,
                                    normalize_game_logs, normalize_rules, period_dates)
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


def season(first_day, off_days, final=174):
    """Calendar shaped like a real season: period ID -> local game date."""
    first = pd.Timestamp(first_day).date()
    return {p: {pd.Timestamp(first + pd.Timedelta(days=p - 1)).date()}
            for p in range(1, final + 1) if p not in off_days}


def matchups_with_all_star():
    """ESPN's observed layout: {matchup: (first, last, week IDs)} for 2025 and 2026."""
    layout = {1: (1, 6, [1])}
    for week in range(2, 17):
        layout[week] = (7 * (week - 1), 7 * week - 1, [week])
    layout[17] = (112, 125, [17])
    for week in range(18, 21):
        layout[week] = (126 + 7 * (week - 18), 132 + 7 * (week - 18), [week])
    layout[21] = (147, 160, [21, 22])
    layout[22] = (161, 174, [23, 24])
    return layout


def midweek_league():
    """Week two has started through period 10 and ESPN lists no later days."""
    raw = raw_league()
    raw["status"]["finalScoringPeriod"] = 40
    raw["schedule"] = [{"matchupPeriodId": 1, "home": {"pointsByScoringPeriod": {str(i): 0 for i in range(1, 7)}}},
                       {"matchupPeriodId": 2, "home": {"pointsByScoringPeriod": {str(i): 0 for i in range(7, 11)}}},
                       {"matchupPeriodId": 3, "home": {}}]
    return raw


def monday_pro_schedule(tipoff="23:00:00"):
    """Period 1 is a Tuesday and period 7 a Monday, like the real season opener."""
    first = pd.Timestamp("2025-12-30")
    return {1: {str(p): [{"id": f"g{p}", "homeProTeamId": 1, "awayProTeamId": 2,
                          "date": int(pd.Timestamp(f"{(first + pd.Timedelta(days=p - 1)).date()}T{tipoff}Z").timestamp() * 1000),
                          "statsOfficial": p < 10, "startTimeTBD": False, "validForLocking": True}]
                for p in range(1, 41)}}


def snapshot_for(raw, pro, config=None):
    request = Mock()
    request.get_league.return_value = raw
    request.league_get.return_value = {"players": []}
    request.get_player_card.return_value = {"players": []}
    league = SimpleNamespace(espn_request=request, league_id=123, year=2026, finalScoringPeriod=raw["status"]["finalScoringPeriod"],
                             pro_schedule=pro, recent_activity=Mock(return_value=[]))
    return fetch_snapshot(league, None, 1, config=config)


class MatchupBoundaryTests(unittest.TestCase):
    def test_ordinary_week_ends_on_sunday_from_any_elapsed_day(self):
        dates = season("2026-10-20", {15, 38})
        for elapsed in range(14, 21):
            self.assertEqual(infer_matchup_periods(3, list(range(14, elapsed + 1)), list(range(7, 14)), [3], dates, 174),
                             list(range(14, 21)))

    def test_season_opener_starts_midweek_and_ends_that_sunday(self):
        self.assertEqual(infer_matchup_periods(1, [1, 2], None, [1], season("2026-10-20", set()), 174), list(range(1, 7)))

    def test_playoff_matchup_with_two_week_ids_runs_fourteen_days(self):
        self.assertEqual(infer_matchup_periods(21, [147], list(range(140, 147)), [21, 22], season("2025-10-21", set()), 174),
                         list(range(147, 161)))

    def test_never_wrong_on_observed_seasons_from_any_day(self):
        # Period layout and off days match the 2025 and 2026 seasons.
        for first_day, off_days in (("2025-10-21", {38, 57, 65, 116, 117, 118, 119, 120, 121, 173}),
                                    ("2024-10-22", {15, 38, 57, 58, 64, 116, 117, 118, 119, 120, 173})):
            dates, layout = season(first_day, off_days), matchups_with_all_star()
            for week, (first, last, week_ids) in layout.items():
                previous = list(range(layout[week - 1][0], layout[week - 1][1] + 1)) if week > 1 else None
                for elapsed in range(first, last + 1):
                    got = infer_matchup_periods(week, list(range(first, elapsed + 1)), previous, week_ids, dates, 174)
                    with self.subTest(season=first_day, week=week, elapsed=elapsed):
                        self.assertIn(got, (None, list(range(first, last + 1))))

    def test_all_star_matchup_and_the_week_before_stop_the_inference(self):
        dates = season("2025-10-21", {116, 117, 118, 119, 120, 121})
        self.assertIsNone(infer_matchup_periods(16, [105], list(range(98, 105)), [16], dates, 174))
        self.assertIsNone(infer_matchup_periods(17, [112], list(range(105, 112)), [17], dates, 174))
        self.assertEqual(infer_matchup_periods(15, [98], list(range(91, 98)), [15], dates, 174), list(range(98, 105)))
        self.assertEqual(infer_matchup_periods(18, [126], list(range(112, 126)), [18], dates, 174), list(range(126, 133)))

    def test_gap_that_starts_after_this_week_still_stops_it(self):
        # The current season's break begins late in the week after a possible 14-day start.
        dates = season("2026-10-20", {123, 124, 125, 126, 127, 128})
        self.assertIsNone(infer_matchup_periods(17, [112], list(range(105, 112)), [17], dates, 174))

    def test_short_off_days_do_not_stop_it(self):
        dates = season("2026-10-20", {15, 16, 17})
        self.assertEqual(infer_matchup_periods(3, [14], list(range(7, 14)), [3], dates, 174), list(range(14, 21)))

    def test_unverifiable_inputs_return_none(self):
        dates = season("2026-10-20", set())
        previous = list(range(7, 14))
        cases = {
            "no started days": (3, [], previous, [3], dates, 174),
            "no previous matchup": (3, [14], None, [3], dates, 174),
            "not contiguous": (3, [15], previous, [3], dates, 174),
            "missing week IDs": (3, [14], previous, None, dates, 174),
            "empty week IDs": (3, [14], previous, [], dates, 174),
            "malformed week IDs": (3, [14], previous, "3", dates, 174),
            "no schedule": (3, [14], previous, [3], {}, 174),
            "not a Monday": (3, [15], list(range(8, 15)), [3], dates, 174),
            "past the season": (3, [14], previous, [3], dates, 18),
            "already past a Sunday": (3, list(range(14, 23)), previous, [3], dates, 174),
            "periods skip days": (3, [14], previous, [3], {**dates, 30: {pd.Timestamp("2027-02-01").date()}}, 174),
        }
        for name, args in cases.items():
            with self.subTest(name):
                self.assertIsNone(infer_matchup_periods(*args))

    def test_late_tipoff_keeps_its_local_date(self):
        # 02:30 UTC is the previous evening in New York.
        games = build_nba_schedule(monday_pro_schedule("02:30:00"), season_year=2026)
        dates = period_dates(games, "America/New_York")
        self.assertEqual(dates[7], {pd.Timestamp("2026-01-04").date()})

    def test_snapshot_marks_midweek_boundary_as_inferred(self):
        result = snapshot_for(midweek_league(), monday_pro_schedule())
        self.assertTrue(result["schedule_complete"])
        weeks = {g["scoring_period"]: g["fantasy_week"] for g in result["games"]}
        for period in range(7, 14):
            self.assertEqual(weeks[period], 2)
        self.assertEqual(weeks[14], -1)
        self.assertTrue(any("end inferred as scoring period 13" in text for text in result["limitations"]))

    def test_snapshot_stays_partial_when_inference_fails(self):
        # Period 7 is not a Monday in this calendar.
        result = snapshot_for(midweek_league(), pro_schedule())
        self.assertFalse(result["schedule_complete"])
        self.assertFalse(any("inferred" in text for text in result["limitations"]))

    def test_explicit_periods_win_and_are_not_labeled_inferred(self):
        raw = midweek_league()
        raw["teams"][0]["transactionCounter"]["matchupAcquisitionTotals"]["2"] = 0
        result = snapshot_for(raw, monday_pro_schedule(), {"matchup_scoring_periods": list(range(7, 12))})
        self.assertTrue(result["schedule_complete"])
        self.assertEqual({g["scoring_period"] for g in result["games"] if g["fantasy_week"] == 2}, set(range(7, 12)))
        self.assertFalse(any("inferred" in text for text in result["limitations"]))

    def test_finished_matchup_is_not_labeled_inferred(self):
        raw = raw_league()
        raw["status"]["finalScoringPeriod"] = 40
        result = snapshot_for(raw, monday_pro_schedule())
        self.assertTrue(result["schedule_complete"])
        self.assertFalse(any("inferred" in text for text in result["limitations"]))


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
        self.assertEqual({k: result["players"][0][k] for k in ("injury_return_date", "injury_type", "out_for_season")},
                         {"injury_return_date": None, "injury_type": None, "out_for_season": None})

    def test_snapshot_keeps_espns_injury_details_from_the_card(self):
        raw = raw_league()
        own = raw_player(1)
        own["status"] = "ONTEAM"
        raw["teams"][0]["roster"]["entries"] = [{"playerId": 1, "lineupSlotId": 0, "playerPoolEntry": own}]
        card = copy.deepcopy(own)
        card["player"].update(injuryStatus="OUT", injuryDetails={"expectedReturnDate": [2026, 1, 16],
                                                                  "outForSeason": False, "type": "Rest"})
        request = Mock()
        request.get_league.return_value = raw
        request.league_get.return_value = {"players": []}
        request.get_player_card.return_value = {"players": [card]}
        league = SimpleNamespace(espn_request=request, league_id=123, year=2026,
                                 finalScoringPeriod=20, pro_schedule=pro_schedule(), recent_activity=Mock(return_value=[]))
        player = fetch_snapshot(league, None, 1)["players"][0]
        self.assertEqual((player["injury_status"], player["injury_type"], player["injury_return_date"], player["out_for_season"]),
                         ("OUT", "Rest", "2026-01-16", False))

    def test_injury_details_keep_only_well_formed_fields(self):
        self.assertEqual(injury_details({"injuryDetails": {"expectedReturnDate": [2026, 11, 11], "outForSeason": False,
                                                           "type": "Rest"}}),
                         {"injury_return_date": "2026-11-11", "injury_type": "Rest", "out_for_season": False})
        empty = {"injury_return_date": None, "injury_type": None, "out_for_season": None}
        for raw in ({}, None, {"injuryDetails": None}, {"injuryDetails": "Rest"},
                    {"injuryDetails": {"expectedReturnDate": [2026, 2, 30], "outForSeason": "no", "type": " "}},
                    {"injuryDetails": {"expectedReturnDate": [2026, 11], "outForSeason": 0, "type": 5}},
                    {"injuryDetails": {"expectedReturnDate": "2026-11-11", "outForSeason": None}},
                    {"injuryDetails": {"expectedReturnDate": [2026, 11, True]}}):
            with self.subTest(raw=raw):
                self.assertEqual(injury_details(raw), empty)

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
