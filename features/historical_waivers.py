"""Reconstructed, real-data waiver demos; not contemporaneous backtests.

The baseline and recent form use only games before the selected date. Future
actuals are returned separately, never passed into the recommendation engine.
"""

from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
from statistics import mean

import pandas as pd
from espn_api.basketball.constant import POSITION_MAP, PRO_TEAM_MAP
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNUnknownError

from features.nba_schedule import build_nba_schedule
from features.recent_form import number, timestamp
from features.waiver_inputs import matchup_mapping, normalize_game_logs, normalize_rules


def historical_cutoff(date, zone="America/New_York"):
    value = pd.Timestamp(date)
    if pd.isna(value) or value.tzinfo is not None or value != value.normalize():
        raise ValueError("--historical-date must be a local date, YYYY-MM-DD")
    cutoff = value.tz_localize(zone).tz_convert("UTC")
    if cutoff >= pd.Timestamp.now(tz=zone).normalize().tz_convert("UTC"):
        raise ValueError("Choose a completed historical date before today")
    return cutoff


def historical_rosters(boxes):
    rosters = {}
    for match in boxes.get("schedule", []):
        for side in ("home", "away"):
            team = match.get(side, {})
            if "teamId" in team and "rosterForCurrentScoringPeriod" in team:
                rosters[team["teamId"]] = team["rosterForCurrentScoringPeriod"]["entries"]
    return rosters


