import pandas as pd
from cache import (
    espn_player_to_row,
    _fetch_free_agents,
    _index_players,
    _lookup_player,
    _player_summary,
)

MIN_GAMES_PLAYED = 21
DRAFT_BUCKET_SIZE = 5

PLAYER_STAT_COLUMNS = [
    "team_name",
    "position",
    "pro_team",
    "pos_rank",
    "total_points",
    "avg_points",
    "projected_total_points",
    "projected_avg_points",
    "games_played",
]

TEAM_SUMMARY_COLUMNS = [
    "fantasy_team",
    "total_picks",
    "scored_picks",
    "unscored_picks",
    "total_vope_score",
    "average_vope_score_per_pick",
]


def draft_analysis(players_df, draft_df, league=None, output_dir=None,
                   free_agent_cache=None):
    draft_analysis_df = draft_df.merge(
        players_df,
        on="player_id",
        how="left",
        suffixes=("", "_roster"),
    )

    if league is not None:
        missing_player_stats = fetch_missing_draft_analysis_data(
            draft_analysis_df,
            league,
            free_agent_cache=free_agent_cache,
        )
        draft_analysis_df = merge_missing_draft_analysis_data(
            draft_analysis_df,
            missing_player_stats,
        )

    was_missing_stats = (
        draft_analysis_df["total_points"].isna()
        | draft_analysis_df["games_played"].isna()
    )
    draft_analysis_df = fill_missing_draft_analysis_data(draft_analysis_df)

    teams_count = draft_df["round_pick"].max()
    draft_analysis_df["overall_pick"] = (
        (draft_analysis_df["round_num"] - 1) * teams_count
        + draft_analysis_df["round_pick"]
    )
    draft_analysis_df["_draft_bucket_start"] = (
        ((draft_analysis_df["overall_pick"] - 1) // DRAFT_BUCKET_SIZE)
        * DRAFT_BUCKET_SIZE
        + 1
    )
    draft_analysis_df["draft_bucket"] = (
        draft_analysis_df["_draft_bucket_start"].astype(str)
        + "-"
        + (
            draft_analysis_df["_draft_bucket_start"] + DRAFT_BUCKET_SIZE - 1
        ).astype(str)
    )

    has_stats = (
        ~was_missing_stats
        & draft_analysis_df["total_points"].notna()
        & draft_analysis_df["games_played"].notna()
    )
    is_eligible = has_stats & (draft_analysis_df["games_played"] >= MIN_GAMES_PLAYED)

    draft_analysis_df["actual_total_points_rank"] = pd.Series(
        pd.NA,
        index=draft_analysis_df.index,
        dtype="Int64",
    )
    draft_analysis_df.loc[is_eligible, "actual_total_points_rank"] = (
        draft_analysis_df.loc[is_eligible, "total_points"]
        .rank(ascending=False, method="min")
        .astype("Int64")
    )

    draft_analysis_df["expected_total_points"] = pd.NA
    expected_total_points = (
        draft_analysis_df.loc[is_eligible]
        .groupby("_draft_bucket_start")["total_points"]
        .transform("mean")
    )
    draft_analysis_df.loc[is_eligible, "expected_total_points"] = (
        expected_total_points
    )

    draft_analysis_df["vope_score"] = pd.NA
    draft_analysis_df.loc[is_eligible, "vope_score"] = (
        draft_analysis_df.loc[is_eligible, "total_points"]
        - draft_analysis_df.loc[is_eligible, "expected_total_points"]
    )

    draft_analysis_df["percent_above_expected"] = pd.NA
    has_expected_points = (
        is_eligible
        & draft_analysis_df["expected_total_points"].notna()
        & (draft_analysis_df["expected_total_points"] != 0)
    )
    draft_analysis_df.loc[has_expected_points, "percent_above_expected"] = (
        draft_analysis_df.loc[has_expected_points, "vope_score"]
        / draft_analysis_df.loc[has_expected_points, "expected_total_points"]
        * 100
    )

    draft_analysis_df["vope_percentile"] = pd.NA
    eligible_vope_scores = draft_analysis_df.loc[is_eligible, "vope_score"]
    if len(eligible_vope_scores) > 1:
        vope_ranks = eligible_vope_scores.rank(method="average")
        vope_percentiles = (vope_ranks - 1) / (len(eligible_vope_scores) - 1) * 100
    else:
        vope_percentiles = pd.Series(50.0, index=eligible_vope_scores.index)
    draft_analysis_df.loc[is_eligible, "vope_percentile"] = (
        vope_percentiles.round(2)
    )

    def get_value_status(row):
        if was_missing_stats.loc[row.name]:
            return "Missing Stats"
        if row["games_played"] < MIN_GAMES_PLAYED:
            return "Insufficient GP"
        return value_status_for_vope_percentile(row["vope_percentile"])

    draft_analysis_df["value_status"] = draft_analysis_df.apply(get_value_status, axis=1)

    output_columns = [
        "player_name",
        "fantasy_team",
        "overall_pick",
        "draft_bucket",
        "total_points",
        "games_played",
        "actual_total_points_rank",
        "expected_total_points",
        "vope_score",
        "vope_percentile",
        "percent_above_expected",
        "value_status",
    ]

    draft_analysis_df["_has_value_score"] = draft_analysis_df[
        "vope_score"
    ].notna()
    sorted_draft_analysis_df = draft_analysis_df.sort_values(
        by=[
            "_has_value_score",
            "vope_score",
            "vope_percentile",
            "total_points",
            "games_played",
            "overall_pick",
        ],
        ascending=[False, False, False, False, False, False],
    ).drop(columns="_has_value_score")

    sorted_draft_analysis_df = sorted_draft_analysis_df[output_columns].reset_index(
        drop=True,
    )
    team_summary_df = build_team_draft_summary(sorted_draft_analysis_df)

    if output_dir:
        sorted_draft_analysis_df.to_csv(output_dir / "draft_analysis.csv", index=False)
        team_summary_df.to_csv(output_dir / "draft_analysis_by_team.csv", index=False)

    return sorted_draft_analysis_df


def value_status_for_vope_percentile(vope_percentile):
    if vope_percentile >= 90:
        return "Elite Steal"
    if vope_percentile >= 75:
        return "Steal"
    if vope_percentile > 25:
        return "Fair"
    if vope_percentile > 10:
        return "Bust"
    return "Major Bust"


def build_team_draft_summary(draft_analysis_df):
    summary_source = draft_analysis_df.copy()
    summary_source["_vope_score"] = pd.to_numeric(
        summary_source["vope_score"],
        errors="coerce",
    )
    summary_source["_is_scored_pick"] = summary_source["_vope_score"].notna()

    team_summary_df = (
        summary_source.groupby("fantasy_team", dropna=False)
        .agg(
            total_picks=("player_name", "count"),
            scored_picks=("_is_scored_pick", "sum"),
            total_vope_score=("_vope_score", "sum"),
            average_vope_score_per_pick=("_vope_score", "mean"),
        )
        .reset_index()
    )
    team_summary_df["unscored_picks"] = (
        team_summary_df["total_picks"] - team_summary_df["scored_picks"]
    )

    team_summary_df = team_summary_df[TEAM_SUMMARY_COLUMNS].sort_values(
        by=[
            "total_vope_score",
            "average_vope_score_per_pick",
            "scored_picks",
            "fantasy_team",
        ],
        ascending=[False, False, False, True],
    )

    return team_summary_df.reset_index(drop=True)


def fetch_missing_draft_analysis_data(draft_analysis_df, league,
                                     free_agent_cache=None):
    missing_players = draft_analysis_df[
        draft_analysis_df["total_points"].isna()
        | draft_analysis_df["games_played"].isna()
    ]

    player_rows = []
    if free_agent_cache is None:
        free_agent_cache = {}

    for _, row in missing_players.iterrows():
        player_id = row["player_id"]
        player_name = row["player_name"]
        position = row.get("position", None)
        if pd.isna(position):
            position = None

        season_year = getattr(league, "year", None)
        cache_key = (season_year, position)

        if cache_key not in free_agent_cache:
            free_agent_rows = [
                espn_player_to_row(player, season_year)
                for player in _fetch_free_agents(league, position=position)
            ]
            free_agent_cache[cache_key] = _index_players(
                free_agent_rows
            )

        missing_player = _lookup_player(
            free_agent_cache[cache_key],
            player_id,
            player_name,
        )

        if missing_player is not None:
            player_rows.append(_draft_player_summary(missing_player, player_id))

    return pd.DataFrame(player_rows, columns=["player_id"] + PLAYER_STAT_COLUMNS)


def merge_missing_draft_analysis_data(draft_analysis_df, missing_player_stats):
    if missing_player_stats.empty:
        return draft_analysis_df

    merged_df = draft_analysis_df.merge(
        missing_player_stats,
        on="player_id",
        how="left",
        suffixes=("", "_missing"),
    )

    for column in PLAYER_STAT_COLUMNS:
        missing_column = f"{column}_missing"
        if missing_column not in merged_df.columns:
            continue

        merged_df[column] = merged_df[column].combine_first(merged_df[missing_column])
        merged_df = merged_df.drop(columns=missing_column)

    return merged_df


def fill_missing_draft_analysis_data(draft_analysis_df):
    filled_df = draft_analysis_df.copy()
    numeric_columns = [
        "avg_points",
        "games_played",
        "total_points",
        "projected_total_points",
        "projected_avg_points",
        "pos_rank",
    ]

    for column in numeric_columns:
        if column in filled_df.columns:
            filled_df[column] = filled_df[column].fillna(0)

    text_columns = ["team_name", "position", "pro_team"]
    for column in text_columns:
        if column in filled_df.columns:
            filled_df[column] = filled_df[column].fillna("Unknown")

    return filled_df


def _draft_player_summary(player, fallback_player_id):
    summary = _player_summary(player)

    return {
        "player_id": summary["player_id"] or fallback_player_id,
        "team_name": None,
        "position": summary["position"],
        "pro_team": summary["pro_team"],
        "pos_rank": player.get("pos_rank"),
        "total_points": summary["total_points"],
        "avg_points": summary["avg_points"],
        "projected_total_points": player.get("projected_total_points"),
        "projected_avg_points": player.get("projected_avg_points"),
        "games_played": summary["games_played"],
    }
