import pandas as pd

OUTPUT_COLUMNS = [
    "season_year",
    "week",
    "winner",
    "winner_team_id",
    "winner_score",
    "winner_season_avg_entering_week",
    "loser",
    "loser_team_id",
    "loser_score",
    "loser_season_avg_entering_week",
    "avg_gap",
    "margin",
]


def _team_week_scores(matchups_df):
    home_rows = matchups_df[
        ["season_year", "week", "home_team_id", "home_team", "home_score"]
    ].rename(
        columns={
            "home_team_id": "team_id",
            "home_team": "team_name",
            "home_score": "score",
        }
    )
    away_rows = matchups_df[
        ["season_year", "week", "away_team_id", "away_team", "away_score"]
    ].rename(
        columns={
            "away_team_id": "team_id",
            "away_team": "team_name",
            "away_score": "score",
        }
    )
    return pd.concat([home_rows, away_rows], ignore_index=True)


def _entering_week_averages(matchups_df):
    team_week_scores = _team_week_scores(matchups_df).sort_values(
        ["season_year", "team_id", "week"]
    )
    team_week_scores["season_avg_entering_week"] = team_week_scores.groupby(
        ["season_year", "team_id"], group_keys=False
    )["score"].apply(lambda scores: scores.shift().expanding().mean())
    return team_week_scores[
        ["season_year", "week", "team_id", "season_avg_entering_week"]
    ]


def biggest_upset(matchups_df, output_dir=None):
    if matchups_df.empty:
        upsets_df = pd.DataFrame(columns=OUTPUT_COLUMNS)
        if output_dir is not None:
            upsets_df.to_csv(output_dir / "biggest_upsets.csv", index=False)
        return upsets_df

    entering_week_averages = _entering_week_averages(matchups_df)

    merged = matchups_df.merge(
        entering_week_averages.rename(
            columns={
                "team_id": "home_team_id",
                "season_avg_entering_week": "home_season_avg_entering_week",
            }
        ),
        on=["season_year", "week", "home_team_id"],
        how="left",
    ).merge(
        entering_week_averages.rename(
            columns={
                "team_id": "away_team_id",
                "season_avg_entering_week": "away_season_avg_entering_week",
            }
        ),
        on=["season_year", "week", "away_team_id"],
        how="left",
    )

    decided = merged[merged["winner_team_id"].notna()].copy()

    is_home_winner = decided["winner_team_id"] == decided["home_team_id"]
    decided["winner_score"] = decided["home_score"].where(
        is_home_winner, decided["away_score"]
    )
    decided["loser_score"] = decided["away_score"].where(
        is_home_winner, decided["home_score"]
    )
    decided["winner_season_avg_entering_week"] = decided[
        "home_season_avg_entering_week"
    ].where(is_home_winner, decided["away_season_avg_entering_week"])
    decided["loser_season_avg_entering_week"] = decided[
        "away_season_avg_entering_week"
    ].where(is_home_winner, decided["home_season_avg_entering_week"])

    decided = decided.dropna(
        subset=["winner_season_avg_entering_week", "loser_season_avg_entering_week"]
    )

    upsets_df = decided[
        decided["winner_season_avg_entering_week"]
        < decided["loser_season_avg_entering_week"]
    ].copy()

    upsets_df["avg_gap"] = (
        upsets_df["loser_season_avg_entering_week"]
        - upsets_df["winner_season_avg_entering_week"]
    )

    upsets_df = upsets_df.sort_values("avg_gap", ascending=False)
    upsets_df = upsets_df[OUTPUT_COLUMNS].reset_index(drop=True)

    if output_dir is not None:
        upsets_df.to_csv(output_dir / "biggest_upsets.csv", index=False)

    return upsets_df
