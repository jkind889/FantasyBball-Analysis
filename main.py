from espn_api.basketball import League
import pandas as pd

MIN_GAMES_PLAYED = 41

league = League(
    league_id=608130406,
    year=2026,
    espn_s2='AECj80AOBz%2BlYAxOFREb5q8eEPiYCLShOWz%2FvMn2Oi7mADVDJh9WnwjXPZGqMEgrsLJsqDYJ0ODRkJUVibWuwZgLAIf3VDzs94cgeFreYaJ8%2B0JXz0MPXOErOk5%2Fgh%2FYVH4jg4hno5dlqPH6SOWbmho2pbMTNC4gH6MQO5e%2FLr1DZXaE5HLFstVjG3nGljywJoAEO3pG0GjgGh%2BCT3Hl16RQrSEKO8pijebpMybZJFhcftSgdCNkpOZF7iN3AjreVotTkyrvJpsfjRUcsNGRCn%2BuZlt61xPI%2Be8M9QZb0pY7VQ%3D%3D',
    swid='{911ABF90-450F-4439-99F8-EC64207B15CB}')


player_rows = []
for team in league.teams:
    for player in team.roster:
        season_key = f"{player.year}_total"
        season_stats = player.stats.get(season_key, {})
        total_stats = season_stats.get('total', {})

        player_rows.append({
            "player_id": player.playerId,
            "player_name": player.name,
            "team_name": team.team_name,
            "position": player.position,
            "pro_team": player.proTeam,
            "pos_rank": player.posRank,
            "total_points": player.total_points,
            "avg_points": player.avg_points,
            "projected_total_points": player.projected_total_points,
            "projected_avg_points": player.projected_avg_points,
            "games_played": total_stats.get("GP", 0),})

players_df = pd.DataFrame(player_rows)

draft_rows = []

for pick in league.draft:
    draft_rows.append({
        "player_id": pick.playerId,
        "player_name": pick.playerName,
        "round_num": pick.round_num,
        "round_pick": pick.round_pick,
        "fantasy_team": pick.team.team_name
    })

draft_df = pd.DataFrame(draft_rows)

players_df.to_csv("players.csv", index=False)
draft_df.to_csv("draft.csv", index=False)

draft_analysis_df = draft_df.merge(
    players_df,
    on="player_id",
    how="left",
    suffixes=("", "_roster")
)

teams_count = draft_df["round_pick"].max()
draft_analysis_df["overall_pick"] = (
    (draft_analysis_df["round_num"] - 1) * teams_count
    + draft_analysis_df["round_pick"]
)

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

sorted_draft_analysis_df.to_csv("draft_analysis.csv", index=False)

print(sorted_draft_analysis_df[output_columns].to_string(index=False))
