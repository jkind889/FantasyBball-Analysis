import argparse
import os
import sys
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
import features.biggest_upset as biggest_upset
import features.lineup_efficiency as lineup_efficiency
import features.manager_power_rankings as manager_power_rankings
import features.build_digest as build_digest
import features.nba_schedule as nba_schedule
import features.predictive_engine as predictive_engine
import features.decision_modes as decision_modes
from alerts.send_email import send_digest_email
from cache import _fetch_free_agents
from database import get_connection


MIN_GAMES_PLAYED = 41
OUTPUT_DIR = Path("data")
RESULTS_DIR = Path("reports")
load_dotenv()

parser = argparse.ArgumentParser(description="FantasyBball-Analysis pipeline")
parser.add_argument(
    "--mode",
    choices=("weekly",) + decision_modes.DECISION_MODES,
    default="weekly",
    help=(
        "weekly (default): full retrospective + predictive run. "
        "draft / waivers / startsit: a single forward-looking decision report."
    ),
)
parser.add_argument(
    "--week",
    type=int,
    default=None,
    help="fantasy week for waivers/startsit (default: current week)",
)
parser.add_argument(
    "--team",
    type=int,
    default=os.environ.get("ESPN_TEAM_ID"),
    help="your team id for startsit (default: ESPN_TEAM_ID env var)",
)
args = parser.parse_args()

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


if args.mode in decision_modes.DECISION_MODES:
    team_id = int(args.team) if args.team is not None else None
    report_df = decision_modes.run(
        args.mode,
        current_league,
        previous_league,
        week=args.week,
        team_id=team_id,
        output_dir=RESULTS_DIR,
    )
    csv_name = {
        "draft": "draft_board.csv",
        "waivers": "waiver_board.csv",
        "startsit": "start_sit.csv",
    }[args.mode]
    print(f"--mode {args.mode}: wrote reports/{csv_name} ({len(report_df)} rows)")
    print(report_df.head(20).to_string(index=False))
    sys.exit(0)


conn = get_connection()
cursor = conn.cursor()

for team in current_league.teams:
    team_id = team.team_id
    team_name = team.team_name
    owner = team.owners[0] if team.owners else {}
    owner_name = (
        f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
        or team_name
    )
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

def insert_matchups(cursor,matchups_df):
    for row in matchups_df.itertuples(index=False):
        cursor.execute(
            """
            INSERT INTO matchups (season_year,week,home_team_id,away_team_id,home_score,away_score)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
            home_score = VALUES(home_score),
            away_score = VALUES(away_score)
            """,
            (
                row.season_year,
                row.week,
                row.home_team_id,
                row.away_team_id,
                row.home_score,
                row.away_score,
            ),
        )
    conn.commit()

def insert_nba_schedule(cursor, schedule_df):
    cursor.execute(nba_schedule.CREATE_TABLE_SQL)
    for row in schedule_df.itertuples(index=False):
        cursor.execute(
            """
            INSERT INTO nba_schedule
                (season_year,pro_team,opponent,is_home,game_date,scoring_period,fantasy_week)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
            opponent = VALUES(opponent),
            is_home = VALUES(is_home),
            scoring_period = VALUES(scoring_period),
            fantasy_week = VALUES(fantasy_week)
            """,
            (
                row.season_year,
                row.pro_team,
                row.opponent,
                row.is_home,
                row.game_date,
                row.scoring_period,
                row.fantasy_week,
            ),
        )
    conn.commit()

insert_players_from_draft(current_league)
insert_players_from_finalrosters(current_league)
insert_playerseason_players(current_league,previous_league)
insert_finalrosters(current_league)


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
        NULL AS projected_total_points,
        NULL AS projected_avg_points,
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

# player_season doesn't store ESPN projections, so pull them straight off the
# live roster objects instead of comparing against a hardcoded 0.
_projected_total = {}
_projected_avg = {}
for team in current_league.teams:
    for player in team.roster:
        _projected_total[player.playerId] = getattr(
            player, "projected_total_points", None
        )
        _projected_avg[player.playerId] = getattr(
            player, "projected_avg_points", None
        )
