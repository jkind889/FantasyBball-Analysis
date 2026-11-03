"""CLI orchestration and inspectable exports for the streaming planner."""

import json
from pathlib import Path

import pandas as pd

from features.player_news import MAX_NEWS_PLAYERS, NEWS_ENDPOINT, select_news_entries
from features.streaming_context import dump_context, merge_context_entries, normalize_context
from features.streaming_planner import plan_streaming
from features.streaming_review import review_plans


REASON_TEXT = {
    "reserve_acquisitions": "Preserves acquisitions compared with a supplied higher-use plan",
    "higher_projected_gain": "Has higher modeled gain than a supplied alternative",
    "injury_caution": "Consider the cited injury evidence before using this plan",
    "matchup_context": "Consider the cited descriptive opponent history and its sample size",
    "hold": "Keep the current roster and reserve acquisitions",
    "season_value_tradeoff": "Compare incoming/outgoing season value before a short-term move",
}

REVIEW_STATUS_TEXT = {
    "disabled": "Not requested; no API request sent.",
    "missing_key_or_model": "Skipped before API request: configure an API key and model ID.",
    "insufficient_plan_data": "Skipped before API request: no evidence-complete plans. See blocking reasons below.",
    "no_alternatives": "Skipped before API request: only one evidence-complete plan.",
    "input_bound_exceeded": "Skipped before API request: comparison exceeds the input size limit.",
    "error_or_invalid_output": "Review failed validation or the request failed. Deterministic selection retained. Without a request diagnostic below, request dispatch is not recorded.",
    "validated": "Response validated; selected a supplied, independently checked plan.",
}


def _allowance(value):
    return "unlimited" if value is None else str(value)


def console_summary(frame):
    """Format only the terminal table; preserve numeric/null export contracts."""
    frame = frame.drop(columns=["actions", "daily_budget", "issues"]).copy()
    for column in ("acquisitions_remaining", "matchup_acquisitions_remaining", "season_acquisitions_remaining"):
        if column in frame:
            frame[column] = frame[column].map(lambda v: "unlimited" if pd.isna(v) else str(int(v)))
    return frame


def _issue_detail(issue, players):
    identifier, separator, reason = issue.partition(": ")
    if separator and identifier.isdigit() and int(identifier) in players:
        issue = f"{players[int(identifier)]['player_name']} [{identifier}]: {reason}"
    if "Schedule coverage or scoring-period mapping is incomplete" in issue:
        action = "Verify the complete matchup's daily scoring IDs and schedule; supply matchup_scoring_periods only after verification."
    elif "Unknown schedule:" in issue:
        action = "Verify this player's NBA team assignment and schedule in the provider data."
    elif "fewer than three fresh appearances" in issue:
        action = "Baseline-only estimate; at least three verified appearances within 21 days are required for recent form."
    elif "missing or incomplete game logs" in issue:
        action = "Check current-season player-card game logs and their scoring-period/date mapping."
    elif "missing baseline projection" in issue:
        action = "Check baseline projection availability; do not substitute zero for missing data."
    elif "Available player pool incomplete" in issue:
        action = "Refresh and verify complete free-agent pagination."
    elif "unknown injury availability" in issue:
        action = "Verify the player's current injury availability in the provider data."
    else:
        action = "Verify the corresponding snapshot inputs before treating this plan as a recommendation."
    return f"{issue.rstrip('.')}. {action}"


def _blocking_lines(result, players):
    partial = [p for p in result["plans"] if p["status"] != "available"]
    if not partial:
        return []
    # A shared legend avoids repeating roster/schedule problems for every plan.
    issues = sorted({issue for plan in partial for issue in plan["issues"]})
    codes = {issue: f"B{index}" for index, issue in enumerate(issues, 1)}
    visible = issues[:20]
    lines = ["Blocking reasons (partial gains are provisional; no recommendation for these plans):"]
    for issue in visible:
        lines.append(f"  {codes[issue]}: {_issue_detail(issue, players)}")
    if len(issues) > len(visible):
        lines.append(f"  {len(issues) - len(visible)} additional reasons omitted; full details are in streaming_plans.csv (issues) and streaming_plan.json.")
    for plan in partial:
        shown = [codes[issue] for issue in visible if issue in plan["issues"]]
        omitted = len(set(plan["issues"]) - set(visible))
        detail = ", ".join(shown) or "see exported issues"
        if omitted:
            detail += f"; {omitted} additional reasons in exports"
        lines.append(f"  Plan {plan['plan_id']} (provisional gain {plan['net_gain']:+.1f}): {detail}")
    return lines


