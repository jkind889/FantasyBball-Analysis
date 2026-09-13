import argparse
import json
import os
import sys
import warnings
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
    default=None,
    help="your team id for startsit or the waiver advisor (default: ESPN_TEAM_ID)",
)
parser.add_argument("--recent-window", type=int, choices=(5, 6), default=None,
                    help="recent appearances used by the waiver advisor")
parser.add_argument("--protect", type=int, action="append", default=[], metavar="PLAYER_ID",
                    help="protect a rostered player from waiver drops (repeatable)")
parser.add_argument("--waiver-config", type=Path, help="JSON rule/player overrides for live waiver advice")
parser.add_argument("--snapshot", type=Path, help="replay a saved waiver snapshot without ESPN or MySQL")
parser.add_argument("--basic-waivers", action="store_true", help="use the original non-roster-aware waiver board")
parser.add_argument("--historical-date", help="reconstruct a previous-season waiver demo at local midnight (YYYY-MM-DD)")
parser.add_argument("--season", type=int, help="ESPN season ending year for a historical demo")
parser.add_argument("--output-dir", type=Path, help="directory for waiver outputs (historical demos default to a separate folder)")
args = parser.parse_args()

if (args.snapshot or args.waiver_config or args.protect or args.basic_waivers or args.recent_window is not None or args.historical_date or args.season or args.output_dir) and args.mode != "waivers":
    parser.error("Waiver options require --mode waivers")
if args.basic_waivers and (args.snapshot or args.waiver_config or args.protect or args.recent_window is not None or args.historical_date):
    parser.error("--basic-waivers cannot be combined with advisor options")
if args.season is not None and not args.historical_date:
    parser.error("--season requires --historical-date")
if args.historical_date and (args.snapshot or args.waiver_config):
    parser.error("--historical-date cannot be combined with --snapshot or --waiver-config")
if args.output_dir:
    RESULTS_DIR = args.output_dir
