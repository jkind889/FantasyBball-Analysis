import numpy as np
import pandas as pd
from features.provider_projections import PROJECTION_COLUMNS, attach_projections

from features.draft_analysis import (
    fetch_missing_draft_analysis_data,
    merge_missing_draft_analysis_data,
)


OUTPUT_COLUMNS = [
    "player_name",
    "roster_source",
    "draft_team",
    "final_team",
    "position",
    "pro_team",
    "games_played",
    "actual_total_points",
    *PROJECTION_COLUMNS,
    "season_year",
    "projection_difference",
    "projection_percentile",
    "percent_above_projection",
    "projection_status",
]


def projection_analysis(players_df, draft_df, league=None, output_dir=None,
                        free_agent_cache=None, provider_projections=None):
    """Compare observed full-season provider totals with actual season-to-date
    totals. Capture time is not proof that a forecast existed before the season.
    """
    final_players = players_df.drop_duplicates("player_id", keep="last").rename(
        columns={
            "player_name": "final_player_name",
            "team_name": "final_team",
        }
    )
    drafted_players = (
        draft_df.drop_duplicates("player_id", keep="last")
        [["player_id", "player_name", "fantasy_team"]]
        .rename(
            columns={
                "player_name": "draft_player_name",
                "fantasy_team": "draft_team",
            }
        )
    )

    combined_df = drafted_players.merge(
        final_players,
        on="player_id",
        how="outer",
    )
    combined_df["player_name"] = combined_df["draft_player_name"].combine_first(
        combined_df["final_player_name"]
    )

    in_draft = combined_df["draft_team"].notna()
    in_final = combined_df["final_team"].notna()
    combined_df["roster_source"] = "Both"
    combined_df.loc[in_draft & ~in_final, "roster_source"] = "Draft"
    combined_df.loc[~in_draft & in_final, "roster_source"] = "Final"

    if league is not None:
        missing_player_stats = fetch_missing_draft_analysis_data(
            combined_df,
            league,
            free_agent_cache=free_agent_cache,
        )
        combined_df = merge_missing_draft_analysis_data(
            combined_df,
            missing_player_stats,
        )

    if provider_projections is not None:
        combined_df = attach_projections(combined_df, provider_projections)
    for column in PROJECTION_COLUMNS + ["season_year"]:
        if column not in combined_df:
            combined_df[column] = None
    if league is not None and getattr(league, "year", None) is not None:
        combined_df["season_year"] = combined_df["season_year"].fillna(league.year)

    actual_points = pd.to_numeric(combined_df["total_points"], errors="coerce")
    projected_points = pd.to_numeric(
        combined_df["projected_total_points"],
        errors="coerce",
    )
    actual_points = actual_points.where(np.isfinite(actual_points))
    projected_points = projected_points.where(np.isfinite(projected_points))
    forecast_season = pd.to_numeric(combined_df["projection_season"], errors="coerce")
    actual_season = pd.to_numeric(combined_df["season_year"], errors="coerce")
    compatible = (forecast_season.eq(actual_season) & actual_season.notna()
                  & combined_df["projection_horizon"].eq("full_season"))
    verified = (combined_df["projection_source"].fillna("").ne("")
                & pd.to_datetime(combined_df["projection_observed_at"], errors="coerce", format="mixed", utc=True).notna())
    is_resolved = actual_points.notna() & projected_points.notna() & compatible & verified

    combined_df["actual_total_points"] = actual_points
    combined_df["projected_total_points"] = projected_points
    combined_df["projection_difference"] = pd.NA
    combined_df.loc[is_resolved, "projection_difference"] = (
        actual_points[is_resolved] - projected_points[is_resolved]
    )

    combined_df["percent_above_projection"] = pd.NA
    has_nonzero_projection = is_resolved & (projected_points != 0)
    combined_df.loc[has_nonzero_projection, "percent_above_projection"] = (
        combined_df.loc[has_nonzero_projection, "projection_difference"]
        / projected_points[has_nonzero_projection].abs()
        * 100
    ).round(2)

    combined_df["projection_percentile"] = pd.NA
    resolved_differences = combined_df.loc[is_resolved, "projection_difference"]
    if len(resolved_differences) > 1:
        difference_ranks = resolved_differences.rank(method="average")
        percentiles = (
            (difference_ranks - 1) / (len(resolved_differences) - 1) * 100
        )
    else:
        percentiles = pd.Series(50.0, index=resolved_differences.index)
    combined_df.loc[is_resolved, "projection_percentile"] = percentiles.round(2)

    combined_df["projection_status"] = "Missing Projection"
    combined_df.loc[projected_points.notna() & ~verified, "projection_status"] = "Unverified Projection"
    combined_df.loc[projected_points.notna() & verified & ~compatible, "projection_status"] = "Incompatible Projection"
    combined_df.loc[actual_points.isna(), "projection_status"] = "Missing Stats"
    # Percentiles describe the cohort, not whether a player beat a forecast.
    # Use the signed difference so all-negative/tied/singleton cohorts are honest.
    combined_df.loc[is_resolved, "projection_status"] = combined_df.loc[
        is_resolved, "projection_difference"
    ].map(projection_status_for_difference)

    combined_df["_is_resolved"] = is_resolved
    combined_df = combined_df.sort_values(
        by=[
            "_is_resolved",
            "projection_difference",
            "projection_percentile",
            "actual_total_points",
            "player_name",
        ],
        ascending=[False, False, False, False, True],
    )
    result_df = combined_df[OUTPUT_COLUMNS].reset_index(drop=True)

    if output_dir:
        result_df.to_csv(output_dir / "projection_analysis.csv", index=False)

    return result_df


def projection_status_for_difference(difference):
    if difference > 0:
        return "Above Projection"
    if difference < 0:
        return "Below Projection"
    return "At Projection"
