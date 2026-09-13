"""Validated rules for the daily points-league waiver advisor."""

from collections import Counter
from zoneinfo import ZoneInfo


STARTING_SLOTS = {"PG", "SG", "SF", "PF", "C", "G", "F", "SG/SF", "G/F", "PF/C", "F/C", "UT"}


def validate_rules(rules):
    """Fail explicitly when a rule requires a different optimizer."""
    if rules.get("scoring_type") != "H2H_POINTS":
        raise ValueError("Waiver advisor currently supports H2H_POINTS leagues only")
    if rules.get("lineup_lock") != "INDIVIDUAL_GAME":
        raise ValueError("Waiver advisor requires verified INDIVIDUAL_GAME daily lineup locks")
    if rules.get("acquisition_timing") not in {"SAME_DAY", "NEXT_DAY"}:
        raise ValueError("Set acquisition_timing to SAME_DAY or NEXT_DAY in --waiver-config")
    if rules.get("drop_lock") not in {"INDIVIDUAL_GAME", "FIRSTGAME_SCORINGPERIOD"}:
        raise ValueError("Waiver advisor requires verified individual-game or first-game roster locks")
    if rules.get("games_cap", "unknown") is not None:
        raise ValueError("Games/starts caps are unknown or unsupported; configure games_cap: null only if unlimited")
    if rules.get("lineup_move_limit", "unknown") is not None:
        raise ValueError("Finite/unknown lineup move limits are unsupported")
    slots = rules.get("starting_slots", [])
    if not slots or any(s not in STARTING_SLOTS for s in slots):
        raise ValueError("Missing or unsupported starting slots")
    capacity = rules.get("roster_capacity")
    if type(capacity) is not int or capacity < len(slots):
        raise ValueError("roster_capacity must include starters and bench, excluding IR")
    remaining = rules.get("acquisitions_remaining", "unknown")
    if remaining is not None and (type(remaining) is not int or remaining < 0):
        raise ValueError("Set acquisitions_remaining to a nonnegative count, or null if unlimited")
    daily_limit = rules.get("daily_acquisition_limit")
    if daily_limit is not None:
        if type(daily_limit) is not int or daily_limit < 0:
            raise ValueError("daily_acquisition_limit must be a nonnegative count or null")
        used = rules.get("daily_acquisitions_used")
        if type(used) is not int or used < 0:
            raise ValueError("Daily acquisition usage is unknown; set daily_acquisitions_used in --waiver-config")
    limits = rules.get("position_limits")
    if not isinstance(limits, dict) or any(
        p not in {"PG", "SG", "SF", "PF", "C"} or type(n) is not int or n < 0
        for p, n in limits.items()
    ):
        raise ValueError("position_limits must map primary positions to nonnegative capacities")
    if rules.get("ir_legal") is not True:
        raise ValueError("IR legality is unverified; resolve in ESPN or configure ir_legal explicitly")
    ZoneInfo(rules["timezone"])
    return rules


def roster_legal(players, rules):
    active = [p for p in players if p.get("lineup_slot") != "IR"]
    if len(active) > rules["roster_capacity"]:
        return False
    counts = Counter(p.get("position") for p in active)
    return all(counts[p] <= limit for p, limit in rules["position_limits"].items())