if args.snapshot:
    if args.waiver_config:
        parser.error("Replay uses the rules stored in the snapshot; do not combine --snapshot and --waiver-config")
    from features.waiver_advisor import advise_waivers, write_advice
    try:
        snapshot = json.loads(args.snapshot.read_text())
        if args.team is not None and args.team != snapshot["team_id"]:
            parser.error("--team must match the saved snapshot")
        if args.week is not None and args.week != snapshot["week"]:
            parser.error("--week must match the saved snapshot")
        result = advise_waivers(snapshot, window=args.recent_window, protected_ids=args.protect)
        if snapshot.get("mode") == "historical_demo" and not args.output_dir:
            RESULTS_DIR = Path("reports/historical") / str(snapshot["season_year"]) / f"team-{snapshot['team_id']}" / str(snapshot["as_of"])[:10]
        write_advice(snapshot, result, RESULTS_DIR)
        if snapshot.get("mode") == "historical_demo":
            from features.historical_waivers import write_historical_summary
            actuals_path = args.snapshot.with_name("historical_actuals.json")
            actuals = json.loads(actuals_path.read_text()) if actuals_path.exists() else []
            write_historical_summary(result, actuals, RESULTS_DIR)
            if actuals_path.exists():
                (RESULTS_DIR / "historical_actuals.json").write_text(json.dumps(actuals, indent=2) + "\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(f"Snapshot replay as of {snapshot['as_of']}: {RESULTS_DIR / 'waiver_recommendations.csv'}")
    print(result["recommendations"][["rank", "action", "add_name", "drop_name", "net_gain", "status", "recommended"]].head(20).to_string(index=False))
    sys.exit(0)

waiver_config = None
if args.team is None and os.environ.get("ESPN_TEAM_ID"):
    try:
        args.team = int(os.environ["ESPN_TEAM_ID"])
    except ValueError:
        parser.error("ESPN_TEAM_ID must be an integer")
if args.waiver_config:
    try:
        waiver_config = json.loads(args.waiver_config.read_text())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
if args.mode == "waivers" and not args.team and (waiver_config or args.protect or args.recent_window is not None):
    parser.error("The waiver advisor requires --team or ESPN_TEAM_ID")

if args.historical_date:
    if args.team is None:
        parser.error("Historical demos require --team or ESPN_TEAM_ID")
    from espn_api.requests.espn_requests import EspnFantasyRequests, ESPNUnknownError, ESPNAccessDenied
    from features.historical_waivers import fetch_historical_snapshot, write_historical_summary
    from features.waiver_advisor import advise_waivers, write_advice
    try:
        year = args.season if args.season is not None else int(os.environ["ESPN_YEAR"])
        league_id = int(os.environ["ESPN_LEAGUE_ID"])
        request = EspnFantasyRequests(sport="nba", year=year, league_id=league_id,
                                     cookies={"espn_s2": os.environ["ESPN_S2"], "SWID": os.environ["ESPN_SWID"]})
        snapshot, actuals = fetch_historical_snapshot(request, year, league_id, args.team, args.historical_date,
                                                      progress=lambda message: print(message, flush=True))
        if args.week is not None and args.week != snapshot["week"]:
            parser.error("--week does not match the historical date")
        result = advise_waivers(snapshot, window=args.recent_window, protected_ids=args.protect)
        if not args.output_dir:
            RESULTS_DIR = Path("reports/historical") / str(year) / f"team-{args.team}" / args.historical_date
        write_advice(snapshot, result, RESULTS_DIR)
        write_historical_summary(result, actuals, RESULTS_DIR)
        (RESULTS_DIR / "historical_actuals.json").write_text(json.dumps(actuals, indent=2) + "\n")
    except (OSError, ValueError, KeyError, TypeError, ESPNUnknownError, ESPNAccessDenied) as exc:
        parser.error(str(exc))
    print(f"Historical demonstration for {snapshot['team_name']}: {RESULTS_DIR / 'historical_demo.md'}")
    print("Reconstructed assumptions apply; these are simulated moves, not verified historical waiver opportunities.")
    print(result["recommendations"][["rank", "add_name", "drop_name", "net_gain", "status"]].head(10).to_string(index=False))
    sys.exit(0)

current_year = int(os.environ["ESPN_YEAR"])

current_league = League(
    league_id=int(os.environ["ESPN_LEAGUE_ID"]),
    year=current_year,
    espn_s2=os.environ["ESPN_S2"],
    swid=os.environ["ESPN_SWID"],
)


try:
    previous_league = League(
        league_id=int(os.environ["ESPN_LEAGUE_ID"]),
        year=current_year - 1,
        espn_s2=os.environ["ESPN_S2"],
        swid=os.environ["ESPN_SWID"],
    )
except Exception as exc:
    if args.mode not in decision_modes.DECISION_MODES:
        raise
    warnings.warn(f"Previous season unavailable ({type(exc).__name__}); using current/ESPN projections")
    previous_league = None

OUTPUT_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)


if args.mode in decision_modes.DECISION_MODES:
    team_id = int(args.team) if args.team is not None and not args.basic_waivers else None
    try:
        report_df = decision_modes.run(
            args.mode, current_league, previous_league, week=args.week,
            team_id=team_id, output_dir=RESULTS_DIR, waiver_config=waiver_config,
            recent_window=args.recent_window, protected_ids=args.protect,
        )
    except ValueError as exc:
        parser.error(str(exc))
    csv_name = {
        "draft": "draft_board.csv",
        "waivers": "waiver_recommendations.csv" if team_id is not None else "waiver_board.csv",
        "startsit": "start_sit.csv",
    }[args.mode]
    print(f"--mode {args.mode}: wrote {RESULTS_DIR / csv_name} ({len(report_df)} rows)")
    columns = ["rank", "action", "add_name", "drop_name", "net_gain", "status", "recommended", "explanation"]
    display = report_df[columns] if "action" in report_df else report_df
    print(display.head(20).to_string(index=False))
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
