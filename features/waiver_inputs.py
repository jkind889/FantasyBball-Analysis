"""ESPN read adapter for waiver snapshots. Network access stays in fetch_snapshot.

Raw player cards retain actual per-day stats and player-pool availability fields
that the espn-api Player wrapper otherwise discards. No raw responses are saved.
"""

import json
import warnings
from collections import defaultdict
from types import SimpleNamespace

import pandas as pd
from espn_api.basketball.constant import POSITION_MAP
from espn_api.basketball.player import Player
from espn_api.requests.espn_requests import ESPNUnknownError, ESPNAccessDenied

from features.league_rules import validate_rules
from features.nba_schedule import build_nba_schedule
from features.predictive_engine import run_projection_pipeline
from features.recent_form import number, timestamp


def normalize_rules(raw, team, config, daily_used=None):
    settings = raw["settings"]
    roster = settings.get("rosterSettings", {})
    acquisition = settings.get("acquisitionSettings", {})
    scoring = settings.get("scoringSettings", {})
    slots = []
    capacity = 0
    for key, count in roster.get("lineupSlotCounts", {}).items():
        if not count:
            continue
        slot = POSITION_MAP.get(int(key), "UNKNOWN")
        if slot != "IR":
            capacity += count
        if slot not in {"BE", "IR"}:
            slots.extend([slot] * count)
    position_limits = {}
    for key, limit in roster.get("positionLimits", {}).items():
        if int(key) == 0:
            continue  # ESPN's unused position sentinel, not PG.
        if limit >= 0 and limit < capacity:
            # ESPN positionLimits keys refer to defaultPositionId (1..5).
            position_limits[POSITION_MAP.get(int(key) - 1, "UNKNOWN")] = limit
    remaining = "unknown"
    season_limit = acquisition.get("acquisitionLimit")
    matchup_limit = acquisition.get("matchupAcquisitionLimit")
    daily = acquisition.get("matchupLimitPerScoringPeriod") is True
    if season_limit == -1 and (matchup_limit == -1 or daily):
        remaining = None
    elif (matchup_limit == -1 or daily) and season_limit is not None and season_limit >= 0:
        used = team.get("transactionCounter", {}).get("acquisitions")
        if used is not None:
            remaining = max(0, season_limit - used)
    if not daily and matchup_limit is not None and matchup_limit >= 0:
        used = team.get("transactionCounter", {}).get("matchupAcquisitionTotals")
        if isinstance(used, dict):
            left = max(0, int(matchup_limit) - int(used.get(str(raw["status"]["currentMatchupPeriod"]), 0)))
            if season_limit == -1:
                remaining = left
            elif season_limit is not None and season_limit >= 0:
                total_used = team.get("transactionCounter", {}).get("acquisitions")
                if total_used is not None:
                    remaining = min(left, max(0, season_limit - total_used))
    # No guessing that lifetime acquisitions are this matchup's acquisitions.
    rules = {
        "scoring_type": scoring.get("scoringType"),
        "starting_slots": slots, "roster_capacity": capacity,
        "lineup_lock": roster.get("lineupLocktimeType"),
        "drop_lock": roster.get("rosterLocktimeType"),
        "acquisition_timing": "SAME_DAY" if roster.get("rosterLocktimeType") == "FIRSTGAME_SCORINGPERIOD" else None,
        "games_cap": None if roster.get("lineupSlotStatLimits") == {} else "unknown",
        "lineup_move_limit": None if roster.get("moveLimit") == -1 else "unknown",
        "acquisitions_remaining": remaining, "position_limits": position_limits,
        "daily_acquisition_limit": int(matchup_limit) if daily and matchup_limit is not None and matchup_limit >= 0 else None,
        "daily_acquisitions_used": daily_used,
        "timezone": config.get("timezone", "America/New_York"),
        "ir_legal": not any(e.get("lineupSlotId") == 13 for e in team.get("roster", {}).get("entries", [])),
    }
    overrides = config.get("rules", {})
    unknown = set(overrides) - set(rules)
    if unknown:
        raise ValueError(f"Unknown rule overrides: {sorted(unknown)}")
    rules.update(overrides)
    validate_rules(rules)
    return rules


