"""League-wide scan: save ESPN blurbs for players flagged with an injury status.

Only appends to the news history. It never touches MySQL, reports, email, plans or
Claude. A player can be missed by this scan if they are hurt and back between runs.
"""

from collections import Counter
import time

from features.player_news import HISTORY_PATH, fetch_player_news, save_news_history
from features.waiver_inputs import fetch_unowned_pool


MAX_SCAN_PLAYERS = 250
MAX_SCAN_SECONDS = 300
PAUSE_SECONDS = 0.25
MAX_CONSECUTIVE_FAILURES = 5
# Anything else counts as flagged, so a status ESPN adds later is captured, not skipped.
HEALTHY_STATUSES = {"ACTIVE"}


def flagged_players(raw_league, pool):
    """Flagged players in roster-then-ownership order, plus counts of every status.

    A missing status is unknown rather than healthy; it is counted, not scanned.
    """
    entries = [entry.get("playerPoolEntry", {}) for team in raw_league.get("teams", [])
               for entry in team.get("roster", {}).get("entries", [])] + list(pool)
    flagged, seen, statuses, unknown = {}, set(), Counter(), 0
    for entry in entries:
        player = entry.get("player") or {}
        player_id = player.get("id")
        if type(player_id) is not int or player_id in seen:
            continue
        seen.add(player_id)
        status = player.get("injuryStatus")
        if not isinstance(status, str) or not status:
            unknown += 1
            continue
        statuses[status] += 1
        if status not in HEALTHY_STATUSES:
            flagged[player_id] = player.get("fullName")
    return flagged, {"players_checked": len(seen), "statuses": dict(statuses), "unknown_status": unknown}


def fetch_flagged_news(player_ids, *, fetcher=fetch_player_news, sleep=time.sleep, clock=time.monotonic):
    """Fetch one player at a time; stop early if ESPN keeps failing or time runs out."""
    results, failures, stopped, started = {}, 0, None, clock()
    for index, player_id in enumerate(player_ids):
        if clock() - started > MAX_SCAN_SECONDS:
            stopped = "time_limit"
            break
        if index:
            sleep(PAUSE_SECONDS)
        results.update(fetcher([player_id]))
        ok = results.get(player_id, {}).get("status") == "ok"
        failures = 0 if ok else failures + 1
        if failures >= MAX_CONSECUTIVE_FAILURES:
            stopped = "repeated_failures"
            break
    return results, stopped


def run_scan(league, *, fetcher=fetch_player_news, history_path=HISTORY_PATH, sleep=time.sleep, clock=time.monotonic):
    """Read current statuses from ESPN, then save the flagged players' blurbs."""
    request = league.espn_request
    raw = request.get_league()
    pool, pool_complete = fetch_unowned_pool(request, raw["scoringPeriodId"])
    flagged, summary = flagged_players(raw, pool)
    requested = list(flagged)[:MAX_SCAN_PLAYERS]
    results, stopped = fetch_flagged_news(requested, fetcher=fetcher, sleep=sleep, clock=clock)
    ok = sum(result.get("status") == "ok" for result in results.values())
    history = save_news_history(results, flagged, history_path)
    complete = pool_complete and stopped is None and ok == len(flagged)
    return {**summary, "status": "ok" if complete else "partial", "pool_complete": pool_complete,
            "flagged": len(flagged), "over_limit": len(flagged) - len(requested),
            "requested": len(results), "not_attempted": len(requested) - len(results),
            "failed": len(results) - ok, "stopped": stopped, "history": history}


def format_summary(summary):
    history = summary["history"]
    lines = [f"News scan ({summary['status']}): {summary['flagged']} flagged of {summary['players_checked']} players; "
             f"statuses {summary['statuses']}, {summary['unknown_status']} with no status.",
             f"Fetched {summary['requested'] - summary['failed']} players; {summary['failed']} failed, "
             f"{summary['not_attempted']} not attempted, {summary['over_limit']} over the {MAX_SCAN_PLAYERS}-player limit.",
             f"History: saved {history['added']} new blurbs ({history['already_saved']} already saved)."]
    if history["unreadable_lines"]:
        lines.append(f"{history['unreadable_lines']} existing history lines could not be read and were left in place.")
    if summary["stopped"]:
        lines.append(f"Stopped early: {summary['stopped']}.")
    if not summary["pool_complete"]:
        lines.append("ESPN's free-agent pool was incomplete, so some flagged players may not have been seen.")
    return "\n".join(lines)
