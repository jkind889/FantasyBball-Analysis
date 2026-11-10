"""Offline implementation tests. Invented values are not historical sports data."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from features.draft_replay import load_snapshot, replay_draft


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "draft_snapshot.synthetic.json"


def snapshot():
    return json.loads(EXAMPLE.read_text())


class DraftReplayTests(unittest.TestCase):
    def test_available_choices_ties_missingness_and_value_gap(self):
        report = replay_draft(snapshot())
        last = report["picks"][-1]
        self.assertEqual(last["available_player_ids"], ["p005", "p006", "p007", "p008", "p009"])
        self.assertEqual(last["selected_rank_among_covered_available"], 2)
        self.assertIsNone(last["selected_global_available_rank"])
        self.assertEqual(last["selected_value"], 950)
        self.assertEqual(last["opportunity_gap_among_covered"], 10)
        self.assertEqual(last["best_covered_available_player_ids"], ["p005"])
        self.assertEqual(last["comparison_scope"], "covered_available")
        alternatives = {a["player_id"]: a for a in last["alternatives"]}
        self.assertEqual(alternatives["p007"]["rank_among_covered_available"], 2)
        self.assertEqual(alternatives["p009"]["value"], 0)
        self.assertIsNone(alternatives["p008"]["value"])
        self.assertEqual(last["roster_before"]["observed_player_ids"], ["p002"])
        self.assertEqual(last["roster_after"]["observed_player_ids"], ["p002", "p006"])
        self.assertEqual(last["starting_slot_coverage_change"], 1)

    def test_complete_value_coverage_allows_global_rank(self):
        data = snapshot()
        data["rankings"]["entries"][7]["value"] = 100
        report = replay_draft(data)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["picks"][-1]["comparison_scope"], "all_available")
        self.assertEqual(report["picks"][-1]["selected_global_available_rank"], 2)

    def test_replay_removes_each_pick_only_after_its_comparison(self):
        data = snapshot()
        data["picks"]["events"].append({"overall_pick": 6, "team_id": "team-c", "player_id": "p005"})
        report = replay_draft(data)
        self.assertIn("p006", report["picks"][4]["available_player_ids"])
        self.assertNotIn("p006", report["picks"][5]["available_player_ids"])
        self.assertIn("p005", report["picks"][5]["available_player_ids"])
        self.assertEqual(report["observed_final_rosters"]["team-c"], ["p003", "p005"])

    def test_ranking_only_values_do_not_become_projected_points(self):
        data = snapshot()
        data["rankings"]["metric"] = "pre_draft_rank"
        for entry, value in zip(data["rankings"]["entries"], [1, 2, 3, 4, 5, 6, 6, None, 9]):
            entry["value"] = value
            entry["source"] = data["rankings"]["source"]
        report = replay_draft(data)
        self.assertEqual(report["value_unit"], "source_rank_positions")
        self.assertEqual(report["picks"][-1]["opportunity_gap_among_covered"], 1)
        self.assertEqual(report["picks"][-1]["selected_rank_among_covered_available"], 2)

    def test_rank_positions_from_different_boards_or_revisions_are_not_comparable(self):
        for field, value in [("source", "another-ranking-board"), ("observed_at", "2025-10-15T21:00:00Z")]:
            with self.subTest(field=field):
                data = snapshot()
                data["rankings"]["metric"] = "pre_draft_rank"
                for index, entry in enumerate(data["rankings"]["entries"], 1):
                    entry["value"] = index
                    entry["source"] = data["rankings"]["source"]
                data["rankings"]["entries"][5][field] = value
                last = replay_draft(data)["picks"][-1]
                self.assertIsNone(last["selected_value"])
                self.assertIsNone(last["selected_rank_among_covered_available"])
                self.assertIsNone(last["opportunity_gap_among_covered"])

    def test_nonfinite_computed_difference_is_rejected(self):
        data = snapshot()
        data["rankings"]["entries"][4]["value"] = 1e308
        data["rankings"]["entries"][5]["value"] = -1e308
        with self.assertRaisesRegex(ValueError, "computed opportunity gap"):
            replay_draft(data)

    def test_negative_scores_and_explicit_zero_are_valid(self):
        data = snapshot()
        for entry in data["rankings"]["entries"]:
            entry["value"] = -10
        data["rankings"]["entries"][5]["value"] = 0
        last = replay_draft(data)["picks"][-1]
        self.assertEqual(last["selected_global_available_rank"], 1)
        self.assertEqual(last["opportunity_gap_among_covered"], 0)

    def test_equal_values_have_no_gap_or_order_advantage(self):
        data = snapshot()
        data["rankings"]["entries"][4]["value"] = 950
        last = replay_draft(data)["picks"][-1]
        self.assertEqual(last["selected_rank_among_covered_available"], 1)
        self.assertEqual(last["best_covered_available_player_ids"], ["p005", "p006", "p007"])
        self.assertEqual(last["opportunity_gap_among_covered"], 0)

    def test_incomplete_pool_or_log_blocks_available_claims(self):
        for section in ("player_pool", "picks"):
            with self.subTest(section=section):
                data = snapshot()
                data[section]["complete"] = False
                last = replay_draft(data)["picks"][-1]
                self.assertFalse(last["availability_complete"])
                self.assertIsNone(last["available_player_ids"])
                self.assertIsNone(last["selected_rank_among_covered_available"])
                self.assertIsNone(last["opportunity_gap_among_covered"])
                self.assertEqual(last["alternatives"], [])
                self.assertIn("p006", last["observed_remaining_player_ids"])

    def test_incomplete_log_can_retain_gaps_without_inventing_roster(self):
        data = snapshot()
        data["picks"]["complete"] = False
        data["picks"]["events"].pop(1)
        last = replay_draft(data)["picks"][-1]
        self.assertEqual(last["roster_before"]["observed_player_ids"], [])
        self.assertFalse(last["roster_after"]["complete"])
        self.assertIsNone(last["roster_after"]["remaining_capacity"])
        self.assertIsNone(last["roster_after"]["all_players_fit"])

    def test_missing_rules_rankings_or_keepers_stays_partial(self):
        for section in ("rules", "rankings", "keepers"):
            with self.subTest(section=section):
                data = snapshot()
                data[section] = None
                report = replay_draft(data)
                self.assertEqual(report["status"], "partial")
                self.assertIsNone(report["picks"][-1]["selected_rank_among_covered_available"])
                if section == "rules":
                    self.assertIsNone(report["picks"][-1]["roster_after"]["assignment"])

    def test_cutoff_applies_to_pool_rules_and_global_forecasts(self):
        for section in ("player_pool", "rules", "rankings"):
            for timestamp in (None, "2025-10-15T23:00:00Z", "2025-10-16T00:00:00Z"):
                with self.subTest(section=section, timestamp=timestamp):
                    data = snapshot()
                    data[section]["observed_at"] = timestamp
                    report = replay_draft(data)
                    self.assertEqual(report["status"], "partial")
                    self.assertIsNone(report["picks"][-1]["selected_rank_among_covered_available"])

    def test_per_player_provenance_cannot_mix_season_horizon_or_capture(self):
        variants = [("observed_at", "2025-10-16T00:00:00Z"), ("observed_at", None),
                    ("season_year", 2025), ("horizon", "rest_of_season"),
                    ("league_id", "another-league"), ("scoring_version", "other-rules"), ("source", None)]
        for key, value in variants:
            with self.subTest(key=key, value=value):
                data = snapshot()
                data["rankings"]["entries"][5][key] = value
                report = replay_draft(data)
                self.assertIsNone(report["picks"][-1]["selected_value"])
                self.assertIsNone(report["picks"][-1]["opportunity_gap_among_covered"])
                self.assertIn("p006", report["picks"][-1]["unranked_observed_player_ids"])

    def test_global_scope_mismatch_invalidates_every_score(self):
        for key, value in [("season_year", 2025), ("horizon", "rest_of_season"),
                           ("league_id", "other"), ("scoring_version", "other")]:
            with self.subTest(key=key):
                data = snapshot()
                data["rankings"][key] = value
                self.assertTrue(all(row["value"] is None for row in replay_draft(data)["score_coverage"].values()))

    def test_missing_draft_start_cannot_establish_predraft_values(self):
        data = snapshot()
        data["draft_started_at"] = None
        report = replay_draft(data)
        self.assertIn("draft_started_at_missing", report["limitations"])
        self.assertIsNone(report["picks"][-1]["selected_value"])
        self.assertIsNone(report["picks"][-1]["roster_after"]["assignment"])

    def test_explicit_timezones_compare_as_instants(self):
        data = snapshot()
        data["rankings"]["entries"][5]["observed_at"] = "2025-10-15T18:59:00-04:00"
        self.assertEqual(replay_draft(data)["picks"][-1]["selected_value"], 950)
        data["rankings"]["entries"][5]["observed_at"] = "2025-10-15T19:00:00-04:00"
        self.assertIsNone(replay_draft(data)["picks"][-1]["selected_value"])

    def test_invalid_time_or_cutoff_fails(self):
        for field, value in [("cutoff_at", "2025-10-15T23:00:00"), ("cutoff_at", "bad"),
                             ("draft_started_at", "2025-10-15T23:00:00Z")]:
            with self.subTest(field=field, value=value):
                data = snapshot()
                data[field] = value
                with self.assertRaises(ValueError):
                    replay_draft(data)

    def test_invalid_values_fail_instead_of_becoming_zero(self):
        for value in (True, "950", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=str(value)[:20]):
                data = snapshot()
                data["rankings"]["entries"][5]["value"] = value
                with self.assertRaises(ValueError):
                    replay_draft(data)

    def test_duplicates_unknown_ids_and_inconsistent_complete_log_fail(self):
        def bad(kind):
            data = snapshot()
            if kind == "player": data["player_pool"]["players"].append(deepcopy(data["player_pool"]["players"][0]))
            if kind == "team": data["teams"].append(deepcopy(data["teams"][0]))
            if kind == "ranking": data["rankings"]["entries"].append(deepcopy(data["rankings"]["entries"][0]))
            if kind == "pick_number": data["picks"]["events"][-1]["overall_pick"] = 1
            if kind == "picked_player": data["picks"]["events"][-1]["player_id"] = "p001"
            if kind == "unknown_player": data["picks"]["events"][-1]["player_id"] = "unknown"
            if kind == "unknown_team": data["picks"]["events"][-1]["team_id"] = "unknown"
            if kind == "gap": data["picks"]["events"].pop(1)
            return data
        for kind in ("player", "team", "ranking", "pick_number", "picked_player", "unknown_player", "unknown_team", "gap"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                replay_draft(bad(kind))

    def test_keepers_are_owned_and_unavailable_before_pick_one(self):
        data = snapshot()
        data["keepers"] = [{"player_id": "p007", "team_id": "team-b"}]
        report = replay_draft(data)
        self.assertNotIn("p007", report["picks"][0]["available_player_ids"])
        self.assertEqual(report["picks"][-1]["roster_before"]["observed_player_ids"], ["p002", "p007"])
        data["keepers"].append({"player_id": "p001", "team_id": "team-a"})
        with self.assertRaises(ValueError):
            replay_draft(data)

    def test_flexible_player_matching_avoids_greedy_slot_error(self):
        data = snapshot()
        data["rules"]["roster"] = {"starting_slots": ["PG", "SG"], "bench_slots": 0}
        data["player_pool"]["players"][0]["eligible_positions"] = ["PG", "SG"]
        data["player_pool"]["players"][3]["eligible_positions"] = ["PG"]
        roster = replay_draft(data)["picks"][3]["roster_after"]
        self.assertEqual(roster["starting_slots_filled"], 2)
        self.assertEqual({r["slot"]: r["player_id"] for r in roster["assignment"]}, {"PG": "p004", "SG": "p001"})
        self.assertTrue(roster["all_players_fit"])

    def test_one_multi_position_player_never_fills_two_slots(self):
        data = snapshot()
        data["rules"]["roster"] = {"starting_slots": ["PG", "SG"], "bench_slots": 0}
        data["player_pool"]["players"][0]["eligible_positions"] = ["PG", "SG"]
        roster = replay_draft(data)["picks"][0]["roster_after"]
        self.assertEqual(roster["starting_slots_filled"], 1)
        self.assertEqual(len(roster["assignment"]), 1)

    def test_bench_only_capacity_and_overflow_are_visible(self):
        data = snapshot()
        data["rules"]["roster"] = {"starting_slots": [], "bench_slots": 2}
        data["picks"]["events"].append({"overall_pick": 6, "team_id": "team-b", "player_id": "p005"})
        report = replay_draft(data)
        self.assertTrue(report["picks"][4]["roster_after"]["all_players_fit"])
        self.assertEqual(report["picks"][4]["starting_slot_coverage_change"], 0)
        self.assertFalse(report["picks"][5]["roster_after"]["all_players_fit"])
        self.assertEqual(report["picks"][5]["roster_after"]["remaining_capacity"], -1)

    def test_unknown_positions_do_not_prove_roster_feasibility(self):
        data = snapshot()
        data["player_pool"]["players"][5]["eligible_positions"] = None
        report = replay_draft(data)
        self.assertIn("player_pool.positions_missing", report["limitations"])
        self.assertIsNone(report["picks"][-1]["roster_after"]["all_players_fit"])
        self.assertIsNone(report["picks"][-1]["starting_slot_coverage_change"])

    def test_input_is_not_mutated_and_reordered_inputs_replay_identically(self):
        data = snapshot()
        original = deepcopy(data)
        first = replay_draft(data)
        self.assertEqual(data, original)
        for items in (data["picks"]["events"], data["player_pool"]["players"], data["rankings"]["entries"]):
            items.reverse()
        second = replay_draft(data)
        self.assertEqual(first["picks"], second["picks"])
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])

    def test_evidence_and_actuals_are_outside_the_input_contract(self):
        for key in ("evidence", "observed_actuals", "claude_review"):
            data = snapshot()
            data[key] = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                replay_draft(data)

    def test_duplicate_json_keys_and_nonfinite_constants_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for content in ('{"schema_version":1,"schema_version":2}', '{"value":NaN}'):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_snapshot(path)

    def test_cli_writes_complete_json_and_preserves_existing_output(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            command = [sys.executable, "-B", "-m", "features.draft_replay", str(EXAMPLE), "--output", str(output)]
            first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            saved = output.read_bytes()
            self.assertEqual(len(json.loads(saved)["picks"]), 5)
            second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_bytes(), saved)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_invalid_cli_input_leaves_no_output(self):
        with TemporaryDirectory() as directory:
            source, output = Path(directory) / "bad.json", Path(directory) / "report.json"
            source.write_text('{"schema_version":999}')
            result = subprocess.run([sys.executable, "-B", "-m", "features.draft_replay", str(source), "--output", str(output)], cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


    def test_assumption_preserves_late_capture_and_strict_default(self):
        data = snapshot()
        stamp = "2026-10-30T23:57:24.342957+00:00"
        data["rankings"]["observed_at"] = stamp
        for entry in data["rankings"]["entries"]:
            entry["observed_at"] = stamp
        original = deepcopy(data)
        self.assertIsNone(replay_draft(data)["picks"][-1]["selected_value"])
        report = replay_draft(data, assume_forecasts_pre_draft=True)
        self.assertEqual(report["analysis_mode"], "user_assumed_forecasts_pre_draft")
        self.assertIn("forecast_timing_user_assumed", report["limitations"])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["picks"][-1]["opportunity_gap_among_covered"], 10)
        self.assertEqual(report["score_coverage"]["p006"]["provenance"]["observed_at"], stamp)
        self.assertEqual(data, original)

    def test_unknown_cutoff_is_explicit_and_never_invented(self):
        data = snapshot()
        data["cutoff_at"] = data["draft_started_at"] = None
        with self.assertRaisesRegex(ValueError, "cutoff_at"):
            replay_draft(data)
        report = replay_draft(data, assume_forecasts_pre_draft=True)
        self.assertIsNone(report["cutoff_at"])
        self.assertIsNone(report["draft_started_at"])
        self.assertIn("cutoff_at_missing", report["limitations"])
        self.assertIn("player_pool.cutoff_unknown", report["limitations"])
        self.assertIsNone(report["picks"][-1]["opportunity_gap_among_covered"])

    def test_assumption_does_not_waive_missing_rules_pool_or_keepers(self):
        for section in ("rules", "keepers", "player_pool"):
            data = snapshot()
            if section == "player_pool":
                data[section]["complete"] = False
            else:
                data[section] = None
            report = replay_draft(data, assume_forecasts_pre_draft=True)
            last = report["picks"][-1]
            self.assertIsNone(last["opportunity_gap_among_covered"])
            self.assertEqual(last["observed_projection_value"], 950)
            if section == "rules":
                self.assertIsNone(last["selected_value"])

    def test_assumption_retains_provenance_and_compatibility_checks(self):
        cases = [("source", None), ("observed_at", None), ("season_year", 2027),
                 ("league_id", "wrong"), ("horizon", "rest_of_season"),
                 ("scoring_version", None)]
        for field, value in cases:
            with self.subTest(field=field):
                data = snapshot()
                data["rankings"]["entries"][5][field] = value
                report = replay_draft(data, assume_forecasts_pre_draft=True)
                self.assertIsNone(report["picks"][-1]["selected_value"])
                self.assertEqual(report["score_coverage"]["p006"]["observed_value"], 950)

    def test_assumption_rejects_malformed_times_and_ordinal_boards(self):
        for field, value in [("observed_at", "not a timestamp"), ("metric", "pre_draft_rank")]:
            data = snapshot()
            data["rankings"][field] = value
            with self.assertRaises(ValueError):
                replay_draft(data, assume_forecasts_pre_draft=True)
        with self.assertRaisesRegex(ValueError, "true or false"):
            replay_draft(snapshot(), assume_forecasts_pre_draft="yes")

    def test_assumption_never_waives_late_pool_or_rules(self):
        for section in ("rules", "player_pool"):
            data = snapshot()
            data[section]["observed_at"] = "2026-10-30T23:57:24Z"
            report = replay_draft(data, assume_forecasts_pre_draft=True)
            self.assertIn(section + ".not_before_cutoff", report["limitations"])
            self.assertIsNone(report["picks"][-1]["opportunity_gap_among_covered"])

    def test_assumption_cli_requires_explicit_flag(self):
        with TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.json", Path(directory) / "report.json"
            data = snapshot()
            data["cutoff_at"] = None
            source.write_text(json.dumps(data))
            command = [sys.executable, "-B", "-m", "features.draft_replay", str(source), "--output", str(output)]
            strict = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(strict.returncode, 2)
            self.assertFalse(output.exists())
            assumed = subprocess.run(command + ["--assume-forecasts-pre-draft"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(assumed.returncode, 0, assumed.stderr)
            self.assertEqual(json.loads(output.read_text())["analysis_mode"], "user_assumed_forecasts_pre_draft")


if __name__ == "__main__":
    unittest.main()
