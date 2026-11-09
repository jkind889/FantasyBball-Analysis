"""ESPN player news: bounded fetching, clip filtering and dated context entries.

Selection is pure and takes the decision time explicitly, so it can be reused
for any point-in-time question. `published` is what ESPN declares; retrieval time
is recorded separately. Absence of news never means a player is healthy.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import unicodedata

import pandas as pd
import requests

from features.recent_form import timestamp


NEWS_ENDPOINT = "https://site.api.espn.com/apis/fantasy/v3/games/fba/news/players"
BLURB_TYPE = "Rotowire"
SOURCE_LABEL = "ESPN player news (RotoWire blurb)"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_NEWS_PLAYERS = 40
MAX_REQUEST_SECONDS = 20
MAX_TOTAL_SECONDS = 90
MAX_SUMMARY_CHARS = 240
# Judgment-call blurbs keep the story; 1,000 characters held 99% of saved blurbs whole.
MAX_JUDGMENT_SUMMARY_CHARS = 1000
MAX_ITEMS_PER_PLAYER = 2
# A first limit only: the review's 16 KB bound must also hold plan rows that grow
# with the horizon, so it withholds the lowest-priority news itself when needed.
MAX_CONTEXT_BYTES = 4000
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
# Git-ignored and append-only: ESPN's feed is count-capped, so old blurbs can't be re-fetched.
HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "news_history" / "blurbs.jsonl"
HISTORY_FIELDS = ("player_id", "item_id", "published", "headline", "story")
# Injury wording only counts shortly after the player's own name in the headline,
# with no capitalized name in between: stories and later clauses often describe
# teammates ("Smith will start with Jones sidelined").
_GAP_WORD = r"(?:[a-z][\w'-]*,?|[A-Z]{2,}|(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day(?:'s)?)"
_NEAR_NAME = rf"^,?(?:\s+{_GAP_WORD}){{0,6}}?\s+[\"']?"
ABSENCE_PHRASE = re.compile(
    rf"{_NEAR_NAME}(?i:ruled out|will miss|won't play|won't suit up|will not play|did not play|"
    r"didn't play|not playing|won't be available|be rested|sit out|sat out|sitting out|game-time decision|expected to miss|"
    r"set to miss|missed|sidelined|out\s+for|out\s+indefinitely|unable to return|minutes restriction|"
    r"doubtful|questionable|probable|isn't expected|not expected|dealing with|underwent|undergo|undergoes|"
    r"undergoing|surgery|MRI|diagnosed|exited|locker room|left\s+(?:the\s+|[\w-]+'s\s+)(?:[\w'-]+\s+)?(?:game|contest|win|loss|practice)|"
    r"won't return|will not return|did not return|didn't return|stretchered|injured|suffer|suffered|sustained|"
    r"day-to-day|re-evaluated|torn|tore|tear of|tear in (?:his|the)|sprain|sprained|strain|strained|fracture|fractured)\b")
# Wording that may or may not mean an injury ("limited" minutes, a practice skipped for
# rest). These blurbs still qualify, but carry more of the story so the reader can judge.
JUDGMENT_PHRASE = re.compile(
    rf"{_NEAR_NAME}(?i:did not practice|didn't practice|did not participate|didn't participate|(?:very )?limited|will rest)\b")
# "Available" is a return from absence: the player may not be at full strength, so these
# blurbs always carry the story, even under an injury tag. Negated wording ("won't be
# available", "not expected to be available") is left to the absence phrases instead.
AVAILABLE_PHRASE = re.compile(
    rf"^,?(?:\s+(?!(?:not|never|unlikely|doubtful)\b|[\w-]*n't\b){_GAP_WORD}){{0,6}}?\s+[\"']?(?i:available)\b")
# Parenthetical tags that name no injury. Alone they don't route a blurb, but wording
# after them still does ("Smith (rest) has been ruled out").
NON_INJURY_TAGS = {"rest", "coach's decision", "not injury related", "recently traded", "two-way",
                   "personal", "trade pending", "recently signed"}


def _plain(text):
    decomposed = unicodedata.normalize("NFKD", text).replace("\u2019", "'")
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _subject(player_name):
    """Headline subject for a player: the surname, ignoring generational suffixes."""
    if not isinstance(player_name, str):
        return None
    tokens = [token.strip(".,") for token in _plain(player_name).split()]
    while tokens and tokens[-1].lower() in NAME_SUFFIXES:
        tokens.pop()
    return tokens[-1] if tokens else None


def injury_match(headline, player_name):
    """How an injury/absence marker sits on this player's name in the headline.

    Returns "clear", "judgment" (wording that needs the story to interpret) or None.
    Availability wins, then clear markers, then other judgment wording.
    A heuristic for routing blurbs, never a medical or availability judgment.
    """
    subject = _subject(player_name)
    if not subject:
        return None
    text = _plain(headline if isinstance(headline, str) else "")
    found = set()
    for match in re.finditer(rf"(?<![\w'-]){re.escape(subject)}(?:'s|')?(?![\w-])", text, re.I):
        rest = text[match.end():]
        tag = re.match(r"\s*\(([^)]{2,40})\)", rest)
        if tag:
            parts = {part.strip() for part in re.split(r"[,/]", tag.group(1).lower())}
            if not parts <= NON_INJURY_TAGS:
                found.add("clear")
            rest = rest[tag.end():]
        if AVAILABLE_PHRASE.match(rest):
            found.add("available")
        elif ABSENCE_PHRASE.match(rest):
            found.add("clear")
        elif JUDGMENT_PHRASE.match(rest):
            found.add("judgment")
    if "available" in found:
        return "judgment"
    return "clear" if "clear" in found else ("judgment" if found else None)


def is_injury_headline(headline, player_name):
    """True when any injury/absence marker, clear or judgment, sits on this player's name."""
    return injury_match(headline, player_name) is not None


