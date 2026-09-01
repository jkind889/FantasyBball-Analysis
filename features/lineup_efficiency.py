import ast

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

BENCH_SLOTS = {"BE", "IR"}
UNPLAYABLE_STATUSES = {"OUT", "SUSPENSION"}

_FORBIDDEN = -1e9
_NEG_SENTINEL = -1e6

SEASON_COLUMNS = [
    "season_year",
    "team_id",
    "team_name",
    "weeks",
    "avg_weekly_efficiency",
    "total_points_left_on_bench",
    "avg_points_left_per_week",
    "management_misses",
    "worst_week",
    "worst_week_points_left",
]

WEEKLY_COLUMNS = [
    "season_year",
    "week",
    "team_id",
    "team_name",
    "actual_points",
    "optimal_points",
    "points_left",
    "efficiency",
    "management_misses",
]


def _as_slot_list(value):
    if isinstance(value, (list, tuple, set, np.ndarray)):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, (list, tuple, set)):
                return list(parsed)
        except (ValueError, SyntaxError):
            pass
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _is_unplayable(row):
    status = row.get("injury_status")
    if status == "OUT":
        return True
    return bool(row.get("injured")) and status in UNPLAYABLE_STATUSES


def _optimal_points(candidates, starting_slots):
    """Max total points from candidates assigned to starting_slots, respecting eligibility."""
    if not starting_slots or not candidates:
        return 0.0

    n_slots = len(starting_slots)
    n_rows = len(candidates) + n_slots  # pad with dummy players so any slot can stay empty

    profit = np.zeros((n_rows, n_slots), dtype=float)
    for i, player in enumerate(candidates):
        eligible = set(_as_slot_list(player["eligible_slots"]))
        for j, slot in enumerate(starting_slots):
            profit[i, j] = player["points"] if slot in eligible else _FORBIDDEN

    row_idx, col_idx = linear_sum_assignment(profit, maximize=True)
    return float(sum(profit[r, c] for r, c in zip(row_idx, col_idx) if profit[r, c] > _NEG_SENTINEL))


def _management_misses(candidates, started_rows):
    misses = 0
    for bench in candidates:
        if bench["was_started"]:
            continue
        eligible = set(_as_slot_list(bench["eligible_slots"]))
        for starter in started_rows:
            if starter["lineup_slot"] in eligible and bench["points"] > starter["points"]:
                misses += 1
                break
    return misses


def lineup_efficiency(players_weekly_df, season_year=None, output_dir=None):
    if players_weekly_df is None or players_weekly_df.empty:
        season_df = pd.DataFrame(columns=SEASON_COLUMNS)
        if output_dir is not None:
            season_df.to_csv(output_dir / "lineup_efficiency.csv", index=False)
            pd.DataFrame(columns=WEEKLY_COLUMNS).to_csv(
                output_dir / "lineup_efficiency_weekly.csv", index=False
            )
        return season_df

    df = players_weekly_df.copy()
    if "season_year" not in df.columns:
        df["season_year"] = season_year

    weekly_rows = []
    for (season, week, team_id), group in df.groupby(
        ["season_year", "week", "fantasy_team_id"], dropna=False
    ):
        rows = group.to_dict("records")
        team_name = rows[0].get("fantasy_team")

        started_rows = [r for r in rows if r["lineup_slot"] not in BENCH_SLOTS]
        starting_slots = [r["lineup_slot"] for r in started_rows]
        actual_points = float(sum(r["points"] for r in started_rows))

        candidates = [
            r
            for r in rows
            if r["lineup_slot"] != "IR" and not _is_unplayable(r)
        ]
        optimal_points = _optimal_points(candidates, starting_slots)
        # actual is always achievable, so optimal can never be below it
        optimal_points = max(optimal_points, actual_points)
        points_left = optimal_points - actual_points
        efficiency = 1.0 if optimal_points <= 0 else actual_points / optimal_points

        weekly_rows.append(
            {
                "season_year": season,
                "week": week,
                "team_id": team_id,
                "team_name": team_name,
                "actual_points": round(actual_points, 2),
                "optimal_points": round(optimal_points, 2),
                "points_left": round(points_left, 2),
                "efficiency": round(efficiency, 4),
                "management_misses": _management_misses(candidates, started_rows),
            }
        )

    weekly_df = pd.DataFrame(weekly_rows, columns=WEEKLY_COLUMNS)

    season_records = []
    for (season, team_id), group in weekly_df.groupby(
        ["season_year", "team_id"], dropna=False
    ):
        worst_idx = group["points_left"].idxmax()
        season_records.append(
            {
                "season_year": season,
                "team_id": team_id,
                "team_name": group["team_name"].iloc[0],
                "weeks": len(group),
                "avg_weekly_efficiency": round(group["efficiency"].mean(), 4),
                "total_points_left_on_bench": round(group["points_left"].sum(), 2),
                "avg_points_left_per_week": round(group["points_left"].mean(), 2),
                "management_misses": int(group["management_misses"].sum()),
                "worst_week": int(weekly_df.loc[worst_idx, "week"]),
                "worst_week_points_left": round(
                    float(weekly_df.loc[worst_idx, "points_left"]), 2
                ),
            }
        )

    season_df = pd.DataFrame(season_records, columns=SEASON_COLUMNS)
    season_df = season_df.sort_values(
        ["season_year", "avg_weekly_efficiency"], ascending=[True, False]
    ).reset_index(drop=True)

    if output_dir is not None:
        season_df.to_csv(output_dir / "lineup_efficiency.csv", index=False)
        weekly_df.sort_values(["season_year", "week", "team_id"]).to_csv(
            output_dir / "lineup_efficiency_weekly.csv", index=False
        )

    return season_df
