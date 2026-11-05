"""Argument parsing and an opt-in terminal wizard; no provider or storage I/O."""

import argparse
from datetime import date
import os
from pathlib import Path
import shlex
import sys


def build_parser():
    parser = argparse.ArgumentParser(description="FantasyBball-Analysis pipeline")
    parser.add_argument(
        "--mode",
        choices=("weekly", "draft", "waivers", "startsit", "news-scan"),
        default="weekly",
        help=(
            "weekly (default): full retrospective + predictive run. "
            "draft / waivers / startsit: a single forward-looking decision report. "
            "news-scan: save ESPN blurbs for flagged players to the news history only."
        ),
    )
    parser.add_argument(
        "--week",
        type=int,
        default=None,
        help="fantasy week for waivers/startsit (default: current week)",
    )
    parser.add_argument(
        "--team",
        type=int,
        default=None,
        help="your team id for startsit or the waiver advisor (default: ESPN_TEAM_ID)",
    )
    parser.add_argument("--recent-window", type=int, choices=(5, 6), default=None,
                        help="recent appearances used by the waiver advisor")
    parser.add_argument("--protect", type=int, action="append", default=[], metavar="PLAYER_ID",
                        help="protect a rostered player from waiver drops (repeatable)")
    parser.add_argument("--waiver-config", type=Path, help="JSON rule/player overrides for live waiver advice")
    parser.add_argument("--snapshot", type=Path, help="replay a saved waiver snapshot without ESPN or MySQL")
    parser.add_argument("--basic-waivers", action="store_true", help="use the original non-roster-aware waiver board")
    parser.add_argument("--historical-date", help="reconstruct a previous-season waiver demo at local midnight (YYYY-MM-DD)")
    parser.add_argument("--season", type=int, help="ESPN season ending year for a historical demo or explicit draft capture")
    parser.add_argument("--capture-draft-projections", action="store_true",
                        help="with --mode draft, --season and --output-dir: append observed ESPN forecasts for stored draft picks")
    parser.add_argument("--output-dir", type=Path, help="directory for waiver outputs, or a new directory for explicit draft capture")
    parser.add_argument("--streaming-plan", action="store_true", help="plan a bounded sequence of current-matchup acquisitions")
    parser.add_argument("--max-moves", type=int, choices=range(4), help="streaming search depth, 0-3 (default 2)")
    parser.add_argument("--streaming-context", type=Path, help="dated injury/opponent context JSON")
    parser.add_argument("--streaming-news", action="store_true",
                        help="add recent ESPN injury blurbs to the context (network; no cookies sent)")
    parser.add_argument("--claude-review", action="store_true", help="opt in to one bounded Claude comparison request")
    parser.add_argument("--claude-model", help="explicit Claude model ID for optional review")
    parser.add_argument("--interactive", action="store_true",
                        help="choose a mode and options from a numbered menu (use on its own)")
    return parser


def validate_args(parser, args):
    if args.mode == "news-scan" and (args.team is not None or args.week is not None):
        parser.error("--mode news-scan covers the whole league; it does not accept --team or --week")
    if args.capture_draft_projections:
        if args.mode != "draft" or args.season is None or args.output_dir is None:
            parser.error("--capture-draft-projections requires --mode draft, explicit --season and --output-dir")
        if not 2000 <= args.season <= 9999:
            parser.error("Draft capture --season must be a season ending year from 2000 to 9999")
        if args.team is not None or args.week is not None:
            parser.error("Draft capture does not accept --team or --week")
    if args.streaming_plan and (args.mode != "waivers" or args.basic_waivers or args.historical_date):
        parser.error("--streaming-plan requires roster-aware waivers or a saved snapshot; historical demos are unsupported")
    if (args.max_moves is not None or args.streaming_context or args.streaming_news or args.claude_review or args.claude_model) and not args.streaming_plan:
        parser.error("Streaming options require --streaming-plan")
    if args.streaming_news and args.snapshot:
        parser.error("--streaming-news cannot be combined with --snapshot; replays stay offline, so pass the exported streaming_context.json")
    if (args.snapshot or args.waiver_config or args.protect or args.basic_waivers or args.recent_window is not None or args.historical_date) and args.mode != "waivers":
        parser.error("Waiver options require --mode waivers")
    if args.basic_waivers and (args.snapshot or args.waiver_config or args.protect or args.recent_window is not None or args.historical_date):
        parser.error("--basic-waivers cannot be combined with advisor options")
    if args.season is not None and not args.historical_date and not args.capture_draft_projections:
        parser.error("--season requires --historical-date or --capture-draft-projections")
    if args.output_dir and args.mode != "waivers" and not args.capture_draft_projections:
        parser.error("--output-dir requires waivers mode or --capture-draft-projections")
    if args.historical_date and (args.snapshot or args.waiver_config):
        parser.error("--historical-date cannot be combined with --snapshot or --waiver-config")


class Cancelled(Exception):
    """The user left the wizard before running anything."""


def _ask(label, *, default=None, convert=str):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value.lower() in {"q", "quit", "cancel"}:
            raise Cancelled
        if not value and default is not None:
            value = str(default)
        try:
            return convert(value)
        except ValueError as exc:
            print(exc)


