"""Forward-looking decision modes: draft board, waiver board, start/sit.

Each mode turns the shared projection pipeline
(:mod:`features.predictive_engine`) into an actionable table. The scoring
functions are pure DataFrame code and unit-tested; :func:`run` is the thin
orchestrator that talks to the ESPN ``League`` object. See
``docs/predictive_engine.md``.
"""

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from cache import _fetch_free_agents
from features.player_value import DEFAULT_ROSTER_SLOTS
from features.nba_schedule import games_per_week
from features.predictive_engine import run_projection_pipeline

DECISION_MODES = ("draft", "waivers", "startsit")

# Slots a recommended lineup fills, expanded from the roster template.
STARTING_SLOTS = [
    slot
    for slot, count in DEFAULT_ROSTER_SLOTS.items()
    for _ in range(count)
]

UNPLAYABLE_STATUSES = {"OUT", "SUSPENSION"}


# --------------------------------------------------------------------------- #
# Pure scoring functions
# --------------------------------------------------------------------------- #

def _week_games_by_team(schedule_df, week):
    """``{pro_team: games in that fantasy week}`` (empty when unknown)."""
    if week is None:
        return {}
    wide = games_per_week(schedule_df)
    if wide.empty or week not in wide.columns:
        return {}
    return wide[week].to_dict()


def draft_board(value_df, inputs_df, output_dir=None):
    """Projected value-over-replacement board with ESPN's own numbers alongside
    for comparison. Ranks a post-draft roster + free-agent pool."""
    board = value_df.merge(
        inputs_df[[
            "player_id", "pro_team", "espn_proj_avg_points",
            "prior_avg_points", "current_games_played",
        ]],
        on="player_id",
        how="left",
    )
    columns = [
        "rank", "player_name", "position", "eligible_positions", "pro_team",
        "proj_pts_per_game", "games_remaining", "vorp_per_game", "vorp_ros",
        "replacement_pts_per_game", "espn_proj_avg_points", "prior_avg_points",
        "current_games_played",
    ]
    board = board[[c for c in columns if c in board.columns]]
    board = board.sort_values("vorp_ros", ascending=False).reset_index(drop=True)
    board["rank"] = board.index + 1

    if output_dir is not None:
        board.to_csv(output_dir / "draft_board.csv", index=False)
    return board


def waiver_board(value_df, inputs_df, schedule_df, free_agent_ids, week=None,
                 output_dir=None):
    """Free agents ranked by rest-of-season VORP, with next-week value
    (projection x games that fantasy week) alongside for streamers."""
    fa = value_df[value_df["player_id"].isin(set(free_agent_ids))].copy()
    fa = fa.merge(
        inputs_df[["player_id", "pro_team"]], on="player_id", how="left"
    )

    week_games = _week_games_by_team(schedule_df, week)
    fa["next_week_games"] = fa["pro_team"].map(week_games).fillna(0).astype(int)
    fa["next_week_value"] = (
        fa["proj_pts_per_game"] * fa["next_week_games"]
    ).round(1)

    fa = fa.sort_values("vorp_ros", ascending=False).reset_index(drop=True)
    fa["ros_rank"] = fa.index + 1
    fa["next_week_rank"] = (
        fa["next_week_value"].rank(ascending=False, method="min").astype("Int64")
    )

    columns = [
        "ros_rank", "next_week_rank", "player_name", "position",
        "eligible_positions", "pro_team", "proj_pts_per_game",
        "games_remaining", "vorp_per_game", "vorp_ros",
        "next_week_games", "next_week_value",
    ]
    fa = fa[[c for c in columns if c in fa.columns]]

    if output_dir is not None:
        fa.to_csv(output_dir / "waiver_board.csv", index=False)
    return fa


def _is_unplayable(injury_status):
    return injury_status in UNPLAYABLE_STATUSES


