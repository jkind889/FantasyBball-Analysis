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
from database import get_connection


MIN_GAMES_PLAYED = 41
OUTPUT_DIR = Path("data")
RESULTS_DIR = Path("reports")
load_dotenv()
conn = get_connection()
cursor = conn.cursor()

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


for team in current_league.teams:
    team_id = team.team_id
    team_name = team.team_name
    owner = team.owners[0]
    owner_name = f"{owner["firstName"]}" + " " + f"{owner["lastName"]}"
    season_year = current_year

    cursor.execute(
        """
        INSERT INTO teams (team_id,team_name,owner_name,season_year)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE team_id = VALUES(team_id), team_name = VALUES(team_name), season_year = VALUES(season_year)
        """,
        (team_id,team_name,owner_name,season_year)
    )
conn.commit()

def insert_player(cursor,player_id,player_name, position=None, pro_team=None):
    cursor.execute(
        """
        INSERT INTO players(player_id,player_name,position, pro_team)
        VALUES (%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            player_name = VALUES(player_name),
            position = COALESCE(VALUES(position), position),
            pro_team = COALESCE(VALUES(pro_team), pro_team)
        """,
        (player_id,player_name,position,pro_team),
    )

def insert_players_from_draft(current_league):
    for pick in current_league.draft:
        insert_player(cursor,pick.playerId,pick.playerName)
        
        round_pick = pick.round_pick
        team_id = pick.team.team_id
        round_num = pick.round_num
        round_pick = pick.round_pick
        season_year = current_year
        overall_pick = ((pick.round_num - 1) * len(current_league.teams)) + pick.round_pick
        cursor.execute(
            """
            INSERT INTO draft_picks(season_year,overall_pick,round_num,team_id,player_id,round_pick)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
            round_num = VALUES(round_num),
            round_pick = VALUES(round_pick),
            team_id = VALUES(team_id),
            player_id = VALUES(player_id)
            """,
            (season_year,overall_pick,round_num,team_id,pick.playerId,round_pick)
        )
    conn.commit()

def insert_players_from_finalrosters(current_league):
    for team in current_league.teams:
        for player in team.roster:
            insert_player(cursor,player.playerId,player.name,player.position,player.proTeam)
    conn.commit()

def insert_playerseasons(cursor,season_year,player_id,avg_points,total_points,games_played):
    cursor.execute(
        """
        INSERT INTO player_season(season_year,player_id,avg_points,total_points,games_played)
        VALUES (%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
        avg_points = VALUES(avg_points),
        total_points = VALUES(total_points),
        games_played = VALUES(games_played)
        """,
        (season_year,player_id,avg_points,total_points,games_played)
    )



def insert_playerseason_players(current_league,previous_league):
    for team in previous_league.teams:
        for player in team.roster:
            insert_player(cursor,player.playerId,player.name,player.position,player.proTeam)
            season_key = f"{player.year}_total"
            season_stats = player.stats.get(season_key, {})
            total_stats = season_stats.get('total', {})
            insert_playerseasons(cursor,previous_league.year,player.playerId,player.avg_points,player.total_points,total_stats.get("GP",0))
    
    for team in current_league.teams:
        for player in team.roster:
            insert_player(cursor,player.playerId,player.name,player.position,player.proTeam)
            season_key = f"{player.year}_total"
            season_stats = player.stats.get(season_key, {})
            total_stats = season_stats.get('total', {})
            insert_playerseasons(cursor,current_league.year,player.playerId,player.avg_points,player.total_points,total_stats.get("GP",0))
    conn.commit()

def insert_finalrosters(current_league):
    for team in current_league.teams:
        for player in team.roster:
            season_year = current_league.year
            team_id = team.team_id
            player_id = player.playerId
            pro_team = player.proTeam
            player_name = player.name

            insert_player(cursor,player_id,player_name,player.position,pro_team)
            cursor.execute(
                """
                INSERT INTO final_rosters (season_year,team_id,player_id,pro_team,player_name)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                team_id = VALUES(team_id),
                pro_team = VALUES(pro_team),
                player_name = VALUES(player_name)
                """,
                (season_year,team_id,player_id,pro_team,player_name)
            )
    conn.commit()

players_df = pd.read_sql(
    """
    SELECT
        p.player_id,
        COALESCE(fr.player_name,p.player_name) AS player_name,
        t.team_name,
        p.position,
        COALESCE(fr.pro_team,p.pro_team) AS pro_team,
        NULL AS pos_rank,
        ps.total_points,
        ps.avg_points,
        0 AS projected_total_points,
        0 AS projected_avg_points,
        ps.games_played
    FROM final_rosters fr
    JOIN players p
        ON p.player_id = fr.player_id
    JOIN teams t
        ON t.team_id = fr.team_id
        AND t.season_year = fr.season_year
    LEFT JOIN player_season ps
        ON ps.player_id = fr.player_id
        AND ps.season_year = fr.season_year
    WHERE fr.season_year = %s
    ORDER BY t.team_name, player_name
    """, 
    conn,
    params=(current_year,),
)

draft_df = pd.read_sql(
    """
    SELECT
        dp.player_id,
        p.player_name,
        dp.round_num,
        dp.round_pick,
        t.team_name AS fantasy_team
    FROM draft_picks dp
    JOIN players p
        ON p.player_id = dp.player_id
    JOIN teams t
        ON t.team_id = dp.team_id
        AND t.season_year = dp.season_year
    WHERE dp.season_year = %s
    ORDER BY dp.overall_pick
    """,
    conn,
    params = (current_year,),
)



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

insert_players_from_draft(current_league)
insert_players_from_finalrosters(current_league)
insert_playerseason_players(current_league,previous_league)
insert_finalrosters(current_league)