def news_priority(result):
    """Players in candidate moves first (usable plans before partial), then the roster."""
    ordered = sorted(result["plans"], key=lambda p: p["status"] != "available")
    ids = [pid for plan in ordered for action in plan["actions"] for pid in (action["add_id"], action["drop_id"])]
    ids += [p["player_id"] for p in result["snapshot"]["players"] if p.get("rostered")]
    return list(dict.fromkeys(pid for pid in ids if pid is not None))


def add_player_news(result, document, news_fetcher):
    """Add dated injury blurbs to the context; a news failure never blocks planning."""
    snapshot = result["snapshot"]
    normalize_context(snapshot, document)  # Reject a bad caller file before any request.
    priority = news_priority(result)
    requested = priority[:MAX_NEWS_PLAYERS]
    news = {"endpoint": NEWS_ENDPOINT, "status": "ok", "requested": len(requested),
            "not_requested": len(priority) - len(requested), "entries_added": 0, "entries_dropped": 0, "players": [],
            "limitations": ["ESPN's news feed is undocumented; published times are source-declared",
                            "Injury blurbs are chosen by a headline heuristic and can be missed or misrouted",
                            "No blurb does not mean a player is healthy; news older than 24 hours is not used"]}
    try:
        fetched = news_fetcher(requested)
        names = {p["player_id"]: p.get("player_name") for p in snapshot["players"]}
        entries, news["players"] = select_news_entries(fetched, names, as_of=snapshot["as_of"])
    except Exception as exc:
        return document, dict(news, status="error", error_type=type(exc).__name__)
    document, news["entries_added"], news["entries_dropped"] = merge_context_entries(snapshot, document, entries)
    return document, news


def run_streaming(snapshot, *, max_moves=2, protected_ids=(), window=None,
                  context_document=None, claude=False, model=None, output_dir=None, news_fetcher=None):
    result = plan_streaming(snapshot, max_moves=max_moves, protected_ids=protected_ids, window=window)
    if news_fetcher is not None:
        context_document, result["news"] = add_player_news(result, context_document, news_fetcher)
    result["context"] = normalize_context(result["snapshot"], context_document)
    result["review"] = review_plans(result, result["context"], enabled=claude, model=model)
    result["selected_plan_id"] = result["review"]["selected_plan_id"]
    if output_dir is not None:
        write_streaming(result, output_dir)
    return result


def summary_frame(result):
    rows = []
    for rank, plan in enumerate(result["plans"], 1):
        rows.append({"rank": rank, "plan_id": plan["plan_id"],
                     "selected": plan["plan_id"] == result["selected_plan_id"],
                     "recommended": result["recommended"] and plan["plan_id"] == result["selected_plan_id"],
                     "status": plan["status"], "net_gain": plan["net_gain"],
                     "candidate_points": plan["candidate_points"],
                     "acquisitions_consumed": plan["acquisitions_consumed"],
                     **plan["budget_after"], "actions": json.dumps(plan["actions"]),
                     "daily_budget": json.dumps(plan["daily_budget"]),
                     "issues": "; ".join(plan["issues"])})
    return pd.DataFrame(rows)


def _news_lines(news):
    if news is None:
        return []
    if news["status"] != "ok":
        return [f"ESPN news: unavailable ({news['error_type']}); no news context was added."]
    rows = news["players"]
    trimmed = sum(r["trimmed"] for r in rows) + news["entries_dropped"]
    return [f"ESPN news: requested {news['requested']} players ({news['not_requested']} over the request limit); "
            f"{sum(r['status'] != 'ok' for r in rows)} failed; dropped {sum(r['clips'] for r in rows)} clips and "
            f"{sum(r['other_types'] for r in rows)} other items; added {news['entries_added']} injury blurbs from the last "
            f"24 hours ({trimmed} over limits, {sum(r['older_injury_blurbs'] for r in rows)} older, "
            f"{sum(r['future_injury_blurbs'] for r in rows)} newer than the snapshot and not used; "
            f"{sum(r['name_missing'] for r in rows)} players without a name could not be checked).",
            *news["limitations"]]


