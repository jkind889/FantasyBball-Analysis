from espn_api.basketball import League
import pandas as pd

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