def build_historical_snapshot(raw, schedule, rosters, cards, *, year, league_id,
                              team_id, cutoff, period, week, pool_complete=True):
    """Build demo inputs from historical prior-day rosters and complete cards."""
    cutoff = timestamp(cutoff)
    zone = "America/New_York"
    own_entries = rosters.get(team_id)
    if not own_entries:
        raise ValueError("ESPN did not return the selected team's historical daily roster")
    if len(rosters) != len(raw["teams"]):
        raise ValueError("Historical rosters missing for some league teams; cannot infer unrostered players")
    own = {e["playerId"]: e for e in own_entries}
    all_owned = {e["playerId"] for entries in rosters.values() for e in entries}
    cards = [c for c in cards if c["player"]["id"] in own or c["player"]["id"] not in all_owned]
    card_map = {c["player"]["id"]: c for c in cards}
    for pid, entry in own.items():
        card_map.setdefault(pid, {"player": entry["playerPoolEntry"]["player"]})

    # Copy and REMOVE all post-cutoff splits (including same-day games) before
    # normalization. Never use end-of-season totals or updated ESPN projections.
    prior_cards = deepcopy(list(card_map.values()))
    for card in prior_cards:
        card["player"]["stats"] = [s for s in card["player"].get("stats", [])
            if s.get("seasonId") == year and s.get("statSourceId") == 0
            and s.get("statSplitTypeId") in (1, 5)
            and 0 < s.get("scoringPeriodId", 0) < period]
    logs, coverage = normalize_game_logs(prior_cards, schedule, year, period,
                                         cutoff.isoformat(), zone)
    grouped = defaultdict(list)
    seen_logs = {}
    for log in logs:
        key = (log["player_id"], log["game_id"])
        if key in seen_logs:
            if seen_logs[key] != log:
                coverage[log["player_id"]] = False
            continue
        seen_logs[key] = log
        grouped[log["player_id"]].append(log)
    players, recent_logs = [], []
    for card in prior_cards:
        metadata = card["player"]
        pid = metadata["id"]
        played = sorted(grouped[pid], key=lambda x: x["completed_at"])
        values = [number(g["points"]) for g in played]
        values = [v for v in values if v is not None]
        if not values and pid not in own:
            continue  # No evidence this candidate had played by the cutoff.
        baseline = mean(values) if values else None
        recent_logs.extend(played[-6:])
        archived = own.get(pid, {}).get("playerPoolEntry", {}).get("player", {})
        player_metadata = archived or metadata
        prior_stats = sorted(metadata.get("stats", []), key=lambda s: s["scoringPeriodId"], reverse=True)
        # Prefer the team of the most recent pre-cutoff appearance over a later
        # trade reflected in today's metadata. Historical roster metadata is the
        # fallback when a game stat does not carry a team.
        pro_team_id = next((s["proTeamId"] for s in prior_stats if s.get("proTeamId")
                            and number(s.get("stats", {}).get("42")) == 1), player_metadata.get("proTeamId"))
        players.append({
            "player_id": pid, "player_name": player_metadata.get("fullName", str(pid)),
            "position": POSITION_MAP.get(player_metadata.get("defaultPositionId", 0) - 1),
            "pro_team": PRO_TEAM_MAP.get(pro_team_id),
            "eligible_slots": [POSITION_MAP[i] for i in player_metadata.get("eligibleSlots", []) if i in POSITION_MAP],
            "lineup_slot": POSITION_MAP.get(own.get(pid, {}).get("lineupSlotId")),
            "rostered": pid in own,
            "injury_status": "ACTIVE",  # Explicit demo assumption, never a later injury report.
            "undroppable": False, "availability": "ONTEAM" if pid in own else "FREEAGENT",
            "available_at": cutoff.isoformat(), "season_avg_points": baseline,
            "proj_pts_per_game": baseline, "vorp_ros": None,
            "baseline_source": "mean of pre-cutoff completed appearances only",
            "baseline_games": len(values), "baseline_total_points": sum(values) if values else None,
            "recent_logs_complete": coverage.get(pid, False),
        })
    team = next(t for t in raw["teams"] if t["id"] == team_id)
    simulated_raw = deepcopy(raw)
    simulated_raw["status"]["currentMatchupPeriod"] = week
    overrides = {"ir_legal": True, "acquisitions_remaining": 1, "daily_acquisitions_used": 0}
    if raw["settings"].get("rosterSettings", {}).get("rosterLocktimeType") == "INDIVIDUAL_GAME":
        overrides["acquisition_timing"] = "SAME_DAY"
    rules = normalize_rules(simulated_raw, team, {"rules": overrides}, daily_used=0)
    games = schedule[["game_id", "pro_team", "opponent", "fantasy_week", "scoring_period", "tipoff", "status"]].to_dict("records")
    for game in games:
        if timestamp(game["tipoff"]) >= cutoff:
            # Retrospective statsOfficial/FINAL must not make future games disappear.
            game["status"] = "SCHEDULED"
    assumptions = [
        "Reconstructed historical demo, not a contemporaneously saved snapshot or validated backtest",
        "Roster/ownership use the prior scoring day's archived rosters; same-day transactions may differ",
        "Candidates are inferred unrostered players; immediate availability and droppability are assumed",
        "One acquisition is assumed available; historical usage and pending claims are not reconstructed",
        "All non-IR players are assumed playable; historical injury bulletins are not reconstructed",
        "Eligibility and league rules may reflect later updates; NBA team is inferred from prior appearances",
        "Schedule uses actual historical dates and may include rescheduling unknown at the cutoff",
        "Baseline is pre-cutoff season-to-date mean; final-season totals and ESPN projections are excluded",
    ]
    snapshot = {"schema_version": 1, "mode": "historical_demo", "league_id": league_id,
                "season_year": year, "team_id": team_id, "team_name": team.get("name", str(team_id)),
                "week": week, "as_of": cutoff.isoformat(), "rules": rules,
                "roster_scoring_period": period - 1, "decision_scoring_period": period,
                "players": players, "games": games, "game_logs": recent_logs,
                "pool_complete": pool_complete, "schedule_complete": not schedule.empty,
                "limitations": assumptions, "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat()}

    # Held-out outcomes live outside snapshot. DNP=0 only with an explicit card
    # entry and official game; absent records remain unknown.
    game_index = {str(g["game_id"]): g for g in schedule.to_dict("records")}
    outcomes = []
    for card in card_map.values():
        for stat in card["player"].get("stats", []):
            if stat.get("seasonId") != year or stat.get("statSourceId") != 0 or stat.get("statSplitTypeId") != 5:
                continue
            gid = str(stat.get("externalId", ""))
            game = game_index.get(gid)
            if not game or game["fantasy_week"] != week or timestamp(game["tipoff"]) < cutoff or game["status"] != "FINAL":
                continue
            points = number(stat.get("appliedTotal"))
            if points is not None:
                outcomes.append({"player_id": card["player"]["id"], "game_id": gid, "points": points})
    return snapshot, outcomes


def fetch_historical_snapshot(request, year, league_id, team_id, date, progress=None):
    """Read a completed season and reconstruct a morning decision scenario."""
    log = progress or (lambda message: None)
    cutoff = historical_cutoff(date)
    raw = request.get_league()
    pro = request.get_pro_schedule()
    pro_schedule = {t["id"]: t.get("proGamesByScoringPeriod", {}) for t in pro["settings"]["proTeams"]}
    first_week = min(m["matchupPeriodId"] for m in raw["schedule"])
    mapping = matchup_mapping(raw, {}, first_week)
    schedule = build_nba_schedule(pro_schedule, season_year=year, matchup_ids=mapping)
    if schedule.empty:
        raise ValueError("No NBA schedule available for the historical season")
    local_days = pd.to_datetime(schedule.tipoff, utc=True).dt.tz_convert("America/New_York").dt.date
    periods = set(schedule.loc[local_days == cutoff.tz_convert("America/New_York").date(), "scoring_period"])
    if len(periods) != 1:
        raise ValueError("Choose a historical date with a known NBA scoring period")
    period = int(next(iter(periods)))
    week = next((w for w, ps in mapping.items() if period in ps), None)
    prior_week = next((w for w, ps in mapping.items() if period - 1 in ps), None)
    if week is None or prior_week is None:
        raise ValueError("Historical daily matchup boundaries are missing; choose a later date")
    schedule.loc[~schedule.scoring_period.isin({p for ps in mapping.values() for p in ps}), "fantasy_week"] = -1
    log(f"Reading archived rosters for scoring day {period - 1}; decision day {period}, matchup {week}")
    boxes = request.league_get(
        params={"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": period - 1},
        headers={"x-fantasy-filter": json.dumps({"schedule": {"filterMatchupPeriodIds": {"value": [prior_week]}}})})
    rosters = historical_rosters(boxes)
    if team_id not in rosters:
        raise ValueError(f"No archived roster for team {team_id}")
    owned = {e["playerId"] for entries in rosters.values() for e in entries}
    ids = {e["playerId"] for e in rosters[team_id]}
    seen, complete = set(), False
    for offset in range(0, 10000, 250):
        # No ownership filter: today's ownership must not select the historical pool.
        filters = {"players": {"limit": 250, "offset": offset, "filterSlotIds": {"value": []},
                                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                                "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"}}}
        page = request.league_get(params={"view": "kona_player_info", "scoringPeriodId": period - 1},
                                 headers={"x-fantasy-filter": json.dumps(filters)})["players"]
        new = {p["player"]["id"] for p in page} - seen
        seen.update(new)
        ids.update(new - owned)
        if len(page) < 250:
            complete = True
            break
        if not new:
            break
    cards = []
    ids = sorted(ids)
    log(f"Fetching game logs for {len(ids)} players (roster plus inferred historical candidates)")
    for start in range(0, len(ids), 50):
        try:
            response = request.get_player_card(ids[start:start + 50], raw["status"]["finalScoringPeriod"])
            cards.extend(response["players"])
        except (ESPNUnknownError, ESPNAccessDenied, OSError, KeyError, ValueError):
            complete = False
        if (start + 50) % 250 == 0:
            log(f"Fetched {min(start + 50, len(ids))}/{len(ids)} player cards")
    if set(ids) - {c["player"]["id"] for c in cards}:
        complete = False
    return build_historical_snapshot(raw, schedule, rosters, cards, year=year, league_id=league_id,
                                     team_id=team_id, cutoff=cutoff, period=period, week=week, pool_complete=complete)


