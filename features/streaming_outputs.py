"""CLI orchestration and inspectable exports for the streaming planner."""

import json
from pathlib import Path

import pandas as pd

from features.streaming_context import normalize_context
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


def run_streaming(snapshot, *, max_moves=2, protected_ids=(), window=None,
                  context_document=None, claude=False, model=None, output_dir=None):
    result = plan_streaming(snapshot, max_moves=max_moves, protected_ids=protected_ids, window=window)
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


def explanation(result):
    review = result["review"]
    lines = [f"As of {result['snapshot']['as_of']} | team {result['snapshot']['team_id']} | matchup {result['snapshot']['week']}",
             f"Selected plan: {result['selected_plan_id']}; deterministic best: {result['deterministic_plan_id']}",
             f"Claude review: {review['status']}. {review['explanation']}"]
    if not result["recommended"]:
        lines.append("Partial evidence: no definitive recommendation.")
    plans = {p["plan_id"]: p for p in result["plans"]}
    players = {p["player_id"]: p for p in result["snapshot"]["players"]}
    sources = {e["id"]: e for e in result["context"]["entries"]}
    selected = plans[result["selected_plan_id"]]
    lines.append(f"Projected gain versus HOLD: {selected['net_gain']:+.1f}; "
                 f"acquisitions used by plan: {selected['acquisitions_consumed']}; "
                 f"remaining: {json.dumps(selected['budget_after'])}.")
    if not selected["actions"]:
        lines.append("HOLD: keep the current roster.")
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
                     f"remaining {json.dumps(plan['budget_after'])}.")
        for pid in item["player_ids"]:
            p = players[pid]
            lines.append(f"  {pid} {p['player_name']}: season VORP {p.get('vorp_ros')}; snapshot injury status {p.get('injury_status')}")
        for eid in item["evidence_ids"]:
            e = sources[eid]
            lines.append(f"  Supplied evidence {eid} ({e['observed_at']}, {e['source']}): {e['summary']}")
    lines.append(f"Missing fresh context: {len(result['context']['coverage'])} player/kind entries (see JSON coverage).")
    for e in result["context"]["entries"]:
        lines.append(f"Context {e['id']}: {e['status']} ({e['observed_at']}, {e['source']})")
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
