"""Daily legal lineup assignments shared by waiver scenarios and CSV audit."""

from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from features.recent_form import number, timestamp


UNAVAILABLE = {"OUT", "SUSPENSION", "SUSPENDED", "INJURED_RESERVE"}
IGNORED_GAMES = {"CANCELED", "CANCELLED", "POSTPONED"}


def prepare_schedule(snapshot):
    now = timestamp(snapshot["as_of"])
    zone = snapshot["rules"]["timezone"]
    by_day = defaultdict(dict)
    known_teams, issues, seen = set(), [], set()
    for game in snapshot["games"]:
        known_teams.add(game["pro_team"])
        if game.get("fantasy_week") != snapshot["week"]:
            continue
        try:
            tipoff = timestamp(game.get("tipoff"))
        except (ValueError, TypeError):
            issues.append(f"Unknown tipoff for {game['pro_team']}")
            continue
        day = tipoff.tz_convert(zone).date().isoformat()
        if day < now.tz_convert(zone).date().isoformat():
            continue
        if game.get("status") in IGNORED_GAMES:
            continue
        key = (game["pro_team"], game["game_id"])
        if key in seen:
            continue
        seen.add(key)
        if game["pro_team"] in by_day[day]:
            raise ValueError("Multiple games per team on one scoring day are unsupported")
        by_day[day][game["pro_team"]] = dict(game, tipoff=tipoff)
    if snapshot.get("schedule_complete") is not True:
        issues.append("Schedule coverage or scoring-period mapping is incomplete")
    return {"now": now, "zone": zone, "days": dict(sorted(by_day.items())),
            "known_teams": known_teams, "issues": issues}


def effective_time(player, drop, context, rules):
    """Earliest modeled legal move; a locked drop waits until the next day."""
    now = context["now"]
    available = player.get("available_at")
    if player.get("availability") not in {"FREEAGENT", "WAIVERS"}:
        raise ValueError("unknown acquisition status")
    if available is None:
        raise ValueError("unknown acquisition/waiver effective time")
    effective = max(now, timestamp(available))
    if rules["acquisition_timing"] == "NEXT_DAY":
        effective = (effective.tz_convert(context["zone"]).normalize()
                     + pd.DateOffset(days=1)).tz_convert("UTC")
    today = now.tz_convert(context["zone"]).date()
    if (rules.get("daily_acquisition_limit") is not None
            and rules.get("daily_acquisitions_used", 0) >= rules["daily_acquisition_limit"]
            and effective.tz_convert(context["zone"]).date() == today):
        effective = (effective.tz_convert(context["zone"]).normalize()
                     + pd.DateOffset(days=1)).tz_convert("UTC")
    if rules["drop_lock"] == "FIRSTGAME_SCORINGPERIOD":
        day = effective.tz_convert(context["zone"]).date().isoformat()
        games = context["days"].get(day, {}).values()
        if games and min(g["tipoff"] for g in games) <= effective:
            effective = (effective.tz_convert(context["zone"]).normalize()
                         + pd.DateOffset(days=1)).tz_convert("UTC")
    if drop:
        day = effective.tz_convert(context["zone"]).date().isoformat()
        game = context["days"].get(day, {}).get(drop.get("pro_team"))
        if game and game["tipoff"] <= effective:
            effective = (effective.tz_convert(context["zone"]).normalize()
                         + pd.DateOffset(days=1)).tz_convert("UTC")
    return effective


def optimize_lineup(roster, context, rules, forms, add=None, drop=None,
                    effective=None):
    """Return future points, usable starts, rows and coverage issues.

    Started games have zero *future decision* contribution and reserve their
    current slot for today. All scenarios keep these assignments fixed.
    """
    rows, issues = [], list(context["issues"])
    now = context["now"]
    today = now.tz_convert(context["zone"]).date().isoformat()
    total, starts = 0.0, 0
    for day, games in context["days"].items():
        daily = list(roster)
        if add and day >= effective.tz_convert(context["zone"]).date().isoformat():
            daily = [p for p in daily if not drop or p["player_id"] != drop["player_id"]] + [add]
        daily.sort(key=lambda p: p["player_id"])
        remaining = list(rules["starting_slots"])
        available = []
        for p in daily:
            pid = p["player_id"]
            game = games.get(p.get("pro_team"))
            form = forms[pid]
            row = {"date": day, "player_id": pid, "player_name": p["player_name"],
                   "pro_team": p.get("pro_team"), "slot": "BENCH", "reason": "no game",
                   "projected_points": 0.0, "streaming_ppg": form["streaming_ppg"],
                   "projection_basis": "baseline-only" if form["fallback_reason"] else "recent form",
                   "game_id": game["game_id"] if game else None,
                   "tipoff": game["tipoff"].isoformat() if game else None,
                   "opponent": game.get("opponent") if game else None}
            rows.append(row)
            if p.get("pro_team") not in context["known_teams"]:
                row["reason"] = "unknown schedule"
                row["projected_points"] = None
                issues.append(f"Unknown schedule: {p['player_name']}")
                continue
            if not game:
                continue
            if day == today and game["tipoff"] <= now:
                row["reason"] = "locked / already started"
                slot = p.get("lineup_slot")
                if slot in rules["starting_slots"]:
                    if slot not in remaining:
                        raise ValueError(f"Too many locked assignments at {slot}")
                    remaining.remove(slot)
                    row["slot"] = slot
                elif slot not in {"BE", "IR"}:
                    issues.append(f"Unknown locked slot: {p['player_name']}")
                continue
            if add and pid == add["player_id"] and game["tipoff"] <= effective:
                row["reason"] = "pending acquisition / missed lock"
                continue
            if game.get("status") in {"FINAL", "IN_PROGRESS"}:
                row["reason"] = "already started"
                issues.append(f"Game status and future tipoff disagree: {game['game_id']}")
                continue
            if game.get("status") != "SCHEDULED":
                row["reason"] = "unknown game status"
                row["projected_points"] = None
                issues.append(f"Unknown game status: {game['game_id']}")
                continue
            if p.get("lineup_slot") == "IR" or p.get("injury_status") in UNAVAILABLE:
                row["reason"] = "unavailable (current injury/IR status held constant)"
                continue
            rate = number(form["streaming_ppg"])
            if rate is None or not p.get("eligible_slots"):
                row["reason"] = "unknown projection or eligibility"
                row["projected_points"] = None
                issues.append(f"Missing projection/eligibility: {p['player_name']}")
                continue
            row["reason"] = "bench: stronger options or no eligible slot"
            available.append((p, row, rate))
        # Zero-valued dummy rows allow empty slots, including negative projections.
        profit = np.zeros((len(available) + len(remaining), len(remaining)))
        profit[:len(available)] = -1e9
        for i, (p, row, rate) in enumerate(available):
            for j, slot in enumerate(remaining):
                if slot in p["eligible_slots"]:
                    profit[i, j] = rate
        used_slots = Counter()
        if remaining:
            rr, cc = linear_sum_assignment(profit, maximize=True)
            for i, j in zip(rr, cc):
                if i >= len(available) or profit[i, j] <= 0:
                    continue
                _, row, rate = available[i]
                row.update(slot=remaining[j], reason="recommended start", projected_points=rate)
                used_slots[remaining[j]] += 1
                total += rate
                starts += 1
        for slot, count in (Counter(remaining) - used_slots).items():
            for _ in range(count):
                rows.append({"date": day, "player_id": None, "player_name": "Empty slot",
                             "slot": slot, "reason": "no better eligible player",
                             "projected_points": 0.0})
    return {"points": total, "starts": starts, "rows": rows, "issues": sorted(set(issues))}
