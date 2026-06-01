import os
from pathlib import Path

from espn_api.basketball import League
import pandas as pd
from dotenv import load_dotenv
import features.draft_analysis as draft_analysis
MIN_GAMES_PLAYED = 41
OUTPUT_DIR = Path("reports")

load_dotenv()

league = League(
    league_id=int(os.environ["ESPN_LEAGUE_ID"]),
    year=int(os.environ["ESPN_YEAR"]),
    espn_s2=os.environ["ESPN_S2"],
    swid=os.environ["ESPN_SWID"],
)

OUTPUT_DIR.mkdir(exist_ok=True)


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

players_df.to_csv(OUTPUT_DIR / "players.csv", index=False)
draft_df.to_csv(OUTPUT_DIR / "draft.csv", index=False)
draft_analysis_df = draft_analysis.draft_analysis(players_df, draft_df)

print(draft_analysis_df)