players_df["projected_total_points"] = players_df["player_id"].map(_projected_total)
players_df["projected_avg_points"] = players_df["player_id"].map(_projected_avg)

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

# Fetch the current-season free agents once and share the lookup cache across
# every feature that needs them.
current_free_agents = _fetch_free_agents(current_league)
shared_free_agent_cache = {}

_current_matchup_period = getattr(current_league, "currentMatchupPeriod", None)
last_week = min(
    int(_current_matchup_period) if _current_matchup_period is not None else 22,
    22,
)

draft_analysis_df = draft_analysis.draft_analysis(
    players_df,
    draft_df,
    league=current_league,
    output_dir=RESULTS_DIR,
    free_agent_cache=shared_free_agent_cache,
)
projection_analysis_df = projection_analysis.projection_analysis(
    players_df,
    draft_df,
    league=current_league,
    output_dir=RESULTS_DIR,
    free_agent_cache=shared_free_agent_cache,
)
roster_points_comparison_df = roster_points_comparison.roster_points_comparison(
    players_df,
    draft_analysis_df,
    output_dir=RESULTS_DIR,
)
breakout_players_df = build_breakout_players.build_breakout_players(
    current_league,
    previous_league,
    conn=conn,
    min_current_gp=MIN_GAMES_PLAYED,
    current_free_agents=current_free_agents,
)
breakout_players_df.to_csv(RESULTS_DIR / "breakout_players.csv", index=False)
build_matchups_df = build_matchups.build_matchups(
    current_league, start_week=1, end_week=last_week, season_year=current_year
)
insert_matchups(cursor, build_matchups_df)
build_matchups_df.to_csv(RESULTS_DIR / "matchups.csv", index=False)
biggest_upsets_df = biggest_upset.biggest_upset(build_matchups_df, output_dir=RESULTS_DIR)

players_weekly_df = build_players_weekly.build_players_weekly(
    current_league, 1, last_week
)
players_weekly_df.to_csv(RESULTS_DIR / "players_weekly.csv", index=False)
lineup_efficiency_df = lineup_efficiency.lineup_efficiency(
    players_weekly_df, season_year=current_year, output_dir=RESULTS_DIR
)
manager_power_rankings_df = manager_power_rankings.manager_power_rankings(
    build_matchups_df,
    lineup_efficiency_df=lineup_efficiency_df,
    league=current_league,
    output_dir=RESULTS_DIR,
)

nba_schedule_df = nba_schedule.build_nba_schedule(
    current_league, season_year=current_year
)
if not nba_schedule_df.empty:
    insert_nba_schedule(cursor, nba_schedule_df)
    nba_schedule_df.to_csv(RESULTS_DIR / "nba_schedule.csv", index=False)

# Predictive engine: rest-of-season projection -> player value (VORP).
predictive = predictive_engine.run_projection_pipeline(
    current_league,
    previous_league,
    schedule_df=nba_schedule_df,
    output_dir=RESULTS_DIR,
)
ros_projections_df = predictive["projections"]
player_value_df = predictive["value"]

alert_from = os.environ.get("ALERT_EMAIL_FROM")
alert_to = os.environ.get("ALERT_EMAIL_TO")
alert_app_password = os.environ.get("ALERT_EMAIL_APP_PASSWORD")

if alert_from and alert_to and alert_app_password:
    try:
        subject, body = build_digest.build_digest(
            build_matchups_df,
            breakout_players_df,
            draft_analysis_df,
            season_year=current_year,
            biggest_upsets_df=biggest_upsets_df,
        )
        send_digest_email(subject, body, alert_to, alert_from, alert_app_password)
    except Exception as error:
        print(f"Warning: failed to send digest email: {error}")
else:
    print("Skipping digest email: ALERT_EMAIL_FROM/ALERT_EMAIL_TO/ALERT_EMAIL_APP_PASSWORD not set")

