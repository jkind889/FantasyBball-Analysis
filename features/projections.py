"""Rest-of-season player projections.

Blends prior-season pace, current-season pace, and ESPN's projection into a
single points-per-game estimate, weighted by how much current-season data
exists, then extends it over the games a player has left.

Pure function, no network access. See ``docs/predictive_engine.md``.
"""

import numpy as np
import pandas as pd

# Games of current-season data at which we trust current pace as much as we
# ever will (weight caps at MAX_CURRENT_WEIGHT).
STABILIZE_GP = 25
MAX_CURRENT_WEIGHT = 0.85

# Default games remaining when the caller does not supply a schedule-derived
# value. Roughly a full NBA season; replaced by features/nba_schedule.py later.
DEFAULT_SEASON_GAMES = 82

INPUT_COLUMNS = (
    "player_id",
    "player_name",
    "position",
    "prior_avg_points",
    "current_avg_points",
    "current_games_played",
    "espn_proj_avg_points",
    "games_remaining",
)

OUTPUT_COLUMNS = [
    "player_id",
    "player_name",
    "position",
    "proj_pts_per_game",
    "games_remaining",
    "ros_total",
    "current_weight",
    "basis",
]


def _current_weight(games_played):
    """How much to trust current-season pace, in [0, MAX_CURRENT_WEIGHT]."""
    if not np.isfinite(games_played) or games_played <= 0:
        return 0.0
    frac = min(games_played / STABILIZE_GP, 1.0)
    return frac * MAX_CURRENT_WEIGHT


def _baseline_pg(prior, espn):
    """Pre-season expectation from prior-year pace and ESPN's projection."""
    values = [v for v in (prior, espn) if v is not None and np.isfinite(v)]
    if not values:
        return np.nan
    return float(np.mean(values))


def _project_row(row):
    prior = row.get("prior_avg_points")
    espn = row.get("espn_proj_avg_points")
    current = row.get("current_avg_points")
    gp = row.get("current_games_played")
    gp = float(gp) if gp is not None and np.isfinite(gp) else 0.0

    baseline = _baseline_pg(prior, espn)
    have_current = current is not None and np.isfinite(current) and gp > 0

    weight = _current_weight(gp) if have_current else 0.0
    if not np.isfinite(baseline):
        # Nothing but current-season data to go on.
        weight = 1.0 if have_current else 0.0

    if have_current and weight >= 1.0:
        proj = float(current)
    elif have_current and np.isfinite(baseline):
        proj = weight * float(current) + (1.0 - weight) * baseline
    elif np.isfinite(baseline):
        proj = baseline
    elif have_current:
        proj = float(current)
    else:
        proj = np.nan

    if gp <= 0:
        basis = "preseason"
    elif weight >= MAX_CURRENT_WEIGHT or not np.isfinite(baseline):
        basis = "current"
    else:
        basis = "blended"

    return pd.Series({"proj_pts_per_game": proj, "current_weight": round(weight, 4),
                      "basis": basis})


def project_rest_of_season(players_df, default_games_remaining=DEFAULT_SEASON_GAMES,
                           output_dir=None):
    """Return a projection row per player.

    ``players_df`` needs ``player_id`` and ``player_name``; every other
    :data:`INPUT_COLUMNS` field is optional and filled with NaN when absent.
    ``games_remaining`` may be supplied per player; missing values fall back to
    ``default_games_remaining``.
    """
    if players_df is None or len(players_df) == 0:
        result = pd.DataFrame(columns=OUTPUT_COLUMNS)
        if output_dir is not None:
            result.to_csv(output_dir / "projections.csv", index=False)
        return result

    df = players_df.copy()
    for col in INPUT_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan

    projected = df.apply(_project_row, axis=1)
    df = pd.concat([df, projected], axis=1)

    games_left = pd.to_numeric(df["games_remaining"], errors="coerce")
    # When some players have schedule-derived counts, an unmatched pro team is a
    # data gap, not a preseason-length runway: fall back to the median of the
    # known values so it can't top the board on a full-season projection.
    if games_left.notna().any():
        fallback = games_left.median()
    else:
        fallback = default_games_remaining
    games_left = games_left.fillna(fallback).clip(lower=0)
    df["games_remaining"] = games_left.astype(int)

    df["ros_total"] = (df["proj_pts_per_game"] * df["games_remaining"]).round(1)
    df["proj_pts_per_game"] = df["proj_pts_per_game"].round(3)

    result = df[OUTPUT_COLUMNS].sort_values(
        "ros_total", ascending=False, na_position="last"
    ).reset_index(drop=True)

    if output_dir is not None:
        result.to_csv(output_dir / "projections.csv", index=False)

    return result