def filter_news_items(feed, player_id, player_name):
    """Keep this player's dated blurbs; count every dropped item by reason."""
    kept, seen, counts = [], set(), {"clips": 0, "other_types": 0, "other_player": 0, "malformed": 0, "duplicates": 0}
    for item in feed if isinstance(feed, list) else []:
        if not isinstance(item, dict):
            counts["malformed"] += 1
        elif item.get("type") != BLURB_TYPE:
            is_clip = item.get("type") == "Media" or "video" in item
            counts["clips" if is_clip else "other_types"] += 1
        elif item.get("playerId") != player_id:
            counts["other_player"] += 1
        else:
            headline, published = item.get("headline"), item.get("published")
            try:
                if not isinstance(headline, str) or not headline.strip() or not 1 <= len(str(item.get("id") or "")) <= 40:
                    raise ValueError
                published_at = timestamp(published)
            except (ValueError, TypeError):
                counts["malformed"] += 1
                continue
            if str(item["id"]) in seen:
                counts["duplicates"] += 1
                continue
            seen.add(str(item["id"]))
            story = item.get("story") if isinstance(item.get("story"), str) else ""
            match = injury_match(headline, player_name)
            kept.append({"item_id": str(item["id"]), "player_id": player_id, "published": published,
                         "published_at": published_at, "headline": headline, "story": story,
                         "is_injury": match is not None, "needs_story": match == "judgment"})
    return kept, counts


def _summary(item):
    limit = MAX_JUDGMENT_SUMMARY_CHARS if item.get("needs_story") else MAX_SUMMARY_CHARS
    text = " ".join(f"{item['headline']} {item['story']}".split())
    if len(text) <= limit:
        return text
    return text[:limit - 3].rsplit(" ", 1)[0] + "..."


