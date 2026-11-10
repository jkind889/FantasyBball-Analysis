"""Streaming route with an injected fake news feed; no network and synthetic data."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from features.player_news import MAX_CONTEXT_BYTES
from features.streaming_context import MAX_ENTRIES, load_context, merge_context_entries, normalize_context
from features.streaming_outputs import explanation, news_priority, run_streaming
from features.streaming_planner import plan_streaming
from features.streaming_review import MAX_INPUT_BYTES, SYSTEM
from test_streaming_planner import sequence_snapshot

FRESH = "2026-01-15T10:00:00-05:00"  # The fixture's decision time is noon that day.
RETRIEVED = "2026-01-15T17:00:01+00:00"
NAMES = {1: "Cold incumbent", 2: "Hot free agent", 3: "Steady option"}


def blurb(player_id, item_id=None, published=FRESH, story="Synthetic detail."):
    surname = NAMES[player_id].split()[-1]
    return {"id": item_id or player_id * 100, "type": "Rotowire", "playerId": player_id, "story": story,
            "headline": f"{surname} (ankle) is questionable for Friday's game.", "published": published}


def clip(player_id):
    return {"id": player_id * 1000, "type": "Media", "video": [{}], "playerId": player_id,
            "headline": "Highlights", "published": FRESH}


def feeds(mapping):
    def fetch(ids):
        fetch.calls.append(list(ids))
        return {pid: {"status": "ok", "retrieved_at": RETRIEVED, "feed": mapping.get(pid, [])} for pid in ids}
    fetch.calls = []
    return fetch


class StreamingNewsTests(unittest.TestCase):
    def test_default_route_never_fetches_or_reports_news(self):
        result = run_streaming(sequence_snapshot())
        self.assertNotIn("news", result)
        self.assertNotIn("ESPN news", explanation(result))
        with tempfile.TemporaryDirectory() as directory:
            run_streaming(sequence_snapshot(), output_dir=directory)
            self.assertFalse((Path(directory) / "streaming_context.json").exists())

    def test_fresh_blurb_becomes_context_and_clips_do_not(self):
        fetch = feeds({1: [blurb(1), clip(1)], 2: [clip(2)]})
        result = run_streaming(sequence_snapshot(), news_fetcher=fetch)
        entries = result["context"]["entries"]
        self.assertEqual([(e["player_id"], e["status"]) for e in entries], [(1, "fresh")])
        self.assertEqual((entries[0]["observed_at"], entries[0]["retrieved_at"]), (FRESH, RETRIEVED))
        self.assertEqual(result["news"]["entries_added"], 1)
        missing = {(e["player_id"], e["kind"]) for e in result["context"]["coverage"]}
        self.assertNotIn((1, "injury"), missing)
        self.assertIn((2, "injury"), missing)  # A clip is not evidence of anything.
        text = explanation(result)
        self.assertIn("dropped 2 clips", text)
        self.assertIn("added 1 injury blurbs", text)
        self.assertIn("does not mean a player is healthy", text)

    def test_history_is_written_only_when_a_path_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blurbs.jsonl"
            result = run_streaming(sequence_snapshot(), news_fetcher=feeds({1: [blurb(1)]}), news_history=path)
            self.assertEqual(result["news"]["history"]["added"], 1)
            self.assertEqual(json.loads(path.read_text())["player_id"], 1)
            self.assertIn("News history: saved 1 new blurbs", explanation(result))
        self.assertNotIn("history", run_streaming(sequence_snapshot(), news_fetcher=feeds({1: [blurb(1)]}))["news"])

    def test_history_failure_never_blocks_planning_or_the_news_context(self):
        with tempfile.TemporaryDirectory() as directory:  # A directory can't be opened as the history file.
            result = run_streaming(sequence_snapshot(), news_fetcher=feeds({1: [blurb(1)]}), news_history=directory)
        self.assertEqual(result["news"]["history"]["status"], "error")
        self.assertEqual(result["news"]["entries_added"], 1)
        text = explanation(result)
        self.assertIn("News history: not saved", text)
        self.assertNotIn(directory, json.dumps(result["news"]) + text)

    def test_news_never_changes_numbers_or_the_deterministic_choice(self):
        plain = run_streaming(sequence_snapshot())
        with_news = run_streaming(sequence_snapshot(), news_fetcher=feeds({1: [blurb(1)], 2: [blurb(2)]}))
        self.assertEqual(plain["selected_plan_id"], with_news["selected_plan_id"])
        self.assertEqual(plain["plans"], with_news["plans"])

    def test_candidate_moves_are_requested_before_other_roster_players(self):
        snapshot = sequence_snapshot()
        snapshot["players"].append(dict(snapshot["players"][0], player_id=9, player_name="Bench Guy", rostered=True))
        snapshot["rules"]["roster_capacity"] += 1
        result = plan_streaming(snapshot, max_moves=1, protected_ids=(9,))  # 9 is never a candidate move.
        order = news_priority(result)
        self.assertEqual(order[-1], 9)
        self.assertEqual(set(order[:-1]), {a for p in result["plans"] for m in p["actions"] for a in (m["add_id"], m["drop_id"]) if a})
        with patch("features.streaming_outputs.MAX_NEWS_PLAYERS", 2):
            fetch = feeds({})
            news = run_streaming(snapshot, max_moves=1, protected_ids=(9,), news_fetcher=fetch)["news"]
        self.assertEqual(fetch.calls, [order[:2]])
        self.assertEqual((news["requested"], news["not_requested"]), (2, len(order) - 2))

    def test_failures_stay_visible_and_never_block_planning(self):
        def broken(ids):
            raise RuntimeError("espn_s2=SECRET")
        result = run_streaming(sequence_snapshot(), news_fetcher=broken)
        self.assertEqual((result["news"]["status"], result["news"]["error_type"]), ("error", "RuntimeError"))
        self.assertEqual(result["context"]["entries"], [])
        self.assertTrue(result["plans"])
        self.assertIn("unavailable (RuntimeError)", explanation(result))
        self.assertNotIn("SECRET", json.dumps(result["news"]) + explanation(result))

        def partial(ids):
            return {pid: {"status": "fetch_failed", "retrieved_at": RETRIEVED, "feed": [], "error_type": "Timeout"} for pid in ids}
        result = run_streaming(sequence_snapshot(), news_fetcher=partial)
        self.assertIn(f"{result['news']['requested']} failed", explanation(result))

    def test_invalid_caller_context_is_rejected_before_any_request(self):
        fetch = feeds({})
        with self.assertRaises(ValueError):
            run_streaming(sequence_snapshot(), context_document={"schema_version": 2}, news_fetcher=fetch)
        self.assertEqual(fetch.calls, [])

    def test_caller_entries_win_and_merged_document_stays_within_file_limits(self):
        snapshot = plan_streaming(sequence_snapshot(), max_moves=0)["snapshot"]
        mine = {"id": "espn-1-100", "kind": "injury", "player_id": 1, "observed_at": FRESH,
                "source": "Team report", "summary": "Caller supplied."}
        document = {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12, "entries": [mine]}
        result = run_streaming(sequence_snapshot(), context_document=document, news_fetcher=feeds({1: [blurb(1)], 2: [blurb(2)]}))
        by_id = {e["id"]: e for e in result["context"]["entries"]}
        self.assertEqual(by_id["espn-1-100"]["source"], "Team report")
        self.assertEqual(result["news"]["entries_dropped"], 1)
        self.assertEqual(document["entries"], [mine])  # The caller's document is not modified.

        entry = dict(mine, id="x")
        full = dict(document, entries=[dict(entry, id=f"e{i}") for i in range(MAX_ENTRIES)])
        merged, added, dropped = merge_context_entries(snapshot, full, [entry])
        self.assertEqual((len(merged["entries"]), added, dropped), (MAX_ENTRIES, 0, 1))

    def _export(self, document, mapping):
        with tempfile.TemporaryDirectory() as directory:
            result = run_streaming(sequence_snapshot(), context_document=document, news_fetcher=feeds(mapping), output_dir=directory)
            return result, load_context(Path(directory) / "streaming_context.json")

    def test_export_always_reloads_even_from_a_near_limit_caller_file(self):
        base = {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12}
        entry = {"kind": "injury", "player_id": 1, "observed_at": FRESH, "source": "Team report"}
        for label, summary, count in (("ascii", "x" * 500, 95), ("utf-8", "\u00e9" * 400, 70)):
            with self.subTest(label=label):
                document = dict(base, entries=[dict(entry, id=f"e{i}", summary=summary) for i in range(count)])
                compact = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
                self.assertLessEqual(len(compact), 65536)
                result, reloaded = self._export(document, {1: [blurb(1)]})
                self.assertEqual(len(reloaded["entries"]), count + result["news"]["entries_added"])

    def test_news_stops_at_the_file_limit_and_is_counted(self):
        base = {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12}
        entry = {"kind": "injury", "player_id": 1, "observed_at": FRESH, "source": "s" * 100}
        document = dict(base, entries=[dict(entry, id=f"e{i}", summary="x" * 500) for i in range(90)])
        while len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()) < 65300:
            document["entries"][len(document["entries"]) - 1]["source"] += "s" * 100
            if len(document["entries"][-1]["source"]) >= 500:
                document["entries"].append(dict(entry, id=f"f{len(document['entries'])}", summary="x" * 500))
                if len(document["entries"]) == 100:
                    break
        result, reloaded = self._export(document, {1: [blurb(1)]})
        self.assertEqual((result["news"]["entries_added"], result["news"]["entries_dropped"]), (0, 1))
        self.assertEqual(len(reloaded["entries"]), len(document["entries"]))

    def test_provider_metadata_fields_are_validated(self):
        snapshot = plan_streaming(sequence_snapshot(), max_moves=0)["snapshot"]
        base = {"id": "a", "kind": "injury", "player_id": 1, "observed_at": FRESH, "source": "s", "summary": "t"}
        document = {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12}
        self.assertEqual(normalize_context(snapshot, dict(document, entries=[dict(base, retrieved_at=RETRIEVED, source_item_id="1")]))["entries"][0]["status"], "fresh")
        for bad in ({"retrieved_at": "2026-01-15T10:00:00"}, {"source_item_id": 1}, {"source_item_id": "x" * 65}, {"unknown": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_context(snapshot, dict(document, entries=[dict(base, **bad)]))

    def test_export_replays_offline_with_the_same_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            original = run_streaming(sequence_snapshot(), news_fetcher=feeds({1: [blurb(1)]}), output_dir=directory)
            path = Path(directory)
            saved = json.loads((path / "streaming_snapshot.json").read_text())
            document = load_context(path / "streaming_context.json")
            replay = run_streaming(saved, context_document=document)
        self.assertNotIn("news", replay)
        self.assertEqual(replay["context"]["entries"], original["context"]["entries"])

    def _review(self, mapping, answer=None):
        sent = {}

        def request(encoded, model, key, *, diagnostics=None):
            sent["payload"] = encoded
            return answer(json.loads(encoded)) if answer else {}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-key"}, clear=True), \
                patch("features.streaming_review._request", side_effect=request):
            result = run_streaming(sequence_snapshot(), news_fetcher=feeds(mapping), claude=True, model="fixture")
        return result, sent

    def test_claude_can_cite_a_blurb_and_it_is_validated_end_to_end(self):
        def answer(payload):
            self.assertEqual(payload["fresh_context"][0]["player_id"], 1)
            selected = next(p for p in payload["plans"] if any(a["drop_id"] == 1 for a in p["actions"]))
            return {"selected_plan_id": selected["plan_id"],
                    "comparisons": [{"plan_id": selected["plan_id"], "reason": "injury_caution", "player_ids": [1],
                                     "evidence_ids": [payload["fresh_context"][0]["id"]]}]}
        result, _ = self._review({1: [blurb(1)]}, answer)
        self.assertEqual(result["review"]["status"], "validated")
        self.assertIn("ESPN player news (RotoWire blurb)", explanation(result))
        self.assertIn("(ankle) is questionable", explanation(result))

    def test_review_sees_and_reports_a_rest_day_return_date(self):
        snapshot = sequence_snapshot()
        back = (pd.Timestamp(snapshot["as_of"]) + pd.Timedelta(days=1)).date().isoformat()
        snapshot["players"][0].update(injury_status="OUT", injury_type="Rest", injury_return_date=back, out_for_season=False)
        sent = {}

        def request(encoded, model, key, *, diagnostics=None):
            sent["payload"] = json.loads(encoded)
            plans = sorted(sent["payload"]["plans"], key=lambda p: -p["net_gain"])
            return {"selected_plan_id": plans[0]["plan_id"],
                    "comparisons": [{"plan_id": plans[0]["plan_id"], "reason": "higher_projected_gain",
                                     "player_ids": [1], "evidence_ids": []}]}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-key"}, clear=True), \
                patch("features.streaming_review._request", side_effect=request):
            result = run_streaming(snapshot, claude=True, model="fixture")
        player = next(p for p in sent["payload"]["players"] if p["player_id"] == 1)
        self.assertEqual((player["injury_status"], player["injury_type"], player["injury_return_date"]), ("OUT", "Rest", back))
        self.assertIn("Rest counts from injury_return_date", SYSTEM)
        self.assertEqual(result["review"]["status"], "validated")
        self.assertIn(f"snapshot injury status OUT (Rest; ESPN expected return {back})", explanation(result))

    def test_review_withholds_lowest_priority_news_instead_of_being_skipped(self):
        long_story = "word " * 400
        mapping = {pid: [blurb(pid, story=long_story)] for pid in NAMES}
        full, sent = self._review(mapping)
        ordered = [e["id"] for e in json.loads(sent["payload"])["fresh_context"]]
        self.assertEqual(len(ordered), 3)
        self.assertNotIn("evidence_trimmed", full["review"])
        limit = len(sent["payload"].encode()) + len(SYSTEM.encode()) - 1
        with patch("features.streaming_review.MAX_INPUT_BYTES", limit):
            result, sent = self._review(mapping)
        kept = [e["id"] for e in json.loads(sent["payload"])["fresh_context"]]
        self.assertEqual(kept, ordered[:-1])
        self.assertEqual(result["review"]["evidence_trimmed"], 1)
        self.assertIn("withheld from the review", explanation(result))

    def test_caller_evidence_is_never_withheld_for_the_bound(self):
        document = {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12,
                    "entries": [{"id": "mine", "kind": "injury", "player_id": 1, "observed_at": FRESH,
                                 "source": "Team report", "summary": "x" * 400}]}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-key"}, clear=True), \
                patch("features.streaming_review.MAX_INPUT_BYTES", 500), \
                patch("features.streaming_review._request") as request:
            result = run_streaming(sequence_snapshot(), context_document=document, news_fetcher=feeds({2: [blurb(2)]}),
                                   claude=True, model="fixture")
        request.assert_not_called()
        self.assertEqual(result["review"]["status"], "input_bound_exceeded")
        self.assertEqual(result["review"]["evidence_trimmed"], 1)
        self.assertIn("mine", {e["id"] for e in result["context"]["entries"]})

    def test_budget_keeps_the_review_within_its_input_bound(self):
        long_story = "word " * 400
        mapping = {pid: [blurb(pid, item_id=pid * 100 + i, story=long_story) for i in range(5)] for pid in NAMES}
        result, sent = self._review(mapping)
        self.assertNotEqual(result["review"]["status"], "input_bound_exceeded")
        self.assertLess(len(sent["payload"].encode()), MAX_INPUT_BYTES)
        added = [e for e in result["context"]["entries"] if e["id"].startswith("espn-")]
        self.assertLessEqual(len(json.dumps(added)), MAX_CONTEXT_BYTES + 100)
        self.assertLessEqual(max(len(e["summary"]) for e in added), 240)


if __name__ == "__main__":
    unittest.main()
