"""NBA team schedule -> games remaining and games per fantasy week.

The one genuinely new data source for the predictive engine. ESPN already hands
us the full pro-team schedule on the ``League`` object (``league.pro_schedule``);
this module flattens it into a tidy per-team-per-game table and derives the two
numbers the rest of the engine needs:

- **games remaining** per pro team as of a date  -> exact ``games_remaining``
  input for :mod:`features.projections`
- **games per fantasy week** per pro team        -> streaming value (a 4-game
  week is worth more than a 2-game week)

Pure functions. ``build_nba_schedule`` accepts either a ``League`` or the raw
``pro_schedule`` dict so it can be unit-tested without network access.
See ``docs/predictive_engine.md``.
"""

from datetime import date, datetime

import pandas as pd

from espn_api.basketball.constant import PRO_TEAM_MAP

SCHEDULE_COLUMNS = [
    "season_year",
    "pro_team",
    "opponent",
    "is_home",
    "game_date",
    "scoring_period",
    "fantasy_week",
]

# Days in a standard fantasy-basketball scoring week; used only when an explicit
# scoring-period -> matchup-period map is not available (e.g. pre-season, before
# ESPN has published played weeks).
DEFAULT_WEEK_LENGTH = 7

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS nba_schedule (
    season_year    INT          NOT NULL,
    pro_team       VARCHAR(8)   NOT NULL,
    opponent       VARCHAR(8)   NOT NULL,
    is_home        TINYINT(1)   NOT NULL,
    game_date      DATE         NOT NULL,
    scoring_period INT          NOT NULL,
    fantasy_week   INT          NOT NULL,
    PRIMARY KEY (season_year, pro_team, game_date)
)
"""


def _scoring_period_to_week(matchup_ids):
    """Reverse ``{matchup_period: [scoring_period, ...]}`` into
    ``{scoring_period: matchup_period}``. Returns ``{}`` when unavailable."""
    if not matchup_ids:
        return {}
    reverse = {}
    for week, periods in matchup_ids.items():
        for sp in periods:
            reverse[int(sp)] = int(week)
    return reverse


def _resolve_week(scoring_period, reverse_map, first_period):
    if scoring_period in reverse_map:
        return reverse_map[scoring_period]
    return (scoring_period - first_period) // DEFAULT_WEEK_LENGTH + 1


def _raw_pro_schedule(source):
    if isinstance(source, dict):
        return source
    pro_schedule = getattr(source, "pro_schedule", None)
    if pro_schedule is None:
        raise ValueError(
            "source must be a League with a populated pro_schedule or the raw "
            "pro_schedule dict"
        )
    return pro_schedule


def build_nba_schedule(source, season_year=None, matchup_ids=None):
    """Flatten ESPN's pro schedule into one row per (team, game).

    Each game appears twice — once from each team's perspective — which is what
    downstream per-team aggregates want.
    """
    pro_schedule = _raw_pro_schedule(source)
    if season_year is None:
        season_year = getattr(source, "year", None)

    if matchup_ids is None:
        matchup_ids = getattr(source, "matchup_ids", None)
    reverse_map = _scoring_period_to_week(matchup_ids)

    rows = []
    for pro_team_id, games_by_period in (pro_schedule or {}).items():
        if pro_team_id == 0:  # free-agent / no team
            continue
        for period_key, games in (games_by_period or {}).items():
            try:
                scoring_period = int(period_key)
            except (TypeError, ValueError):
                continue
            for game in games or []:
                home_id = game.get("homeProTeamId")
                away_id = game.get("awayProTeamId")
                is_home = pro_team_id == home_id
                opponent_id = away_id if is_home else home_id
                ts = game.get("date")
                if ts is None:
                    continue
                game_dt = datetime.fromtimestamp(ts / 1000.0).date()
                rows.append(
                    {
                        "season_year": season_year,
                        "pro_team": PRO_TEAM_MAP.get(pro_team_id, str(pro_team_id)),
                        "opponent": PRO_TEAM_MAP.get(opponent_id, str(opponent_id)),
                        "is_home": int(is_home),
                        "game_date": game_dt,
                        "scoring_period": scoring_period,
                        "fantasy_week": None,
                    }
                )

    if not rows:
        return pd.DataFrame(columns=SCHEDULE_COLUMNS)

    df = pd.DataFrame(rows)
    first_period = int(df["scoring_period"].min())
    df["fantasy_week"] = df["scoring_period"].apply(
        lambda sp: _resolve_week(sp, reverse_map, first_period)
    )
    df = df.drop_duplicates(
        subset=["season_year", "pro_team", "game_date"]
    ).sort_values(["pro_team", "game_date"]).reset_index(drop=True)
    return df[SCHEDULE_COLUMNS]


def games_remaining(schedule_df, as_of=None):
    """``{pro_team: games with game_date >= as_of}`` (today if ``as_of`` is None)."""
    if schedule_df is None or schedule_df.empty:
        return {}
    if as_of is None:
        as_of = date.today()
    game_dates = pd.to_datetime(schedule_df["game_date"]).dt.date
    upcoming = schedule_df[game_dates >= as_of]
    return upcoming.groupby("pro_team").size().to_dict()


def games_per_week(schedule_df):
    """Wide table: rows = pro team, columns = fantasy week, values = game count."""
    if schedule_df is None or schedule_df.empty:
        return pd.DataFrame()
    return (
        schedule_df.pivot_table(
            index="pro_team",
            columns="fantasy_week",
            values="game_date",
            aggfunc="count",
            fill_value=0,
        )
        .astype(int)
        .sort_index()
    )


def attach_games_remaining(players_df, schedule_df, as_of=None,
                           pro_team_col="pro_team", out_col="games_remaining"):
    """Add/overwrite ``out_col`` on ``players_df`` from each player's pro team.

    Bridges this module to :func:`features.projections.project_rest_of_season`.
    Players whose team can't be matched are left as NaN for the caller's default.
    """
    remaining = games_remaining(schedule_df, as_of=as_of)
    df = players_df.copy()
    df[out_col] = df[pro_team_col].map(remaining)
    return df
