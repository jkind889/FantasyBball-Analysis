import pandas as pd
from features.build_breakout_players import (
    _fetch_free_agents,
    _index_players,
    _lookup_player,
    _player_summary,
)

MIN_GAMES_PLAYED = 41

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


def draft_analysis(players_df, draft_df, league=None, output_dir=None):
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
        )
        draft_analysis_df = merge_missing_draft_analysis_data(
            draft_analysis_df,
            missing_player_stats,
        )

    was_missing_stats = (
        draft_analysis_df["avg_points"].isna()
        | draft_analysis_df["games_played"].isna()
    )
    draft_analysis_df = fill_missing_draft_analysis_data(draft_analysis_df)

    teams_count = draft_df["round_pick"].max()
    draft_analysis_df["overall_pick"] = (
        (draft_analysis_df["round_num"] - 1) * teams_count
        + draft_analysis_df["round_pick"]
    )

    has_stats = (
        ~was_missing_stats
        & draft_analysis_df["avg_points"].notna()
        & draft_analysis_df["games_played"].notna()
    )
    is_eligible = has_stats & (draft_analysis_df["games_played"] >= MIN_GAMES_PLAYED)

    draft_analysis_df["actual_pg_rank"] = pd.Series(
        pd.NA,
        index=draft_analysis_df.index,
        dtype="Int64",
    )
    draft_analysis_df.loc[is_eligible, "actual_pg_rank"] = (
        draft_analysis_df.loc[is_eligible, "avg_points"]
        .rank(ascending=False, method="min")
        .astype("Int64")
    )

    draft_analysis_df["draft_value_score"] = pd.Series(
        pd.NA,
        index=draft_analysis_df.index,
        dtype="Int64",
    )
    draft_analysis_df.loc[is_eligible, "draft_value_score"] = (
        draft_analysis_df.loc[is_eligible, "overall_pick"]
        - draft_analysis_df.loc[is_eligible, "actual_pg_rank"]
    )

    def get_value_status(row):
        if was_missing_stats.loc[row.name]:
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
        by=[
            "_has_value_score",
            "draft_value_score",
            "avg_points",
            "games_played",
            "overall_pick",
        ],
        ascending=[False, False, False, False, False],
    ).drop(columns="_has_value_score")

    sorted_draft_analysis_df = sorted_draft_analysis_df[output_columns].reset_index(
        drop=True,
    )

    if output_dir:
        sorted_draft_analysis_df.to_csv(output_dir / "draft_analysis.csv", index=False)

    return sorted_draft_analysis_df


def fetch_missing_draft_analysis_data(draft_analysis_df, league):
    missing_players = draft_analysis_df[
        draft_analysis_df["avg_points"].isna()
    ]

    player_rows = []
    free_agent_cache = {}

    for _, row in missing_players.iterrows():
        player_id = row["player_id"]
        player_name = row["player_name"]
        position = row.get("position", None)
        if pd.isna(position):
            position = None

        cache_key = position

        if cache_key not in free_agent_cache:
            free_agent_cache[cache_key] = _index_players(
                _fetch_free_agents(league, position=position)
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
        "pos_rank": getattr(player, "posRank", None),
        "total_points": summary["total_points"],
        "avg_points": summary["avg_points"],
        "projected_total_points": getattr(player, "projected_total_points", None),
        "projected_avg_points": getattr(player, "projected_avg_points", None),
        "games_played": summary["games_played"],
    }
