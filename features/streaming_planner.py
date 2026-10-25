"""Bounded, deterministic current-matchup planning. No provider or storage I/O."""

from collections import defaultdict
from copy import deepcopy
import hashlib
import json

import pandas as pd

from features.league_rules import roster_legal, validate_rules
from features.recent_form import streaming_projection, timestamp
from features.usable_games import UNAVAILABLE, effective_time, optimize_lineup, prepare_schedule


MAX_MOVES = 3
MAX_CANDIDATES = 12
BEAM_WIDTH = 8
MAX_DAYS = 14
BUDGET_KEYS = ("acquisitions_remaining", "matchup_acquisitions_remaining",
               "season_acquisitions_remaining")


def _prepare(snapshot, protected_ids=(), window=None):
    s = deepcopy(snapshot)
    if s.get("schema_version") != 1:
        raise ValueError("Unsupported streaming snapshot schema")
    rules = validate_rules(s["rules"])
    if rules["acquisition_timing"] != "SAME_DAY":
        raise ValueError("Streaming plans currently require SAME_DAY acquisition timing")
    if s.get("mode") == "historical_demo":
        raise ValueError("Streaming plans require a saved decision snapshot, not a reconstructed historical demo")
    for key in BUDGET_KEYS:
        if key in rules and rules[key] is not None and (type(rules[key]) is not int or rules[key] < 0):
            raise ValueError(f"Unknown {key}; provide a verified count or null for unlimited")
    players = s["players"]
    if any(type(p.get("player_id")) is not int for p in players):
        raise ValueError("Player IDs must be integers")
    if len({p["player_id"] for p in players}) != len(players):
        raise ValueError("Duplicate player IDs")
    roster = [p for p in players if p.get("rostered")]
    if not roster_legal(roster, rules):
        raise ValueError("Current roster exceeds capacity or position limits")
    protected = set(protected_ids)
    if protected - {p["player_id"] for p in roster}:
        raise ValueError("Protected IDs must belong to the selected roster")
    for p in players:
        if p["player_id"] in protected:
            p["protected"] = True
    logs = defaultdict(list)
    for log in s.get("game_logs", []):
        logs[log["player_id"]].append(log)
    s["recent_window"] = window if window is not None else s.get("recent_window", 6)
    forms = {p["player_id"]: streaming_projection(p, logs[p["player_id"]], s["as_of"],
                                                  window=s["recent_window"]) for p in players}
    context = prepare_schedule(s)
    if s.get("pool_complete") is not True:
        context["issues"].append("Available player pool incomplete")
    if context["days"]:
        today = context["now"].tz_convert(context["zone"]).date().isoformat()
        span = (pd.Timestamp(max(context["days"])) - pd.Timestamp(today)).days + 1
        if span > MAX_DAYS or len(context["days"]) > MAX_DAYS:
            raise ValueError("Streaming planning horizon is limited to 14 days")
    return s, rules, roster, forms, context


def _budget(rules, consumed):
    return {key: None if rules[key] is None else rules[key] - consumed
            for key in BUDGET_KEYS if key in rules}


def _daily_budget(context, rules, actions):
    today = context["now"].tz_convert(context["zone"]).date().isoformat()
    limit = rules.get("daily_acquisition_limit")
    ledger = []
    for day in context["days"]:
        used = (rules.get("daily_acquisitions_used") or 0) if day == today else 0
        consumed = sum(a["date"] == day for a in actions)
        ledger.append({"date": day, "limit": limit, "previously_used": used,
                       "planned": consumed, "remaining": None if limit is None else max(0, limit - used - consumed)})
    return ledger


def _move_time(add, drop, day, context, rules):
    now = context["now"]
    local = now.tz_convert(context["zone"])
    requested = max(now, pd.Timestamp(day, tz=context["zone"]).tz_convert("UTC"))
    day_rules = dict(rules, daily_acquisitions_used=(rules.get("daily_acquisitions_used", 0)
                                                   if day == local.date().isoformat() else 0))
    if day_rules.get("daily_acquisition_limit") == 0:
        raise ValueError("Daily acquisition limit exhausted")
    effective = effective_time(add, drop, dict(context, now=requested), day_rules)
    if effective.tz_convert(context["zone"]).date().isoformat() != day:
        raise ValueError("Move cannot become effective on this date")
    return effective


