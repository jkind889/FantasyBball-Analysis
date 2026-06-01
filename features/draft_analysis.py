import pandas as pd


MIN_GAMES_PLAYED = 41

def draft_analysis(players_df, draft_df,output_dir=None):
    draft_analysis_df = draft_df.merge(
    players_df,
    on="player_id",
    how="left",
    suffixes=("", "_roster"))

    teams_count = draft_df["round_pick"].max()
    draft_analysis_df["overall_pick"] = (
        (draft_analysis_df["round_num"] - 1) * teams_count
        + draft_analysis_df["round_pick"])

    has_stats = draft_analysis_df["avg_points"].notna() & draft_analysis_df["games_played"].notna()
    is_eligible = has_stats & (draft_analysis_df["games_played"] >= MIN_GAMES_PLAYED)

    draft_analysis_df["actual_pg_rank"] = pd.Series(pd.NA, index=draft_analysis_df.index, dtype="Int64")
    draft_analysis_df.loc[is_eligible, "actual_pg_rank"] = (
        draft_analysis_df.loc[is_eligible, "avg_points"]
        .rank(ascending=False, method="min")
        .astype("Int64")
    )

    draft_analysis_df["draft_value_score"] = pd.Series(pd.NA, index=draft_analysis_df.index, dtype="Int64")
    draft_analysis_df.loc[is_eligible, "draft_value_score"] = (
        draft_analysis_df.loc[is_eligible, "overall_pick"]
        - draft_analysis_df.loc[is_eligible, "actual_pg_rank"]
    )


    def get_value_status(row):
        if pd.isna(row["avg_points"]) or pd.isna(row["games_played"]):
            return "Missing Stats"
        if row["games_played"] < MIN_GAMES_PLAYED:
            return "Insufficient GP"
        if row["draft_value_score"] >= 30:
            return "Steal"
        if row["draft_value_score"] >= 10:
            return "Good Value"
        if row["draft_value_score"] > -10:
            return "Fair"
        return "Reach"


    draft_analysis_df["value_status"] = draft_analysis_df.apply(get_value_status, axis=1)

    output_columns = [
        "player_name",
        "fantasy_team",
        "overall_pick",
        "avg_points",
        "games_played",
        "actual_pg_rank",
        "draft_value_score",
        "value_status",
    ]

    draft_analysis_df["_has_value_score"] = draft_analysis_df["draft_value_score"].notna()
    sorted_draft_analysis_df = draft_analysis_df.sort_values(
        by=["_has_value_score", "draft_value_score", "avg_points", "games_played", "overall_pick"],
        ascending=[False, False, False, False, False],
    ).drop(columns="_has_value_score")

    if output_dir:
        sorted_draft_analysis_df.to_csv(output_dir / "draft_analysis.csv", index=False)

    return sorted_draft_analysis_df[output_columns] 

