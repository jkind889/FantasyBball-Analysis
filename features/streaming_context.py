"""Dated, source-attributed context supplied explicitly by the caller."""

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from features.recent_form import timestamp


def load_context(path):
    if path is None:
        return None
    with Path(path).open("rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError("Streaming context must be at most 64 KiB")
    return json.loads(data)


def normalize_context(snapshot, document=None):
    identity = {key: snapshot.get(key) for key in ("league_id", "season_year", "week")}
    document = document if document is not None else {"schema_version": 1, **identity, "entries": []}
    if not isinstance(document, dict) or set(document) != {"schema_version", "entries", *identity} or document["schema_version"] != 1:
        raise ValueError("Context requires schema_version: 1, league_id, season_year, week and entries")
    if any(document[key] != value for key, value in identity.items()):
        raise ValueError("Context league, season and matchup must match the decision snapshot")
    entries = document["entries"]
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("Context permits at most 100 entries")
    now = timestamp(snapshot["as_of"])
    players = {p["player_id"]: p for p in snapshot["players"]}
    team_opponents = defaultdict(set)
    for game in snapshot["games"]:
        if game.get("fantasy_week") != snapshot["week"] or game.get("status") in {"FINAL", "IN_PROGRESS", "CANCELED", "CANCELLED", "POSTPONED"}:
            continue
        try:
            if timestamp(game.get("tipoff")) > now:
                team_opponents[game.get("pro_team")].add(game.get("opponent"))
        except (ValueError, TypeError):
            continue  # The planner already labels incomplete schedules partial.
    opponents = {p["player_id"]: team_opponents[p.get("pro_team")] for p in players.values()}
    normalized, seen = [], set()
    required = {"id", "kind", "player_id", "observed_at", "source", "summary"}
    for entry in entries:
        if not isinstance(entry, dict) or not required <= set(entry) or set(entry) - required - {
            "opponent", "sample_games", "window_start", "window_end"}:
            raise ValueError("Invalid context entry fields")
        if not isinstance(entry["id"], str) or not 1 <= len(entry["id"]) <= 64 or entry["id"] in seen:
            raise ValueError("Context IDs must be unique short strings")
        seen.add(entry["id"])
        if type(entry["player_id"]) is not int or entry["player_id"] not in players:
            raise ValueError("Context player ID is not in the snapshot")
        if entry["kind"] not in {"injury", "matchup"}:
            raise ValueError("Context kind must be injury or matchup")
        if any(not isinstance(entry[k], str) or not entry[k].strip() or len(entry[k]) > 500
               for k in ("source", "summary")):
            raise ValueError("Context source and summary must be nonempty and at most 500 characters")
        observed = timestamp(entry["observed_at"])
        age = now - observed
        status = "future" if age < pd.Timedelta(0) else "fresh"
        freshness = pd.Timedelta(hours=24 if entry["kind"] == "injury" else 168)
        if age > freshness:
            status = "stale"
        if entry["kind"] == "matchup":
            if type(entry.get("sample_games")) is not int or not 1 <= entry["sample_games"] <= 1000:
                raise ValueError("Matchup context requires sample_games between 1 and 1000")
            start, end = timestamp(entry.get("window_start")), timestamp(entry.get("window_end"))
            if start > end or end > observed:
                raise ValueError("Matchup sample window must end by observed_at")
            if entry.get("opponent") not in opponents[entry["player_id"]]:
                status = "irrelevant_opponent"
        normalized.append(dict(entry, status=status))
    missing = [{"player_id": pid, "kind": kind, "status": "missing_fresh_context"}
               for pid in players for kind in ("injury", "matchup")
               if not any(e["player_id"] == pid and e["kind"] == kind and e["status"] == "fresh" for e in normalized)]
    return {"entries": normalized, "coverage": missing,
            "limitations": ["Context is caller-supplied evidence, not independently verified provider data",
                            "Injury context expires after 24 hours; matchup context after 7 days",
                            "Stale, future and irrelevant-opponent entries cannot support model selection",
                            "Opponent history is descriptive; sample size does not establish causation"]}
