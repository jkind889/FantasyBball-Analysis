import pandas as pd

def _player_id(row):
    return row.get("player_id")


def _player_name(row):
    return row.get("player_name")

def _normalized_name(name):
    if not name:
        return None
    return " ".join(str(name).lower().split())

def _index_players(rows):
    by_id = {}
    by_name = {}
    for row in rows:
        row_player_id = _player_id(row)
        if row_player_id is not None:
            by_id[row_player_id] = row

        player_name = _normalized_name(_player_name(row))
        if player_name:
            by_name[player_name] = row

    return {"by_id": by_id, "by_name": by_name}

def _lookup_player(index, row_player_id, row_player_name):
    if row_player_id is not None and row_player_id in index["by_id"]:
        return index["by_id"][row_player_id]

    normalized_name = _normalized_name(row_player_name)
    if normalized_name:
        return index["by_name"].get(normalized_name)

    return None


def _number(value):
    if value is None:
        return 0
    return value


def _player_summary(row):
    return {
        "player_id": row.get("player_id"),
        "player_name": row.get("player_name"),
        "position": row.get("position"),
        "pro_team": row.get("pro_team"),
        "avg_points": _number(row.get("avg_points",None)),
        "total_points": _number(row.get("total_points",None)),
        "games_played": int(_number(row.get("games_played",None))),
    }



def espn_player_to_row(player,season_year):
    # convert espn objects to dict to insert in database
    if season_year is None:
        season_year = getattr(player, "year", None)

    season_key =  f"{season_year}_total"
    stats = getattr(player, "stats", {}) or {}
    season_stats = stats.get(season_key, {})
    if not season_stats:
        for key, value in stats.items():
            if str(key).endswith("_total"):
                season_stats = value or {}
                break
    total_stats = season_stats.get("total", {})

    return {
        "player_id": getattr(player, "playerId", None),
        "player_name": getattr(player, "name", None),
        "position": getattr(player, "position", None),
        "pro_team": getattr(player, "proTeam", None),
        "pos_rank": getattr(player, "posRank", None),
        "avg_points": getattr(player, "avg_points", None),
        "total_points": getattr(player, "total_points", None),
        "projected_total_points": getattr(player, "projected_total_points", None),
        "projected_avg_points": getattr(player, "projected_avg_points", None),
        "games_played": total_stats.get("GP", 0),
    }



def _fetch_free_agents(league, position=None):
    try:
        return league.free_agents(size=500, position=position)
    except TypeError:
        if position is None:
            return league.free_agents()
        return league.free_agents(position=position)
    
def _fetch_previous_rostered_players(conn, previous_year):
    return pd.read_sql(
        """
        SELECT
            p.player_id,
            p.player_name,
            p.position,
            p.pro_team,
            ps.avg_points,
            ps.total_points,
            ps.games_played
        FROM players AS p
        JOIN player_season AS ps
            ON ps.player_id = p.player_id
        WHERE ps.season_year = %s
        """,
        conn,
        params=(previous_year,),
    )

class BreakoutPlayerCache:
    def __init__(self, conn, previous_year, previous_league=None):
        self.conn = conn
        self.previous_year = previous_year
        self.previous_league = previous_league
        self.previous_players = _index_players(
            _fetch_previous_rostered_players(conn, previous_year).to_dict("records")
        )
        self.free_agents_by_position = {}


    def find_previous_player(self, current_player_row):
        previous_player = _lookup_player(
            self.previous_players,
            _player_id(current_player_row),
            _player_name(current_player_row),
        )

        if previous_player is not None:
            return previous_player, "database"

        if self.previous_league is None:
            return None, "missing"

        previous_player = self._find_previous_free_agent(current_player_row)
        if previous_player is not None:
            return previous_player, "free_agent"

        return None, "missing"

    def _find_previous_free_agent(self, current_player_row):
        position = current_player_row.get("position")
        cache_key = (self.previous_year, position)

        if cache_key not in self.free_agents_by_position:
            free_agent_rows = [
                espn_player_to_row(player, self.previous_year)
                for player in _fetch_free_agents(self.previous_league, position=position)
            ]
            self.free_agents_by_position[cache_key] = _index_players(free_agent_rows)

        return _lookup_player(
            self.free_agents_by_position[cache_key],
            _player_id(current_player_row),
            _player_name(current_player_row),
        )