def evaluate_planned_lineups(result, outcomes):
    """Score the already-chosen lineups; NEVER re-optimize using future actuals."""
    actuals = {(x["player_id"], str(x["game_id"])): x["points"] for x in outcomes}
    rows = []
    hold_calendar = result["calendar"].query("action_id == 'hold' and reason == 'recommended start'")
    hold_starts = Counter((r.player_id, str(r.game_id)) for r in hold_calendar.itertuples())
    for action_id, calendar in result["calendar"].groupby("action_id", sort=False):
        starts = calendar[calendar.reason == "recommended start"]
        values = [actuals.get((r.player_id, str(r.game_id))) for r in starts.itertuples()]
        missing = sum(v is None for v in values)
        scenario_starts = Counter((r.player_id, str(r.game_id)) for r in starts.itertuples())
        # Shared unknown starts cancel in a before/after difference. Only games
        # that enter or leave the planned lineup require known outcomes for gain.
        differences = {key: scenario_starts[key] - hold_starts[key]
                       for key in scenario_starts.keys() | hold_starts.keys()
                       if scenario_starts[key] != hold_starts[key]}
        missing_changed = sum(actuals.get(key) is None for key in differences)
        gain = sum(actuals[key] * count for key, count in differences.items()) if not missing_changed else None
        rows.append({"action_id": action_id, "planned_starts": len(values), "missing_actuals": missing,
                     "actual_planned_points": sum(values) if not missing else None,
                     "missing_changed_actuals": missing_changed, "actual_gain_vs_hold": gain})
    return pd.DataFrame(rows, columns=["action_id", "planned_starts", "missing_actuals", "actual_planned_points",
                                      "missing_changed_actuals", "actual_gain_vs_hold"])