def recommend_lineup(roster_rows, projections_df, schedule_df, week=None,
                     output_dir=None):
    """Assign a starting lineup that maximises projected points for ``week``.

    ``roster_rows`` is a list of dicts with ``player_id``, ``player_name``,
    ``position``, ``pro_team``, ``eligible_slots`` (list) and
    ``injury_status``.
    """
    df = pd.DataFrame(roster_rows)
    if df.empty:
        result = pd.DataFrame(
            columns=["player_name", "position", "pro_team", "games_this_week",
                     "proj_points_week", "recommended_slot"]
        )
        if output_dir is not None:
            result.to_csv(output_dir / "start_sit.csv", index=False)
        return result

    proj = projections_df[["player_id", "proj_pts_per_game"]]
    df = df.merge(proj, on="player_id", how="left")
    df["proj_pts_per_game"] = df["proj_pts_per_game"].fillna(0.0)

    week_games = _week_games_by_team(schedule_df, week)
    df["games_this_week"] = df["pro_team"].map(week_games).fillna(0).astype(int)
    df["proj_points_week"] = (
        df["proj_pts_per_game"] * df["games_this_week"]
    ).round(1)

    df["playable"] = ~df["injury_status"].apply(_is_unplayable)

    # Assignment: rows = players, cols = starting slots. A player can only take
    # a slot they're eligible for and are playable in.
    n_slots = len(STARTING_SLOTS)
    profit = np.full((len(df), n_slots), -1e9, dtype=float)
    for i, row in enumerate(df.itertuples(index=False)):
        if not row.playable:
            continue
        eligible = set(row.eligible_slots or [])
        for j, slot in enumerate(STARTING_SLOTS):
            if slot in eligible:
                profit[i, j] = row.proj_points_week

    df["recommended_slot"] = "BENCH"
    if len(df) and n_slots:
        rows_idx, cols_idx = linear_sum_assignment(profit, maximize=True)
        for r, c in zip(rows_idx, cols_idx):
            if profit[r, c] > -1e8:
                df.iloc[r, df.columns.get_loc("recommended_slot")] = (
                    STARTING_SLOTS[c]
                )

    df["starting"] = df["recommended_slot"] != "BENCH"
    df = df.sort_values(
        ["starting", "proj_points_week"], ascending=[False, False]
    ).reset_index(drop=True)

    result = df[[
        "player_name", "position", "pro_team", "games_this_week",
        "proj_points_week", "recommended_slot",
    ]]
    if output_dir is not None:
        result.to_csv(output_dir / "start_sit.csv", index=False)
    return result


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _current_week(current_league):
    return getattr(
        current_league,
        "currentMatchupPeriod",
        getattr(current_league, "current_week", None),
    )


def _find_team(current_league, team_id):
    for team in current_league.teams:
        if team.team_id == team_id:
            return team
    raise ValueError(f"team_id {team_id!r} not found in league")


def _roster_row(player):
    return {
        "player_id": getattr(player, "playerId", None),
        "player_name": getattr(player, "name", None),
        "position": getattr(player, "position", None),
        "pro_team": getattr(player, "proTeam", None),
        "eligible_slots": list(getattr(player, "eligibleSlots", []) or []),
        "injury_status": getattr(player, "injuryStatus", None),
    }


def run(mode, current_league, previous_league=None, week=None, team_id=None,
        output_dir=None):
    """Entry point for ``main.py --mode <mode>``. Returns the mode's DataFrame
    and writes its CSV to ``output_dir``."""
    if mode not in DECISION_MODES:
        raise ValueError(f"unknown mode {mode!r}; choose from {DECISION_MODES}")

    week = week if week is not None else _current_week(current_league)

    free_agents = []
    if mode in ("draft", "waivers"):
        free_agents = _fetch_free_agents(current_league)
    free_agent_ids = {getattr(p, "playerId", None) for p in free_agents}

    pipeline = run_projection_pipeline(
        current_league,
        previous_league,
        extra_players=free_agents or None,
        output_dir=output_dir,
    )

    if mode == "draft":
        return draft_board(pipeline["value"], pipeline["inputs"], output_dir)
    if mode == "waivers":
        return waiver_board(
            pipeline["value"], pipeline["inputs"], pipeline["schedule"],
            free_agent_ids, week, output_dir,
        )
    # startsit
    if team_id is None:
        raise ValueError(
            "startsit mode needs a team id (set ESPN_TEAM_ID or pass --team)"
        )
    team = _find_team(current_league, team_id)
    roster_rows = [_roster_row(p) for p in team.roster]
    return recommend_lineup(
        roster_rows, pipeline["projections"], pipeline["schedule"], week,
        output_dir,
    )
