import numpy as np
import pandas as pd

OUTPUT_COLUMNS = [
    "rank",
    "season_year",
    "team_id",
    "team_name",
    "power_score",
    "all_play_win_pct",
    "actual_win_pct",
    "luck",
    "points_for_per_week",
    "avg_weekly_efficiency",
    "recent_form",
    "wins",
    "losses",
    "standing",
]

BASE_WEIGHTS = {
    "all_play_win_pct": 0.40,
    "points_for_per_week": 0.25,
    "actual_win_pct": 0.15,
    "avg_weekly_efficiency": 0.10,
    "recent_form": 0.10,
}

RECENT_WEEKS = 3


def _team_week_rows(matchups_df):
    rows = []
    for m in matchups_df.itertuples(index=False):
        for side, opp in (("home", "away"), ("away", "home")):
            team_id = getattr(m, f"{side}_team_id")
            score = getattr(m, f"{side}_score")
            opp_score = getattr(m, f"{opp}_score")
            won = m.winner_team_id == team_id if pd.notna(m.winner_team_id) else False
            tie = pd.isna(m.winner_team_id)
            rows.append(
                {
                    "season_year": m.season_year,
                    "week": m.week,
                    "team_id": team_id,
                    "team_name": getattr(m, f"{side}_team"),
                    "score": score,
                    "opp_score": opp_score,
                    "won": won,
                    "tie": tie,
                }
            )
    return pd.DataFrame(rows)


def _all_play_win_pct(week_group):
    """Fraction of the rest of the league a team outscored in a single week."""
    scores = week_group["score"].to_numpy()
    n = len(scores)
    if n <= 1:
        return pd.Series(np.nan, index=week_group.index)
    values = []
    for score in scores:
        beat = np.sum(scores < score)
        drew = np.sum(scores == score) - 1
        values.append((beat + 0.5 * drew) / (n - 1))
    return pd.Series(values, index=week_group.index)


def _zscore(series):
    std = series.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return pd.Series(0.0, index=series.index)
    return (series - series.mean()) / std


def manager_power_rankings(
    matchups_df, lineup_efficiency_df=None, league=None, output_dir=None
):
    if matchups_df is None or matchups_df.empty:
        result = pd.DataFrame(columns=OUTPUT_COLUMNS)
        if output_dir is not None:
            result.to_csv(output_dir / "manager_power_rankings.csv", index=False)
        return result

    tw = _team_week_rows(matchups_df)
    tw["all_play"] = tw.groupby(["season_year", "week"], group_keys=False).apply(
        _all_play_win_pct
    )

    max_week = tw.groupby("season_year")["week"].transform("max")
    tw["is_recent"] = tw["week"] > (max_week - RECENT_WEEKS)

    standings = {}
    if league is not None:
        for team in getattr(league, "teams", []):
            standings[team.team_id] = getattr(team, "standing", np.nan)

    records = []
    for (season, team_id), group in tw.groupby(["season_year", "team_id"]):
        games = len(group)
        wins = int(group["won"].sum())
        ties = int(group["tie"].sum())
        losses = games - wins - ties
        recent = group.loc[group["is_recent"], "all_play"]
        records.append(
            {
                "season_year": season,
                "team_id": team_id,
                "team_name": group["team_name"].iloc[-1],
                "all_play_win_pct": group["all_play"].mean(),
                "actual_win_pct": (wins + 0.5 * ties) / games if games else np.nan,
                "points_for_per_week": group["score"].mean(),
                "recent_form": recent.mean() if not recent.empty else np.nan,
                "wins": wins,
                "losses": losses,
                "standing": standings.get(team_id, np.nan),
            }
        )

    df = pd.DataFrame(records)

    eff_col = "avg_weekly_efficiency"
    if lineup_efficiency_df is not None and not lineup_efficiency_df.empty:
        df = df.merge(
            lineup_efficiency_df[["season_year", "team_id", eff_col]],
            on=["season_year", "team_id"],
            how="left",
        )
    else:
        df[eff_col] = np.nan

    has_efficiency = df[eff_col].notna().any()
    weights = dict(BASE_WEIGHTS)
    if not has_efficiency:
        dropped = weights.pop(eff_col)
        scale = 1.0 / (1.0 - dropped)
        weights = {k: v * scale for k, v in weights.items()}
        df[eff_col] = np.nan

    results = []
    for season, group in df.groupby("season_year"):
        group = group.copy()
        score = pd.Series(0.0, index=group.index)
        for metric, weight in weights.items():
            filled = group[metric]
            if filled.notna().any():
                filled = filled.fillna(filled.mean())
            score = score + weight * _zscore(filled)
        group["power_score"] = score.round(4)
        group["luck"] = (group["actual_win_pct"] - group["all_play_win_pct"]).round(4)
        group = group.sort_values("power_score", ascending=False).reset_index(drop=True)
        group["rank"] = group.index + 1
        results.append(group)

    result = pd.concat(results, ignore_index=True)
    for col in ("all_play_win_pct", "actual_win_pct", "points_for_per_week",
                "avg_weekly_efficiency", "recent_form"):
        result[col] = result[col].round(4)
    result = result[OUTPUT_COLUMNS].sort_values(
        ["season_year", "rank"]
    ).reset_index(drop=True)

    if output_dir is not None:
        result.to_csv(output_dir / "manager_power_rankings.csv", index=False)

    return result
