"""Assemble the per-player input frame for :mod:`features.projections`.

For every player, this pulls together the columns ``project_rest_of_season``
blends:

- current-season points-per-game and games played
- ESPN's own projected points-per-game
- prior-season points-per-game (from last year's rosters)

Kept separate from ``projections.py`` because this part touches ESPN ``Player``
objects; ``projections.py`` stays a pure DataFrame function. See
``docs/predictive_engine.md``.
"""

import pandas as pd

from cache import _index_players, _lookup_player, espn_player_to_row

PROJECTION_INPUT_COLUMNS = [
    "player_id",
    "player_name",
    "position",
    "pro_team",
    "prior_avg_points",
    "current_avg_points",
    "current_games_played",
    "espn_proj_avg_points",
]


def _season_games_played(player, season_year):
    stats = getattr(player, "stats", {}) or {}
    season = stats.get(f"{season_year}_total", {}) or {}
    total = season.get("total", {}) or {}
    return total.get("GP", 0) or 0


def build_prior_index(previous_league):
    """Name/id index of last season's rostered players, for prior-year pace."""
    if previous_league is None:
        return {"by_id": {}, "by_name": {}}
    prior_year = getattr(previous_league, "year", None)
    rows = [
        espn_player_to_row(player, prior_year)
        for team in previous_league.teams
        for player in team.roster
    ]
    return _index_players(rows)


def projection_input_rows(players, prior_index, season_year):
    """Turn an iterable of ESPN ``Player`` objects into input dicts, de-duped
    by ``playerId``."""
    rows = []
    seen = set()
    for player in players:
        player_id = getattr(player, "playerId", None)
        if player_id in seen:
            continue
        seen.add(player_id)

        player_name = getattr(player, "name", None)
        prior = _lookup_player(prior_index, player_id, player_name) or {}

        rows.append(
            {
                "player_id": player_id,
                "player_name": player_name,
                "position": getattr(player, "position", None),
                "pro_team": getattr(player, "proTeam", None),
                "prior_avg_points": prior.get("avg_points"),
                "current_avg_points": getattr(player, "avg_points", None),
                "current_games_played": _season_games_played(player, season_year),
                "espn_proj_avg_points": getattr(
                    player, "projected_avg_points", None
                ),
            }
        )
    return rows


def build_projection_inputs(current_league, previous_league=None,
                            extra_players=None):
    """One row per rostered current-season player (plus any ``extra_players``,
    e.g. free agents), ready for
    :func:`features.projections.project_rest_of_season`."""
    prior_index = build_prior_index(previous_league)
    current_year = getattr(current_league, "year", None)

    players = [
        player
        for team in current_league.teams
        for player in team.roster
    ]
    if extra_players:
        players = players + list(extra_players)

    rows = projection_input_rows(players, prior_index, current_year)
    return pd.DataFrame(rows, columns=PROJECTION_INPUT_COLUMNS)