def _evaluate(prepared, actions):
    """Replay every transaction and recompute all points; never trust plan totals."""
    s, rules, original, forms, context = prepared
    if len(actions) > MAX_MOVES:
        raise ValueError("Too many moves")
    if any(v is not None and v < 0 for v in _budget(rules, len(actions)).values()):
        raise ValueError("Acquisition budget exceeded")
    players = {p["player_id"]: p for p in s["players"]}
    roster = list(original)
    seen = {p["player_id"] for p in roster}
    by_day = {}
    previous = ""
    for action in actions:
        if set(action) != {"date", "add_id", "drop_id", "effective_at"}:
            raise ValueError("Unexpected action fields")
        day = action["date"]
        if day not in context["days"] or day <= previous:
            raise ValueError("Plans allow one move per scoring date, in chronological order")
        if type(action["add_id"]) is not int or (action["drop_id"] is not None and type(action["drop_id"]) is not int):
            raise ValueError("Invalid transaction IDs")
        by_day[day] = action
        previous = day
    rows, issues, points, starts = [], list(context["issues"]), 0.0, 0
    for day, games in context["days"].items():
        action = by_day.get(day)
        add = drop = effective = None
        if action:
            add = players.get(action["add_id"])
            if not add or add["player_id"] in seen or add.get("availability") != "FREEAGENT":
                raise ValueError("Only original confirmed free agents may be acquired, once each")
            drop = next((p for p in roster if p["player_id"] == action["drop_id"]), None)
            if action["drop_id"] is not None and (drop is None or drop.get("undroppable") is not False
                                                  or drop.get("protected") or drop.get("lineup_slot") == "IR"):
                raise ValueError("Illegal or protected drop")
            after = [p for p in roster if p is not drop] + [add]
            if not roster_legal(after, rules):
                raise ValueError("Roster or position limit")
            effective = _move_time(add, drop, day, context, rules)
            if action["effective_at"] != effective.isoformat():
                raise ValueError("Invalid effective time")
            seen.add(add["player_id"])
        result = optimize_lineup(roster, dict(context, days={day: games}), rules, forms,
                                 add, drop, effective)
        for p in roster + ([add] if add else []):
            if p.get("lineup_slot") != "IR" and not p.get("injury_status"):
                issues.append(f"{p['player_id']}: unknown injury availability")
            if p.get("lineup_slot") != "IR" and forms[p["player_id"]]["fallback_reason"]:
                issues.append(f"{p['player_id']}: {forms[p['player_id']]['fallback_reason']}")
        points += result["points"]
        starts += result["starts"]
        rows.extend(result["rows"])
        issues.extend(result["issues"])
        if action:
            roster = after
    return {"candidate_points": points, "candidate_starts": starts, "calendar": rows,
            "issues": sorted(set(issues)), "roster": roster}


def validate_plan(snapshot, plan):
    """Public boundary for imported/model-selected plans: validate IDs, timing, totals."""
    prepared = _prepare(snapshot)
    result = _evaluate(prepared, plan["actions"])
    expected_id = (hashlib.sha256(json.dumps(plan["actions"], sort_keys=True).encode()).hexdigest()[:16]
                   if plan["actions"] else "hold")
    if plan.get("plan_id") != expected_id:
        raise ValueError("Plan ID does not match its actions")
    baseline = _evaluate(prepared, [])
    expected = {"candidate_points": result["candidate_points"],
                "candidate_starts": result["candidate_starts"],
                "baseline_points": baseline["candidate_points"],
                "net_gain": result["candidate_points"] - baseline["candidate_points"],
                "acquisitions_consumed": len(plan["actions"])}
    for key, value in expected.items():
        if type(plan.get(key)) not in (int, float) or plan[key] != value:
            raise ValueError(f"Invalid plan number: {key}")
    if json.dumps(plan.get("budget_after"), sort_keys=True) != json.dumps(_budget(prepared[1], len(plan["actions"])), sort_keys=True):
        raise ValueError("Invalid budget balance")
    if json.dumps(plan.get("daily_budget"), sort_keys=True) != json.dumps(_daily_budget(prepared[4], prepared[1], plan["actions"]), sort_keys=True):
        raise ValueError("Invalid daily budget ledger")
    expected_status = "partial" if result["issues"] or baseline["issues"] else "available"
    if plan.get("status") != expected_status:
        raise ValueError("Invalid plan coverage status")
    return result