def select_news_entries(results, players, *, as_of, max_age=pd.Timedelta(hours=24),
                        max_bytes=MAX_CONTEXT_BYTES, max_entries=100):
    """Build context entries from fetch results, in the caller's priority order.

    Only injury blurbs published in (as_of - max_age, as_of] qualify, newest first
    per player. Returns (entries, report); the report keeps failures, drops and
    budget trimming visible instead of letting them read as "no news".
    """
    as_of = timestamp(as_of)
    entries, used, report = [], 0, []
    for player_id, fetched in results.items():
        row = {"player_id": player_id, "status": fetched.get("status"), "retrieved_at": fetched.get("retrieved_at"),
               "error_type": fetched.get("error_type"), "clips": 0, "other_types": 0, "other_player": 0, "malformed": 0,
               "duplicates": 0, "name_missing": _subject(players.get(player_id)) is None, "blurbs": 0, "injury_blurbs": 0, "older_injury_blurbs": 0, "future_injury_blurbs": 0,
               "selected": 0, "trimmed": 0}
        if fetched.get("status") == "ok":
            items, counts = filter_news_items(fetched.get("feed"), player_id, players.get(player_id))
            row.update(counts, blurbs=len(items))
            injuries = [i for i in items if i["is_injury"]]
            row["injury_blurbs"] = len(injuries)
            row["future_injury_blurbs"] = sum(i["published_at"] > as_of for i in injuries)
            row["older_injury_blurbs"] = sum(as_of - i["published_at"] > max_age for i in injuries)
            fresh = [i for i in injuries if i["published_at"] <= as_of and as_of - i["published_at"] <= max_age]
            fresh.sort(key=lambda i: (i["published_at"], i["item_id"]), reverse=True)
            row["trimmed"] = max(0, len(fresh) - MAX_ITEMS_PER_PLAYER)
            over_limit = False  # Never let an older, smaller blurb replace a newer trimmed one.
            for item in fresh[:MAX_ITEMS_PER_PLAYER]:
                entry = {"id": f"espn-{player_id}-{item['item_id']}", "kind": "injury", "player_id": player_id,
                         "observed_at": item["published"], "source": SOURCE_LABEL, "summary": _summary(item),
                         "retrieved_at": fetched["retrieved_at"], "source_item_id": item["item_id"]}
                size = len(json.dumps(entry))
                over_limit = over_limit or len(entries) >= max_entries or used + size > max_bytes
                if over_limit:
                    row["trimmed"] += 1
                    continue
                entries.append(entry)
                used += size
                row["selected"] += 1
        report.append(row)
    return entries, report


def save_news_history(results, players, path=HISTORY_PATH):
    """Append this player's blurbs not saved yet, whatever their age or topic.

    Rows are never rewritten. `retrieved_at` is when this text was first seen, and an
    edited blurb gets its own row, so a later reader can tell what was known when.
    Unreadable lines stay in place and are only counted.
    """
    path = Path(path)
    seen, unreadable, ends_cleanly = set(), 0, True
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                ends_cleanly = line.endswith("\n")
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    seen.add(tuple(row[field] for field in HISTORY_FIELDS))
                except (ValueError, KeyError, TypeError):
                    unreadable += 1
    new_rows, already_saved = [], 0
    for player_id, fetched in results.items():
        if fetched.get("status") != "ok":
            continue
        for item in filter_news_items(fetched.get("feed"), player_id, players.get(player_id))[0]:
            key = tuple(item[field] for field in HISTORY_FIELDS)
            if key in seen:
                already_saved += 1
                continue
            seen.add(key)
            new_rows.append({**{field: item[field] for field in HISTORY_FIELDS},
                             "player_name": players.get(player_id), "source": SOURCE_LABEL,
                             "retrieved_at": fetched["retrieved_at"]})
    if new_rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(json.dumps(row, allow_nan=False) + "\n" for row in new_rows)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(lines if ends_cleanly else "\n" + lines)  # Don't join an interrupted line.
    return {"status": "ok", "added": len(new_rows), "already_saved": already_saved, "unreadable_lines": unreadable}


def fetch_player_news(player_ids, *, get=requests.get, clock=time.monotonic):
    """One bounded, cookie-free request per player; a failure never aborts the rest.

    Errors are reported by exception type only and raw responses are not logged.
    """
    results, started = {}, clock()
    for player_id in player_ids:
        retrieved_at = datetime.now(timezone.utc).isoformat()
        try:
            request_started = clock()
            if request_started - started > MAX_TOTAL_SECONDS:
                raise TimeoutError("overall deadline reached")
            with get(NEWS_ENDPOINT, params={"playerId": player_id}, timeout=(5, 15),
                     allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ValueError("unexpected status")
                chunks, size = [], 0
                for chunk in response.iter_content(4096):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError("response too large")
                    if clock() - request_started > MAX_REQUEST_SECONDS:
                        raise TimeoutError("response too slow")
                    chunks.append(chunk)
            feed = json.loads(b"".join(chunks))["news"]["feed"]
            if not isinstance(feed, list):
                raise TypeError("feed is not a list")
            results[player_id] = {"status": "ok", "retrieved_at": retrieved_at, "feed": feed}
        except (requests.RequestException, ValueError, TypeError, KeyError, RecursionError, OSError) as exc:
            results[player_id] = {"status": "fetch_failed", "retrieved_at": retrieved_at, "feed": [],
                                  "error_type": type(exc).__name__}
    return results