def normalize_game_logs(cards, schedule, year, current_period, as_of, zone):
    """Normalize actual per-game cards, with a legacy daily-split fallback.

    Applied totals are already scored for the league. GP=1 is required: GP=0 is
    a DNP, while a played zero fantasy-point game remains legitimate evidence.
    ESPN daily splits are not individual games when GP>1, so reject those.
    """
    dates = defaultdict(set)
    game_index = {}
    for game in schedule.to_dict("records"):
        dates[int(game["scoring_period"])].add(timestamp(game["tipoff"]).tz_convert(zone).date())
        game_index[str(game["game_id"])] = game
    logs, coverage = [], {}
    now = timestamp(as_of)
    for card in cards:
        player = card["player"]
        pid = player["id"]
        valid = True
        stats = player.get("stats", [])
        has_games = any(s.get("seasonId") == year and s.get("statSourceId") == 0
                        and s.get("statSplitTypeId") == 5 for s in stats)
        observed = False
        for stat in stats:
            split = stat.get("statSplitTypeId")
            if stat.get("seasonId") != year or stat.get("statSourceId") != 0 or split not in (1, 5):
                continue
            if has_games and split != 5:
                continue
            period = stat.get("scoringPeriodId", 0)
            if period > current_period or period <= 0:
                continue
            gp = number(stat.get("stats", {}).get("42"))
            if gp == 0 or not stat.get("stats"):
                observed = True
                continue
            gid = str(stat.get("externalId", "")) if split == 5 else f"{year}:{pid}:{period}"
            game = game_index.get(gid) if split == 5 else None
            if gp != 1 or (game is None and len(dates.get(period, set())) != 1):
                valid = False
                continue
            day = timestamp(game["tipoff"]).tz_convert(zone).date() if game else next(iter(dates[period]))
            ended = pd.Timestamp(day, tz=zone) + pd.DateOffset(days=1)
            if period == current_period:
                if not game or game.get("status") != "FINAL" or timestamp(game["tipoff"]) > now:
                    continue
                ended = min(ended, now)  # Completion observed by snapshot time.
            if ended > now:
                continue
            observed = True
            points = number(stat.get("appliedTotal"))
            if points is None:
                valid = False
            logs.append({"player_id": pid, "game_id": gid,
                         "completed_at": ended.tz_convert("UTC").isoformat(),
                         "completed": True, "played": True, "points": points,
                         "minutes": number(stat.get("stats", {}).get("40"))})
        coverage[pid] = valid and observed
    return logs, coverage


def daily_acquisitions(league, team_id, zone):
    """Read enough activity to cover today; unknown on failure/truncation."""
    midnight = pd.Timestamp.now(tz=zone).normalize().tz_convert("UTC")
    used, seen = 0, set()
    try:
        for offset in range(0, 2000, 100):
            page = league.recent_activity(size=100, offset=offset)
            reached_prior_day = False
            new = False
            for item in page:
                date = pd.Timestamp(item.date, unit="ms", tz="UTC")
                if date < midnight:
                    reached_prior_day = True
                    continue
                for team, action, player, _ in item.actions:
                    if action not in {"FA ADDED", "WAIVER ADDED"} or getattr(team, "team_id", None) != team_id:
                        continue
                    key = (item.date, action, str(player))
                    if key not in seen:
                        used += 1
                        seen.add(key)
                        new = True
            if reached_prior_day or len(page) < 100:
                return used
            if offset and not new:
                return None
    except (ESPNUnknownError, ESPNAccessDenied, KeyError, ValueError, OSError):
        return None
    return None


def matchup_mapping(raw, config, week):
    """Use daily point keys, never scheduleSettings.matchupPeriods (week IDs)."""
    mapping = defaultdict(set)
    for match in raw.get("schedule", []):
        for side in ("home", "away"):
            mapping[int(match["matchupPeriodId"])].update(
                int(p) for p in match.get(side, {}).get("pointsByScoringPeriod", {}))
    if config.get("matchup_scoring_periods"):
        periods = config["matchup_scoring_periods"]
        if not isinstance(periods, list) or any(type(p) is not int or p < 1 for p in periods):
            raise ValueError("matchup_scoring_periods must list the target matchup's daily scoring IDs")
        mapping[week] = set(periods)
    if not mapping.get(week):
        raise ValueError("Missing daily matchup mapping; configure matchup_scoring_periods explicitly")
    return {w: sorted(periods) for w, periods in mapping.items()}


