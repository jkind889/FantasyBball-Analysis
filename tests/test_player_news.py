"""Offline player news tests; every feed item here is synthetic."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import pandas as pd
import requests

from features.streaming_context import normalize_context
from features.player_news import (
    MAX_ITEMS_PER_PLAYER, MAX_RESPONSE_BYTES, MAX_SUMMARY_CHARS, NEWS_ENDPOINT,
    fetch_player_news, filter_news_items, is_injury_headline, save_news_history, select_news_entries,
)

AS_OF = "2026-11-03T18:00:00+00:00"


def blurb(item_id, headline="Smith (ankle) is questionable for Friday's game.", published="2026-11-03T12:00:00Z", **extra):
    return {"id": item_id, "type": "Rotowire", "playerId": 1, "headline": headline,
            "story": "Synthetic detail.", "published": published, "lastModified": published, **extra}


def fetched(*items, status="ok"):
    return {"status": status, "retrieved_at": "2026-11-03T18:00:05+00:00", "feed": list(items)}


class HeadlineTests(unittest.TestCase):
    def test_marker_must_sit_on_this_players_name(self):
        cases = [
            ("Smith (ankle) is questionable.", "Jalen Smith", True),
            ("The Wizards said Wednesday that Smith (elbow) will be re-evaluated.", "Jalen Smith", True),
            ("The Hornets announced that Smith underwent surgery.", "Jalen Smith", True),
            ("Smith is expected to be sidelined for four weeks.", "Jalen Smith", True),
            ("Smith exited Friday's win and did not return.", "Jalen Smith", True),
            ("Smith (rest) is available for Friday's game.", "Jalen Smith", True),
            ("Porter (knee) is fine.", "Kevin Porter Jr.", True),
            ("Butler (hip) is probable.", "Jimmy Butler III", True),
            ("Jokić (illness) played.", "Nikola Jokic", True),
            ("Jokic (illness) played.", "Nikola Jokić", True),
            ("Gilgeous-Alexander (wrist) is out for Friday's game.", "Shai Gilgeous-Alexander", True),
            ("Smith left Friday's game with a head injury.", "Jalen Smith", True),
            ("Smith will be rested for Friday's game.", "Jalen Smith", True),
            ("Smith sat out Friday's win.", "Jalen Smith", True),
            ("Smith appeared to suffer a knee injury.", "Jalen Smith", True),
            ("Smith is a game-time decision for Friday.", "Jalen Smith", True),
            ('Coach said Smith is "day-to-day" with a sprain.', "Jalen Smith", True),
            ("Smith suffered a torn ACL.", "Jalen Smith", True),
            ("Smith has a sprained wrist.", "Jalen Smith", True),
            ("Smith on Monday underwent surgery.", "Jalen Smith", True),
            ("Smith, who has been ruled out for Friday, is progressing.", "Jalen Smith", True),
            ("Green (toe) is out.", "A.J. Green.", True),
            ("Porter (knee) is fine.", "Kevin Porter, Jr.", True),
            ("O\u2019Neale (back) is out.", "Royce O'Neale", True),
            ("Jones' knee surgery went well.", "Tre Jones", True),
            ("Young guards step up as Young (ankle) sits.", "Trae Young", True),
            # A teammate's injury or a box score is not this player's injury.
            ("Smith will start Friday with Jones sidelined.", "Jalen Smith", False),
            ("Smith expected to start with Jones ruled out.", "Jalen Smith", False),
            ("Smith on a tear lately.", "Jalen Smith", False),
            ("Smith scored with his left hand in the win.", "Jalen Smith", False),
            ("Jones will start Friday with Smith (ankle) out.", "Tre Jones", False),
            ("Smith recorded 20 points (8-11 FG) in a win.", "Jalen Smith", False),
            ("Porterfield (ankle) is out for Friday's game.", "Kevin Porter Jr.", False),
            ("Smith said he wants to stay with the team.", "Jalen Smith", False),
            ("", "Jalen Smith", False),
            ("Smith (ankle) is out.", None, False),
            ("Smith (ankle) is out.", float("nan"), False),
        ]
        for headline, name, expected in cases:
            with self.subTest(headline=headline, name=name):
                self.assertEqual(is_injury_headline(headline, name), expected)


class FilterTests(unittest.TestCase):
    def test_keeps_only_this_players_blurbs_and_counts_every_drop(self):
        feed = [
            blurb(1),
            {"id": 2, "type": "Media", "video": [{}], "headline": "Highlights", "published": "2026-11-03T12:00:00Z"},
            {"id": 3, "type": "Story", "video": [{}], "headline": "Video story", "published": "2026-11-03T12:00:00Z"},
            {"id": 4, "type": "Story", "headline": "Draft board", "story": "Long", "published": "2026-11-03T12:00:00Z"},
            blurb(5, playerId=99),
            {k: v for k, v in blurb(9).items() if k != "playerId"},
            "not a dict",
            blurb(6, published="2026-11-03T12:00:00"),  # No timezone.
            blurb(7, published=None),
            blurb(8, headline="  "),
            blurb(None),
            blurb("x" * 41),
            blurb(1),
        ]
        kept, counts = filter_news_items(feed, 1, "Jalen Smith")
        self.assertEqual([i["item_id"] for i in kept], ["1"])
        self.assertTrue(kept[0]["is_injury"])
        self.assertEqual(counts, {"clips": 2, "other_types": 1, "other_player": 2, "malformed": 6, "duplicates": 1})

    def test_non_list_feed_is_empty(self):
        self.assertEqual(filter_news_items(None, 1, "Jalen Smith")[0], [])


class SelectionTests(unittest.TestCase):
    def select(self, results, **kwargs):
        names = {pid: "Jalen Smith" for pid in results}
        return select_news_entries(results, names, as_of=AS_OF, **kwargs)

    def test_window_is_inclusive_of_24_hours_and_excludes_older_and_future(self):
        results = {1: fetched(blurb(1, published="2026-11-02T18:00:00Z"), blurb(2, published="2026-11-02T17:59:59Z"),
                              blurb(3, published="2026-11-03T18:00:01Z"))}
        entries, report = self.select(results)
        self.assertEqual([e["source_item_id"] for e in entries], ["1"])
        self.assertEqual((report[0]["older_injury_blurbs"], report[0]["future_injury_blurbs"]), (1, 1))

    def test_non_injury_blurbs_are_never_selected_but_are_counted(self):
        results = {1: fetched(blurb(1, headline="Smith recorded 20 points (8-11 FG) in a win."))}
        entries, report = self.select(results)
        self.assertEqual(entries, [])
        self.assertEqual((report[0]["blurbs"], report[0]["injury_blurbs"]), (1, 0))

    def test_newest_items_win_the_per_player_limit(self):
        results = {1: fetched(*[blurb(i, published=f"2026-11-03T1{i}:00:00Z") for i in range(1, 5)])}
        entries, report = self.select(results)
        self.assertEqual(len(entries), MAX_ITEMS_PER_PLAYER)
        self.assertEqual([e["source_item_id"] for e in entries], ["4", "3"])
        self.assertEqual(report[0]["trimmed"], 2)

    def test_byte_budget_serves_priority_order_and_reports_trimming(self):
        results = {pid: fetched(blurb(pid, playerId=pid)) for pid in (7, 8, 9)}
        size = len(json.dumps(self.select({7: results[7]})[0][0]))
        entries, report = self.select(results, max_bytes=size * 2)
        self.assertEqual([e["player_id"] for e in entries], [7, 8])
        self.assertEqual([r["trimmed"] for r in report], [0, 0, 1])
        self.assertEqual(self.select(results, max_entries=1)[0][0]["player_id"], 7)

    def test_older_smaller_blurb_never_replaces_a_newer_trimmed_one(self):
        newer = blurb(1, published="2026-11-03T12:00:00Z", story="word " * 40)
        older = blurb(2, published="2026-11-03T11:00:00Z", story="")
        budget = len(json.dumps(self.select({1: fetched(older)})[0][0]))
        entries, report = self.select({1: fetched(newer, older)}, max_bytes=budget)
        self.assertEqual(entries, [])
        self.assertEqual(report[0]["trimmed"], 2)

    def test_unusable_names_are_flagged_and_do_not_lose_other_players(self):
        results = {1: fetched(blurb(1)), 2: fetched(blurb(2, playerId=2)), 3: fetched(blurb(3, playerId=3))}
        entries, report = select_news_entries(results, {1: None, 2: float("nan"), 3: "Jalen Smith"}, as_of=AS_OF)
        self.assertEqual([e["player_id"] for e in entries], [3])
        self.assertEqual([r["name_missing"] for r in report], [True, True, False])

    def test_entries_pass_the_real_context_validator_and_stay_fresh(self):
        snapshot = {"league_id": 1, "season_year": 2027, "week": 3, "as_of": AS_OF, "games": [],
                    "players": [{"player_id": 1, "player_name": "Jalen Smith"}]}
        entries = self.select({1: fetched(blurb(1, story="word " * 200), blurb(2, published="2026-11-02T18:00:00Z"))})[0]
        context = normalize_context(snapshot, {"schema_version": 1, "league_id": 1, "season_year": 2027, "week": 3, "entries": entries})
        self.assertEqual([e["status"] for e in context["entries"]], ["fresh", "fresh"])

    def test_failed_fetch_stays_visible_and_produces_no_entries(self):
        results = {1: {"status": "fetch_failed", "retrieved_at": AS_OF, "feed": [], "error_type": "Timeout"}}
        entries, report = self.select(results)
        self.assertEqual(entries, [])
        self.assertEqual((report[0]["status"], report[0]["error_type"]), ("fetch_failed", "Timeout"))

    def test_entry_keeps_separate_publish_and_retrieval_times_and_bounds_summary(self):
        item = blurb(1, story="word " * 200)
        entry = self.select({1: fetched(item)})[0][0]
        self.assertEqual(entry["observed_at"], item["published"])
        self.assertEqual(entry["retrieved_at"], "2026-11-03T18:00:05+00:00")
        self.assertLessEqual(len(entry["summary"]), MAX_SUMMARY_CHARS)
        self.assertEqual((entry["id"], entry["kind"], entry["player_id"]), ("espn-1-1", "injury", 1))

    def test_custom_window_supports_other_decision_times(self):
        results = {1: fetched(blurb(1, published="2026-10-01T00:00:00Z"))}
        entries, _ = select_news_entries(results, {1: "Jalen Smith"}, as_of="2026-10-20T00:00:00Z",
                                         max_age=pd.Timedelta(days=30))
        self.assertEqual(len(entries), 1)


class HistoryTests(unittest.TestCase):
    NAMES = {1: "Jalen Smith"}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "blurbs.jsonl"

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_saves_old_and_non_injury_blurbs_but_not_clips(self):
        feed = fetched(blurb(1, published="2020-01-01T00:00:00Z"), blurb(2, headline="Smith scores 30 points."),
                       {"id": 3, "type": "Media", "video": [{}], "playerId": 1, "headline": "Highlights"})
        report = save_news_history({1: feed}, self.NAMES, self.path)
        self.assertEqual(report, {"status": "ok", "added": 2, "already_saved": 0, "unreadable_lines": 0})
        rows = self.rows()
        self.assertEqual([row["item_id"] for row in rows], ["1", "2"])
        self.assertEqual(rows[0]["player_name"], "Jalen Smith")
        self.assertEqual(rows[0]["story"], "Synthetic detail.")  # Full text, not the 240-character summary.
        self.assertEqual(rows[0]["published"], "2020-01-01T00:00:00Z")
        self.assertEqual(rows[0]["retrieved_at"], "2026-11-03T18:00:05+00:00")

    def test_rerun_keeps_the_first_retrieval_and_adds_only_new_items(self):
        save_news_history({1: fetched(blurb(1))}, self.NAMES, self.path)
        later = dict(fetched(blurb(1), blurb(2)), retrieved_at="2026-11-05T00:00:00+00:00")
        report = save_news_history({1: later}, self.NAMES, self.path)
        self.assertEqual((report["added"], report["already_saved"]), (1, 1))
        self.assertEqual([(r["item_id"], r["retrieved_at"]) for r in self.rows()],
                         [("1", "2026-11-03T18:00:05+00:00"), ("2", "2026-11-05T00:00:00+00:00")])

    def test_edited_blurb_keeps_both_observations(self):
        save_news_history({1: fetched(blurb(1))}, self.NAMES, self.path)
        save_news_history({1: fetched(blurb(1, headline="Smith (ankle) has been ruled out."))}, self.NAMES, self.path)
        self.assertEqual(len(self.rows()), 2)
        self.assertIn("questionable", self.rows()[0]["headline"])

    def test_same_item_for_two_players_stays_separate(self):
        results = {1: fetched(blurb(1)), 2: fetched(blurb(1, playerId=2))}
        self.assertEqual(save_news_history(results, self.NAMES, self.path)["added"], 2)

    def test_failed_fetch_and_nothing_new_leave_no_file(self):
        report = save_news_history({1: fetched(status="fetch_failed")}, self.NAMES, self.path)
        self.assertEqual(report["added"], 0)
        self.assertFalse(self.path.exists())

    def test_interrupted_and_bad_lines_are_kept_and_not_joined(self):
        save_news_history({1: fetched(blurb(1))}, self.NAMES, self.path)
        original = self.path.read_text()
        self.path.write_text(original + "5\n{\"player_id\": 1}\n{\"interrupted\": ")
        report = save_news_history({1: fetched(blurb(1), blurb(2))}, self.NAMES, self.path)
        self.assertEqual((report["added"], report["already_saved"], report["unreadable_lines"]), (1, 1, 3))
        lines = self.path.read_text().splitlines()
        self.assertEqual(lines[:4], [original.strip(), "5", "{\"player_id\": 1}", "{\"interrupted\": "])
        self.assertEqual(json.loads(lines[4])["item_id"], "2")


class FetchTests(unittest.TestCase):
    def response(self, chunks=None, status=200):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = chunks if chunks is not None else [
            json.dumps({"news": {"feed": [blurb(1)]}}).encode()]
        return response

    def test_request_is_bounded_and_sends_no_cookies(self):
        get = Mock(return_value=self.response())
        result = fetch_player_news([1], get=get)
        self.assertEqual(result[1]["status"], "ok")
        self.assertEqual(len(result[1]["feed"]), 1)
        self.assertIsNotNone(pd.Timestamp(result[1]["retrieved_at"]).tzinfo)
        args, kwargs = get.call_args
        self.assertEqual(args, (NEWS_ENDPOINT,))
        self.assertEqual(kwargs["params"], {"playerId": 1})
        self.assertEqual(kwargs["timeout"], (5, 15))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("cookies", kwargs)
        self.assertNotIn("headers", kwargs)

    def test_slow_responses_and_the_overall_deadline_are_bounded(self):
        ticks = iter([0, 0, 100])  # Start, request start, then a slow chunk.
        result = fetch_player_news([1], get=Mock(return_value=self.response()), clock=lambda: next(ticks))
        self.assertEqual(result[1]["status"], "fetch_failed")
        ticks = iter([0, 1000])
        get = Mock()
        result = fetch_player_news([1], get=get, clock=lambda: next(ticks))
        self.assertEqual((result[1]["status"], result[1]["error_type"]), ("fetch_failed", "TimeoutError"))
        get.assert_not_called()

    def test_an_os_error_does_not_lose_other_players(self):
        get = Mock(side_effect=[OSError("network down"), self.response()])
        result = fetch_player_news([1, 2], get=get)
        self.assertEqual((result[1]["status"], result[1]["error_type"]), ("fetch_failed", "OSError"))
        self.assertEqual(result[2]["status"], "ok")

    def test_each_failure_is_type_only_and_never_aborts_other_players(self):
        secret = "espn_s2=SECRET"
        failures = {
            1: Mock(side_effect=requests.ConnectionError(secret)),
            2: Mock(return_value=self.response(status=500)),
            3: Mock(return_value=self.response([b"not json"])),
            4: Mock(return_value=self.response([b'{"news": {}}'])),
            5: Mock(return_value=self.response([b'{"news": {"feed": {}}}'])),
            6: Mock(return_value=self.response([b"x" * (MAX_RESPONSE_BYTES + 1)])),
            7: Mock(return_value=self.response([b"[" * 200000])),
            8: Mock(return_value=self.response()),
        }
        result = fetch_player_news(failures, get=lambda url, params, **kw: failures[params["playerId"]](url, params=params, **kw))
        for player_id in range(1, 8):
            with self.subTest(player_id=player_id):
                self.assertEqual(result[player_id]["status"], "fetch_failed")
                self.assertEqual(result[player_id]["feed"], [])
                self.assertNotIn("SECRET", json.dumps(result[player_id]))
                self.assertTrue(result[player_id]["error_type"])
        self.assertEqual(result[8]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
