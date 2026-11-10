"""News scan with synthetic league data and a fake feed; no network or live league."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from features import news_scan
from features.news_scan import fetch_flagged_news, flagged_players, format_summary, run_scan

RETRIEVED = "2026-11-05T00:00:00+00:00"


def player(player_id, status="ACTIVE", name=None):
    entry = {"id": player_id, "fullName": name or f"Player {player_id}"}
    if status is not None:
        entry["injuryStatus"] = status
    return {"player": entry}


def league_data(rostered=(), pool=()):
    raw = {"scoringPeriodId": 20, "teams": [
        {"id": 1, "roster": {"entries": [{"playerId": p["player"]["id"], "playerPoolEntry": p} for p in rostered]}}]}
    league = MagicMock()
    league.espn_request.get_league.return_value = raw
    league.espn_request.league_get.return_value = {"players": list(pool)}
    return raw, league


def blurb(player_id, item_id=None):
    return {"id": item_id or player_id * 10, "type": "Rotowire", "playerId": player_id, "story": "Synthetic.",
            "headline": f"Player (ankle) is out, item {player_id}.", "published": "2026-11-04T00:00:00Z"}


def feed_fetcher(failing=()):
    def fetch(ids):
        fetch.calls.extend(ids)
        return {pid: ({"status": "fetch_failed", "retrieved_at": RETRIEVED, "feed": [], "error_type": "ValueError"}
                      if pid in failing else {"status": "ok", "retrieved_at": RETRIEVED, "feed": [blurb(pid)]})
                for pid in ids}
    fetch.calls = []
    return fetch


class FlaggedPlayersTests(unittest.TestCase):
    def test_flags_any_non_active_status_and_counts_unknown_separately(self):
        raw, _ = league_data([player(1, "OUT"), player(2)],
                             [player(3, "DAY_TO_DAY"), player(4, "NEW_ESPN_STATUS"), player(5, None), player(6, "")])
        flagged, summary = flagged_players(raw, [player(3, "DAY_TO_DAY"), player(4, "NEW_ESPN_STATUS"),
                                                 player(5, None), player(6, "")])
        self.assertEqual(list(flagged), [1, 3, 4])
        self.assertEqual(flagged[1], "Player 1")
        self.assertEqual(summary, {"players_checked": 6, "unknown_status": 2,
                                   "statuses": {"OUT": 1, "ACTIVE": 1, "DAY_TO_DAY": 1, "NEW_ESPN_STATUS": 1}})

    def test_a_player_in_both_lists_is_counted_once_and_rosters_come_first(self):
        raw, _ = league_data([player(2, "OUT")])
        flagged, summary = flagged_players(raw, [player(9, "OUT"), player(2, "OUT")])
        self.assertEqual(list(flagged), [2, 9])
        self.assertEqual(summary["players_checked"], 2)

    def test_malformed_entries_are_skipped(self):
        raw = {"teams": [{"id": 1}, {"roster": {"entries": [{"playerId": 1}]}}]}
        flagged, summary = flagged_players(raw, [{}, {"player": None}, {"player": {"id": "7", "injuryStatus": "OUT"}}])
        self.assertEqual((flagged, summary["players_checked"]), ({}, 0))


class FetchTests(unittest.TestCase):
    def test_pauses_between_requests_only(self):
        sleep = MagicMock()
        results, stopped = fetch_flagged_news([1, 2, 3], fetcher=feed_fetcher(), sleep=sleep)
        self.assertEqual((sorted(results), stopped), ([1, 2, 3], None))
        self.assertEqual(sleep.call_count, 2)

    def test_stops_after_consecutive_failures(self):
        fetch = feed_fetcher(failing=range(100))
        results, stopped = fetch_flagged_news(list(range(20)), fetcher=fetch, sleep=MagicMock())
        self.assertEqual(stopped, "repeated_failures")
        self.assertEqual(len(fetch.calls), news_scan.MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(len(results), news_scan.MAX_CONSECUTIVE_FAILURES)

    def test_a_success_resets_the_failure_count(self):
        ids = list(range(1, 21))
        failing = [i for i in ids if i % 4]  # Never more than three failures in a row.
        results, stopped = fetch_flagged_news(ids, fetcher=feed_fetcher(failing), sleep=MagicMock())
        self.assertIsNone(stopped)
        self.assertEqual(len(results), 20)

    def test_stops_at_the_time_limit(self):
        times = iter([0, 1, news_scan.MAX_SCAN_SECONDS + 1])
        results, stopped = fetch_flagged_news([1, 2, 3], fetcher=feed_fetcher(), sleep=MagicMock(),
                                              clock=lambda: next(times))
        self.assertEqual((sorted(results), stopped), ([1], "time_limit"))


class RunScanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "blurbs.jsonl"

    def scan(self, league, fetch):
        return run_scan(league, fetcher=fetch, history_path=self.path, sleep=MagicMock())

    def test_only_flagged_players_are_fetched_and_saved(self):
        _, league = league_data([player(1, "OUT"), player(2)], [player(3, "DAY_TO_DAY"), player(4)])
        fetch = feed_fetcher()
        summary = self.scan(league, fetch)
        self.assertEqual(fetch.calls, [1, 3])
        self.assertEqual(summary["status"], "ok")
        self.assertEqual((summary["flagged"], summary["requested"], summary["failed"]), (2, 2, 0))
        self.assertEqual(summary["history"]["added"], 2)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([(r["player_id"], r["player_name"]) for r in rows], [(1, "Player 1"), (3, "Player 3")])

    def test_rerun_adds_nothing_new(self):
        _, league = league_data([player(1, "OUT")])
        self.scan(league, feed_fetcher())
        self.assertEqual(self.scan(league, feed_fetcher())["history"], {
            "status": "ok", "added": 0, "already_saved": 1, "unreadable_lines": 0})

    def test_failures_are_partial_and_successes_are_still_saved(self):
        _, league = league_data([player(1, "OUT"), player(2, "OUT")])
        summary = self.scan(league, feed_fetcher(failing=[1]))
        self.assertEqual((summary["status"], summary["failed"], summary["history"]["added"]), ("partial", 1, 1))
        self.assertIn("1 failed", format_summary(summary))

    def test_early_stop_reports_players_not_attempted(self):
        pool = [player(i, "OUT") for i in range(1, 11)]
        _, league = league_data(pool=pool)
        summary = self.scan(league, feed_fetcher(failing=range(1, 11)))
        self.assertEqual((summary["status"], summary["stopped"]), ("partial", "repeated_failures"))
        self.assertEqual(summary["not_attempted"], 10 - news_scan.MAX_CONSECUTIVE_FAILURES)
        self.assertIn("Stopped early: repeated_failures", format_summary(summary))
        self.assertFalse(self.path.exists())

    def test_player_limit_keeps_the_first_players_and_is_reported(self):
        pool = [player(i, "OUT") for i in range(1, news_scan.MAX_SCAN_PLAYERS + 3)]
        _, league = league_data(pool=pool)
        fetch = feed_fetcher()
        summary = self.scan(league, fetch)
        self.assertEqual(fetch.calls, list(range(1, news_scan.MAX_SCAN_PLAYERS + 1)))
        self.assertEqual((summary["over_limit"], summary["status"]), (2, "partial"))

    def test_incomplete_pool_is_partial(self):
        # A full page of players with nothing new stops paging without completing the pool.
        page = [player(i) for i in range(250)]
        raw, league = league_data()
        league.espn_request.league_get.return_value = {"players": page}
        summary = self.scan(league, feed_fetcher())
        self.assertEqual((summary["pool_complete"], summary["status"]), (False, "partial"))
        self.assertIn("pool was incomplete", format_summary(summary))

    def test_nothing_flagged_writes_nothing(self):
        _, league = league_data([player(1)])
        summary = self.scan(league, feed_fetcher())
        self.assertEqual((summary["status"], summary["flagged"]), ("ok", 0))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
