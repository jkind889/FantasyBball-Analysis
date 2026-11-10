"""Pure, as-of short-term projections from league-scored game logs."""

from math import isfinite
from statistics import mean, median

import pandas as pd


def timestamp(value):
    """Require explicit timezone information at decision boundaries."""
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError(f"Expected a timezone-aware timestamp, got {value!r}")
    return result.tz_convert("UTC")


def number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def streaming_projection(player, logs, as_of, window=6, freshness_days=21,
                         weight=0.60, threshold=30):
    """Blend mean/median recent form with a baseline, without mutating it.

    Logs need player_id, game_id, completed_at, points, played, and completed.
    A conflicting duplicate invalidates coverage instead of choosing a result.
    """
    if window not in (5, 6) or freshness_days <= 0 or not 0 <= weight <= 1:
        raise ValueError("Use a 5/6-game window, positive freshness, and weight in [0, 1]")
    now = timestamp(as_of)
    baseline = number(player.get("proj_pts_per_game"))
    seen, appearances = {}, []
    invalid = False
    for log in logs:
        if log.get("player_id") != player["player_id"]:
            continue
        if not log.get("completed") or not log.get("played"):
            continue
        try:
            ended = timestamp(log.get("completed_at"))
        except (ValueError, TypeError):
            invalid = True
            continue
        if ended > now:
            continue
        points = number(log.get("points"))
        gid = log.get("game_id")
        if points is None or gid is None:
            invalid = True
            continue
        item = (ended, points, number(log.get("minutes")))
        if gid in seen:
            if seen[gid] != item:
                invalid = True
            continue
        seen[gid] = item
        appearances.append(item)
    appearances.sort(key=lambda item: item[0], reverse=True)
    selected = appearances[:window]
    recent = [item for item in selected
              if item[0] >= now - pd.Timedelta(days=freshness_days)]
    values = [item[1] for item in recent]
    n = len(values)
    fallback = ""
    if baseline is None:
        fallback = "missing baseline projection"
    elif invalid or player.get("recent_logs_complete") is not True:
        fallback = "missing or incomplete game logs"
    elif n < 3:
        fallback = "fewer than three fresh appearances"
    applied_weight = weight * n / window if not fallback else 0.0
    recent_mean = mean(values) if n else None
    recent_median = median(values) if n else None
    projected = baseline
    if applied_weight:
        projected = applied_weight * (recent_mean + recent_median) / 2 + (1 - applied_weight) * baseline
    season = number(player.get("season_avg_points"))
    minutes = [item[2] for item in recent if item[2] is not None]
    return {
        "player_id": player["player_id"],
        "season_avg_points": season,
        "last_five_avg": mean([x[1] for x in appearances[:5]]) if appearances else None,
        "last_six_avg": mean([x[1] for x in appearances[:6]]) if appearances else None,
        "selected_window": window, "recent_games": n,
        "recent_mean": recent_mean, "recent_median": recent_median,
        "recent_change": recent_mean - season if n and season is not None else None,
        "recent_minutes": mean(minutes) if minutes else None,
        "recent_start": recent[-1][0].isoformat() if recent else None,
        "recent_end": recent[0][0].isoformat() if recent else None,
        "threshold": threshold, "games_above_threshold": sum(v >= threshold for v in values),
        "baseline_ppg": baseline, "streaming_ppg": projected,
        "recent_weight": applied_weight, "fallback_reason": fallback,
    }
