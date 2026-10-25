from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

from features.streaming_context import load_context, normalize_context
from features.streaming_outputs import run_streaming, summary_frame
from features.streaming_planner import plan_streaming, validate_plan
from features.streaming_review import review_plans
from features.waiver_inputs import normalize_rules, daily_acquisitions
from test_waiver_advisor import example_snapshot
from test_waiver_inputs import raw_league


def sequence_snapshot():
    s = example_snapshot()
    s["rules"].update(matchup_acquisitions_remaining=2, season_acquisitions_remaining=4,
                      daily_acquisition_limit=1, daily_acquisitions_used=0)
    for p, team, rate in zip(s["players"], ["CHI", "ATL", "BOS"], [10, 30, 28]):
        p.update(pro_team=team, proj_pts_per_game=rate)
        for log in s["game_logs"]:
            if log["player_id"] == p["player_id"]:
                log["points"] = rate
    games = s["games"]
    s["games"] = [dict(g, pro_team="CHI", game_id="chi" + g["game_id"]) for g in games]
    s["games"] += [dict(games[0], pro_team="ATL", opponent="NYK")]
    s["games"] += [dict(g, pro_team="BOS", game_id="bos" + g["game_id"], opponent="NYK") for g in games[1:]]
    return s


def context_entry(**changes):
    return dict({"id": "injury-2", "kind": "injury", "player_id": 2,
                 "observed_at": "2026-01-15T15:00:00Z", "source": "Synthetic injury report",
                 "summary": "Available but reports ankle soreness; no minutes limit verified."}, **changes)


class AcquisitionInputTests(unittest.TestCase):
    def test_missing_current_matchup_is_unknown_but_explicit_zero_is_real(self):
        raw = raw_league()
        raw["settings"]["acquisitionSettings"].update(matchupLimitPerScoringPeriod=False, matchupAcquisitionLimit=5)
        for totals in (None, {}, {"1": 0}, {"2": None}, {"2": -1}, {"2": True}, {"2": 1.5}):
            raw["teams"][0]["transactionCounter"]["matchupAcquisitionTotals"] = totals
            with self.subTest(totals=totals), self.assertRaisesRegex(ValueError, "acquisitions_remaining"):
                normalize_rules(raw, raw["teams"][0], {})
        raw["teams"][0]["transactionCounter"]["matchupAcquisitionTotals"] = {"2": 0}
        self.assertEqual(normalize_rules(raw, raw["teams"][0], {})["acquisitions_remaining"], 5)

    def test_season_matchup_and_daily_semantics_stay_separate(self):
        raw = raw_league()
        settings = raw["settings"]["acquisitionSettings"]
        settings.update(acquisitionLimit=9, matchupAcquisitionLimit=5, matchupLimitPerScoringPeriod=False)
        rules = normalize_rules(raw, raw["teams"][0], {})
        self.assertEqual((rules["acquisitions_remaining"], rules["matchup_acquisitions_remaining"],
                          rules["season_acquisitions_remaining"]), (1, 2, 1))
        settings.pop("matchupLimitPerScoringPeriod")
        with self.assertRaises(ValueError):
            normalize_rules(raw, raw["teams"][0], {})
        settings.update(matchupLimitPerScoringPeriod=True, matchupAcquisitionLimit=float("inf"))
        with self.assertRaises(ValueError):
            normalize_rules(raw, raw["teams"][0], {}, daily_used=0)

    def test_daily_activity_failure_is_unknown(self):
        league = Mock()
        league.recent_activity.side_effect = OSError("fixture failure")
        self.assertIsNone(daily_acquisitions(league, 1, "America/New_York"))


