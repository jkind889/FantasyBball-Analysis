import pandas as pd


BREAKOUT_COLUMNS = [
    "player_id",
    "player_name",
    "position",
    "pro_team",
    "current_avg_points",
    "previous_avg_points",
    "avg_points_jump",
    "current_games_played",
    "previous_games_played",
    "current_total_points",
    "previous_total_points",
    "source_previous_season",
]


def build_breakout_players(
    current_league,
    previous_league,
    min_current_gp=41,
    min_previous_gp=20,
):
    current_players = _dedupe_players(
        _rostered_players(current_league) + _fetch_free_agents(current_league)
    )
    previous_rostered = _index_players(_rostered_players(previous_league))
    free_agent_cache = {}

    rows = []
    for current_player in current_players:
        current_summary = _player_summary(current_player)
        if current_summary["games_played"] < min_current_gp:
            continue

        previous_player, previous_source = _find_previous_player(
            current_player,
            previous_league,
            previous_rostered,
            free_agent_cache,
        )
        if previous_player is None:
            continue

        previous_summary = _player_summary(previous_player)
        if previous_summary["games_played"] < min_previous_gp:
            continue

        rows.append(
            {
                "player_id": current_summary["player_id"],
                "player_name": current_summary["player_name"],
                "position": current_summary["position"],
                "pro_team": current_summary["pro_team"],
                "current_avg_points": current_summary["avg_points"],
                "previous_avg_points": previous_summary["avg_points"],
                "avg_points_jump": (
                    current_summary["avg_points"] - previous_summary["avg_points"]
                ),
                "current_games_played": current_summary["games_played"],
                "previous_games_played": previous_summary["games_played"],
                "current_total_points": current_summary["total_points"],
                "previous_total_points": previous_summary["total_points"],
                "source_previous_season": previous_source,
            }
        )

    if not rows:
        return pd.DataFrame(columns=BREAKOUT_COLUMNS)

    breakout_df = pd.DataFrame(rows)
    breakout_df["_dedupe_key"] = breakout_df.apply(_dedupe_key_from_row, axis=1)

    return (
        breakout_df.drop_duplicates(subset=["_dedupe_key"], keep="first")
        .sort_values(
            by=["avg_points_jump", "current_avg_points", "current_games_played"],
            ascending=[False, False, False],
        )
        .reset_index(drop=True)[BREAKOUT_COLUMNS]
    )


def _rostered_players(league):
    # Return a flat list of all players on every team's roster in `league`.
    # Used to gather the pool of currently rostered players for comparisons.
    players = []
    for team in league.teams:
        players.extend(team.roster)
    return players


def _fetch_free_agents(league, position=None):
    # Fetch free agents from the league. Different league APIs may accept
    # different argument signatures, so try the size+position call first and
    # fall back to alternative signatures on TypeError.
    try:
        return league.free_agents(size=500, position=position)
    except TypeError:
        if position is None:
            return league.free_agents()
        return league.free_agents(position=position)


def _find_previous_player(
    current_player,
    previous_league,
    previous_rostered,
    free_agent_cache,
):
    # Try to find the matching player in the previous season.
    # 1) Prefer exact `playerId` matches in the previous roster index.
    # 2) If missing, fall back to a position-scoped free-agent index cached
    #    in `free_agent_cache` to avoid repeated expensive fetches/indexing.
    # The function returns (player_or_None, source) where source is
    # one of "roster", "free_agent", or "missing".
    player_id = _player_id(current_player)
    player_name = _player_name(current_player)

    previous_player = _lookup_player(previous_rostered, player_id, player_name)
    if previous_player is not None:
        return previous_player, "roster"

    # Build a cache key per previous season year and position so that free
    # agents are fetched and indexed at most once per (year, position).
    position = getattr(current_player, "position", None)
    cache_key = (getattr(previous_league, "year", None), position)
    if cache_key not in free_agent_cache:
        free_agent_cache[cache_key] = _index_players(
            _fetch_free_agents(previous_league, position=position)
        )

    previous_player = _lookup_player(free_agent_cache[cache_key], player_id, player_name)
    if previous_player is not None:
        return previous_player, "free_agent"

    return None, "missing"


def _index_players(players):
    # Create two lookup maps for fast matching:
    # - by_id: maps playerId -> player (when available)
    # - by_name: maps normalized name -> player (fallback)
    by_id = {}
    by_name = {}
    for player in players:
        player_id = _player_id(player)
        if player_id is not None:
            by_id[player_id] = player

        player_name = _normalized_name(_player_name(player))
        if player_name:
            by_name[player_name] = player

    return {"by_id": by_id, "by_name": by_name}


def _lookup_player(index, player_id, player_name):
    # Look up by id first (most reliable). If id is missing or not found,
    # normalize the name and try name-based lookup. Returns None if no match.
    if player_id is not None and player_id in index["by_id"]:
        return index["by_id"][player_id]

    normalized_name = _normalized_name(player_name)
    if normalized_name:
        return index["by_name"].get(normalized_name)

    return None


def _dedupe_players(players):
    # Remove duplicate player objects while preserving order.
    # Preference: unique `playerId` when present; otherwise dedupe by
    # normalized name. First-seen entries are kept.
    seen_ids = set()
    seen_names = set()
    deduped = []

    for player in players:
        player_id = _player_id(player)
        player_name = _normalized_name(_player_name(player))

        if player_id is not None:
            if player_id in seen_ids:
                continue
            seen_ids.add(player_id)
        elif player_name:
            if player_name in seen_names:
                continue
            seen_names.add(player_name)

        deduped.append(player)

    return deduped


def _dedupe_key_from_row(row):
    # Produce a stable key used to drop duplicate rows in the final DF.
    # Prefer id-based keys when available, else use normalized name.
    if row["player_id"] is not None:
        return f"id:{row['player_id']}"
    return f"name:{_normalized_name(row['player_name'])}"


def _player_summary(player):
    season_stats = _season_stats(player)
    total_stats = season_stats.get("total", {})

    return {
        "player_id": _player_id(player),
        "player_name": _player_name(player),
        "position": getattr(player, "position", None),
        "pro_team": getattr(player, "proTeam", None),
        "avg_points": _number(getattr(player, "avg_points", None)),
        "total_points": _number(getattr(player, "total_points", None)),
        "games_played": int(_number(total_stats.get("GP", 0))),
    }


def _season_stats(player):
    # Extract the season totals block from `player.stats`:
    # - If `player.year` exists and a matching "{year}_total" key exists,
    #   return that block (preferred).
    # - Otherwise, return the first key that ends with "_total".
    # This is used to obtain GP and other aggregate fields.
    stats = getattr(player, "stats", {}) or {}
    year = getattr(player, "year", None)
    if year is not None:
        season_key = f"{year}_total"
        if season_key in stats:
            return stats.get(season_key, {}) or {}

    for key, value in stats.items():
        if str(key).endswith("_total"):
            return value or {}

    return {}


def _number(value):
    # Normalize numeric-like fields so arithmetic doesn't crash on None.
    if value is None:
        return 0
    return value


def _player_id(player):
    return getattr(player, "playerId", None)


def _player_name(player):
    return getattr(player, "name", None)


def _normalized_name(name):
    # Lowercase and collapse whitespace to create a canonical name key.
    if not name:
        return None
    return " ".join(str(name).lower().split())
