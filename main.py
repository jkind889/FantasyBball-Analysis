import os
from pathlib import Path

from espn_api.basketball import League
import pandas as pd
from dotenv import load_dotenv
import features.draft_analysis as draft_analysis
import features.projection_analysis as projection_analysis
import features.roster_points_comparison as roster_points_comparison
import features.build_players_weekly as build_players_weekly
import features.build_matchups as build_matchups
import features.build_breakout_players as build_breakout_players
MIN_GAMES_PLAYED = 41
OUTPUT_DIR = Path("data")
RESULTS_DIR = Path("reports")
load_dotenv()


current_year = int(os.environ["ESPN_YEAR"])

current_league = League(
    league_id=int(os.environ["ESPN_LEAGUE_ID"]),
    year=current_year,
    espn_s2=os.environ["ESPN_S2"],
    swid=os.environ["ESPN_SWID"],
)


previous_league = League(
    league_id=int(os.environ["ESPN_LEAGUE_ID"]),
    year=current_year - 1,
    espn_s2=os.environ["ESPN_S2"],
    swid=os.environ["ESPN_SWID"],
)

OUTPUT_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)


player_rows = []
for team in current_league.teams:
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

for pick in current_league.draft:
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
draft_analysis_df = draft_analysis.draft_analysis(
    players_df,
    draft_df,
    league=current_league,
    output_dir=RESULTS_DIR,
)
projection_analysis_df = projection_analysis.projection_analysis(
    players_df,
    draft_df,
    league=current_league,
    output_dir=RESULTS_DIR,
)
roster_points_comparison_df = roster_points_comparison.roster_points_comparison(
    players_df,
    draft_analysis_df,
    output_dir=RESULTS_DIR,
)
breakout_players_df = build_breakout_players.build_breakout_players(
    current_league,
    previous_league,
    min_current_gp=MIN_GAMES_PLAYED,
)
breakout_players_df.to_csv(RESULTS_DIR / "breakout_players.csv", index=False)
build_matchups_df = build_matchups.build_matchups(current_league, start_week=1, end_week=22)