def plan_streaming(snapshot, *, max_moves=2, protected_ids=(), window=None):
    """Beam search by day AND acquisition count, preserving reserving alternatives."""
    if type(max_moves) is not int or not 0 <= max_moves <= MAX_MOVES:
        raise ValueError("max_moves must be between 0 and 3")
    prepared = _prepare(snapshot, protected_ids, window)
    s, rules, roster, forms, context = prepared
    baseline = _evaluate(prepared, [])
    candidates = [p for p in s["players"] if not p.get("rostered")
                  and p.get("availability") == "FREEAGENT" and p.get("available_at") is not None
                  and p.get("injury_status") not in UNAVAILABLE
                  and forms[p["player_id"]]["streaming_ppg"] is not None]
    candidates.sort(key=lambda p: (-forms[p["player_id"]]["streaming_ppg"], p["player_id"]))
    pool_size = len(candidates)
    candidates = candidates[:MAX_CANDIDATES]
    # Each node is a complete hold-after-last-move scenario, not an independent
    # list of recommendations that could overspend the common budget.
    nodes = [((), baseline)]
    for day in context["days"]:
        expanded = list(nodes)  # no move today: explicitly reserve capacity
        for actions, evaluation in nodes:
            if len(actions) >= max_moves:
                continue
            if any(v is not None and v < 1 for v in _budget(rules, len(actions)).values()):
                continue
            current = evaluation["roster"]
            drops = [p for p in current if p.get("undroppable") is False
                     and not p.get("protected") and p.get("lineup_slot") != "IR"]
            options = ([None] if sum(p.get("lineup_slot") != "IR" for p in current) < rules["roster_capacity"] else []) + drops
            for add in candidates:
                if any(a["add_id"] == add["player_id"] for a in actions):
                    continue
                for drop in options:
                    try:
                        effective = _move_time(add, drop, day, context, rules)
                        action = {"date": day, "add_id": add["player_id"],
                                  "drop_id": drop["player_id"] if drop else None,
                                  "effective_at": effective.isoformat()}
                        proposal = actions + (action,)
                        result = _evaluate(prepared, proposal)
                    except (ValueError, TypeError):
                        continue
                    expanded.append((proposal, result))
        buckets = defaultdict(dict)
        for actions, evaluation in expanded:
            identity = json.dumps(actions, sort_keys=True)
            buckets[len(actions)][identity] = (actions, evaluation)
        nodes = []
        for bucket in buckets.values():
            nodes.extend(sorted(bucket.values(), key=lambda n: (bool(n[1]["issues"]),
                         -n[1]["candidate_points"], json.dumps(n[0], sort_keys=True)))[:BEAM_WIDTH])
    plans = []
    for actions, evaluation in nodes:
        identity = json.dumps(actions, sort_keys=True)
        issues = sorted(set(baseline["issues"] + evaluation["issues"]))
        plans.append({"plan_id": "hold" if not actions else hashlib.sha256(identity.encode()).hexdigest()[:16],
                      "actions": list(actions), "baseline_points": baseline["candidate_points"],
                      "candidate_points": evaluation["candidate_points"],
                      "net_gain": evaluation["candidate_points"] - baseline["candidate_points"],
                      "candidate_starts": evaluation["candidate_starts"],
                      "acquisitions_consumed": len(actions), "budget_after": _budget(rules, len(actions)),
                      "daily_budget": _daily_budget(context, rules, actions),
                      "status": "partial" if issues else "available", "issues": issues,
                      "calendar": evaluation["calendar"]})
    plans.sort(key=lambda p: (p["status"] != "available", -p["net_gain"],
                              p["acquisitions_consumed"], p["plan_id"]))
    selected = next((p["plan_id"] for p in plans if p["status"] == "available"), "hold")
    limitations = list(s.get("limitations", [])) + [
        "Bounded beam search; not a globally optimal weekly plan",
        "At most one move per day; confirmed free agents only; no reacquisition of dropped players",
        "Future availability assumes no other manager claims the player; refresh before executing",
        "Current injury status held constant; matchup/injury context does not alter numerical projections",
        "Unspent acquisitions are shown explicitly; no modeled future opportunity value",
        "Rest-of-season values are tradeoff evidence, not an automatic core-player protection rule",
    ]
    if pool_size > MAX_CANDIDATES:
        limitations.append(f"Candidate pool truncated from {pool_size} to {MAX_CANDIDATES} by streaming PPG")
    if any(k not in rules for k in BUDGET_KEYS[1:]):
        limitations.append("Legacy snapshot has only the combined matchup/season allowance")
    return {"schema_version": 1, "snapshot": s, "plans": plans,
            "deterministic_plan_id": selected, "selected_plan_id": selected,
            "recommended": any(p["plan_id"] == selected and p["status"] == "available" for p in plans),
            "search": {"max_moves": max_moves, "candidate_limit": MAX_CANDIDATES,
                       "beam_width_per_move_count": BEAM_WIDTH}, "limitations": limitations}