def _integer(value, *, minimum=1, maximum=None):
    try:
        number = int(value)
    except ValueError:
        raise ValueError("Enter a whole number.") from None
    if number < minimum or (maximum is not None and number > maximum):
        limit = f" through {maximum}" if maximum is not None else " or greater"
        raise ValueError(f"Enter {minimum}{limit}.")
    return number


def _yes_no(value):
    if value.lower() in {"y", "yes"}:
        return True
    if value.lower() in {"n", "no"}:
        return False
    raise ValueError("Enter y or n.")


def _file(value):
    path = Path(value).expanduser()
    if not value or not path.is_file():
        raise ValueError("Enter the path to an existing file.")
    return str(path)


def _model(value):
    if not value or any(char.isspace() for char in value):
        raise ValueError("Enter a model ID without spaces (not an API key).")
    return value


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError("Enter a valid date in YYYY-MM-DD format.") from None
    return value


def interactive_argv():
    """Collect ordinary CLI arguments without reading providers or running reports."""
    print("\nFantasy Basketball — choose a report")
    print("  1. Weekly pipeline (updates MySQL, writes reports, may email)")
    print("  2. Draft board")
    print("  3. Waiver add/drop advisor")
    print("  4. Start/sit recommendations")
    print("  5. Streaming plan (optional Claude review)")
    print("  6. Basic waiver board")
    print("  7. Replay a saved waiver snapshot")
    print("  8. Historical waiver demo")
    print("  0. Cancel")
    print("Enter q at any prompt to cancel. Live reports use your existing .env settings.")
    choice = _ask("Mode", default=0,
                  convert=lambda v: _integer(v, minimum=0, maximum=8))
    if choice == 0:
        raise Cancelled
    mode = {1: "weekly", 2: "draft", 4: "startsit"}.get(choice, "waivers")
    argv = ["--mode", mode]

    def option(flag, label, *, default=None, convert=str, optional=False):
        converter = (lambda v: None if not v else convert(v)) if optional else convert
        value = _ask(label, default=default, convert=converter)
        if value is not None:
            argv.extend([flag, str(value)])

    streaming = choice == 5
    if choice == 7:
        option("--snapshot", "Snapshot JSON path", convert=_file)
        streaming = _ask("Build a streaming plan? (y/n)", default="n", convert=_yes_no)
    elif choice in {3, 4, 5, 8}:
        # Invalid configured defaults are explained and can be replaced at the prompt.
        option("--team", "Team ID", default=os.environ.get("ESPN_TEAM_ID"), convert=_integer)
    if choice in {3, 4, 5, 6}:
        option("--week", "Fantasy week (Enter for current)", convert=_integer, optional=True)
    if choice == 6:
        argv.append("--basic-waivers")
    if choice == 8:
        option("--historical-date", "Historical date (YYYY-MM-DD)", convert=_date)
        option("--season", "Season ending year", default=os.environ.get("ESPN_YEAR"),
               convert=lambda v: _integer(v, minimum=2000, maximum=9999))
    if choice in {3, 5, 7, 8}:
        option("--recent-window", "Recent appearances (5 or 6; Enter for stored/default window)",
               convert=lambda v: _integer(v, minimum=5, maximum=6), optional=True)

        def player_ids(value):
            return [_integer(part) for part in value.replace(",", " ").split()]

        for player_id in _ask("Protected player IDs, separated by spaces (Enter for none)", convert=player_ids):
            argv.extend(["--protect", str(player_id)])
        if choice in {3, 5}:
            option("--waiver-config", "Rule overrides JSON path (Enter for none)", convert=_file, optional=True)
    if streaming:
        argv.append("--streaming-plan")
        option("--max-moves", "Maximum acquisitions (0–3)", default=2,
               convert=lambda v: _integer(v, minimum=0, maximum=3))
        option("--streaming-context", "Dated context JSON path (Enter for none)", convert=_file, optional=True)
        if choice == 5 and _ask("Add recent ESPN injury news (network, no cookies)? (y/n)", default="n", convert=_yes_no):
            argv.append("--streaming-news")
        if _ask("Request one Claude comparison (may incur API charges)? (y/n)", default="n", convert=_yes_no):
            argv.append("--claude-review")
            option("--claude-model", "Claude model ID (uses your configured API credentials)", convert=_model)
    if mode == "waivers":
        option("--output-dir", "Output directory (Enter for existing mode default)",
               convert=lambda v: str(Path(v).expanduser()), optional=True)
    return argv


def parse_args(argv=None):
    """Return the parser and validated options; prompting is strictly opt-in."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interactive:
        if argv != ["--interactive"]:
            parser.error("Use --interactive on its own; use the existing flags for advanced scripted runs.")
        if not sys.stdin.isatty():
            parser.error("--interactive requires a terminal; use --mode and other flags for scripts.")
        try:
            selected = interactive_argv()
            args = parser.parse_args(selected)
            validate_args(parser, args)
            print("\nEquivalent command:\n  " + shlex.join([sys.executable, "main.py", *selected]))
            if not _ask("Run this report now? (y/n)", default="n", convert=_yes_no):
                raise Cancelled
        except (Cancelled, EOFError, KeyboardInterrupt):
            print("\nCancelled. No report was run.")
            parser.exit(0)
    validate_args(parser, args)
    return parser, args
