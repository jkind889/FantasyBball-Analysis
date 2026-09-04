import pandas as pd

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
    "projected_total_points",
    "projection_difference",
    "projection_percentile",
    "percent_above_projection",
    "projection_status",
]


def projection_analysis(players_df, draft_df, league=None, output_dir=None,
                        free_agent_cache=None):
    """Compare ESPN projected totals with actual totals for rostered players."""
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

    actual_points = pd.to_numeric(combined_df["total_points"], errors="coerce")
    projected_points = pd.to_numeric(
        combined_df["projected_total_points"],
        errors="coerce",
    )
    is_resolved = actual_points.notna() & projected_points.notna()

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
        / projected_points[has_nonzero_projection]
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

    combined_df["projection_status"] = "Missing Stats"
    combined_df.loc[is_resolved, "projection_status"] = combined_df.loc[
        is_resolved,
        "projection_percentile",
    ].map(projection_status_for_percentile)

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


def projection_status_for_percentile(percentile):
    if percentile >= 90:
        return "Far Above Projection"
    if percentile >= 75:
        return "Above Projection"
    if percentile > 25:
        return "Near Projection"
    if percentile > 10:
        return "Below Projection"
    return "Far Below Projection"