def write_historical_summary(result, outcomes, directory):
    directory = Path(directory)
    evaluation = evaluate_planned_lineups(result, outcomes)
    evaluation.to_csv(directory / "historical_outcomes.csv", index=False)
    snapshot = result["snapshot"]
    table = result["recommendations"].merge(evaluation, on="action_id", how="left")
    candidates = table[table.action != "HOLD"].drop_duplicates("add_id").head(6)
    lines = [f"# Historical waiver demo: {snapshot['team_name']}", "",
             f"Season **{snapshot['season_year']}**, matchup **{snapshot['week']}**, decision date "
             f"**{timestamp(snapshot['as_of']).tz_convert(snapshot['rules']['timezone']).date()}**.", "",
             "This is a reconstructed demonstration using real games. Availability, injury state, "
             "and transaction eligibility are assumed, so these are simulated moves rather than "
             "verified historical waiver opportunities.", "",
             f"The input contains {sum(p['rostered'] for p in snapshot['players'])} rostered players and "
             f"{sum(not p['rostered'] for p in snapshot['players'])} inferred candidates. "
             "Baseline averages and recent form use only games before the decision date. "
             "This is a short-term gain comparison; it does not price the season-long cost of dropping a star. "
             "Use `--protect PLAYER_ID` to keep core players out of drop scenarios.", "",
             "| Add | Drop | Season-to-date avg | Recent avg | Streaming estimate | Projected lineup gain | Actual gain, fixed planned lineup |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    def fmt(value):
        return f"{value:.1f}" if number(value) is not None else "Unknown"
    def name(value):
        return str(value).replace("|", "\\|").replace("\n", " ") if pd.notna(value) else "Empty spot"
    for r in candidates.to_dict("records"):
        lines.append("| " + " | ".join([name(r["add_name"]), name(r["drop_name"]),
            *[fmt(r.get(k)) for k in ("add_season_avg_points", "add_recent_mean", "add_streaming_ppg", "net_gain", "actual_gain_vs_hold")]]) + " |")
    lines += ["", "The table is ordered by projected gain, not the outcomes. Actuals score the lineups "
              "selected before looking at the remaining games; missing actuals stay unknown. "
              "An incremental gain can still be known when missing games are identical in both lineups and cancel. "
              "A single example does not establish prediction accuracy.", "", "## Inspect the results", "",
              "- [All add/drop comparisons](waiver_recommendations.csv)",
              "- [Daily lineup contributions](remaining_week_calendar.csv)",
              "- [Recent form for every player](recent_form.csv)",
              "- [Saved replay snapshot](waiver_snapshot.json)",
              "- [Held-out outcomes for planned lineups](historical_outcomes.csv)",
              "", "## Reconstruction assumptions", ""]
    lines += [f"- {item}" for item in snapshot["limitations"]]
    (directory / "historical_demo.md").write_text("\n".join(lines) + "\n")
    return evaluation
