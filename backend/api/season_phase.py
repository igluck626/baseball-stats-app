"""A season's regular-season span — Opening Day and the last regular-season
day — derived from balldontlie's schedule, for `GET /season/phase`.

⚠️ OPENING DAY = THE FIRST DATE BY WHICH AT LEAST 15 TEAMS HAVE PLAYED (or
are scheduled to play) A REGULAR-SEASON GAME. Overseas openers don't count:
2025's Tokyo Series (03-18, two teams) is not Opening Day; 03-27 is. A lone
opening-night game is one too: 2026's 03-25 Yankees-Giants night game made two
teams, and the full slate on 03-26 is Opening Day.

⚠️ THE LAST REGULAR DAY IS THE SCHEDULE'S, NOT THE FINALS SO FAR. During the
season the latest FINAL game is always yesterday, so "the last final game"
would end the regular season every night; the last SCHEDULED regular-season
game is the real end, and once the season is over it is also the last final.

A postponed or canceled slot is not a game on that day (a makeup is its own
row, on its own date). Dates are Eastern — MLB's calendar.

⚠️ NEVER COMPUTED ON THE REQUEST PATH. The derivation walks the whole season
(~30 balldontlie pages: `/games` ignores `postseason=false` and `start_date`
without an error). It runs in the background — at boot and after the nightly,
retrying on failure — and the endpoint only ever reads the stored value,
stale if need be.
"""
from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Callable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_NOT_PLAYED = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"}
OPENING_DAY_TEAMS = 15


def eastern_date(iso: str) -> Optional[datetime.date]:
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return dt.astimezone(_ET).date()


def derive(games: list[dict]) -> dict:
    """{"opening_day", "last_regular_day"} as ISO dates, each None when the
    schedule doesn't reach it (no regular-season games published yet)."""
    by_day: dict[datetime.date, set] = {}
    for g in games:
        if g.get("season_type") != "regular" or g.get("status") in _NOT_PLAYED:
            continue
        day = eastern_date(g.get("date") or "")
        if day is None:
            continue
        teams = by_day.setdefault(day, set())
        for side in ("home_team", "away_team"):
            tid = (g.get(side) or {}).get("id")
            if tid is not None:
                teams.add(tid)
    opening, seen = None, set()
    for day in sorted(by_day):
        seen |= by_day[day]
        if len(seen) >= OPENING_DAY_TEAMS:
            opening = day
            break
    last = max(by_day) if by_day else None
    return {"opening_day": opening.isoformat() if opening else None,
            "last_regular_day": last.isoformat() if last else None}


# ---------------------------------------------------------------------------
# The stored value, refreshed in the background
# ---------------------------------------------------------------------------

_store: dict[int, dict] = {}
_store_lock = threading.Lock()


def cached(season: int) -> Optional[dict]:
    """The last derived value for `season`, however old; None if never derived."""
    with _store_lock:
        return _store.get(season)


def walk(fetch: Callable[[str, dict], dict], season: int) -> list[dict]:
    """Every balldontlie game of `season` (spring training, regular, postseason)."""
    games: list[dict] = []
    cursor = None
    for _ in range(80):
        params: dict = {"seasons[]": [season], "per_page": 100}
        if cursor is not None:
            params["cursor"] = cursor
        page = fetch("games", params)
        games.extend(page.get("data") or [])
        cursor = (page.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return games


def refresh(fetch: Callable[[str, dict], dict], seasons: list[int]) -> bool:
    """Re-derive each season and store it. A failed season keeps its old
    value. True only if every season refreshed."""
    ok = True
    for season in seasons:
        try:
            body = {"season": season, **derive(walk(fetch, season))}
        except Exception as exc:  # noqa: BLE001
            log.warning("season phase %s: refresh failed, keeping the stored value: %s", season, exc)
            ok = False
            continue
        with _store_lock:
            _store[season] = body
        log.info("season phase %s: %s .. %s", season, body["opening_day"], body["last_regular_day"])
    return ok


def current_seasons(now: Optional[datetime.datetime] = None) -> list[int]:
    """This Eastern year and the one before (January still reads last season)."""
    year = (now or datetime.datetime.now(_ET)).astimezone(_ET).year
    return [year, year - 1]


def start_refresh(reason: str, fetch: Callable[[str, dict], dict],
                  retries: int = 8, retry_seconds: int = 900) -> threading.Thread:
    """Refresh in a daemon thread — never on the caller's path — retrying a
    failure every `retry_seconds` up to `retries` more times."""
    def _run():
        for attempt in range(retries + 1):
            if refresh(fetch, current_seasons()):
                return
            if attempt < retries:
                log.warning("season phase (%s): retrying in %ss", reason, retry_seconds)
                time.sleep(retry_seconds)
        log.error("season phase (%s): gave up after %d retries; serving the stored value", reason, retries)
    t = threading.Thread(target=_run, name=f"season-phase-{reason}", daemon=True)
    t.start()
    return t