def fetch_snapshot(league, previous_league, team_id, week=None, config=None):
    """Fetch a fresh roster, exhaustive paginated waiver pool, and batched logs.

    Explicit config supplies rules the wrapper cannot establish, plus optional
    per-player availability/droppability corrections. Calls are read-only.
    """
    config = config or {}
    if not isinstance(config, dict) or set(config) - {"timezone", "rules", "players", "matchup_scoring_periods"}:
        raise ValueError("waiver config supports timezone, rules, players, and matchup_scoring_periods only")
    request = league.espn_request
    raw = request.get_league()
    if raw["scoringPeriodId"] > raw["status"]["finalScoringPeriod"]:
        raise ValueError("This ESPN season has ended. Set ESPN_YEAR to the active season, or use --snapshot for offline replay")
    raw_team = next((t for t in raw["teams"] if t["id"] == team_id), None)
    if raw_team is None:
        raise ValueError(f"team_id {team_id!r} not found in league")
    acquisition = raw["settings"].get("acquisitionSettings", {})
    daily_used = None
    if acquisition.get("matchupLimitPerScoringPeriod") and acquisition.get("matchupAcquisitionLimit", -1) >= 0:
        daily_used = daily_acquisitions(league, team_id, config.get("timezone", "America/New_York"))
    rules = normalize_rules(raw, raw_team, config, daily_used)
    week = week if week is not None else raw["status"]["currentMatchupPeriod"]
    if week != raw["status"]["currentMatchupPeriod"]:
        raise ValueError("Roster-aware waivers currently target the current matchup only")
    # Full future period mapping is needed even when no matchup has played yet.
    mapping = matchup_mapping(raw, config, week)
    schedule = build_nba_schedule(league, matchup_ids=mapping)
    mapped = {p for periods in mapping.values() for p in periods}
    # Never allow build_nba_schedule's retrospective seven-day fallback in advice.
    schedule.loc[~schedule.scoring_period.isin(mapped), "fantasy_week"] = -1
    max_period = max(mapping[week])
    full_horizon = (bool(config.get("matchup_scoring_periods"))
                    or max_period == raw["status"]["finalScoringPeriod"]
                    or bool(mapping.get(week + 1)) and max_period + 1 == min(mapping[week + 1]))
    pool, seen, complete = [], set(), False
    for offset in range(0, 10000, 250):
        filters = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                               "limit": 250, "offset": offset,
                               "filterSlotIds": {"value": []},
                               "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
                               "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        response = request.league_get(params={"view": "kona_player_info", "scoringPeriodId": raw["scoringPeriodId"]},
                                      headers={"x-fantasy-filter": json.dumps(filters)})
        page = response["players"]
        new = [p for p in page if p["player"]["id"] not in seen]
        pool.extend(new)
        seen.update(p["player"]["id"] for p in new)
        if len(page) < 250:
            complete = True
            break
        if not new:
            break
    raw_entries = raw_team.get("roster", {}).get("entries", [])
    own = {e["playerId"]: e for e in raw_entries}
    owned_ids = {e["playerId"] for t in raw["teams"] for e in t.get("roster", {}).get("entries", [])}
    pool = [p for p in pool if p["player"]["id"] not in owned_ids]
    entries = [e["playerPoolEntry"] for e in raw_entries] + pool
    ids = [e["player"]["id"] for e in entries]
    cards = []
    failures = []
    for start in range(0, len(ids), 50):
        try:
            response = request.get_player_card(ids[start:start + 50], league.finalScoringPeriod)
            cards.extend(response["players"])
        except (ESPNUnknownError, ESPNAccessDenied, KeyError, ValueError, OSError) as exc:
            failures.append(type(exc).__name__)
    card_map = {c["player"]["id"]: c for c in cards}
    # Preserve pool status separately; the card response may omit it.
    objects = {e["player"]["id"]: Player(card_map.get(e["player"]["id"], e), league.year, league.pro_schedule)
               for e in entries}
    for entry in entries:
        pid = entry["player"]["id"]
        splits = card_map.get(pid, entry)["player"].get("stats", [])
        # Player defaults absent projections to 0; preserve missingness for advice.
        if not any(s.get("seasonId") == league.year and s.get("statSourceId") == 1
                   and s.get("statSplitTypeId") == 0 and "appliedAverage" in s for s in splits):
            objects[pid].projected_avg_points = None
        if not any(s.get("seasonId") == league.year and s.get("statSourceId") == 0
                   and s.get("statSplitTypeId") == 0 and "appliedAverage" in s for s in splits):
            objects[pid].avg_points = None
    current_teams = [SimpleNamespace(roster=[Player(e, league.year, league.pro_schedule)
                                             for e in t.get("roster", {}).get("entries", [])]) for t in raw["teams"]]
    current = SimpleNamespace(year=league.year, teams=current_teams, pro_schedule=league.pro_schedule)
    # Extra card objects give projections/logs for own roster as well as the pool.
    # Replace own roster objects to prevent first-occurrence deduplication using old stats.
    for t in current.teams:
        t.roster = [objects.get(p.playerId, p) for p in t.roster]
    pipeline = run_projection_pipeline(current, previous_league, schedule_df=schedule,
                                       extra_players=list(objects.values()))
    as_of = pd.Timestamp.now(tz="UTC").isoformat()
    logs, coverage = normalize_game_logs(cards, schedule, league.year, raw["scoringPeriodId"], as_of, rules["timezone"])
    projections = pipeline["projections"].set_index("player_id").to_dict("index")
    values = pipeline["value"].set_index("player_id").to_dict("index")
    players = []
    for entry in entries:
        raw_player = entry["player"]
        pid = raw_player["id"]
        p = objects[pid]
        status = entry.get("status")
        waiver_at = entry.get("waiverProcessDate")
        available = as_of if status == "FREEAGENT" else (
            pd.Timestamp(waiver_at, unit="ms", tz="UTC").isoformat() if status == "WAIVERS" and waiver_at else None)
        player = {"player_id": pid, "player_name": p.name, "position": p.position,
                  "pro_team": p.proTeam, "eligible_slots": p.eligibleSlots,
                  "injury_status": p.injuryStatus if isinstance(p.injuryStatus, str) else None, "rostered": pid in own,
                  "lineup_slot": POSITION_MAP.get(own.get(pid, {}).get("lineupSlotId")),
                  "undroppable": not raw_player["droppable"] if type(raw_player.get("droppable")) is bool else None,
                  "availability": status, "available_at": available,
                  "season_avg_points": number(p.avg_points),
                  "proj_pts_per_game": number(projections.get(pid, {}).get("proj_pts_per_game")),
                  "vorp_ros": number(values.get(pid, {}).get("vorp_ros")),
                  "recent_logs_complete": coverage.get(pid, False)}
        override = config.get("players", {}).get(str(pid), {})
        if set(override) - {"available_at", "availability", "undroppable", "protected"}:
            raise ValueError(f"Unsupported player override for {pid}")
        player.update(override)
        players.append(player)
    limitations = ["Same-day game logs included only when the NBA schedule marks stats official",
                   "Season-long VORP uses the existing approximate replacement baseline",
                   "Future schedule entries without a reported status are assumed scheduled"]
    if config:
        limitations.append("Explicit local rule/player overrides applied; verify against current ESPN rules")
    if failures:
        limitations.append(f"{len(failures)} game-log batches failed; affected players use baseline-only estimates")
    if not complete:
        limitations.append("Available player pool pagination incomplete; ranking is limited to fetched players")
    if previous_league is None:
        limitations.append("Previous-season priors unavailable")
    if not complete:
        warnings.warn("Available player pool pagination incomplete; recommendations are partial")
    return {"schema_version": 1, "league_id": league.league_id, "season_year": league.year,
            "team_id": team_id, "week": week, "as_of": as_of, "rules": rules,
            "players": players, "games": schedule[["game_id", "pro_team", "opponent", "fantasy_week", "scoring_period", "tipoff", "status"]].to_dict("records"),
            "schedule_complete": not schedule.empty and full_horizon, "pool_complete": complete,
            "game_logs": logs, "limitations": limitations}
