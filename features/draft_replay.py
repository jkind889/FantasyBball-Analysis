"""Offline, no-hindsight draft replay. No provider, database, or model imports.

Run ``python -m features.draft_replay snapshot.json [--output report.json]``.
See docs/draft_replay.md for the versioned, contemporaneous-capture contract.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


POSITIONS = {"PG", "SG", "SF", "PF", "C"}
SLOTS = {**{p: {p} for p in POSITIONS}, "G": {"PG", "SG"},
         "F": {"SF", "PF"}, "UT": POSITIONS, "SG/SF": {"SG", "SF"},
         "G/F": {"PG", "SG", "SF", "PF"}, "PF/C": {"PF", "C"},
         "F/C": {"SF", "PF", "C"}}
MAX_BYTES = 10 * 1024 * 1024


def _object(value, allowed, label, required=()):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) - set(allowed):
        raise ValueError(f"{label} has unsupported fields: {sorted(set(value) - set(allowed))}")
    if set(required) - set(value):
        raise ValueError(f"{label} is missing fields: {sorted(set(required) - set(value))}")
    return value


def _list(value, label, maximum=10000):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be an array of at most {maximum} items")
    return value


def _text(value, label, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _id(value, label):
    if type(value) is int and value >= 0:
        return str(value)
    return _text(value, label)


def _integer(value, label, minimum=1, maximum=100000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _number(value, label):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a finite number (not a Boolean)")
    return value


def _time(value, label, nullable=False):
    if value is None and nullable:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{label} must be an ISO timestamp with an explicit timezone") from None


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} must be true or false")
    return value


def _capture(section, cutoff, label):
    """Validate supplied provenance; return reasons it cannot establish history."""
    source = _text(section.get("source"), f"{label}.source", nullable=True)
    observed = _time(section.get("observed_at"), f"{label}.observed_at", nullable=True)
    reasons = []
    if source is None:
        reasons.append(f"{label}.source_missing")
    if observed is None:
        reasons.append(f"{label}.observed_at_missing")
    elif observed >= cutoff:
        reasons.append(f"{label}.not_before_cutoff")
    return reasons


def normalize_snapshot(document):
    """Validate without modifying the caller's input; missing history stays explicit."""
    data = deepcopy(document)
    _object(data, {"schema_version", "snapshot_id", "league_id", "season_year", "cutoff_at",
                   "draft_started_at", "teams", "player_pool", "rankings", "rules", "picks",
                   "keepers", "synthetic"}, "snapshot",
            {"schema_version", "snapshot_id", "league_id", "season_year", "cutoff_at",
             "teams", "player_pool", "picks"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    _text(data["snapshot_id"], "snapshot_id")
    data["league_id"] = _id(data["league_id"], "league_id")
    _integer(data["season_year"], "season_year", 1900, 9999)
    if "synthetic" in data:
        _boolean(data["synthetic"], "synthetic")
    cutoff = _time(data["cutoff_at"], "cutoff_at")
    start = _time(data.get("draft_started_at"), "draft_started_at", nullable=True)
    if start is not None and cutoff >= start:
        raise ValueError("cutoff_at must be strictly before draft_started_at")
    missing = [] if start else ["draft_started_at_missing"]
    teams = {}
    for team in _list(data["teams"], "teams", 100):
        _object(team, {"team_id", "name"}, "team", {"team_id"})
        team["team_id"] = _id(team["team_id"], "team_id")
        if team["team_id"] in teams:
            raise ValueError("Duplicate team_id")
        _text(team.get("name"), "team.name", nullable=True)
        teams[team["team_id"]] = team
    if not teams:
        raise ValueError("At least one team is required")

    pool = _object(data["player_pool"], {"complete", "source", "observed_at", "players"},
                   "player_pool", {"complete", "players"})
    _boolean(pool["complete"], "player_pool.complete")
    pool_reasons = _capture(pool, cutoff, "player_pool")
    missing.extend(pool_reasons)
    if not pool["complete"]:
        missing.append("player_pool.incomplete")
    players = {}
    for player in _list(pool["players"], "player_pool.players", 5000):
        _object(player, {"player_id", "name", "eligible_positions"}, "player", {"player_id"})
        player["player_id"] = _id(player["player_id"], "player_id")
        pid = player["player_id"]
        if pid in players:
            raise ValueError("Duplicate player_id")
        _text(player.get("name"), "player.name", nullable=True)
        positions = player.get("eligible_positions")
        if positions is not None:
            _list(positions, "eligible_positions", 5)
            if any(not isinstance(p, str) or p not in POSITIONS for p in positions) or len(set(positions)) != len(positions):
                raise ValueError("eligible_positions must contain unique PG/SG/SF/PF/C values, or null")
            player["eligible_positions"] = sorted(positions)
        players[pid] = player
    if any(p.get("eligible_positions") is None for p in players.values()):
        missing.append("player_pool.positions_missing")

    rules = data.get("rules")
    rules_reasons = ["rules_missing"] if rules is None else []
    scoring = roster = None
    if rules is not None:
        _object(rules, {"source", "observed_at", "scoring", "roster"}, "rules")
        rules_reasons = _capture(rules, cutoff, "rules")
        scoring, roster = rules.get("scoring"), rules.get("roster")
        if scoring is not None:
            _object(scoring, {"type", "version", "weights"}, "scoring", {"type", "version", "weights"})
            if scoring["type"] != "H2H_POINTS":
                raise ValueError("Only H2H_POINTS scoring is supported")
            _text(scoring["version"], "scoring.version")
            if not isinstance(scoring["weights"], dict) or not scoring["weights"]:
                raise ValueError("scoring.weights must be a nonempty stat-to-weight object")
            for stat, weight in scoring["weights"].items():
                _text(stat, "scoring stat")
                _number(weight, "scoring weight")
        if roster is not None:
            _object(roster, {"starting_slots", "bench_slots"}, "roster", {"starting_slots", "bench_slots"})
            slots = _list(roster["starting_slots"], "roster.starting_slots", 50)
            slots = ["UT" if s == "UTIL" else s for s in slots]
            if any(not isinstance(s, str) or s not in SLOTS for s in slots):
                raise ValueError("Unsupported starting slot; use bench_slots for bench capacity")
            roster["starting_slots"] = slots
            _integer(roster["bench_slots"], "roster.bench_slots", 0, 50)
            if not slots and not roster["bench_slots"]:
                raise ValueError("Roster capacity must be positive")
    missing.extend(rules_reasons)
    if scoring is None:
        missing.append("rules.scoring_missing")
    if roster is None:
        missing.append("rules.roster_missing")

    keepers = data.get("keepers")
    keeper_ids = set()
    if keepers is None:
        missing.append("keepers_unknown")
    else:
        for keeper in _list(keepers, "keepers", 5000):
            _object(keeper, {"player_id", "team_id"}, "keeper", {"player_id", "team_id"})
            _validate_event_ids(keeper, players, teams)
            if keeper["player_id"] in keeper_ids:
                raise ValueError("Duplicate keeper player")
            keeper_ids.add(keeper["player_id"])

    picks = _object(data["picks"], {"complete", "events"}, "picks", {"complete", "events"})
    _boolean(picks["complete"], "picks.complete")
    events = _list(picks["events"], "picks.events", 5000)
    picked_ids, pick_numbers = set(keeper_ids), set()
    for event in events:
        _object(event, {"overall_pick", "team_id", "player_id"}, "pick", {"overall_pick", "team_id", "player_id"})
        _integer(event["overall_pick"], "overall_pick")
        _validate_event_ids(event, players, teams)
        if event["overall_pick"] in pick_numbers or event["player_id"] in picked_ids:
            raise ValueError("Duplicate overall pick, drafted player, or keeper selected again")
        pick_numbers.add(event["overall_pick"])
        picked_ids.add(event["player_id"])
    events.sort(key=lambda e: e["overall_pick"])
    if picks["complete"] and [e["overall_pick"] for e in events] != list(range(1, len(events) + 1)):
        raise ValueError("A complete pick log must be contiguous from pick 1; mark incomplete logs complete:false")
    if not picks["complete"]:
        missing.append("picks.incomplete")

    ranking = data.get("rankings")
    ranking_reasons = ["rankings_missing"] if ranking is None else []
    scores = {pid: {"value": None, "status": "missing_entry"} for pid in players}
    if ranking is not None:
        fields = {"metric", "source", "observed_at", "season_year", "league_id", "horizon", "scoring_version", "entries"}
        _object(ranking, fields, "rankings", {"metric", "entries"})
        if not isinstance(ranking["metric"], str) or ranking["metric"] not in {"projected_total_points", "pre_draft_rank"}:
            raise ValueError("rankings.metric must be projected_total_points or pre_draft_rank")
        ranking_reasons = _capture(ranking, cutoff, "rankings")
        for field, expected in [("season_year", data["season_year"]), ("league_id", data["league_id"]),
                                ("horizon", "full_season"), ("scoring_version", scoring["version"] if scoring else None)]:
            value = ranking.get(field)
            if field == "league_id" and value is not None:
                value = _id(value, "rankings.league_id")
                ranking[field] = value
            if value is None or value != expected or (field == "season_year" and type(value) is not int):
                ranking_reasons.append(f"rankings.{field}_missing_or_incompatible")
        seen = set()
        for entry in _list(ranking["entries"], "rankings.entries", 5000):
            _object(entry, {"player_id", "value", "source", "observed_at", "season_year", "league_id", "horizon", "scoring_version"}, "ranking entry", {"player_id"})
            pid = entry["player_id"] = _id(entry["player_id"], "ranking.player_id")
            if pid not in players or pid in seen:
                raise ValueError("Ranking entries require unique known player IDs")
            seen.add(pid)
            value = entry.get("value")
            if value is not None:
                _number(value, "ranking.value")
                if ranking["metric"] == "pre_draft_rank":
                    _integer(value, "pre_draft_rank")
            combined = {**ranking, **entry}
            reasons = list(ranking_reasons) + rules_reasons
            if start is None:
                reasons.append("draft_started_at_missing")
            reasons.extend(_capture(combined, cutoff, "entry"))
            if ranking["metric"] == "pre_draft_rank":
                # Ordinal positions belong to one board, unlike comparable point units.
                if combined.get("source") != ranking.get("source"):
                    reasons.append("entry.rank_board_source_incompatible")
                if _time(combined.get("observed_at"), "entry.observed_at", nullable=True) != _time(ranking.get("observed_at"), "rankings.observed_at", nullable=True):
                    reasons.append("entry.rank_board_revision_incompatible")
            for field in ("season_year", "league_id", "horizon", "scoring_version"):
                item = combined.get(field)
                if field == "league_id" and item is not None:
                    item = _id(item, "entry.league_id")
                if item != ranking.get(field) or (field == "season_year" and type(item) is not int):
                    reasons.append(f"entry.{field}_incompatible")
            if scoring is None:
                reasons.append("rules.scoring_missing")
            scores[pid] = {"value": value if not reasons else None,
                           "status": "incompatible" if reasons else ("missing_value" if value is None else "available"),
                           "limitations": sorted(set(reasons)),
                           "provenance": {k: combined.get(k) for k in ("source", "observed_at", "season_year", "league_id", "horizon", "scoring_version")}}
    missing.extend(ranking_reasons)
    if any(s["value"] is None for s in scores.values()):
        missing.append("rankings.player_coverage_incomplete")
    return data, players, scores, sorted(set(missing)), pool_reasons, rules_reasons


def _validate_event_ids(event, players, teams):
    for key, known in (("player_id", players), ("team_id", teams)):
        event[key] = _id(event[key], key)
        if event[key] not in known:
            raise ValueError(f"Unknown {key} in pick/keeper; add an explicit player/team record, with unknown values if needed")


def _roster_state(ids, players, roster, complete, positions_verified):
    result = {"observed_player_ids": sorted(ids), "complete": complete,
              "assignment": None, "unfilled_starting_slots": None,
              "starting_slots_filled": None, "remaining_capacity": None,
              "all_players_fit": None, "unassigned_player_ids": None}
    if roster is None:
        return result
    slots = roster["starting_slots"]
    capacity = len(slots) + roster["bench_slots"]
    known_positions = positions_verified and all(players[pid].get("eligible_positions") is not None for pid in ids)
    if complete:
        result["remaining_capacity"] = capacity - len(ids)
        if len(ids) > capacity:
            result["all_players_fit"] = False
    if not known_positions:
        return result
    assigned = {}

    def place(pid, visited):
        for index, slot in enumerate(slots):
            if index in visited or not (set(players[pid]["eligible_positions"]) & SLOTS[slot]):
                continue
            visited.add(index)
            if index not in assigned or place(assigned[index], visited):
                assigned[index] = pid
                return True
        return False

    for pid in sorted(ids):
        place(pid, set())
    remaining = sorted(set(ids) - set(assigned.values()))
    bench = remaining[:roster["bench_slots"]]
    result["assignment"] = [{"slot_index": i, "slot": slots[i], "player_id": assigned[i]} for i in sorted(assigned)]
    result["assignment"] += [{"slot_index": len(slots) + i, "slot": "BE", "player_id": pid} for i, pid in enumerate(bench)]
    result["unassigned_player_ids"] = remaining[len(bench):]
    if complete:
        result["unfilled_starting_slots"] = [{"slot_index": i, "slot": s} for i, s in enumerate(slots) if i not in assigned]
        result["starting_slots_filled"] = len(assigned)
        result["all_players_fit"] = not result["unassigned_player_ids"]
    return result


def replay_draft(document):
    """Return deterministic per-pick facts, with no outcome data or prose judgment."""
    data, players, scores, missing, pool_reasons, rules_reasons = normalize_snapshot(document)
    ranking = data.get("rankings")
    metric = ranking["metric"] if ranking else None
    direction = -1 if metric == "projected_total_points" else 1
    roster = (data["rules"].get("roster") if data.get("rules") and not rules_reasons
              and data.get("draft_started_at") is not None else None)
    roster_complete = data["picks"]["complete"] and data.get("keepers") is not None
    availability_complete = (roster_complete and data["player_pool"]["complete"] and not pool_reasons
                             and data.get("draft_started_at") is not None)
    owned = {team["team_id"]: [] for team in data["teams"]}
    selected = set()
    for keeper in data.get("keepers") or []:
        owned[keeper["team_id"]].append(keeper["player_id"])
        selected.add(keeper["player_id"])
    records = []
    for pick in data["picks"]["events"]:
        pid, team = pick["player_id"], pick["team_id"]
        remaining = sorted(set(players) - selected)
        covered = [p for p in remaining if scores[p]["value"] is not None]
        covered.sort(key=lambda p: (direction * scores[p]["value"], p))
        comparable = availability_complete and bool(covered)
        value = scores[pid]["value"]
        best_value = scores[covered[0]]["value"] if comparable else None
        rank = (1 + sum(direction * scores[p]["value"] < direction * value for p in covered)
                if comparable and value is not None else None)
        gap = direction * (value - best_value) if rank is not None else None
        if gap is not None:
            _number(gap, "computed opportunity gap")
        # Positive opportunity gap means the selected value trails the best covered value.
        before = _roster_state(owned[team], players, roster, roster_complete, not pool_reasons)
        owned[team].append(pid)
        after = _roster_state(owned[team], players, roster, roster_complete, not pool_reasons)
        coverage_complete = availability_complete and len(covered) == len(remaining)
        alternatives = []
        if availability_complete:
            for other in covered + [p for p in remaining if p not in covered]:
                if other == pid:
                    continue
                other_value = scores[other]["value"]
                alternatives.append({"player_id": other, "name": players[other].get("name"),
                                     "value": other_value, "value_status": scores[other]["status"],
                                     "eligible_positions": players[other].get("eligible_positions"),
                                     "rank_among_covered_available": (1 + sum(direction * scores[p]["value"] < direction * other_value for p in covered)
                                                                      if other_value is not None else None)})
        record = {**pick, "player_name": players[pid].get("name"),
                  "availability_complete": availability_complete,
                  "observed_remaining_player_ids": remaining,
                  "available_player_ids": remaining if availability_complete else None,
                  "selected_value": value, "selected_value_status": scores[pid]["status"],
                  "selected_rank_among_covered_available": rank,
                  "selected_global_available_rank": rank if coverage_complete else None,
                  "best_covered_available_player_ids": [p for p in covered if scores[p]["value"] == best_value] if comparable else [],
                  "opportunity_gap_among_covered": gap,
                  "comparison_scope": ("all_available" if coverage_complete else "covered_available") if comparable else "unknown",
                  "unranked_observed_player_ids": [p for p in remaining if scores[p]["value"] is None],
                  "alternatives": alternatives,
                  "roster_before": before, "roster_after": after,
                  "starting_slot_coverage_change": (after["starting_slots_filled"] - before["starting_slots_filled"]
                                                     if after["starting_slots_filled"] is not None and before["starting_slots_filled"] is not None else None)}
        records.append(record)
        selected.add(pid)
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"schema_version": 1, "snapshot_id": data["snapshot_id"], "input_sha256": hashlib.sha256(raw).hexdigest(),
            "synthetic": data.get("synthetic", False), "league_id": data["league_id"], "season_year": data["season_year"],
            "cutoff_at": data["cutoff_at"], "draft_started_at": data.get("draft_started_at"),
            "status": "partial" if missing else "complete", "limitations": missing,
            "metric": metric, "value_unit": ("projected_points" if metric == "projected_total_points" else "source_rank_positions") if metric else None,
            "rank_policy": "competition rank; equal values share rank; stable player ID display order",
            "roster_scope": "Unique-player starting-slot coverage and bench capacity only; no numeric fit bonus or overall draft grade",
            "provenance_scope": "Declared contemporaneous captures; hashes establish input identity, not historical authenticity",
            "score_coverage": scores, "picks": records,
            "observed_final_rosters": {team: sorted(ids) for team, ids in sorted(owned.items())}}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def load_snapshot(path):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Snapshot exceeds 10 MiB")
    def invalid_constant(value):
        raise ValueError(f"Nonfinite JSON number: {value}")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=invalid_constant)


def _write_report(path, text):
    """Create only after successful computation; never clobber an existing file."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = output.name
            output.write(text)
        os.link(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="frozen draft JSON bundle")
    parser.add_argument("--output", type=Path, help="new JSON output file (default: stdout; existing files are preserved)")
    args = parser.parse_args(argv)
    try:
        result = replay_draft(load_snapshot(args.snapshot))
        text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.output:
            _write_report(args.output, text)
        else:
            print(text, end="")
    except (ValueError, OSError, UnicodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
