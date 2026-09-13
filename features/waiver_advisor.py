"""Roster-aware single-move waiver recommendations from a saved snapshot."""

import hashlib
import json
from copy import deepcopy
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from features.league_rules import roster_legal, validate_rules
from features.recent_form import streaming_projection, timestamp
from features.usable_games import effective_time, optimize_lineup, prepare_schedule


def advise_waivers(snapshot, window=None, protected_ids=(), calendar_limit=20):
    """No network or writes; returns recommendations, calendar, form and rejects."""
    if snapshot.get("schema_version") != 1:
        raise ValueError("Unsupported waiver snapshot schema_version")
    snapshot = deepcopy(snapshot)
    window = window if window is not None else snapshot.get("recent_window", 6)
    snapshot["recent_window"] = window
    rules = validate_rules(snapshot["rules"])
    timestamp(snapshot["as_of"])
    players = sorted(snapshot["players"], key=lambda p: p["player_id"])
    if len({p["player_id"] for p in players}) != len(players):
        raise ValueError("Duplicate player IDs in snapshot")
    roster = [p for p in players if p.get("rostered")]
    if not roster_legal(roster, rules):
        raise ValueError("Current roster exceeds configured capacity or position limits")
    protected = set(protected_ids)
    if protected - {p["player_id"] for p in roster}:
        raise ValueError("Protected player IDs must belong to the selected roster")
    for player in players:
        if player["player_id"] in protected:
            player["protected"] = True
    grouped = defaultdict(list)
    for log in snapshot.get("game_logs", []):
        grouped[log["player_id"]].append(log)
    forms = {p["player_id"]: streaming_projection(
        p, grouped[p["player_id"]], snapshot["as_of"], window=window,
    ) for p in players}
    context = prepare_schedule(snapshot)
    if snapshot.get("pool_complete") is not True:
        context["issues"].append("Available player pool incomplete; comparison is partial")
    if snapshot.get("mode") == "historical_demo":
        context["issues"].append("Historical reconstruction: availability and eligibility assumptions are unverified")
    baseline = optimize_lineup(roster, context, rules, forms)
    snapshot_id = hashlib.sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
    meta = {"snapshot_id": snapshot_id, "as_of": snapshot["as_of"],
            "league_id": snapshot.get("league_id"), "season_year": snapshot.get("season_year"),
            "team_id": snapshot["team_id"], "week": snapshot["week"],
            "timezone": rules["timezone"], "selected_window": window}
    meta["analysis_mode"] = snapshot.get("mode", "live_snapshot")
    assumptions = list(snapshot.get("limitations", []))
    assumptions.append("Current injury status held constant through target matchup; no injury probability model")
    baseline_coverage = {forms[p["player_id"]]["fallback_reason"] for p in roster
                         if p.get("lineup_slot") != "IR"}
    baseline_coverage.discard("")
    hold_issues = baseline["issues"] + sorted(baseline_coverage)
    hold = dict(meta, action_id="hold", action="HOLD", add_id=None, drop_id=None,
                add_name=None, drop_name=None, baseline_points=baseline["points"],
                candidate_points=baseline["points"], net_gain=0.0,
                baseline_starts=baseline["starts"], candidate_starts=baseline["starts"],
                acquisitions_consumed=0, effective_at=snapshot["as_of"],
                status="partial" if hold_issues else "available", recommended=False,
                explanation="Hold current roster", limitations="; ".join(sorted(set(hold_issues + assumptions))))
    recommendations, rejected, scenarios = [hold], [], {}
    open_spot = sum(p.get("lineup_slot") != "IR" for p in roster) < rules["roster_capacity"]
    drops = [p for p in roster if p["player_id"] not in protected
             and not p.get("protected") and p.get("undroppable") is False
             and p.get("lineup_slot") != "IR"]
    for add in [p for p in players if not p.get("rostered")]:
        options = ([None] if open_spot else []) + drops
        if not options:
            rejected.append({"add_id": add["player_id"], "drop_id": None,
                             "reason": "No empty spot or verified legal unprotected drop"})
        for drop in options:
            rejection = {"add_id": add["player_id"], "drop_id": drop["player_id"] if drop else None}
            if rules["acquisitions_remaining"] == 0 or rules.get("daily_acquisition_limit") == 0:
                rejected.append(dict(rejection, reason="Acquisition limit exhausted"))
                continue
            candidate_roster = [p for p in roster if not drop or p["player_id"] != drop["player_id"]] + [add]
            if not roster_legal(candidate_roster, rules):
                rejected.append(dict(rejection, reason="Roster or position limit"))
                continue
            try:
                effective = effective_time(add, drop, context, rules)
            except ValueError as exc:
                rejected.append(dict(rejection, reason=str(exc)))
                continue
            result = optimize_lineup(roster, context, rules, forms, add, drop, effective)
            form = forms[add["player_id"]]
            issues = sorted(set(hold_issues + result["issues"] + ([form["fallback_reason"]] if form["fallback_reason"] else [])))
            conditional = add.get("availability") == "WAIVERS"
            status = "partial" if issues else "conditional" if conditional else "available"
            action_id = f"add-{add['player_id']}-drop-{drop['player_id'] if drop else 'none'}"
            daily_delta = defaultdict(float)
            for row in result["rows"]:
                daily_delta[row["date"]] += row.get("projected_points") or 0
            for row in baseline["rows"]:
                daily_delta[row["date"]] -= row.get("projected_points") or 0
            explanation = "; ".join(f"{day}: {delta:+.1f} points" for day, delta in daily_delta.items() if abs(delta) > 1e-8)
            if form["recent_weight"]:
                season_text = f" versus {form['season_avg_points']:.1f} for the season" if form["season_avg_points"] is not None else ""
                explanation = (f"{form['recent_mean']:.1f} points over {form['recent_games']} recent games"
                               f"{season_text}; streaming estimate {form['streaming_ppg']:.1f}. "
                               + (explanation or "No improvement to usable lineup points"))
            record = dict(meta, action_id=action_id, action="ADD_DROP" if drop else "ADD",
                          add_id=add["player_id"], drop_id=drop["player_id"] if drop else None,
                          add_name=add["player_name"], drop_name=drop["player_name"] if drop else None,
                          baseline_points=baseline["points"], candidate_points=result["points"],
                          net_gain=result["points"] - baseline["points"],
                          baseline_starts=baseline["starts"], candidate_starts=result["starts"],
                          acquisitions_consumed=1, effective_at=effective.isoformat(),
                          availability=add.get("availability"), conditional=conditional,
                          status=status, recommended=False,
                          add_vorp_ros=add.get("vorp_ros"), drop_vorp_ros=drop.get("vorp_ros") if drop else None,
                          explanation=explanation or "No improvement to usable lineup points",
                          limitations="; ".join(sorted(set(issues + assumptions + (["Waiver claim must be awarded"] if conditional else [])))))
            for prefix, player in (("add", add), ("drop", drop)):
                if player:
                    record.update({f"{prefix}_{key}": value for key, value in forms[player["player_id"]].items() if key != "player_id"})
            recommendations.append(record)
            scenarios[action_id] = (add, drop, effective)
    actionable = [r for r in recommendations if r["status"] == "available" and r["net_gain"] > 1e-8]
    best = min(actionable, key=lambda r: (-r["net_gain"], r["add_id"], r["drop_id"] or -1)) if actionable else hold
    best["recommended"] = not (best is hold and hold_issues)
    if best is hold:
        hold["explanation"] = ("Insufficient data for a definitive recommendation; inspect partial scenarios"
                               if hold_issues else "Hold: no confirmed available move improves the lineup; inspect conditional claims separately")
    recommendations.sort(key=lambda r: (not r["recommended"],
                                        {"available": 0, "conditional": 1, "partial": 2}[r["status"]],
                                        -r["net_gain"], r["acquisitions_consumed"], r["add_id"] or -1, r["drop_id"] or -1))
    calendar = [dict(meta, action_id="hold", **row) for row in baseline["rows"]]
    for r in [r for r in recommendations if r["action_id"] != "hold"][:calendar_limit]:
        result = optimize_lineup(roster, context, rules, forms, *scenarios[r["action_id"]])
        calendar.extend(dict(meta, action_id=r["action_id"], **row) for row in result["rows"])
    board = pd.DataFrame(recommendations)
    board.insert(0, "rank", range(1, len(board) + 1))
    return {"recommendations": board, "calendar": pd.DataFrame(calendar, columns=list(meta) + ["action_id", "date", "player_id", "player_name", "pro_team", "slot", "reason", "projected_points", "streaming_ppg", "projection_basis", "game_id", "tipoff", "opponent"]),
            "recent_form": pd.DataFrame([dict(meta, **{k: v for k, v in f.items() if k not in meta}) for f in forms.values()]),
            "rejected": pd.DataFrame(rejected, columns=["add_id", "drop_id", "reason"]),
            "snapshot": snapshot}


def write_advice(snapshot, result, output_dir):
    """Write only normalized inputs; no ESPN credentials or raw API responses."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat()
    for key, filename in {"recommendations": "waiver_recommendations.csv",
                          "calendar": "remaining_week_calendar.csv", "recent_form": "recent_form.csv",
                          "rejected": "waiver_rejected.csv"}.items():
        frame = result[key].copy()
        frame["generated_at"] = generated
        frame.to_csv(directory / filename, index=False)
    (directory / "waiver_snapshot.json").write_text(json.dumps(result["snapshot"], indent=2, allow_nan=False) + "\n")
