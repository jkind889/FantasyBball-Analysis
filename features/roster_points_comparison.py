import pandas as pd


OUTPUT_COLUMNS = [
    "fantasy_team",
    "draft_roster_total_points",
    "final_roster_total_points",
    "point_difference",
]


def roster_points_comparison(players_df, draft_analysis_df, output_dir=None):
    """Compare each team's drafted-player points with its final-roster points."""
    draft_totals = draft_analysis_df.assign(
        total_points=pd.to_numeric(
            draft_analysis_df["total_points"],
            errors="coerce",
        ).fillna(0)
    ).groupby("fantasy_team", as_index=False)["total_points"].sum()
    draft_totals = draft_totals.rename(
        columns={"total_points": "draft_roster_total_points"}
    )

    final_totals = players_df.assign(
        total_points=pd.to_numeric(
            players_df["total_points"],
            errors="coerce",
        ).fillna(0)
    ).groupby("team_name", as_index=False)["total_points"].sum()
    final_totals = final_totals.rename(
        columns={
            "team_name": "fantasy_team",
            "total_points": "final_roster_total_points",
        }
    )

    comparison_df = draft_totals.merge(
        final_totals,
        on="fantasy_team",
        how="outer",
    ).fillna(
        {
            "draft_roster_total_points": 0,
            "final_roster_total_points": 0,
        }
    )
    comparison_df["point_difference"] = (
        comparison_df["final_roster_total_points"]
        - comparison_df["draft_roster_total_points"]
    )
    comparison_df = comparison_df[OUTPUT_COLUMNS].sort_values(
        by=["point_difference", "fantasy_team"],
        ascending=[False, True],
    ).reset_index(drop=True)

    if output_dir:
        comparison_df.to_csv(
            output_dir / "roster_points_comparison.csv",
            index=False,
        )

    return comparison_df