class PlannerTests(unittest.TestCase):
    def test_shared_budgets_and_calendar_reconciliation(self):
        s = sequence_snapshot()
        original = deepcopy(s)
        r = plan_streaming(s)
        best = r["plans"][0]
        self.assertEqual([a["add_id"] for a in best["actions"]], [2, 3])
        self.assertEqual([a["drop_id"] for a in best["actions"]], [1, 2])
        self.assertEqual(best["net_gain"], 56)
        self.assertEqual(best["budget_after"]["matchup_acquisitions_remaining"], 0)
        self.assertEqual(best["budget_after"]["season_acquisitions_remaining"], 2)
        self.assertEqual(s, original)
        self.assertEqual({p["acquisitions_consumed"] for p in r["plans"]}, {0, 1, 2})
        for p in r["plans"]:
            validate_plan(r["snapshot"], p)
            self.assertAlmostEqual(sum(row["projected_points"] or 0 for row in p["calendar"]), p["candidate_points"])
            self.assertEqual(len({a["date"] for a in p["actions"]}), len(p["actions"]))
            self.assertTrue(all(row["planned"] <= 1 and (row["remaining"] is None or row["remaining"] >= 0)
                                for row in p["daily_budget"]))

    def test_each_budget_constrains_whole_plan(self):
        for key in ("acquisitions_remaining", "matchup_acquisitions_remaining", "season_acquisitions_remaining"):
            for budget in (0, 1):
                s = sequence_snapshot()
                s["rules"][key] = budget
                result = plan_streaming(s, max_moves=3)
                self.assertTrue(all(p["acquisitions_consumed"] <= budget for p in result["plans"]))
                if budget == 0:
                    self.assertEqual(result["selected_plan_id"], "hold")

    def test_daily_usage_resets_and_delayed_availability(self):
        s = sequence_snapshot()
        s["rules"]["daily_acquisitions_used"] = 1
        result = plan_streaming(s)
        self.assertTrue(all(a["date"] != "2026-01-15" for p in result["plans"] for a in p["actions"]))
        self.assertGreater(result["plans"][0]["net_gain"], 0)
        s["rules"]["daily_acquisition_limit"] = 0
        self.assertEqual(len(plan_streaming(s)["plans"]), 1)

    def test_protected_unknown_drops_and_waivers_excluded(self):
        s = sequence_snapshot()
        self.assertEqual(len(plan_streaming(s, protected_ids=[1])["plans"]), 1)
        s["players"][0]["undroppable"] = None
        self.assertEqual(len(plan_streaming(s)["plans"]), 1)
        s = sequence_snapshot()
        for p in s["players"][1:]:
            p["availability"] = "WAIVERS"
        self.assertEqual(len(plan_streaming(s)["plans"]), 1)

    def test_open_spot_and_position_capacity(self):
        s = sequence_snapshot()
        s["rules"]["roster_capacity"] = 2
        result = plan_streaming(s)
        self.assertTrue(any(a["drop_id"] is None for p in result["plans"] for a in p["actions"]))
        s["rules"]["position_limits"] = {"PG": 1}
        self.assertTrue(all(a["drop_id"] is not None for p in plan_streaming(s)["plans"] for a in p["actions"]))

    def test_locked_drop_and_injury_exclusion(self):
        s = sequence_snapshot()
        s["as_of"] = "2026-01-15T20:00:00-05:00"
        result = plan_streaming(s)
        self.assertTrue(all(a["date"] != "2026-01-15" for p in result["plans"] for a in p["actions"]))
        s = sequence_snapshot()
        s["players"][1]["injury_status"] = "OUT"
        self.assertNotEqual(result["selected_plan_id"], "hold")
        result = plan_streaming(s)
        self.assertNotIn(2, [a["add_id"] for a in result["plans"][0]["actions"]])

    def test_unknown_data_and_unsupported_modes(self):
        s = sequence_snapshot()
        s["pool_complete"] = False
        self.assertFalse(plan_streaming(s)["recommended"])
        for change in ({"acquisition_timing": "NEXT_DAY"}, {"season_acquisitions_remaining": "unknown"}):
            s = sequence_snapshot()
            s["rules"].update(change)
            with self.assertRaises(ValueError):
                plan_streaming(s)
        for n in (-1, 4, True):
            with self.assertRaises(ValueError):
                plan_streaming(sequence_snapshot(), max_moves=n)
        s = sequence_snapshot()
        s["players"][0]["injury_status"] = None
        self.assertFalse(plan_streaming(s)["recommended"])

    def test_invalid_plan_numbers_ids_times_and_reacquisition(self):
        r = plan_streaming(sequence_snapshot())
        best = r["plans"][0]
        for key, value in (("net_gain", 999), ("candidate_points", float("nan")), ("candidate_starts", 999),
                           ("plan_id", "forged"), ("daily_budget", []),
                           ("acquisitions_consumed", True), ("budget_after", {})):
            p = deepcopy(best)
            p[key] = value
            with self.assertRaises(ValueError):
                validate_plan(r["snapshot"], p)
        for field, value in (("add_id", 999), ("drop_id", 999), ("effective_at", "2026-01-15T00:00:00Z")):
            p = deepcopy(best)
            p["actions"][0][field] = value
            with self.assertRaises(ValueError):
                validate_plan(r["snapshot"], p)
        p = deepcopy(best)
        p["actions"][1]["add_id"] = 1
        with self.assertRaises(ValueError):
            validate_plan(r["snapshot"], p)

    def test_cli_offline_export_and_replay(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_text(json.dumps(sequence_snapshot()))
            out = Path(directory) / "out"
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            command = [sys.executable, "-B", str(root / "main.py"), "--mode", "waivers", "--snapshot",
                       str(source), "--streaming-plan", "--output-dir", str(out)]
            run = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Claude review: disabled", run.stdout)
            self.assertIn("add Hot free agent [2]", run.stdout)
            result = json.loads((out / "streaming_plan.json").read_text())
            replay = run_streaming(json.loads((out / "streaming_snapshot.json").read_text()))
            pd.testing.assert_frame_equal(summary_frame(result), summary_frame(replay))
            self.assertTrue((out / "streaming_calendar.csv").exists())

    def test_live_route_with_mocked_snapshot_never_reads_provider(self):
        from features.decision_modes import run
        league = Mock()
        with patch('features.waiver_inputs.fetch_snapshot', return_value=sequence_snapshot()) as fetch, redirect_stdout(StringIO()):
            board = run('waivers', league, team_id=1, streaming_options={"max_moves": 1})
        fetch.assert_called_once_with(league, None, 1, None, None)
        self.assertTrue((board.acquisitions_consumed <= 1).all())
        self.assertEqual(league.mock_calls, [])

    def test_candidate_and_horizon_bounds_and_protection_replay(self):
        s = sequence_snapshot()
        for pid in range(4, 18):
            p = deepcopy(s["players"][1])
            p.update(player_id=pid, player_name=f"Fixture {pid}")
            s["players"].append(p)
        result = plan_streaming(s, max_moves=0, protected_ids=[1])
        self.assertEqual(result["selected_plan_id"], "hold")
        self.assertTrue(any("truncated" in x for x in result["limitations"]))
        self.assertEqual(plan_streaming(result["snapshot"])["selected_plan_id"], "hold")
        s["games"].append(dict(s["games"][0], game_id="far", tipoff="2026-02-15T19:00:00-05:00"))
        with self.assertRaisesRegex(ValueError, "14 days"):
            plan_streaming(s)


class ContextAndReviewTests(unittest.TestCase):
    def setUp(self):
        self.s = sequence_snapshot()
        self.result = plan_streaming(self.s)
        self.context = normalize_context(self.s, {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12, "entries": [context_entry()]})

    def answer(self):
        return {"selected_plan_id": "hold", "comparisons": [
            {"plan_id": "hold", "reason": "reserve_acquisitions", "player_ids": [], "evidence_ids": []}]}

    def test_context_fresh_stale_future_and_matchup_relevance(self):
        entries = [context_entry(), context_entry(id="old", observed_at="2026-01-13T00:00:00Z"),
                   context_entry(id="future", observed_at="2026-01-16T00:00:00Z"),
                   context_entry(id="opponent", kind="matchup", opponent="LAL", sample_games=3,
                                 window_start="2025-01-01T00:00:00Z", window_end="2026-01-01T00:00:00Z")]
        context = normalize_context(self.s, {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12, "entries": entries})
        self.assertEqual([e["status"] for e in context["entries"]], ["fresh", "stale", "future", "irrelevant_opponent"])
        self.assertTrue(context["coverage"])
        entries[-1]["opponent"] = "NYK"
        self.assertEqual(normalize_context(self.s, {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12, "entries": entries})["entries"][-1]["status"], "fresh")

    def test_context_missing_and_invalid(self):
        self.assertEqual(len(normalize_context(self.s)["coverage"]), 6)
        for key in ("league_id", "season_year", "week"):
            document = {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12, "entries": []}
            document[key] = -1
            with self.assertRaisesRegex(ValueError, "match the decision snapshot"):
                normalize_context(self.s, document)
        for changes in ({"player_id": 999}, {"kind": "matchup"}, {"observed_at": "invalid"}):
            with self.assertRaises(ValueError):
                normalize_context(self.s, {"schema_version": 1, "league_id": 123, "season_year": 2026, "week": 12, "entries": [context_entry(**changes)]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_bytes(b" " * 65537)
            with self.assertRaises(ValueError):
                load_context(path)

    def test_no_key_disabled_and_missing_model_never_call(self):
        with patch.dict(os.environ, {}, clear=True), patch('features.streaming_review._request') as request:
            self.assertEqual(review_plans(self.result, self.context)["status"], "disabled")
            self.assertEqual(review_plans(self.result, self.context, enabled=True, model="fixture")["status"], "missing_key_or_model")
            request.assert_not_called()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-only"}, clear=True), patch('features.streaming_review._request') as request:
            self.assertEqual(review_plans(self.result, self.context, enabled=True)["status"], "missing_key_or_model")
            request.assert_not_called()

    def test_valid_selection_reserves_moves_without_changing_numbers(self):
        before = deepcopy(self.result)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-only"}, clear=True), patch(
                'features.streaming_review._request', return_value=self.answer()) as request:
            reviewed = review_plans(self.result, self.context, enabled=True, model="fixture-model")
        self.assertEqual(reviewed["status"], "validated")
        self.assertEqual(reviewed["selected_plan_id"], "hold")
        self.assertEqual(self.result, before)
        self.assertEqual(request.call_count, 1)

    def test_invalid_model_outputs_fall_back(self):
        variants = []
        for key, val in (("selected_plan_id", "invented"), ("net_gain", 1000), ("comparisons", [])):
            answer = self.answer()
            answer[key] = val
            variants.append(answer)
        for changes in ({"player_ids": [999]}, {"evidence_ids": ["made-up"]},
                        {"reason": "injury_caution"}, {"reason": "higher_projected_gain"}):
            answer = self.answer()
            answer["comparisons"][0].update(changes)
            variants.append(answer)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-only"}, clear=True):
            for answer in variants:
                with self.subTest(answer=answer), patch('features.streaming_review._request', return_value=answer):
                    reviewed = review_plans(self.result, self.context, enabled=True, model="fixture")
                    self.assertEqual(reviewed["status"], "error_or_invalid_output")
                    self.assertEqual(reviewed["selected_plan_id"], self.result["deterministic_plan_id"])

    def test_stale_citation_and_network_failure_fall_back(self):
        context = deepcopy(self.context)
        context["entries"][0]["status"] = "stale"
        answer = self.answer()
        pid = self.result["deterministic_plan_id"]
        answer.update(selected_plan_id=pid, comparisons=[{"plan_id": pid, "reason": "injury_caution",
                                                       "player_ids": [2], "evidence_ids": ["injury-2"]}])
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-only"}, clear=True):
            with patch('features.streaming_review._request', return_value=answer):
                self.assertEqual(review_plans(self.result, context, enabled=True, model="fixture")["status"], "error_or_invalid_output")
                self.assertEqual(review_plans(self.result, self.context, enabled=True, model="fixture")["status"], "validated")
            with patch('features.streaming_review._request', side_effect=requests.Timeout("do not echo secrets")):
                review = review_plans(self.result, self.context, enabled=True, model="fixture")
                self.assertNotIn("secrets", json.dumps(review))
                self.assertEqual(review["status"], "error_or_invalid_output")

    def test_request_bounds_and_mocked_http_contract(self):
        from features.streaming_review import _request, MAX_OUTPUT_TOKENS
        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps({"stop_reason": "end_turn", "content": [
            {"type": "text", "text": json.dumps(self.answer())}]}).encode()]
        with patch.dict(os.environ, {}, clear=True), patch('features.streaming_review.requests.post', return_value=response) as post:
            self.assertEqual(_request("{}", "fixture", "fixture-only"), self.answer())
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], MAX_OUTPUT_TOKENS)
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-only"}, clear=True), patch(
                'features.streaming_review.MAX_INPUT_BYTES', 1), patch('features.streaming_review._request') as request:
            self.assertEqual(review_plans(self.result, self.context, enabled=True, model="fixture")["status"], "input_bound_exceeded")
            request.assert_not_called()

    def test_malformed_truncated_or_oversized_http_response_falls_back(self):
        bodies = [[], {"stop_reason": "max_tokens"}, {"stop_reason": "end_turn", "content": ["invalid"]},
                  {"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]}]
        for body in bodies + [None]:
            response = Mock(status_code=200)
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.iter_content.return_value = [json.dumps(body).encode()] if body is not None else [b'x' * 32001]
            with self.subTest(body=body), patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-only"}, clear=True), patch(
                    'features.streaming_review.requests.post', return_value=response):
                self.assertEqual(review_plans(self.result, self.context, enabled=True, model="fixture")["status"], "error_or_invalid_output")


if __name__ == "__main__":
    unittest.main()
