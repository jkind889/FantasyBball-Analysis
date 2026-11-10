"""Provider season forecasts, separate from the project's modeled ROS estimates.

espn-api's Player defaults absent fields to zero. Raw player cards are the only
way to distinguish an explicitly supplied zero from those defaults reliably.
"""

from datetime import datetime, timezone
from math import isfinite
from uuid import uuid4
import warnings

import pandas as pd
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError
from requests.exceptions import RequestException


PROJECTION_COLUMNS = [
    "projected_total_points", "projected_avg_points", "projection_season",
    "projection_horizon", "projection_source", "projection_observed_at",
]


def finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def empty_projection():
    return dict.fromkeys(PROJECTION_COLUMNS)


def raw_projection(player, season_year, observed_at):
    """Accept only ESPN's requested-season, full-season projection split.

    A missing total is never reconstructed from averages, games, or actuals.
    Conflicting duplicate splits are ambiguous, so return unavailable.
    """
    if not isinstance(player, dict) or not isinstance(player.get("stats", []), list):
        return empty_projection()
    candidates = []
    for split in player.get("stats", []):
        if not isinstance(split, dict):
            continue
        if (split.get("seasonId") != season_year
                or split.get("statSourceId") != 1
                or split.get("statSplitTypeId") != 0):
            continue
        values = (finite_number(split.get("appliedTotal")),
                  finite_number(split.get("appliedAverage")))
        if values != (None, None):
            candidates.append(values)
    if not candidates or len(set(candidates)) != 1:
        return empty_projection()
    total, average = candidates[0]
    return dict(zip(PROJECTION_COLUMNS, [
        total, average, season_year, "full_season", "espn_player_card",
        observed_at,
    ]))


def player_projection(player, season_year):
    """Read verified raw metadata, or conservative evidence from Player.stats.

    Nonzero normalized fields cannot be library zero defaults. A normalized
    zero is ambiguous and must await raw evidence; bare convenience attributes
    (even nonzero ones) do not establish the season or horizon.
    """
    verified = getattr(player, "provider_projection", None)
    if verified is not None:
        if (not isinstance(verified, dict)
                or verified.get("projection_season") != season_year
                or verified.get("projection_horizon") != "full_season"
                or not isinstance(verified.get("projection_source"), str)
                or not verified["projection_source"].strip()
                or not isinstance(verified.get("projection_observed_at"), (str, datetime))):
            return empty_projection()
        if pd.isna(pd.to_datetime(verified["projection_observed_at"], errors="coerce", utc=True)):
            return empty_projection()
        result = {key: verified.get(key) for key in PROJECTION_COLUMNS}
        for key in ("projected_total_points", "projected_avg_points"):
            result[key] = finite_number(result[key])
        return result
    all_stats = getattr(player, "stats", {})
    if not isinstance(all_stats, dict):
        return empty_projection()
    stats = all_stats.get(f"{season_year}_projected", {})
    if not isinstance(stats, dict):
        return empty_projection()
    total = finite_number(stats.get("applied_total"))
    average = finite_number(stats.get("applied_avg"))
    total = total if total != 0 else None
    average = average if average != 0 else None
    if total is None and average is None:
        return empty_projection()
    return dict(zip(PROJECTION_COLUMNS, [
        total, average, season_year, "full_season", "espn_api_season_split",
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
    ]))


def fetch_projections(league, player_ids, batch_size=50):
    """Read raw cards in bounded batches; failures never manufacture forecasts."""
    ids = sorted(set(player_ids))
    rows = {}
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        try:
            data = league.espn_request.get_player_card(batch, league.finalScoringPeriod)
            for entry in data["players"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("player"), dict):
                    continue
                player = entry["player"]
                player_id = player.get("id")
                if player_id not in batch:
                    continue
                projection = raw_projection(player, league.year, observed_at)
                row = {"player_id": player_id, **projection}
                if player_id in rows and rows[player_id] != row:
                    row = {"player_id": player_id, **empty_projection()}
                rows[player_id] = row
        except (ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError, RequestException,
                KeyError, TypeError, ValueError, OSError) as exc:
            # Do not print exception messages: provider errors may contain URLs
            # or account details. A prior stored snapshot is still usable.
            warnings.warn(f"Provider projections unavailable ({type(exc).__name__}); retaining stored snapshots")
    return pd.DataFrame(rows.values(), columns=["player_id"] + PROJECTION_COLUMNS)


def require_projection_schema(cursor):
    """Check before main's existing writes; never create/migrate a live table."""
    cursor.execute("""
        SELECT snapshot_id, league_id, player_id, season_year, horizon, source,
               observed_at, projected_total_points, projected_avg_points
        FROM provider_projection_snapshots WHERE 1 = 0
    """)
    cursor.fetchall()


def save_projection_snapshots(cursor, rows, league_id):
    """Append available observations. The caller owns the transaction.

    Missing responses do not overwrite previous forecasts. Values from different
    observations are never coalesced into an invented provider snapshot.
    """
    for row in rows.to_dict("records"):
        total = finite_number(row.get("projected_total_points"))
        average = finite_number(row.get("projected_avg_points"))
        if total is None and average is None:
            continue
        if (row.get("projection_horizon") != "full_season"
                or not row.get("projection_source")
                or not row.get("projection_observed_at")
                or finite_number(row.get("projection_season")) is None):
            raise ValueError("Projection snapshots require season, horizon, source and observation time")
        cursor.execute("""
            INSERT INTO provider_projection_snapshots
                (snapshot_id, league_id, player_id, season_year, horizon, source,
                 observed_at, projected_total_points, projected_avg_points)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (str(uuid4()), league_id, int(row["player_id"]),
              int(row["projection_season"]), row["projection_horizon"],
              row["projection_source"], row["projection_observed_at"], total, average))


def load_projection_snapshots(conn, league_id, season_year):
    """Latest available season-total snapshot per player, scoped to the league.

    Prefer a total-bearing snapshot for retrospective comparisons. If only an
    average exists, retain it as partial data without estimating a total.
    """
    rows = pd.read_sql("""
        SELECT player_id, projected_total_points, projected_avg_points,
               season_year AS projection_season, horizon AS projection_horizon,
               source AS projection_source, observed_at AS projection_observed_at
        FROM provider_projection_snapshots
        WHERE league_id = %s AND season_year = %s AND horizon = 'full_season'
        ORDER BY (projected_total_points IS NOT NULL) DESC, observed_at DESC, snapshot_id DESC
    """, conn, params=(league_id, season_year))
    return rows.drop_duplicates("player_id", keep="first")


def attach_projections(players_df, projections_df):
    """Replace the projection bundle atomically by player ID, preserving zeros."""
    return players_df.drop(columns=PROJECTION_COLUMNS, errors="ignore").merge(
        projections_df[["player_id"] + PROJECTION_COLUMNS], on="player_id", how="left",
        validate="many_to_one",
    )
