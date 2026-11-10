"""Shared rest-of-season projection -> player value pipeline.

Both the weekly run (``main.py`` with no ``--mode``) and the decision modes
(``--mode draft|waivers|startsit``) need the same chain:

    NBA schedule -> games remaining -> ROS projection -> value over replacement

This wraps that chain once so the two entry points stay in sync. See
``docs/predictive_engine.md``.
"""

import features.nba_schedule as nba_schedule
import features.player_value as player_value
import features.projection_inputs as projection_inputs
import features.projections as projections


def run_projection_pipeline(current_league, previous_league=None,
                            schedule_df=None, extra_players=None,
                            output_dir=None):
    """Return ``{"schedule", "inputs", "projections", "value"}`` DataFrames.

    ``schedule_df`` is built from ``current_league`` when not supplied.
    ``extra_players`` (e.g. free agents) are projected alongside rostered
    players so replacement levels are set against the same pool.
    """
    if schedule_df is None:
        schedule_df = nba_schedule.build_nba_schedule(
            current_league, season_year=getattr(current_league, "year", None)
        )

    inputs_df = projection_inputs.build_projection_inputs(
        current_league, previous_league, extra_players=extra_players
    )
    if schedule_df is not None and not schedule_df.empty:
        inputs_df = nba_schedule.attach_games_remaining(inputs_df, schedule_df)

    projections_df = projections.project_rest_of_season(
        inputs_df, output_dir=output_dir
    )
    value_df = player_value.compute_player_value(
        projections_df,
        league_size=len(current_league.teams),
        output_dir=output_dir,
    )
    return {
        "schedule": schedule_df,
        "inputs": inputs_df,
        "projections": projections_df,
        "value": value_df,
    }
