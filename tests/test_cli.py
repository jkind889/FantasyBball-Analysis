"""Offline CLI coverage. Never import the executing main.py pipeline."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cli import parse_args


ROOT = Path(__file__).resolve().parents[1]


class CliTest(unittest.TestCase):
    def menu(self, answers, env=None):
        output = StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=answers), \
                patch.dict(os.environ, env or {}, clear=True), redirect_stdout(output):
            _, args = parse_args(["--interactive"])
        return args, output.getvalue()

    def test_legacy_defaults_and_flags_do_not_prompt(self):
        with patch("builtins.input", side_effect=AssertionError("Unexpected prompt")):
            self.assertEqual(parse_args([])[1].mode, "weekly")
            args = parse_args(["--mode", "waivers", "--team", "3", "--week", "5",
                               "--streaming-plan", "--max-moves", "0", "--recent-window", "5",
                               "--protect", "12", "--protect", "34", "--claude-review",
                               "--claude-model", "fixture-model", "--streaming-context", "context.json",
                               "--waiver-config", "rules.json", "--output-dir", "out"])[1]
        self.assertEqual((args.team, args.week, args.max_moves, args.recent_window), (3, 5, 0, 5))
        self.assertEqual(args.protect, [12, 34])
        self.assertTrue(args.claude_review)
        self.assertEqual(args.claude_model, "fixture-model")
        self.assertEqual(args.output_dir, Path("out"))

    def test_legacy_invalid_combinations(self):
        invalid = [
            ["--streaming-plan"], ["--claude-model", "fixture"],
            ["--mode", "draft", "--protect", "1"],
            ["--mode", "waivers", "--basic-waivers", "--snapshot", "x"],
            ["--mode", "waivers", "--season", "2026"],
            ["--mode", "waivers", "--historical-date", "2026-01-08", "--snapshot", "x"],
            ["--mode", "waivers", "--streaming-plan", "--max-moves", "4"],
        ]
        for argv in invalid:
            with self.subTest(argv=argv), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as exc:
                parse_args(argv)
            self.assertEqual(exc.exception.code, 2)

    def test_simple_modes_and_configured_team(self):
        cases = [
            (["1", "y"], "weekly"),
            (["2", "y"], "draft"),
            (["3", "", "", "", "", "", "", "y"], "waivers"),
            (["4", "", "", "y"], "startsit"),
            (["6", "5", "", "y"], "waivers"),
            (["8", "", "2026-01-08", "", "", "", "", "y"], "waivers"),
        ]
        for answers, mode in cases:
            with self.subTest(answers=answers):
                args, output = self.menu(answers, {"ESPN_TEAM_ID": "9", "ESPN_YEAR": "2026"})
                self.assertEqual(args.mode, mode)
                self.assertFalse(args.claude_review)
                self.assertIn("Equivalent command:", output)
                if answers[0] in {"3", "4", "8"}:
                    self.assertEqual(args.team, 9)
                if answers[0] == "6":
                    self.assertTrue(args.basic_waivers)
                    self.assertEqual(args.week, 5)
                if answers[0] == "8":
                    self.assertEqual(args.season, 2026)

    def test_streaming_claude_opt_in_and_typed_model(self):
        args, output = self.menu(["5", "3", "", "5", "12, 34", "", "0", "", "y",
                                  "", "two words", "fixture-model-v2", "my reports", "y"])
        self.assertTrue(args.streaming_plan)
        self.assertTrue(args.claude_review)
        self.assertEqual(args.claude_model, "fixture-model-v2")
        self.assertEqual(args.max_moves, 0)
        self.assertEqual(args.protect, [12, 34])
        self.assertIn("Enter a model ID without spaces", output)
        self.assertIn("'my reports'", output)

    def test_streaming_defaults_never_enable_claude(self):
        args, _ = self.menu(["5", "3", "", "", "", "", "", "", "", "", "y"])
        self.assertEqual(args.max_moves, 2)
        self.assertIsNone(args.recent_window)
        self.assertFalse(args.claude_review)
        self.assertIsNone(args.claude_model)

    def test_invalid_inputs_reprompt(self):
        args, output = self.menu(["oops", "9", "8", "-1", "3", "2026-02-30", "2026-01-08",
                                  "bad", "2026", "7", "5", "bad", "12 34", "", "maybe", "yes"])
        self.assertEqual(args.team, 3)
        self.assertEqual(args.protect, [12, 34])
        self.assertIn("Enter a valid date", output)
        self.assertIn("Enter y or n", output)

    def test_cancel_at_menu_or_later_without_run(self):
        for answers in ([""], ["0"], ["q"], [EOFError()], [KeyboardInterrupt()],
                        ["2", ""], ["5", "q"], ["2", KeyboardInterrupt()]):
            with self.subTest(answers=answers), self.assertRaises(SystemExit) as exc:
                self.menu(answers)
            self.assertEqual(exc.exception.code, 0)

    def test_piped_interactive_and_mixed_flags_rejected_without_input(self):
        with patch("builtins.input", side_effect=AssertionError("Unexpected prompt")), \
                patch("sys.stdin.isatty", return_value=False), redirect_stderr(StringIO()):
            for argv in (["--interactive"], ["--interactive", "--mode", "draft"]):
                with self.assertRaises(SystemExit) as exc:
                    parse_args(argv)
                self.assertEqual(exc.exception.code, 2)

    def test_snapshot_path_validation_and_stored_defaults(self):
        source = ROOT / "examples/waiver_snapshot.json"
        args, output = self.menu(["7", "missing-fixture-file", str(source), "", "", "", "", "y"],
                                 {"ESPN_TEAM_ID": "999"})
        self.assertEqual(args.snapshot, source)
        self.assertIsNone(args.team)
        self.assertIsNone(args.week)
        self.assertIsNone(args.recent_window)
        self.assertIn("existing file", output)

    def test_interactive_entrypoint_offline_snapshot(self):
        # Run main only in a child with guarded network/database access, synthetic
        # data and a temporary destination; do not read the user's .env.
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "reports"
            answers = ["7", str(ROOT / "examples/waiver_snapshot.json"), "y", "", "", "", "", "", str(out), "y"]
            script = '''
import builtins, json, runpy, socket, sys
from unittest.mock import patch
def forbidden(*args, **kwargs):
    raise AssertionError("Live I/O forbidden in menu test")
answers = json.loads(sys.argv[1])
main = sys.argv[2]
sys.argv = [main, "--interactive"]
with patch("dotenv.load_dotenv"), patch("builtins.input", side_effect=answers), patch("sys.stdin.isatty", return_value=True), patch("socket.socket.connect", side_effect=forbidden), patch("database.get_connection", side_effect=forbidden), patch("requests.sessions.Session.request", side_effect=forbidden):
    runpy.run_path(main, run_name="__main__")
'''
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
            run = subprocess.run([sys.executable, "-B", "-c", script, json.dumps(answers), str(ROOT / "main.py")],
                                 cwd=directory, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Claude review: disabled", run.stdout)
            self.assertTrue((out / "streaming_plans.csv").is_file())


if __name__ == "__main__":
    unittest.main()