def explanation(result):
    review = result["review"]
    lines = [f"As of {result['snapshot']['as_of']} | team {result['snapshot']['team_id']} | matchup {result['snapshot']['week']}"]
    if result["recommended"]:
        lines.append(f"Recommended plan: {result['selected_plan_id']}; evidence-complete deterministic best: {result['deterministic_plan_id']}")
    else:
        lines.append(f"Fallback selection: {result['selected_plan_id']} (HOLD). No evidence-complete plan; this is not a recommendation.")
    lines.append(f"Claude review: {review['status']}. {REVIEW_STATUS_TEXT.get(review['status'], review['explanation'])}")
    if review.get("evidence_trimmed"):
        lines.append(f"  {review['evidence_trimmed']} lowest-priority ESPN news items were withheld from the review to fit its input limit.")
    if review.get("request_diagnostic"):
        diag = review["request_diagnostic"]
        lines.append("  Request diagnostic: " + "; ".join(
            f"{key}={diag[key]}" for key in ("code", "http_status", "stop_reason") if key in diag)
            + (f"; usage={diag['usage']}" if diag.get("usage") else "")
            + ". A received HTTP status means a request was sent; check service-side usage before retrying.")
    plans ={p["plan_id"]: p for p in result["plans"]}
    players = {p["player_id"]: p for p in result["snapshot"]["players"]}
    sources = {e["id"]: e for e in result["context"]["entries"]}
    selected = plans[result["selected_plan_id"]]
    lines.append(f"Projected gain versus HOLD: {selected['net_gain']:+.1f}; "
                 f"acquisitions used by plan: {selected['acquisitions_consumed']}; "
                 "remaining: " + "; ".join(f"{key}={_allowance(value)}" for key, value in selected["budget_after"].items()) + ".")
    rules = result["snapshot"]["rules"]
    lines.append("Unlimited means no cap for that allowance (JSON null / blank CSV); daily limits still apply.")
    lines.append(f"Daily acquisition limit: {_allowance(rules.get('daily_acquisition_limit'))}; "
                 f"already used on snapshot day: {rules.get('daily_acquisitions_used') if rules.get('daily_acquisitions_used') is not None else 'not recorded'}.")
    if not selected["actions"]:
        lines.append("HOLD: keep the current roster." if result["recommended"] else "HOLD is the comparison baseline while evidence is incomplete.")
    lines.extend(_blocking_lines(result, players))
    for action in selected["actions"]:
        add = players[action["add_id"]]
        drop = players.get(action["drop_id"])
        lines.append(f"{action['date']} (effective {action['effective_at']}): add {add['player_name']} "
                     f"[{add['player_id']}]; " + (f"drop {drop['player_name']} [{drop['player_id']}]" if drop else "use empty roster spot"))
        lines.append(f"  Season VORP: add {add.get('vorp_ros')}; drop {drop.get('vorp_ros') if drop else 'none'}")
    for item in review["comparisons"]:
        plan = plans[item["plan_id"]]
        lines.append(f"{plan['plan_id']}: {REASON_TEXT[item['reason']]}. "
                     f"Modeled gain {plan['net_gain']:+.1f}; acquisitions {plan['acquisitions_consumed']}; "
                     "remaining " + "; ".join(f"{key}={_allowance(value)}" for key, value in plan["budget_after"].items()) + ".")
        for pid in item["player_ids"]:
            p = players[pid]
            lines.append(f"  {pid} {p['player_name']}: season VORP {p.get('vorp_ros')}; snapshot injury status {p.get('injury_status')}")
        for eid in item["evidence_ids"]:
            e = sources[eid]
            lines.append(f"  Supplied evidence {eid} ({e['observed_at']}, {e['source']}): {e['summary']}")
    relevant = {p["player_id"] for p in players.values() if p.get("rostered")}
    relevant.update(a["add_id"] for plan in result["plans"] for a in plan["actions"])
    missing = result["context"]["coverage"]
    relevant_missing = sum(e["player_id"] in relevant for e in missing)
    lines.append(f"Optional context: {relevant_missing} missing fresh player/kind entries for roster and retained-plan additions "
                 f"({len(missing)} across the full player pool). This count does not block Claude review; "
                 "missing evidence cannot support injury/matchup claims. Full coverage is in streaming_plan.json.")
    statuses = pd.Series([e["status"] for e in result["context"]["entries"]], dtype=object).value_counts().to_dict()
    if statuses:
        lines.append("Supplied context entries: " + "; ".join(f"{status}={count}" for status, count in sorted(statuses.items())) + ".")
    lines.extend(_news_lines(result.get("news")))
    lines.extend(result["limitations"] + result["context"]["limitations"])
    return "\n".join(lines) + "\n"


def write_streaming(result, output_dir):
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    summary_frame(result).to_csv(path / "streaming_plans.csv", index=False)
    calendar = [dict(plan_id=p["plan_id"], **row) for p in result["plans"] for row in p["calendar"]]
    pd.DataFrame(calendar).to_csv(path / "streaming_calendar.csv", index=False)
    (path / "streaming_plan.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    (path / "streaming_snapshot.json").write_text(json.dumps(result["snapshot"], indent=2, allow_nan=False) + "\n")
    (path / "streaming_review.txt").write_text(explanation(result))
    if result.get("news"):
        # Replays are offline: this file reproduces the same context without ESPN.
        snapshot = result["snapshot"]
        document = {"schema_version": 1, **{key: snapshot.get(key) for key in ("league_id", "season_year", "week")},
                    "entries": [{k: v for k, v in e.items() if k != "status"} for e in result["context"]["entries"]]}
        (path / "streaming_context.json").write_bytes(dump_context(document))
