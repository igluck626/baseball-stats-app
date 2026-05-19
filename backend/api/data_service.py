"""Data service.

Public API functions (search_player, get_current_stats, get_career_stats,
get_current_pitching_stats, get_career_pitching_stats) read ONLY from
PostgreSQL. There are no pybaseball calls at request time.

The fetch_and_save_* helpers below pull from pybaseball and persist to the DB.
They are used by bulk_load.py and nightly_update.py only.
"""

import csv
import datetime
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import pandas as pd
import pybaseball
from dotenv import load_dotenv

log = logging.getLogger(__name__)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Allow `from database import ...` whether running from /app/api (Docker)
# or backend/api (local venv).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased

from database import connection, crud
from database.models import PitcherSeason as _PitcherSeason
from database.models import PlayerSeason as _PlayerSeason

_PS_COLUMNS       = [c.key for c in _PlayerSeason.__table__.columns  if c.key != "player_id"]
_PS_PITCH_COLUMNS = [c.key for c in _PitcherSeason.__table__.columns if c.key != "player_id"]

# Bio fields exposed by search results and batting/pitching stats responses.
_BIO_COLUMNS = [
    "position", "bats", "throws", "height", "weight",
    "birth_year", "birth_month", "birth_day",
    "birth_city", "birth_state", "birth_country",
    "debut", "final_game",
]

# MLB Stats API headshot URL pattern. Same image space for batters & pitchers.
_HEADSHOT_BASE = (
    "https://img.mlbstatic.com/mlb-photos/image/upload/"
    "d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people"
)
_HEADSHOT_FALLBACK_URL = f"{_HEADSHOT_BASE}/generic/headshot/67/current"


def _headshot_url(player_id: int) -> str:
    """MLB Stats API headshot URL for a given mlbam player_id. The URL pattern
    redirects to a generic silhouette automatically if the player doesn't
    have a portrait, so this never 404s in practice."""
    return f"{_HEADSHOT_BASE}/{player_id}/headshot/67/current"


def _hof_summary(db, player_id: int) -> tuple[bool, Optional[int]]:
    """Look up (is_hof, hof_year). is_hof = any inducted=True row, hof_year =
    that row's year. Returns (False, None) when the player has no HOF entry."""
    rows = crud.get_player_hof(db, player_id)
    inducted = next((h for h in rows if h.inducted), None)
    return (inducted is not None, inducted.year_inducted if inducted else None)


def _bio_dict(row, db=None) -> dict:
    """Pull bio fields off a Player/Pitcher ORM row, plus headshot URL and
    (when a session is provided) HOF status. The session is required for
    is_hof / hof_year — without it those fields are False / None.

    Callers always have a session in scope (search_player and the four
    get_*_stats functions all build the dict inside the with-block), so HOF
    fields are always populated in practice.
    """
    if row is None:
        return {}
    out = {k: getattr(row, k, None) for k in _BIO_COLUMNS}
    y, m, d = out.get("birth_year"), out.get("birth_month"), out.get("birth_day")
    out["birthdate"] = (
        f"{int(y):04d}-{int(m):02d}-{int(d):02d}" if (y and m and d) else None
    )
    out["headshot_url"] = _headshot_url(row.player_id)
    if db is not None:
        is_hof, hof_year = _hof_summary(db, row.player_id)
        out["is_hof"]   = is_hof
        out["hof_year"] = hof_year
    else:
        out["is_hof"]   = False
        out["hof_year"] = None
    return out

pybaseball.cache.enable()

# Historical seasons never change; current season updates throughout the day.
_TTL_HISTORICAL = int(os.getenv("CACHE_TTL_HISTORICAL", str(24 * 3600)))  # 24 h
_TTL_CURRENT    = int(os.getenv("CACHE_TTL_CURRENT",    str(30 * 60)))    # 30 min

# bwar_bat uses Baseball Reference franchise codes; map to full display names
# so team names are consistent whether or not the bref standard-stats merge succeeds.
_TEAM_DISPLAY: dict[str, str] = {
    "ARI": "Arizona",       "ATL": "Atlanta",        "BAL": "Baltimore",
    "BOS": "Boston",        "CHC": "Chicago",        "CHW": "Chicago",
    "CIN": "Cincinnati",    "CLE": "Cleveland",      "COL": "Colorado",
    "DET": "Detroit",       "HOU": "Houston",        "KCR": "Kansas City",
    "LAA": "Los Angeles",   "LAD": "Los Angeles",    "MIA": "Miami",
    "MIL": "Milwaukee",     "MIN": "Minnesota",      "NYM": "New York",
    "NYY": "New York",      "OAK": "Oakland",        "PHI": "Philadelphia",
    "PIT": "Pittsburgh",    "SDP": "San Diego",      "SEA": "Seattle",
    "SFG": "San Francisco", "STL": "St. Louis",      "TBR": "Tampa Bay",
    "TEX": "Texas",         "TOR": "Toronto",        "WSN": "Washington",
}

# Chadwick bbref ↔ mlbam bridge — shipped in `backend/data/lahman/`. Used
# by the targeted backfill to fill `bbref_id` on players whose Lahman
# load skipped them (mostly fresh debuts whose Chadwick row landed
# before they made the People.csv).
_CHADWICK_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "lahman", "chadwick_mlb.csv",
)

# ---------------------------------------------------------------------------
# Simple in-memory TTL cache (used only by the fetch_and_save_* helpers).
# ---------------------------------------------------------------------------

_store: dict = {}

# Cached Chadwick mlbam → bbref reverse map; populated on first access
# and reused for the life of the process.
_chadwick_mlbam_to_bbref: Optional[dict[int, str]] = None


def _load_chadwick_mlbam_to_bbref() -> dict[int, str]:
    """Return the {key_mlbam: key_bbref} reverse map. Cached after the
    first load — the CSV is ~1 MB and only read once per process.
    Returns an empty dict (and logs once) if the CSV is missing, so
    callers can degrade gracefully."""
    global _chadwick_mlbam_to_bbref
    if _chadwick_mlbam_to_bbref is not None:
        return _chadwick_mlbam_to_bbref
    bridge: dict[int, str] = {}
    try:
        with open(_CHADWICK_CSV, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                bbref = row.get("key_bbref")
                mlbam = row.get("key_mlbam")
                if bbref and mlbam:
                    try:
                        bridge[int(mlbam)] = bbref
                    except (TypeError, ValueError):
                        continue
    except FileNotFoundError:
        log.warning("Chadwick bridge CSV not found at %s — bbref_id "
                    "lookups will return None", _CHADWICK_CSV)
    _chadwick_mlbam_to_bbref = bridge
    return bridge


def _cached(key: str, fn, ttl: int = _TTL_CURRENT):
    entry = _store.get(key)
    if entry and (time.monotonic() - entry["ts"]) < ttl:
        return entry["value"]
    result = fn()
    _store[key] = {"value": result, "ts": time.monotonic()}
    return result


def _fetch_with_retry(fn, retries: int = 2, delay: float = 3.0):
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_year() -> int:
    return datetime.date.today().year


def _batting_bref(year: int) -> pd.DataFrame:
    ttl = _TTL_CURRENT if year == _current_year() else _TTL_HISTORICAL
    return _cached(
        f"batting_bref_{year}",
        lambda: _fetch_with_retry(lambda: pybaseball.batting_stats_bref(year)),
        ttl=ttl,
    )


def _bwar_bat_all() -> pd.DataFrame:
    return _cached("bwar_bat_all", lambda: pybaseball.bwar_bat(return_all=True),
                   ttl=_TTL_CURRENT)


def _pitching_bref(year: int) -> pd.DataFrame:
    ttl = _TTL_CURRENT if year == _current_year() else _TTL_HISTORICAL
    return _cached(
        f"pitching_bref_{year}",
        lambda: _fetch_with_retry(lambda: pybaseball.pitching_stats_bref(year)),
        ttl=ttl,
    )


def _bwar_pitch_all() -> pd.DataFrame:
    return _cached("bwar_pitch_all", lambda: pybaseball.bwar_pitch(return_all=True),
                   ttl=_TTL_CURRENT)


def _ip_to_decimal(ip) -> float:
    """Convert baseball IP notation (e.g. 30.1 = 30⅓ innings) to decimal."""
    if pd.isna(ip):
        return 0.0
    ip = float(ip)
    whole = int(ip)
    outs = round((ip - whole) * 10)   # .1 → 1 out, .2 → 2 outs
    return whole + outs / 3


def _fip(hr: float, bb: float, hbp: float, so: float, ip_dec: float) -> Optional[float]:
    """FIP = (13*HR + 3*(BB+HBP) - 2*SO) / IP + ~3.10 constant."""
    if not ip_dec:
        return None
    return round(((13 * hr + 3 * (bb + hbp) - 2 * so) / ip_dec) + 3.10, 2)


def _safe(val):
    """Convert numpy/pandas scalars to JSON-safe Python types."""
    if pd.isna(val):
        return None
    if isinstance(val, float):
        return round(float(val), 3)
    if hasattr(val, "item"):          # numpy scalar
        return val.item()
    return val


def _safe_col(row, col: str):
    """row[col] via _safe(), but None if col is missing from the Series.

    Used when reading bref/bwar fields that may not exist in older / partial
    pybaseball responses (e.g. SH / SF / GIDP / SHO depending on year).
    """
    if col not in row.index:
        return None
    return _safe(row[col])


def _batting_derived(r) -> dict:
    """Compute derived batting stats from a batting_stats_bref row."""
    def _f(col):
        v = r[col]
        return float(v) if pd.notna(v) else 0.0

    pa  = _f("PA");  ab  = _f("AB");  h   = _f("H")
    hr  = _f("HR");  bb  = _f("BB");  ibb = _f("IBB")
    hbp = _f("HBP"); so  = _f("SO");  sf  = _f("SF")
    doubles = _f("2B"); triples = _f("3B")
    ba  = _f("BA");  slg = _f("SLG")

    babip_denom = ab - so - hr + sf
    babip = round((h - hr) / babip_denom, 3) if babip_denom > 0 else None

    iso = round(slg - ba, 3) if slg and ba else None

    bb_pct = round(bb / pa, 3) if pa > 0 else None
    k_pct  = round(so / pa, 3) if pa > 0 else None

    singles = h - doubles - triples - hr
    woba_num = (0.69 * (bb - ibb) + 0.72 * hbp + 0.89 * singles
                + 1.27 * doubles + 1.62 * triples + 2.10 * hr)
    woba_den = ab + (bb - ibb) + sf + hbp
    woba = round(woba_num / woba_den, 3) if woba_den > 0 else None

    return {
        "BABIP": babip,
        "ISO":   iso,
        "BB_pct": bb_pct,
        "K_pct":  k_pct,
        "wOBA":  woba,
    }


def _db_row_to_season(row) -> dict:
    """Convert a PlayerSeason ORM row to a season dict."""
    return {k: getattr(row, k) for k in _PS_COLUMNS}


def _db_pitcher_row_to_season(row) -> dict:
    """Convert a PitcherSeason ORM row to a season dict."""
    return {k: getattr(row, k) for k in _PS_PITCH_COLUMNS}


# ---------------------------------------------------------------------------
# Public API — DB-only reads
# ---------------------------------------------------------------------------

# Reverse maps for resolving the messy `team` column to a stable Lahman-style
# code. The DB stores three different shapes depending on which loader wrote
# the row:
#   • Lahman → raw teamID, e.g. "NYA", "BOS", "SLN"
#   • nightly bref override → bref Tm, e.g. "NYY", "STL"
#   • nightly bwar (no bref) → city via _TEAM_DISPLAY, e.g. "New York"
# The first two are already 2–3 char uppercase codes and pass through. The
# third needs a reverse lookup, sometimes with the league to disambiguate
# two-team cities (NYY vs NYM, LAN vs LAA, CHA vs CHN).
_CITY_TO_CODE: dict[str, str] = {
    "Arizona":       "ARI",
    "Atlanta":       "ATL",
    "Baltimore":     "BAL",
    "Boston":        "BOS",
    "Cincinnati":    "CIN",
    "Cleveland":     "CLE",
    "Colorado":      "COL",
    "Detroit":       "DET",
    "Houston":       "HOU",
    "Kansas City":   "KCA",
    "Miami":         "MIA",
    "Milwaukee":     "MIL",
    "Minnesota":     "MIN",
    "Oakland":       "OAK",
    # 2025 rebrand — bref / pybaseball write the bare team name with no
    # city prefix once the team relocated. Same franchise as OAK so the
    # downstream lookups stay consistent.
    "Athletics":     "OAK",
    "Philadelphia":  "PHI",
    "Pittsburgh":    "PIT",
    "San Diego":     "SDN",
    "San Francisco": "SFN",
    "Seattle":       "SEA",
    "St. Louis":     "SLN",
    "Tampa Bay":     "TBA",
    "Texas":         "TEX",
    "Toronto":       "TOR",
    "Washington":    "WAS",
}

_CITY_LEAGUE_TO_CODE: dict[tuple[str, str], str] = {
    ("Chicago",     "AL"): "CHA",
    ("Chicago",     "NL"): "CHN",
    ("Los Angeles", "AL"): "LAA",
    ("Los Angeles", "NL"): "LAN",
    ("New York",    "AL"): "NYA",
    ("New York",    "NL"): "NYN",
}


def _resolve_team_code(team: Optional[str], league: Optional[str]) -> Optional[str]:
    """Normalize a team-column value to a Lahman-style code.

    Returns None when input is empty. Returns the value unchanged when it
    already looks like a code (uppercase letters/digits, ≤4 chars). For
    city display values, falls back to (city, league) → code, then plain
    city → code. Returns None if none of those resolve."""
    if not team:
        return None

    short = len(team) <= 4 and team.replace(".", "").isalnum() and team.upper() == team
    if short:
        return team

    if league:
        code = _CITY_LEAGUE_TO_CODE.get((team, league))
        if code:
            return code

    return _CITY_TO_CODE.get(team)


def _latest_team_info(
    db, player_id: int, *, pitcher: bool,
) -> tuple[Optional[str], Optional[str]]:
    """(team_display, team_code) from the player's most recent season
    row across BOTH `pitcher_seasons` AND `player_seasons`.

    Looking at only one side leaves stale-data holes for ex-NL
    pitchers (Tyler Rogers et al.): they have batting rows in
    `player_seasons` from their pre-2022 NL plate appearances but
    their current team only lives on the pitcher side. When
    `search_player` runs its pitchers-then-players merge, the
    second pass overwrites the (correct) pitcher result with the
    (stale) batting one. Unifying the lookup picks the truly
    latest row regardless of which side it lives on.

    The `pitcher` parameter is kept for backward compatibility but
    no longer affects the result — both season tables are read
    every time. Two-way players (Ohtani: same team both sides)
    aren't affected; the answer matches either way.

    Returns (None, None) when the player has no season rows in
    either table."""
    _ = pitcher
    rows = (
        crud.get_pitcher_seasons(db, player_id)
        + crud.get_player_seasons(db, player_id)
    )
    if not rows:
        return None, None
    latest = max(rows, key=lambda r: r.year or 0)
    team = latest.team
    # bref / bwar can emit multi-stint aggregate rows for traded
    # players ("Minnesota,New York" or "MIN,NYY") where the team
    # column lists every stint joined by commas in chronological
    # order. The last piece is the player's most recent club —
    # take it so display + code resolution stay sensible instead
    # of bailing to NULL on the multi-team string.
    if team and "," in team:
        team = team.split(",")[-1].strip()
    return team, _resolve_team_code(team, latest.league)


def search_player(name: str) -> list[dict]:
    """Look up players by name in the database (both batters and pitchers).

    Returns merged results from the players and pitchers tables. If a player
    appears in both (e.g. two-way players), the players-table row wins.
    """
    if not connection.db_available():
        return []

    by_id: dict[int, dict] = {}
    try:
        with connection.get_session() as db:
            for r in crud.search_pitchers_by_name(db, name):
                team_display, team_code = _latest_team_info(db, r.player_id, pitcher=True)
                by_id[r.player_id] = {
                    "player_id":       r.player_id,
                    "name":            r.name,
                    "bbref_id":        r.bbref_id,
                    "mlb_debut":       r.mlb_debut,
                    "mlb_last_season": r.mlb_last_season,
                    "current_team":    team_display,
                    "team_code":       team_code,
                    **_bio_dict(r, db),
                }
            # Players (batters) take priority for two-way players.
            for r in crud.search_players_by_name(db, name):
                team_display, team_code = _latest_team_info(db, r.player_id, pitcher=False)
                by_id[r.player_id] = {
                    "player_id":       r.player_id,
                    "name":            r.name,
                    "bbref_id":        r.bbref_id,
                    "mlb_debut":       r.mlb_debut,
                    "mlb_last_season": r.mlb_last_season,
                    "current_team":    team_display,
                    "team_code":       team_code,
                    **_bio_dict(r, db),
                }
    except Exception:
        return []

    return list(by_id.values())


def get_player_by_id(player_id: int) -> Optional[dict]:
    """Look up one player by their MLB Stats API id (which is what
    our `player_id` column stores — MLBAM ids are the canonical key
    everywhere except the Lahman bridge). Returns the same
    PlayerSearchResult-shape `search_player` builds, so the iOS
    `PlayerProfileView` can drive off it directly. Used by the
    Scores tab when the user taps a player in a box score.

    Batters take priority over pitchers for two-way players, mirroring
    `search_player`'s precedence.
    """
    if not connection.db_available():
        return None
    with connection.get_session() as db:
        for getter, is_pitcher in [(crud.get_player, False),
                                   (crud.get_pitcher, True)]:
            row = getter(db, player_id)
            if row is None:
                continue
            team_display, team_code = _latest_team_info(
                db, row.player_id, pitcher=is_pitcher
            )
            return {
                "player_id":       row.player_id,
                "name":            row.name,
                "bbref_id":        row.bbref_id,
                "mlb_debut":       row.mlb_debut,
                "mlb_last_season": row.mlb_last_season,
                "current_team":    team_display,
                "team_code":       team_code,
                **_bio_dict(row, db),
            }
    return None


def get_current_stats(player_id: int) -> Optional[dict]:
    """Return current-season batting stats for a player, from PostgreSQL only."""
    if not connection.db_available():
        return None

    year = _current_year()
    with connection.get_session() as db:
        season_rows = [r for r in crud.get_player_seasons(db, player_id) if r.year == year]
        if not season_rows:
            return None
        season = _db_row_to_season(season_rows[0])
        player = crud.get_player(db, player_id)
        name = player.name if player else None
        bio = _bio_dict(player, db)

    standard = {
        "name":    name,
        "team":    season.get("team"),
        "G":       season.get("G"),
        "PA":      season.get("PA"),
        "AB":      season.get("AB"),
        "R":       season.get("R"),
        "H":       season.get("H"),
        "doubles": season.get("doubles"),
        "triples": season.get("triples"),
        "HR":      season.get("HR"),
        "RBI":     season.get("RBI"),
        "BB":      season.get("BB"),
        "IBB":     season.get("IBB"),
        "HBP":     season.get("HBP"),
        "SO":      season.get("SO"),
        "SB":      season.get("SB"),
        "CS":      season.get("CS"),
        "SH":      season.get("SH"),
        "SF":      season.get("SF"),
        "GIDP":    season.get("GIDP"),
        "BA":      season.get("BA"),
        "OBP":     season.get("OBP"),
        "SLG":     season.get("SLG"),
        "OPS":     season.get("OPS"),
        "BABIP":   season.get("BABIP"),
        "ISO":     season.get("ISO"),
        "BB_pct":  season.get("BB_pct"),
        "K_pct":   season.get("K_pct"),
        "wOBA":    season.get("wOBA"),
    }
    advanced = {
        "WAR":            season.get("WAR"),
        "WAR_off":        season.get("WAR_off"),
        "WAR_def":        season.get("WAR_def"),
        "WAA":            season.get("WAA"),
        "OPS_plus":       season.get("OPS_plus"),
        "runs_above_avg": season.get("runs_above_avg"),
        "runs_above_rep": season.get("runs_above_rep"),
    }

    return {
        "player_id":         player_id,
        "season":            year,
        "bio":               bio,
        "standard":          standard,
        "advanced":          advanced,
        # ISO-8601 UTC stamp of the nightly batch run that last
        # wrote this row. iOS uses it to decide whether a recent
        # box-score line is already in the DB (game started before
        # this stamp) or still missing (started after) and needs
        # to be folded onto the season totals at render time.
        "stats_last_updated": _iso_or_none(season.get("last_updated")),
    }


def _iso_or_none(value) -> Optional[str]:
    """Format a datetime as ISO-8601 with a trailing 'Z' if present;
    nil-pass-through otherwise. Used on the API response so iOS can
    decode straight into Date via `Date.ISO8601FormatStyle`."""
    if value is None:
        return None
    # SQLAlchemy returns naive UTC datetimes; tag the suffix
    # explicitly so the iOS decoder doesn't drift into local time.
    return value.replace(microsecond=0).isoformat() + "Z"


# ---------------------------------------------------------------------------
# League-leader detection for career stat tables
# ---------------------------------------------------------------------------
# Per-season "did this player lead the league / majors?" flags. The career
# endpoints attach a `leaders` dict per season row mapping stat label →
# "league" or "majors" so the iOS table can bold (league) and bold-italic
# (majors) the matching cell.
#
# Catalog entries: (api_label, db_column, sort_direction, qualifier)
#   - api_label is the dict key the iOS row looks up — also the user-facing
#     stat name. AVG / 2B / 3B differ from the DB column.
#   - sort_direction is "max" for higher-is-better stats and "min" for
#     ERA / WHIP where lower wins.
#   - qualifier is "PA" / "IP" / None and gates which rows are eligible
#     to "lead" — re-uses the same pro-rated thresholds as the leaderboard.
_LEADER_BATTING_STATS: list[tuple[str, str, str, Optional[str]]] = [
    # Rate / derived
    ("AVG",  "BA",       "max", "PA"),
    ("OBP",  "OBP",      "max", "PA"),
    ("SLG",  "SLG",      "max", "PA"),
    ("OPS",  "OPS",      "max", "PA"),
    ("OPS+", "OPS_plus", "max", "PA"),
    ("WAR",  "WAR",      "max", None),
    # Counting — primary
    ("HR",   "HR",       "max", None),
    ("RBI",  "RBI",      "max", None),
    ("R",    "R",        "max", None),
    ("H",    "H",        "max", None),
    ("2B",   "doubles",  "max", None),
    ("3B",   "triples",  "max", None),
    ("SB",   "SB",       "max", None),
    ("BB",   "BB",       "max", None),
    # Counting — secondary. bbref still tracks leaders for these even
    # when the leader-status is "anti-leading" (most strikeouts /
    # caught-stealings / GIDP) — mirror their convention so we bold
    # them the same way they do.
    ("SO",   "SO",       "max", None),
    ("CS",   "CS",       "max", None),
    ("HBP",  "HBP",      "max", None),
    ("SH",   "SH",       "max", None),
    ("SF",   "SF",       "max", None),
    ("IBB",  "IBB",      "max", None),
    ("GIDP", "GIDP",     "max", None),
    # TB is stored on player_seasons as of the column-add migration in
    # connection.py — the backfill keeps historical rows in sync. PA
    # qualifier per spec; can be loosened later if we want a separate
    # unqualified total-bases category.
    ("TB",   "TB",       "max", "PA"),
]
_LEADER_PITCHING_STATS: list[tuple[str, str, str, Optional[str]]] = [
    # Rate / derived — IP-qualified.
    ("ERA",   "ERA",      "min", "IP"),
    ("WHIP",  "WHIP",     "min", "IP"),
    ("FIP",   "FIP",      "min", "IP"),
    ("ERA+",  "ERA_plus", "max", "IP"),
    ("BB/9",  "BB_per9",  "min", "IP"),
    ("HR/9",  "HR_per9",  "min", "IP"),
    ("SO/9",  "K_per9",   "max", "IP"),
    ("WAR",   "WAR",      "max", None),
    # Counting — primary.
    ("W",     "W",        "max", None),
    ("SO",    "SO",       "max", None),
    ("SV",    "SV",       "max", None),
    ("IP",    "IP",       "max", None),
    ("GS",    "GS",       "max", None),
    ("CG",    "CG",       "max", None),
    ("SHO",   "SHO",      "max", None),
    # Counting — anti-leader / volume. bbref tracks them; we bold them
    # the same way (most losses, most ER allowed, etc.).
    ("L",     "L",        "max", None),
    ("ER",    "ER",       "max", None),
    ("HR",    "HR",       "max", None),
    ("BB",    "BB",       "max", None),
    ("IBB",   "IBB",      "max", None),
    ("HBP",   "HBP",      "max", None),
    ("BK",    "BK",       "max", None),
    ("WP",    "WP",       "max", None),
    ("BF",    "BFP",      "max", None),
    # H/9 and SO/BB are intentionally skipped — they're computed on
    # the iOS side from H/IP and SO/BB and aren't stored as columns
    # on pitcher_seasons, so the backend has no column to aggregate
    # against. W-L% similarly is computed from W and L.
]


def _leader_extremes(
    db,
    table,
    year: int,
    league: Optional[str],
    catalog: list[tuple[str, str, str, Optional[str]]],
    min_pa: int,
    min_ip: float,
) -> dict[str, object]:
    """One SQL pass that returns the {api_label → extreme value} for every
    stat in `catalog` over the given (year, league) slice. League=None
    targets the whole majors. Rate-stat extremes are computed only over
    rows that meet the PA / IP qualifier — `case` filters those rows
    inside the aggregate, so a single 1-AB pinch hitter never wins AVG.
    """
    selects = []
    for _, column_name, agg, qualifier in catalog:
        col = getattr(table, column_name)
        extreme_fn = func.max if agg == "max" else func.min
        if qualifier == "PA":
            expr = extreme_fn(case(
                (and_(table.PA.isnot(None), table.PA >= min_pa), col),
                else_=None,
            ))
        elif qualifier == "IP":
            expr = extreme_fn(case(
                (and_(table.IP.isnot(None), table.IP >= min_ip), col),
                else_=None,
            ))
        else:
            expr = extreme_fn(col)
        # Aliased with the DB column name so we can read each result by
        # attribute. SQL aliases must be valid identifiers — labels like
        # "2B" and "3B" can't be used directly.
        selects.append(expr.label(f"x_{column_name}"))

    q = db.query(*selects).filter(table.year == year)
    if league:
        q = q.filter(table.league == league)
    row = q.one()
    return {
        api_label: getattr(row, f"x_{column_name}")
        for (api_label, column_name, _, _) in catalog
    }


def _season_leaders(
    db,
    season: dict,
    table,
    catalog: list[tuple[str, str, str, Optional[str]]],
    extremes_cache: dict,
    qualifier_cache: dict,
) -> dict[str, str]:
    """Return {api_label: 'league' | 'majors'} for every catalog stat the
    player tied or led that season. Empty dict for seasons missing year
    or league. Caches extremes per (year, league) and qualifier
    thresholds per year so a 22-season career resolves with at most
    `2 × seasons` extremes queries plus one qualifier query per year."""
    year   = season.get("year")
    league = season.get("league")
    if year is None:
        return {}

    if year not in qualifier_cache:
        qualifier_cache[year] = _qualifier_thresholds(db, year)
    min_pa, min_ip = qualifier_cache[year]

    def cached_extremes(scope_league: Optional[str]) -> dict:
        key = (year, scope_league or "__majors__")
        if key not in extremes_cache:
            extremes_cache[key] = _leader_extremes(
                db, table, year, scope_league, catalog, min_pa, min_ip,
            )
        return extremes_cache[key]

    league_extremes = cached_extremes(league) if league else {}
    majors_extremes = cached_extremes(None)

    leaders: dict[str, str] = {}
    eps = 1e-9
    for api_label, column_name, _, _ in catalog:
        # The season dict is built from the DB row, so key under the
        # column name. AVG / 2B / 3B store as BA / doubles / triples.
        raw_value = season.get(column_name)
        if raw_value is None:
            continue
        try:
            v = float(raw_value)
        except (TypeError, ValueError):
            continue
        majors_v = majors_extremes.get(api_label)
        league_v = league_extremes.get(api_label)
        if majors_v is not None and abs(v - float(majors_v)) <= eps:
            leaders[api_label] = "majors"
        elif league_v is not None and abs(v - float(league_v)) <= eps:
            leaders[api_label] = "league"
    return leaders


def get_career_stats(player_id: int) -> Optional[dict]:
    """Return season-by-season batting stats for a player's career, from DB only."""
    if not connection.db_available():
        return None

    with connection.get_session() as db:
        rows = crud.get_player_seasons(db, player_id)
        if not rows:
            return None
        seasons = [_db_row_to_season(r) for r in sorted(rows, key=lambda r: r.year)]
        player = crud.get_player(db, player_id)
        name = player.name if player else None
        bio = _bio_dict(player, db)

        # Compute league/majors leadership per season inside the
        # session — _season_leaders issues its SQL through `db`. Caches
        # are scoped per request so multi-year careers don't duplicate
        # year-level work.
        extremes_cache: dict = {}
        qualifier_cache: dict = {}
        for s in seasons:
            s["leaders"] = _season_leaders(
                db, s, _PlayerSeason, _LEADER_BATTING_STATS,
                extremes_cache, qualifier_cache,
            )

    career_totals = _batting_career_totals(seasons)

    return {
        "player_id":     player_id,
        "name":          name,
        "bio":           bio,
        "seasons":       seasons,
        "career_totals": career_totals,
    }


def _batting_career_totals(seasons: list[dict]) -> dict:
    """Aggregate season-level batting rows into a `career_totals`
    dict. Counting stats are linear sums; rate stats come off the
    aggregated counting stats (the only correct way — averaging
    per-season rates would double-weight short seasons). Career
    OPS+ is the PA-weighted average across seasons where OPS+ is
    populated, matching bref's career-page convention. Seasons
    without counting stats (the leader / placeholder rows) are
    excluded from the sum block."""
    seasons_with_counting = [s for s in seasons if s.get("H") is not None]
    totals: dict = {
        "seasons": len(seasons),
        "WAR":     round(sum((s.get("WAR")     or 0.0) for s in seasons), 1),
        "WAR_off": round(sum((s.get("WAR_off") or 0.0) for s in seasons), 1),
        "WAR_def": round(sum((s.get("WAR_def") or 0.0) for s in seasons), 1),
    }
    if not seasons_with_counting:
        return totals

    g   = int(sum(s.get("G")   or 0 for s in seasons_with_counting))
    h   = int(sum(s.get("H")   or 0 for s in seasons_with_counting))
    hr  = int(sum(s.get("HR")  or 0 for s in seasons_with_counting))
    rbi = int(sum(s.get("RBI") or 0 for s in seasons_with_counting))
    ab  = int(sum(s.get("AB")  or 0 for s in seasons_with_counting))
    bb  = int(sum(s.get("BB")  or 0 for s in seasons_with_counting))
    hbp = int(sum(s.get("HBP") or 0 for s in seasons_with_counting))
    sf  = int(sum(s.get("SF")  or 0 for s in seasons_with_counting))
    tb  = int(sum(s.get("TB")  or 0 for s in seasons_with_counting))

    totals["G"]   = g
    totals["H"]   = h
    totals["HR"]  = hr
    totals["RBI"] = rbi

    if ab > 0:
        totals["AVG"] = round(h / ab, 3)
        totals["SLG"] = round(tb / ab, 3) if tb else None
    obp_den = ab + bb + hbp + sf
    if obp_den > 0:
        totals["OBP"] = round((h + bb + hbp) / obp_den, 3)
    if totals.get("OBP") is not None and totals.get("SLG") is not None:
        totals["OPS"] = round(totals["OBP"] + totals["SLG"], 3)

    # PA-weighted career OPS+ across seasons that have one. Bref's
    # actual career OPS+ formula uses career-level OBP/SLG vs PA-
    # weighted league OBP/SLG baselines — we don't store lgOBP /
    # lgSLG per season, so PA-weighting season OPS+ is the closest
    # approximation. Empirically lands within ~1 point for Bonds
    # (182.7 vs bref 182) and ~4 points low for Trout (168.6 vs
    # bref 173); the AB-weighted alternative drifts further from
    # bref on both, so PA-weighted is the best fit available.
    num = 0.0
    den = 0.0
    for s in seasons_with_counting:
        ops_plus = s.get("OPS_plus")
        pa = s.get("PA")
        if ops_plus is not None and pa is not None and pa > 0:
            num += float(ops_plus) * float(pa)
            den += float(pa)
    if den > 0:
        totals["OPS_plus"] = round(num / den, 1)

    return totals


def get_current_pitching_stats(player_id: int) -> Optional[dict]:
    """Return current-season pitching stats for a player, from PostgreSQL only."""
    if not connection.db_available():
        return None

    year = _current_year()
    with connection.get_session() as db:
        season_rows = [r for r in crud.get_pitcher_seasons(db, player_id) if r.year == year]
        if not season_rows:
            return None
        season = _db_pitcher_row_to_season(season_rows[0])
        pitcher = crud.get_pitcher(db, player_id)
        name = pitcher.name if pitcher else None
        bio = _bio_dict(pitcher, db)

    standard = {
        "name":  name,
        "team":  season.get("team"),
        "G":     season.get("G"),
        "GS":    season.get("GS"),
        "CG":    season.get("CG"),
        "SHO":   season.get("SHO"),
        "GF":    season.get("GF"),
        # Coerce None → 0 here so the iOS client doesn't have to decide
        # whether "no W/L" means "no record" (rare) or "0 record"
        # (common — relievers, fresh callups). Either way 0 is the
        # right display.
        "W":     season.get("W") or 0,
        "L":     season.get("L") or 0,
        "SV":    season.get("SV"),
        "IP":    season.get("IP"),
        "BFP":   season.get("BFP"),
        "H":     season.get("H"),
        "R":     season.get("R"),
        "ER":    season.get("ER"),
        "HR":    season.get("HR"),
        "BB":    season.get("BB"),
        "IBB":   season.get("IBB"),
        "SO":    season.get("SO"),
        "HBP":   season.get("HBP"),
        "WP":    season.get("WP"),
        "BK":    season.get("BK"),
        "SH":    season.get("SH"),
        "SF":    season.get("SF"),
        "GIDP":  season.get("GIDP"),
        "ERA":   season.get("ERA"),
        "WHIP":  season.get("WHIP"),
        "FIP":   season.get("FIP"),
        "BAOpp": season.get("BAOpp"),
        "BABIP": season.get("BABIP"),
        "K_per9":  season.get("K_per9"),
        "BB_per9": season.get("BB_per9"),
        "HR_per9": season.get("HR_per9"),
    }
    advanced = {
        "WAR":            season.get("WAR"),
        "WAA":            season.get("WAA"),
        "ERA_plus":       season.get("ERA_plus"),
        "runs_above_avg": season.get("runs_above_avg"),
        "runs_above_rep": season.get("runs_above_rep"),
    }

    return {
        "player_id":          player_id,
        "season":             year,
        "bio":                bio,
        "standard":           standard,
        "advanced":           advanced,
        "stats_last_updated": _iso_or_none(season.get("last_updated")),
    }


def get_career_pitching_stats(player_id: int) -> Optional[dict]:
    """Return season-by-season pitching stats for a player's career, from DB only."""
    if not connection.db_available():
        return None

    with connection.get_session() as db:
        rows = crud.get_pitcher_seasons(db, player_id)
        if not rows:
            return None
        seasons = [_db_pitcher_row_to_season(r) for r in sorted(rows, key=lambda r: r.year)]
        pitcher = crud.get_pitcher(db, player_id)
        name = pitcher.name if pitcher else None
        bio = _bio_dict(pitcher, db)

        # See `get_career_stats` for the same leaders pattern.
        extremes_cache: dict = {}
        qualifier_cache: dict = {}
        for s in seasons:
            s["leaders"] = _season_leaders(
                db, s, _PitcherSeason, _LEADER_PITCHING_STATS,
                extremes_cache, qualifier_cache,
            )

    return {
        "player_id":     player_id,
        "name":          name,
        "bio":           bio,
        "seasons":       seasons,
        "career_totals": _pitching_career_totals(seasons),
    }


def _pitching_career_totals(seasons: list[dict]) -> dict:
    """Aggregate season-level pitcher rows into a `career_totals`
    dict. Standard counting stats are linear sums; ERA and WHIP
    come off summed ER / H / BB / IP (the only correct way for a
    rate aggregate). Career ERA+ is the ER-weighted average across
    seasons where ERA+ is populated — matches bref's career-page
    methodology, which boils down algebraically to
    Σ(ER × ERA+) / Σ(ER). An IP-weighted average overestimates
    badly for pitchers with extreme low-pERA peaks (Pedro Martinez
    came out at 170 vs bref's 154 under IP-weighting); the ER-
    weighted form lands at 154 because the dominant seasons that
    pump ERA+ also yield fewer earned runs, so they get less
    weight in the ratio."""
    totals: dict = {
        "seasons": len(seasons),
        "WAR":     round(sum((s.get("WAR") or 0.0) for s in seasons), 1),
        "IP":      round(sum((s.get("IP")  or 0.0) for s in seasons), 1),
        "SO":      int(sum(s.get("SO") or 0 for s in seasons)),
        "BB":      int(sum(s.get("BB") or 0 for s in seasons)),
        "W":       int(sum(s.get("W")  or 0 for s in seasons)),
        "L":       int(sum(s.get("L")  or 0 for s in seasons)),
    }

    ip = sum((s.get("IP") or 0.0) for s in seasons)
    er = sum((s.get("ER") or 0) for s in seasons)
    h  = sum((s.get("H")  or 0) for s in seasons)
    bb = totals["BB"]

    if ip > 0:
        totals["ERA"]  = round(9.0 * float(er) / float(ip), 2)
        totals["WHIP"] = round(float(bb + h) / float(ip), 2)

    # ER-weighted career ERA+. Use stored ER when present; fall
    # back to ER ≈ ERA × IP / 9 for the older rows whose ER column
    # was never backfilled. Seasons missing ERA+ silently drop out
    # of both sides of the ratio.
    era_num = 0.0
    era_den = 0.0
    for s in seasons:
        era_plus = s.get("ERA_plus")
        if era_plus is None:
            continue
        season_er = s.get("ER")
        if season_er is None:
            season_ip = s.get("IP")
            season_era = s.get("ERA")
            if (season_ip is None or season_ip <= 0
                    or season_era is None):
                continue
            season_er = float(season_era) * float(season_ip) / 9.0
        if season_er <= 0:
            continue
        era_num += float(era_plus) * float(season_er)
        era_den += float(season_er)
    if era_den > 0:
        totals["ERA_plus"] = round(era_num / era_den, 1)

    # Career FIP. Algebraically:
    #   season_FIP = (13·HR + 3·(BB+HBP) - 2·SO) / IP + season_C
    # so each season's implied FIP constant is
    #   season_C = season_FIP - (13·HR + 3·(BB+HBP) - 2·SO) / IP
    # The career-correct FIP applies an IP-weighted average of those
    # constants to career-summed components:
    #   career_FIP = career_numerator / Σ IP + Σ(season_C · IP) / Σ IP
    # That collapses to Σ(season_FIP · IP) / Σ IP — i.e. an
    # IP-weighted average of season FIP. Mathematically equivalent
    # to the components form, but skips the per-season constant
    # back-out so we don't need every counting stat populated.
    # Verified on Pedro: 2.92, matches bref/fangraphs' 2.91.
    fip_num = 0.0
    fip_den = 0.0
    for s in seasons:
        fip = s.get("FIP")
        season_ip = s.get("IP")
        if fip is None or season_ip is None or season_ip <= 0:
            continue
        fip_num += float(fip) * float(season_ip)
        fip_den += float(season_ip)
    if fip_den > 0:
        totals["FIP"] = round(fip_num / fip_den, 2)

    return totals


# ---------------------------------------------------------------------------
# Fielding / awards / postseason / teams — DB-only reads
# ---------------------------------------------------------------------------

def _row_to_dict(row, exclude=("player_id",)) -> dict:
    """Materialize a SQLAlchemy ORM row into a plain dict (call inside session)."""
    return {
        c.key: getattr(row, c.key)
        for c in row.__table__.columns
        if c.key not in exclude
    }


def get_fielding(player_id: int) -> list[dict]:
    if not connection.db_available():
        return []
    with connection.get_session() as db:
        rows = crud.get_player_fielding(db, player_id)
        return [_row_to_dict(r) for r in rows]


def get_awards(player_id: int) -> list[dict]:
    if not connection.db_available():
        return []
    with connection.get_session() as db:
        rows = crud.get_player_awards(db, player_id)
        return [_row_to_dict(r) for r in rows]


def get_allstar(player_id: int) -> list[dict]:
    if not connection.db_available():
        return []
    with connection.get_session() as db:
        rows = crud.get_player_allstar(db, player_id)
        return [_row_to_dict(r) for r in rows]


def get_award_shares(player_id: int) -> list[dict]:
    """All MVP / Cy Young / Rookie-of-the-Year vote-share rows for a
    player, sorted by year then award then rank."""
    if not connection.db_available():
        return []
    with connection.get_session() as db:
        rows = crud.get_player_award_shares(db, player_id)
        return [_row_to_dict(r) for r in rows]


# Names in `player_awards.award_name` that count as the canonical
# headline trophies. Kept here rather than in main.py so the
# endpoint layer stays a thin pass-through.
_HEADLINE_AWARD_NAMES: dict[str, str] = {
    "Most Valuable Player": "MVP",
    "Cy Young Award":       "CY Young",
    "Rookie of the Year":   "ROY",
    "Gold Glove":           "Gold Glove",
    "Silver Slugger":       "Silver Slugger",
    "World Series MVP":     "World Series MVP",
}


def get_player_awards_full(player_id: int) -> Optional[dict]:
    """Enriched awards payload for a player. Combines the raw
    `player_awards` and `player_allstar` arrays with two derived
    structures the iOS career table renders:

      • `headline_awards` — counts of the major trophies the player
        has won, plus All-Star appearance count. Zero-count entries
        are omitted.
      • `career_by_year` — one entry per season the player was active
        in awards data, carrying that year's award wins, All-Star
        flag, and MVP / Cy / ROY voting rank + points when present.
    """
    if not connection.db_available():
        return None

    raw_awards  = get_awards(player_id)
    raw_allstar = get_allstar(player_id)
    raw_shares  = get_award_shares(player_id)
    if not raw_awards and not raw_allstar and not raw_shares:
        return None

    # ---- headline counts ----
    counts: dict[str, int] = {}
    for a in raw_awards:
        canonical = _HEADLINE_AWARD_NAMES.get(a.get("award_name") or "")
        if canonical:
            counts[canonical] = counts.get(canonical, 0) + 1
    # All-Star appearances — count any row where the player was
    # selected (GP can be 0 if the player was named but didn't play;
    # the selection itself still counts as an All-Star appearance).
    counts["All-Star"] = len(raw_allstar)

    # World Series wins live in the postseason-team data rather than
    # `player_awards`, so we don't surface a WS count in this dict
    # today. The iOS career-row badge can light up "WS" off the
    # postseason endpoint later if/when that wiring lands.

    headline_awards = {k: v for k, v in counts.items() if v > 0}

    # ---- per-year aggregation ----
    by_year: dict[int, dict] = {}
    for a in raw_awards:
        y = a.get("year")
        if y is None:
            continue
        bucket = by_year.setdefault(y, {"year": y, "awards": [], "allstar": False, "votes": []})
        bucket["awards"].append({
            "award_name": a.get("award_name"),
            "league":     a.get("league"),
            "notes":      a.get("notes"),
            "tie":        a.get("tie"),
        })
    for s in raw_allstar:
        y = s.get("year")
        if y is None:
            continue
        bucket = by_year.setdefault(y, {"year": y, "awards": [], "allstar": False, "votes": []})
        bucket["allstar"] = True
    for sh in raw_shares:
        y = sh.get("year")
        if y is None:
            continue
        bucket = by_year.setdefault(y, {"year": y, "awards": [], "allstar": False, "votes": []})
        bucket["votes"].append({
            "award_id":   sh.get("award_id"),
            "league":     sh.get("league"),
            "rank":       sh.get("rank"),
            "points_won": sh.get("points_won"),
            "points_max": sh.get("points_max"),
            "votes_first": sh.get("votes_first"),
        })

    career_by_year = sorted(by_year.values(), key=lambda b: b["year"])

    return {
        "player_id":        player_id,
        "headline_awards":  headline_awards,
        "career_by_year":   career_by_year,
        "awards":           raw_awards,
        "allstar":          raw_allstar,
        "award_shares":     raw_shares,
    }


def _season_stats_for_voting(db, player_id: int, year: int, award_id: str) -> dict:
    """Compact stat block for one player-year, used as the row
    subtitle on AwardVotingView. Returns both batting and pitching
    sides — iOS picks one or both depending on which is non-null
    (two-way players like 2021 Ohtani get both).

    Pre-2022 NL pitchers had real plate appearances under the old
    no-DH rules — a PlayerSeason row will exist for them, but those
    50-or-150-ish PAs are pitcher-at-the-plate noise, not a real
    batting line. Suppress the batting side when the player is
    primarily a pitcher (meaningful IP) and the batting volume is
    below the two-way floor (200 PA — set high enough to exclude
    full-season starters like Greg Maddux at ~100–150 PA, low
    enough to admit anyone splitting their year). 2014 Kershaw and
    1995 Maddux → pitching only; 2021 Ohtani → both sides.

    Cy Young is a pitching-only award, so the batting block is
    always suppressed regardless of two-way status — even Ohtani's
    Cy Young row (hypothetical or real) renders pitching only.
    """
    bat = (
        db.query(_PlayerSeason)
        .filter(_PlayerSeason.player_id == player_id,
                _PlayerSeason.year == year)
        .first()
    )
    pit = (
        db.query(_PitcherSeason)
        .filter(_PitcherSeason.player_id == player_id,
                _PitcherSeason.year == year)
        .first()
    )

    is_meaningful_pitcher = pit is not None and (pit.IP or 0) > 10
    is_meaningful_batter  = bat is not None and (bat.PA or 0) >= 200

    # A pitcher who batted (sub-200 PA) loses the batting block so
    # the row doesn't show pitcher-at-plate stats next to his real
    # pitching line. A true two-way player keeps both. Cy Young
    # forces pitching-only — batting is irrelevant to that award.
    show_batting = (
        award_id != "CY Young"
        and bat is not None
        and (not is_meaningful_pitcher or is_meaningful_batter)
    )

    batting = None
    if show_batting:
        batting = {
            "AVG":     bat.BA,
            "HR":      bat.HR,
            "RBI":     bat.RBI,
            "WAR":     bat.WAR,
            "PA":      bat.PA,
            "OPSplus": bat.OPS_plus,
        }

    pitching = None
    if pit is not None:
        pitching = {
            "ERA":     pit.ERA,
            "W":       pit.W,
            "L":       pit.L,
            "SO":      pit.SO,
            "WAR":     pit.WAR,
            "IP":      pit.IP,
            "ERAplus": pit.ERA_plus,
        }

    return {"batting": batting, "pitching": pitching}


def get_award_voting(award_id: str, year: int, league: str) -> Optional[dict]:
    """Return the ranked voting leaderboard for a (award_id, year,
    league) tuple, each row carrying a full PlayerSearchResult-shaped
    `player` block so the iOS row can render the same chrome as the
    leaderboard / search rows and tap-to-profile reuses the existing
    PlayerProfileView entry point."""
    if not connection.db_available():
        return None
    with connection.get_session() as db:
        rows = crud.get_award_share_voting(db, award_id, year, league)
        if not rows:
            return None
        entries: list[dict] = []
        for r in rows:
            player_row = crud.get_player(db, r.player_id) or crud.get_pitcher(db, r.player_id)
            if player_row is None:
                continue
            entries.append({
                "rank":         r.rank,
                "points_won":   r.points_won,
                "points_max":   r.points_max,
                "votes_first":  r.votes_first,
                "season_stats": _season_stats_for_voting(db, r.player_id, year, award_id),
                "player": {
                    "player_id":       player_row.player_id,
                    "name":            player_row.name,
                    "bbref_id":        player_row.bbref_id,
                    "mlb_debut":       player_row.mlb_debut,
                    "mlb_last_season": player_row.mlb_last_season,
                    "current_team":    None,
                    "team_code":       None,
                    "is_pitcher":      award_id == "CY Young",
                    **_bio_dict(player_row, db),
                },
            })
    return {
        "award_id": award_id,
        "year":     year,
        "league":   league,
        "entries":  entries,
    }


def get_postseason_batting(player_id: int) -> list[dict]:
    if not connection.db_available():
        return []
    with connection.get_session() as db:
        rows = crud.get_player_postseason_batting(db, player_id)
        return [_row_to_dict(r) for r in rows]


def get_postseason_pitching(player_id: int) -> list[dict]:
    if not connection.db_available():
        return []
    with connection.get_session() as db:
        rows = crud.get_player_postseason_pitching(db, player_id)
        return [_row_to_dict(r) for r in rows]


def get_hof(player_id: int) -> Optional[dict]:
    """Return Hall of Fame summary + full voting history for a player.
    None if there are no HOF ballot rows for them."""
    if not connection.db_available():
        return None
    with connection.get_session() as db:
        rows = crud.get_player_hof(db, player_id)
        if not rows:
            return None
        history = [_row_to_dict(r) for r in rows]
        is_hof, hof_year = _hof_summary(db, player_id)
    return {
        "player_id":       player_id,
        "is_hof":          is_hof,
        "hof_year":        hof_year,
        "voting_history":  history,
    }


# ---------------------------------------------------------------------------
# Game logs — MLB Stats API ingest + per-window splits
# ---------------------------------------------------------------------------

_MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
_MLB_HTTP_TIMEOUT = 30


# MLB Stats API numeric team id → Lahman team code. Authoritative
# canonical for new-school endpoints (roster, schedule) that ship
# abbreviations like "KC"/"LAA" while our DB / Lahman bridge
# expects "KCA"/"LAA". Use this dict whenever you need to write
# `team` into `player_seasons` / `pitcher_seasons` from MLB API
# data — going through the numeric id avoids the abbreviation
# mismatch that left Lane Thomas / Nick Loftin etc. stamped with
# "KC" instead of "KCA". The 108 Angels / 133 Athletics codes
# match the codes the team_seasons rows actually carry.
_MLB_TEAM_ID_TO_LAHMAN_TEAM_ID: dict[int, str] = {
    109: "ARI",   144: "ATL",   110: "BAL",   111: "BOS",
    145: "CHA",   112: "CHN",   113: "CIN",   114: "CLE",
    115: "COL",   116: "DET",   117: "HOU",   118: "KCA",
    108: "LAA",   119: "LAN",   146: "MIA",   158: "MIL",
    142: "MIN",   147: "NYA",   121: "NYN",   133: "ATH",
    143: "PHI",   134: "PIT",   135: "SDN",   137: "SFN",
    136: "SEA",   138: "SLN",   139: "TBA",   140: "TEX",
    141: "TOR",   120: "WAS",
}


def _mlb_get_json(path: str, params: dict) -> dict:
    """GET https://statsapi.mlb.com/api/v1/{path}?... and return parsed JSON.
    Uses urllib (stdlib) — no `requests` dependency."""
    qs  = urllib.parse.urlencode(params)
    url = f"{_MLB_STATS_API}/{path.lstrip('/')}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "baseball-stats-app/1.0"})
    with urllib.request.urlopen(req, timeout=_MLB_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_mlb_stats_api_batter(mlb_id: int, year: int) -> Optional[dict]:
    """Pull standard season batting stats for one player from MLB
    Stats API. Used by the current-season nightly batter pipeline
    so the iOS app sees the same number bref does — without the
    24h lag bref imposes between game completion and its public
    stats table.

    Returns a dict in the `player_seasons` shape (G, PA, AB, R, H,
    doubles, triples, HR, RBI, BB, SO, SB, CS, IBB, HBP, SF, SH,
    GIDP, BA, OBP, SLG, OPS) or None when the API has no current-
    season hitting splits for this player (rare: pitcher who's
    never batted, freshly-DFA'd / no MLB activity yet).
    """
    try:
        data = _mlb_get_json(
            f"people/{mlb_id}/stats",
            {"stats": "season", "season": year, "group": "hitting"},
        )
    except Exception:
        return None
    stats_blocks = data.get("stats") or []
    if not stats_blocks:
        return None
    splits = stats_blocks[0].get("splits") or []
    if not splits:
        return None
    s = splits[0].get("stat") or {}

    return {
        "G":       _to_int(s.get("gamesPlayed")),
        "PA":      _to_int(s.get("plateAppearances")),
        "AB":      _to_int(s.get("atBats")),
        "R":       _to_int(s.get("runs")),
        "H":       _to_int(s.get("hits")),
        "doubles": _to_int(s.get("doubles")),
        "triples": _to_int(s.get("triples")),
        "HR":      _to_int(s.get("homeRuns")),
        "RBI":     _to_int(s.get("rbi")),
        "BB":      _to_int(s.get("baseOnBalls")),
        "SO":      _to_int(s.get("strikeOuts")),
        "SB":      _to_int(s.get("stolenBases")),
        "CS":      _to_int(s.get("caughtStealing")),
        "IBB":     _to_int(s.get("intentionalWalks")),
        "HBP":     _to_int(s.get("hitByPitch")),
        "SF":      _to_int(s.get("sacFlies")),
        "SH":      _to_int(s.get("sacBunts")),
        "GIDP":    _to_int(s.get("groundIntoDoublePlay")),
        # MLB Stats API ships AVG / OBP / SLG / OPS as strings like
        # ".293" — Python's `float()` accepts the leading dot, so
        # no manual stripping needed. None on missing / "---" (the
        # zero-PA sentinel the API uses).
        "BA":      _safe_rate(s.get("avg")),
        "OBP":     _safe_rate(s.get("obp")),
        "SLG":     _safe_rate(s.get("slg")),
        "OPS":     _safe_rate(s.get("ops")),
    }


def _fetch_mlb_stats_api_pitcher(mlb_id: int, year: int) -> Optional[dict]:
    """Pitcher counterpart to `_fetch_mlb_stats_api_batter`. Returns
    the standard `pitcher_seasons` fields populated from MLB Stats
    API's season-pitching splits, or None when the API has no
    pitching activity for this player this season.

    Holds (`HLD`) is included in the response dict so callers that
    care can read it, but our `pitcher_seasons` schema doesn't
    store HLD today — the merge step into the season-row drops
    that key implicitly by not referencing it.
    """
    try:
        data = _mlb_get_json(
            f"people/{mlb_id}/stats",
            {"stats": "season", "season": year, "group": "pitching"},
        )
    except Exception:
        return None
    stats_blocks = data.get("stats") or []
    if not stats_blocks:
        return None
    splits = stats_blocks[0].get("splits") or []
    if not splits:
        return None
    s = splits[0].get("stat") or {}

    return {
        "G":     _to_int(s.get("gamesPlayed")),
        "GS":    _to_int(s.get("gamesStarted")),
        "W":     _to_int(s.get("wins")),
        "L":     _to_int(s.get("losses")),
        "SV":    _to_int(s.get("saves")),
        "HLD":   _to_int(s.get("holds")),
        "IP":    _ip_str_to_decimal(s.get("inningsPitched")),
        "H":     _to_int(s.get("hits")),
        "R":     _to_int(s.get("runs")),
        "ER":    _to_int(s.get("earnedRuns")),
        "HR":    _to_int(s.get("homeRuns")),
        "BB":    _to_int(s.get("baseOnBalls")),
        "IBB":   _to_int(s.get("intentionalWalks")),
        "SO":    _to_int(s.get("strikeOuts")),
        "HBP":   _to_int(s.get("hitByPitch")),
        "BK":    _to_int(s.get("balks")),
        "WP":    _to_int(s.get("wildPitches")),
        "ERA":   _safe_rate(s.get("era")),
        "WHIP":  _safe_rate(s.get("whip")),
    }


def _safe_rate(v) -> Optional[float]:
    """Parse a rate stat string from MLB Stats API. Handles ".293"
    (leading-dot rates), "2.11" (ERA), "---" (zero-AB sentinel),
    and None / empty. Returns None for anything unparseable."""
    if v is None or v == "" or v == "---":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_mlb_player_bio(mlb_id: int) -> Optional[dict]:
    """`/people/{id}` → minimal bio dict in `crud.save_player` /
    `crud.save_pitcher` shape. Used by the nightly update to insert
    rows for brand-new call-ups (Lahman doesn't have them; bref
    surfaces them with a usable mlbID, but no name/position/debut
    info). Returns None on 404 / network failure / unparseable
    response — caller logs + skips the player, retry on next run.

    bbref_id stays nil for these rows; the Lahman bridge will fill
    it once the player makes it into the next Lahman archive.
    """
    try:
        data = _mlb_get_json(f"people/{mlb_id}", {})
    except Exception:
        return None
    people = data.get("people") or []
    if not people:
        return None
    p = people[0]

    # MLB ships height as "5' 11\"" — parse to inches. None on
    # unparseable, which we tolerate (a NULL is fine).
    h_in: Optional[int] = None
    raw_h = p.get("height")
    if isinstance(raw_h, str) and "'" in raw_h:
        try:
            feet, rest = raw_h.split("'", 1)
            inches = rest.replace('"', "").strip()
            h_in = int(feet.strip()) * 12 + (int(inches) if inches else 0)
        except (TypeError, ValueError):
            h_in = None

    debut_iso = p.get("mlbDebutDate")
    debut_year: Optional[int] = None
    if isinstance(debut_iso, str) and len(debut_iso) >= 4:
        try:
            debut_year = int(debut_iso[:4])
        except ValueError:
            debut_year = None

    birth_iso = p.get("birthDate")
    birth_year = birth_month = birth_day = None
    if isinstance(birth_iso, str) and len(birth_iso) >= 10:
        try:
            birth_year  = int(birth_iso[0:4])
            birth_month = int(birth_iso[5:7])
            birth_day   = int(birth_iso[8:10])
        except ValueError:
            pass

    return {
        "player_id":       mlb_id,
        "name":            p.get("fullName") or f"Player {mlb_id}",
        "bbref_id":        None,
        "mlb_debut":       debut_year,
        "mlb_last_season": None,
        "position":        (p.get("primaryPosition") or {}).get("abbreviation"),
        "bats":            (p.get("batSide")        or {}).get("code"),
        "throws":          (p.get("pitchHand")      or {}).get("code"),
        "height":          h_in,
        "weight":          _to_int(p.get("weight")),
        "birth_year":      birth_year,
        "birth_month":     birth_month,
        "birth_day":       birth_day,
        "birth_city":      p.get("birthCity"),
        "birth_state":     p.get("birthStateProvince"),
        "birth_country":   p.get("birthCountry"),
        "debut":           debut_iso,
        "final_game":      None,
    }


def sync_player_current_team(player_id: int) -> dict:
    """Reconcile `team` on the player's current-year season rows
    against the canonical current-team abbreviation from MLB Stats
    API. Creates the row if it doesn't exist yet — this is the
    common case during the offseason / early-season window when
    bref hasn't picked up a player's new club yet, leaving them
    without a current-year row entirely. With the row absent,
    `_latest_team_info` falls back to last year's stale team.

    Returns a status dict the admin endpoint can echo back. Safe
    to call for retired players (the `currentTeam` field is absent
    on those, so we report `no_current_team` and exit without
    touching the DB).
    """
    try:
        data = _mlb_get_json(f"people/{player_id}", {})
    except Exception as exc:
        return {"player_id": player_id, "status": f"mlb_fetch_failed: {exc}"}
    people = data.get("people") or []
    if not people:
        return {"player_id": player_id, "status": "not_found_in_mlb_api"}
    p = people[0]
    current_team = p.get("currentTeam") or {}
    name = current_team.get("name")
    mlb_team_id = current_team.get("id")
    # Resolve to the Lahman code — the abbreviation MLB ships
    # ("KC", "LA") doesn't match what our DB / Lahman bridge
    # writes ("KCA", "LAN"), so going through the numeric id is
    # the only consistent path.
    lahman_code = _MLB_TEAM_ID_TO_LAHMAN_TEAM_ID.get(mlb_team_id) if mlb_team_id else None
    if not lahman_code:
        return {
            "player_id":   player_id,
            "status":      "no_current_team",
            "fullName":    p.get("fullName"),
            "mlb_team_id": mlb_team_id,
        }

    year = _current_year()
    bio_created: Optional[str] = None
    cleared_last_season = False
    with connection.get_session() as db:
        in_pitchers = crud.get_pitcher(db, player_id) is not None
        in_players  = crud.get_player(db, player_id)  is not None

        # Bootstrap a missing bio when /people/{id} confirms an
        # active MLB roster slot but the player has no row in
        # either bio table. Same shape as the Phase 5 roster
        # discovery — keeps single-player one-shot fixes working
        # for fresh rookies (Eury Pérez 691587 was the motivating
        # case: current Marlins pitcher whose mlb_id collides
        # with the retired 1990 OF in bref/Lahman).
        if not in_pitchers and not in_players:
            position = (p.get("primaryPosition") or {}).get("abbreviation") or ""
            is_pitcher = position == "P"
            bio = fetch_mlb_player_bio(player_id)
            if bio is None:
                return {
                    "player_id": player_id,
                    "fullName":  p.get("fullName"),
                    "status":    "bio_fetch_failed",
                }
            if bio.get("mlb_debut") is None:
                bio["mlb_debut"] = year
            if is_pitcher:
                crud.save_pitcher(db, bio)
                in_pitchers = True
                bio_created = "pitcher"
            else:
                crud.save_player(db, bio)
                in_players = True
                bio_created = "batter"

        # Clear stale mlb_last_season — the player is on an active
        # roster right now, so any "retired in YYYY" value from
        # Lahman's `finalGame` is wrong. Connor Phillips (683175)
        # was the motivating case: Lahman stamped 2023 from his
        # late-season debut, then the column was never cleared
        # when he returned in 2026.
        if in_pitchers:
            pit_row = crud.get_pitcher(db, player_id)
            if pit_row is not None and pit_row.mlb_last_season is not None:
                pit_row.mlb_last_season = None
                cleared_last_season = True
        if in_players:
            bat_row = crud.get_player(db, player_id)
            if bat_row is not None and bat_row.mlb_last_season is not None:
                bat_row.mlb_last_season = None
                cleared_last_season = True

        actions = _apply_team_to_season_rows(
            db,
            player_id=player_id,
            year=year,
            abbr=lahman_code,
            create_pitcher=in_pitchers,
            create_batter=in_players,
        )
        if actions or bio_created or cleared_last_season:
            db.commit()

    return {
        "player_id":           player_id,
        "fullName":            p.get("fullName"),
        "new_team":            lahman_code,
        "team_name":           name,
        "actions":             actions,
        "bio_created":         bio_created,
        "cleared_last_season": cleared_last_season,
        "status":              "ok" if (actions or bio_created or cleared_last_season) else "already_current",
    }


def _apply_team_to_season_rows(
    db,
    *,
    player_id: int,
    year: int,
    abbr: str,
    create_pitcher: bool,
    create_batter: bool,
) -> list[str]:
    """Shared mutation step for both single-player and bulk sync
    paths. Updates the existing current-year season rows when their
    `team` differs, or creates a minimal placeholder row (year +
    team only) when the bio table says the player exists but no
    season row has been written yet. Returns a flat list of strings
    describing what changed, suitable for the API response and
    nightly log.

    The flags decide which side(s) to touch: a pure pitcher gets a
    pitcher_seasons row only, a position player gets player_seasons,
    a two-way player gets both. The decision is keyed off bio-table
    presence rather than the roster position so two-way players
    don't lose their batter row to "this player is on the roster
    as a pitcher" inference.
    """
    actions: list[str] = []
    if create_pitcher:
        pit_row = (
            db.query(_PitcherSeason)
            .filter(_PitcherSeason.player_id == player_id,
                    _PitcherSeason.year == year)
            .first()
        )
        if pit_row is None:
            db.add(_PitcherSeason(player_id=player_id, year=year, team=abbr))
            actions.append("pitcher_seasons:created")
        elif pit_row.team != abbr:
            pit_row.team = abbr
            actions.append("pitcher_seasons:updated")
    if create_batter:
        bat_row = (
            db.query(_PlayerSeason)
            .filter(_PlayerSeason.player_id == player_id,
                    _PlayerSeason.year == year)
            .first()
        )
        if bat_row is None:
            db.add(_PlayerSeason(player_id=player_id, year=year, team=abbr))
            actions.append("player_seasons:created")
        elif bat_row.team != abbr:
            bat_row.team = abbr
            actions.append("player_seasons:updated")
    return actions


def sync_all_player_teams_from_rosters(current_year: int) -> dict:
    """Walk all 30 MLB active rosters and reconcile the `team`
    column on each player's current-year `pitcher_seasons` /
    `player_seasons` row. Creates a minimal placeholder row when
    one doesn't exist yet (offseason move + bref hasn't shipped a
    current-year row), so `_latest_team_info` no longer falls back
    to last year's stale team.

    30 API calls (one per team) instead of one-per-player —
    cheaper than `sync_player_current_team` in bulk while covering
    the same correctness bar for everyone currently on a 40-man.
    """
    try:
        teams_resp = _mlb_get_json("teams", {"sportId": 1, "season": current_year})
    except Exception as exc:
        return {"status": f"teams_fetch_failed: {exc}", "updated": 0, "created": 0}
    teams = teams_resp.get("teams") or []

    counts: dict[str, int] = {
        "pitcher_seasons:updated": 0,
        "pitcher_seasons:created": 0,
        "player_seasons:updated":  0,
        "player_seasons:created":  0,
        # Fresh-rookie discovery: when a roster player is missing
        # from both bio tables (bref hasn't shipped them yet),
        # pull the bio from MLB Stats API and insert it so the
        # next nightly's normal loop processes them via the MLB
        # API stat path.
        "pitchers_bio:created":    0,
        "players_bio:created":     0,
        # `mlb_last_season` cleared on existing bios when the
        # player shows up on an active roster — Lahman's
        # `finalGame` year sticks around even after a player
        # returns to MLB (Connor Phillips case).
        "pitchers_bio:reactivated": 0,
        "players_bio:reactivated":  0,
    }
    bio_failed: list[int] = []
    failed_teams: list[str] = []
    unmapped_teams: list[int] = []
    with connection.get_session() as db:
        for team in teams:
            team_id = team.get("id")
            if not team_id:
                continue
            # Resolve to the Lahman code — MLB API ships abbrev like
            # "KC" / "LAA" which don't match our DB's "KCA" / "LAN".
            # Going through the numeric id avoids the abbreviation
            # mismatch that left Lane Thomas et al. stamped with
            # "KC" instead of "KCA".
            lahman_code = _MLB_TEAM_ID_TO_LAHMAN_TEAM_ID.get(team_id)
            if not lahman_code:
                unmapped_teams.append(team_id)
                continue
            try:
                roster_resp = _mlb_get_json(
                    f"teams/{team_id}/roster",
                    {"rosterType": "active"},
                )
            except Exception:
                failed_teams.append(str(team_id))
                continue
            for entry in roster_resp.get("roster") or []:
                person = entry.get("person") or {}
                pid = person.get("id")
                if not pid:
                    continue
                in_pitchers = crud.get_pitcher(db, pid) is not None
                in_players  = crud.get_player(db, pid)  is not None
                # When the player exists on a 40-man but isn't in
                # any of our bio tables — typical for rookies who
                # debuted recently and haven't shown up in bref's
                # batting/pitching stats tables yet (McGonigle,
                # etc.) — pull the bio from MLB Stats API and
                # insert. Position determines which side: P slots
                # go to `pitchers`, everything else to `players`.
                if not in_pitchers and not in_players:
                    position = (entry.get("position") or {}).get("abbreviation") or ""
                    is_pitcher = position == "P"
                    bio = fetch_mlb_player_bio(pid)
                    if bio is None:
                        bio_failed.append(pid)
                        continue
                    if bio.get("mlb_debut") is None:
                        bio["mlb_debut"] = current_year
                    if is_pitcher:
                        crud.save_pitcher(db, bio)
                        in_pitchers = True
                        counts["pitchers_bio:created"] += 1
                    else:
                        crud.save_player(db, bio)
                        in_players = True
                        counts["players_bio:created"] += 1
                else:
                    # Existing bio + active-roster slot → clear any
                    # stale `mlb_last_season` Lahman left behind
                    # when the player came back from a gap year.
                    # `save_pitcher` / `save_player` won't overwrite
                    # non-null fields with null, so do it directly.
                    if in_pitchers:
                        pit_row = crud.get_pitcher(db, pid)
                        if pit_row is not None and pit_row.mlb_last_season is not None:
                            pit_row.mlb_last_season = None
                            counts["pitchers_bio:reactivated"] += 1
                    if in_players:
                        bat_row = crud.get_player(db, pid)
                        if bat_row is not None and bat_row.mlb_last_season is not None:
                            bat_row.mlb_last_season = None
                            counts["players_bio:reactivated"] += 1
                actions = _apply_team_to_season_rows(
                    db,
                    player_id=pid,
                    year=current_year,
                    abbr=lahman_code,
                    create_pitcher=in_pitchers,
                    create_batter=in_players,
                )
                for a in actions:
                    counts[a] = counts.get(a, 0) + 1
        db.commit()

    total = sum(counts.values())
    return {
        "status":         "ok",
        "total":          total,
        "counts":         counts,
        "failed_teams":   failed_teams,
        "unmapped_teams": unmapped_teams,
        "bio_failed":     bio_failed,
    }


def repair_null_stats(current_year: int) -> dict:
    """Find every current-year season row with `last_updated IS NULL`
    — those are placeholder rows the Phase 5 roster sync created but
    that the nightly stat-fill path never landed on (the gating bug
    we just fixed). For each one, fetch the player's MLB Stats API
    splits and write them in, stamping `last_updated` so future
    nightlies treat the row as fresh.

    Idempotent — calling again after a successful run is a no-op
    because the repaired rows now have `last_updated` set.
    """
    now = datetime.datetime.utcnow()
    pit_repaired = 0
    pit_no_data  = 0
    bat_repaired = 0
    bat_no_data  = 0
    with connection.get_session() as db:
        # Pitcher side
        pit_rows = (
            db.query(_PitcherSeason)
            .filter(_PitcherSeason.year == current_year,
                    _PitcherSeason.last_updated.is_(None))
            .all()
        )
        for r in pit_rows:
            stats = _fetch_mlb_stats_api_pitcher(r.player_id, current_year)
            if stats is None:
                pit_no_data += 1
                continue
            for key in ("G", "GS", "W", "L", "SV", "IP",
                        "H", "R", "ER", "HR",
                        "BB", "IBB", "SO", "HBP", "BK", "WP",
                        "ERA", "WHIP"):
                value = stats.get(key)
                if value is not None:
                    setattr(r, key, value)
            # Recompute K/9, BB/9, HR/9 from the new authoritative
            # counts so they don't drift from the merged IP / SO /
            # BB / HR values just written.
            ip = stats.get("IP") or 0.0
            if ip:
                r.K_per9  = round((stats.get("SO") or 0) * 9 / ip, 2)
                r.BB_per9 = round((stats.get("BB") or 0) * 9 / ip, 2)
                r.HR_per9 = round((stats.get("HR") or 0) * 9 / ip, 2)
            r.last_updated = now
            pit_repaired += 1

        # Batter side
        bat_rows = (
            db.query(_PlayerSeason)
            .filter(_PlayerSeason.year == current_year,
                    _PlayerSeason.last_updated.is_(None))
            .all()
        )
        for r in bat_rows:
            stats = _fetch_mlb_stats_api_batter(r.player_id, current_year)
            if stats is None:
                bat_no_data += 1
                continue
            for key in ("G", "PA", "AB", "R", "H", "doubles", "triples", "HR",
                        "RBI", "BB", "SO", "SB", "CS", "IBB", "HBP", "SF", "SH",
                        "GIDP", "BA", "OBP", "SLG", "OPS"):
                value = stats.get(key)
                if value is not None:
                    setattr(r, key, value)
            # TB rebuilt from the merged H / 2B / 3B / HR.
            h  = stats.get("H") or 0
            d2 = stats.get("doubles") or 0
            d3 = stats.get("triples") or 0
            hr = stats.get("HR") or 0
            r.TB = h + d2 + 2 * d3 + 3 * hr
            r.last_updated = now
            bat_repaired += 1

        db.commit()

    return {
        "status":             "ok",
        "year":               current_year,
        "pitcher_repaired":   pit_repaired,
        "pitcher_no_mlb_data": pit_no_data,
        "batter_repaired":    bat_repaired,
        "batter_no_mlb_data": bat_no_data,
    }


def repair_ip_decimals(current_year: int) -> dict:
    """One-shot fix for `pitcher_seasons` rows whose `IP` is stored
    as baseball notation (10.2 meaning 10 ⅔ innings) rather than
    true decimal (10.667). Caused by an older bref-path bug; the
    write path is fixed going forward, but existing rows persist
    the corrupted value until corrected here.

    Detection: a baseball-notation IP has tenths digit ∈ {1, 2}
    after rounding (.1 = ⅓, .2 = ⅔). True-decimal IPs round to
    tenths {0, 3, 7} (.0 / .333 / .667). The .0 case is ambiguous
    (same value in both encodings) and left alone — converting it
    would be a no-op.

    Conversion mirrors `_ip_to_decimal`: split whole + tenths,
    treat tenths as outs (1 or 2), add outs/3 back as the true
    fractional part.
    """
    now = datetime.datetime.utcnow()
    repaired: list[dict] = []
    examined = 0
    with connection.get_session() as db:
        rows = (
            db.query(_PitcherSeason)
            .filter(_PitcherSeason.year == current_year,
                    _PitcherSeason.IP.isnot(None))
            .all()
        )
        for r in rows:
            ip = r.IP
            if ip is None:
                continue
            examined += 1
            tenths = round(float(ip) * 10) % 10
            if tenths not in (1, 2):
                continue
            whole = int(float(ip))
            outs = tenths
            fixed = round(whole + outs / 3, 3)
            repaired.append({
                "player_id": r.player_id,
                "from":      float(ip),
                "to":        fixed,
            })
            r.IP = fixed
            r.last_updated = now
        if repaired:
            db.commit()

    return {
        "status":        "ok",
        "year":          current_year,
        "examined":      examined,
        "rows_repaired": len(repaired),
        # First 10 examples so a curl can confirm the conversion
        # is doing the right thing without dumping every row.
        "examples":      repaired[:10],
    }


def backfill_player_seasons(player_id: int, year_from: int, year_to: int) -> dict:
    """Targeted historical backfill — pulls season splits from the
    MLB Stats API for each year in [year_from, year_to] and writes
    a `player_seasons` / `pitcher_seasons` row per year.

    Motivating case: Riley Greene (682985) debuted 2022 but his
    `players` row has `bbref_id=null`, so the Lahman bridge never
    attached his 2022–2025 batting rows. This endpoint backfills
    them without needing the bref_id mapping.

    Also bootstraps a missing bio (same shape as
    `sync_player_current_team`'s discovery branch) when the
    player_id resolves to a real MLB person but has no row in
    `players` / `pitchers` yet — covers IL-listed rookies like
    Domingo Gonzalez (682445) who don't show on any active roster.

    The MLB Stats API season block has `team`, so each year's row
    gets stamped with the Lahman team code for that year. A second
    pass over `bwar_bat` / `bwar_pitch` fills in WAR / OPS+ /
    WAR_off / WAR_def / WAA / runs_above_avg / runs_above_rep
    (pitchers also get ERA+). The upsert path is column-additive,
    so the bwar merge only writes advanced columns — it doesn't
    overwrite the standard counting stats the MLB API leg already
    wrote, and doesn't NULL out anything that bwar happens to
    lack for an off-year.

    Also opportunistically backfills `bbref_id` on the bio row via
    the Chadwick bridge — without it, the Lahman loader can never
    link this player's historical rows.
    """
    if not connection.db_available():
        return {"status": "no_db"}

    counts: dict[str, int] = {
        "player_seasons:written":      0,
        "pitcher_seasons:written":     0,
        "player_seasons:war_merged":   0,
        "pitcher_seasons:war_merged":  0,
        "pitchers_bio:created":        0,
        "players_bio:created":         0,
        "bbref_id:populated":          0,
    }
    missing_years: list[int] = []

    with connection.get_session() as db:
        in_pitchers = crud.get_pitcher(db, player_id) is not None
        in_players  = crud.get_player(db, player_id)  is not None

        if not in_pitchers and not in_players:
            bio = fetch_mlb_player_bio(player_id)
            if bio is None:
                return {
                    "status":    "bio_fetch_failed",
                    "player_id": player_id,
                }
            position = bio.get("position") or ""
            is_pitcher = position == "P"
            if is_pitcher:
                crud.save_pitcher(db, bio)
                in_pitchers = True
                counts["pitchers_bio:created"] += 1
            else:
                crud.save_player(db, bio)
                in_players = True
                counts["players_bio:created"] += 1
            db.commit()

        # Populate bbref_id from the Chadwick bridge if missing.
        # Without it, the Lahman loader can't link historical rows
        # on the next archive drop — and the standalone bref
        # fetchers below still work either way (bwar keys off
        # mlb_ID, not bbref_id).
        bridge = _load_chadwick_mlbam_to_bbref()
        bbref = bridge.get(player_id)
        if bbref:
            if in_pitchers:
                pit_row = crud.get_pitcher(db, player_id)
                if pit_row is not None and not pit_row.bbref_id:
                    pit_row.bbref_id = bbref
                    counts["bbref_id:populated"] += 1
            if in_players:
                bat_row = crud.get_player(db, player_id)
                if bat_row is not None and not bat_row.bbref_id:
                    bat_row.bbref_id = bbref
                    counts["bbref_id:populated"] += 1
            db.commit()

    # Pre-build bwar lookups so we hit the (cached) full-history
    # frame once instead of slicing on every year iteration.
    bwar_bat_by_year = (
        _player_bwar_batting_seasons(player_id, year_from, year_to)
        if in_players else {}
    )
    bwar_pitch_by_year = (
        _player_bwar_pitching_seasons(player_id, year_from, year_to)
        if in_pitchers else {}
    )

    for year in range(year_from, year_to + 1):
        wrote_any = False
        if in_players:
            entry = _build_backfill_batter_entry(player_id, year)
            if entry is not None:
                with connection.get_session() as db:
                    crud.save_player_seasons(db, player_id, [entry])
                counts["player_seasons:written"] += 1
                wrote_any = True
            war_entry = bwar_bat_by_year.get(year)
            if war_entry:
                with connection.get_session() as db:
                    crud.save_player_seasons(
                        db, player_id, [{"year": year, **war_entry}],
                    )
                counts["player_seasons:war_merged"] += 1
                wrote_any = True
        if in_pitchers:
            entry = _build_backfill_pitcher_entry(player_id, year)
            if entry is not None:
                with connection.get_session() as db:
                    crud.save_pitcher_seasons(db, player_id, [entry])
                counts["pitcher_seasons:written"] += 1
                wrote_any = True
            war_entry = bwar_pitch_by_year.get(year)
            if war_entry:
                with connection.get_session() as db:
                    crud.save_pitcher_seasons(
                        db, player_id, [{"year": year, **war_entry}],
                    )
                counts["pitcher_seasons:war_merged"] += 1
                wrote_any = True
        if not wrote_any:
            missing_years.append(year)

    return {
        "status":        "ok",
        "player_id":     player_id,
        "year_from":     year_from,
        "year_to":       year_to,
        "counts":        counts,
        "missing_years": missing_years,
    }


def _player_bwar_batting_seasons(
    player_id: int, year_from: int, year_to: int,
) -> dict[int, dict]:
    """Aggregate per-stint bwar_bat rows for one player into one
    advanced-stat dict per year in [year_from, year_to]. Returns
    a {year: {WAR, OPS_plus, WAR_off, WAR_def, WAA, ...}} map.
    Empty dict if bwar has no rows for this player (or fails)."""
    try:
        bwar = _bwar_bat_all()
    except Exception as exc:
        log.warning("bwar_bat fetch failed for %s: %s", player_id, exc)
        return {}
    if "mlb_ID" not in bwar.columns or "year_ID" not in bwar.columns:
        return {}
    mlb_ids = pd.to_numeric(bwar["mlb_ID"], errors="coerce")
    years   = pd.to_numeric(bwar["year_ID"], errors="coerce")
    mask = (
        (mlb_ids == float(player_id))
        & (years >= year_from)
        & (years <= year_to)
    )
    slice_ = bwar[mask]
    if slice_.empty:
        return {}
    out: dict[int, dict] = {}
    for year, group in slice_.groupby(years[mask].astype(int)):
        total_pa = float(group["PA"].sum()) if "PA" in group else 0.0
        ops_plus: Optional[float] = None
        if total_pa > 0 and "OPS_plus" in group and not group["OPS_plus"].dropna().empty:
            ops_plus = float(
                (group["OPS_plus"].fillna(0) * group["PA"]).sum() / total_pa
            )
        entry: dict = {
            "WAR":            round(float(group["WAR"].sum()), 2),
            "WAR_off":        round(float(group["WAR_off"].sum()), 2)
                              if "WAR_off" in group else None,
            "WAR_def":        round(float(group["WAR_def"].sum()), 2)
                              if "WAR_def" in group else None,
            "WAA":            round(float(group["WAA"].sum()), 2)
                              if "WAA" in group else None,
            "OPS_plus":       round(ops_plus, 1) if ops_plus is not None else None,
            "runs_above_avg": round(float(group["runs_above_avg"].sum()), 2)
                              if "runs_above_avg" in group else None,
            "runs_above_rep": round(float(group["runs_above_rep"].sum()), 2)
                              if "runs_above_rep" in group else None,
        }
        # Drop None-valued keys so the upsert doesn't overwrite
        # existing non-null values with null.
        out[int(year)] = {k: v for k, v in entry.items() if v is not None}
    return out


def _player_bwar_pitching_seasons(
    player_id: int, year_from: int, year_to: int,
) -> dict[int, dict]:
    """Pitcher counterpart to `_player_bwar_batting_seasons`. ERA+
    is rate-based; weight by IPouts to combine multi-stint seasons."""
    try:
        bwar = _bwar_pitch_all()
    except Exception as exc:
        log.warning("bwar_pitch fetch failed for %s: %s", player_id, exc)
        return {}
    if "mlb_ID" not in bwar.columns or "year_ID" not in bwar.columns:
        return {}
    mlb_ids = pd.to_numeric(bwar["mlb_ID"], errors="coerce")
    years   = pd.to_numeric(bwar["year_ID"], errors="coerce")
    mask = (
        (mlb_ids == float(player_id))
        & (years >= year_from)
        & (years <= year_to)
    )
    slice_ = bwar[mask]
    if slice_.empty:
        return {}
    out: dict[int, dict] = {}
    for year, group in slice_.groupby(years[mask].astype(int)):
        ipouts = float(group["IPouts"].sum()) if "IPouts" in group else 0.0
        era_plus: Optional[float] = None
        if ipouts > 0 and "ERA_plus" in group and not group["ERA_plus"].dropna().empty:
            era_plus = float(
                (group["ERA_plus"].fillna(0) * group["IPouts"]).sum() / ipouts
            )
        entry: dict = {
            "WAR":            round(float(group["WAR"].sum()), 2),
            "WAR_def":        round(float(group["WAR_def"].sum()), 2)
                              if "WAR_def" in group else None,
            "WAA":            round(float(group["WAA"].sum()), 2)
                              if "WAA" in group else None,
            "ERA_plus":       round(era_plus, 1) if era_plus is not None else None,
            "runs_above_avg": round(float(group["runs_above_avg"].sum()), 2)
                              if "runs_above_avg" in group else None,
            "runs_above_rep": round(float(group["runs_above_rep"].sum()), 2)
                              if "runs_above_rep" in group else None,
        }
        out[int(year)] = {k: v for k, v in entry.items() if v is not None}
    return out


def _build_backfill_batter_entry(player_id: int, year: int) -> Optional[dict]:
    """Build a `player_seasons` row from one year's MLB Stats API
    hitting splits. Used by `backfill_player_seasons`. Returns
    None when the player has no batting activity for that year
    (off-year, minors-only, didn't exist yet, etc.)."""
    try:
        data = _mlb_get_json(
            f"people/{player_id}/stats",
            {"stats": "season", "season": year, "group": "hitting"},
        )
    except Exception:
        return None
    stats_blocks = data.get("stats") or []
    if not stats_blocks:
        return None
    splits = stats_blocks[0].get("splits") or []
    if not splits:
        return None
    split = splits[0]
    s = split.get("stat") or {}
    team_id = (split.get("team") or {}).get("id")
    team_code = _MLB_TEAM_ID_TO_LAHMAN_TEAM_ID.get(team_id) if team_id else None

    h  = _to_int(s.get("hits"))     or 0
    d2 = _to_int(s.get("doubles"))  or 0
    d3 = _to_int(s.get("triples"))  or 0
    hr = _to_int(s.get("homeRuns")) or 0

    entry: dict = {
        "year":    year,
        "G":       _to_int(s.get("gamesPlayed")),
        "PA":      _to_int(s.get("plateAppearances")),
        "AB":      _to_int(s.get("atBats")),
        "R":       _to_int(s.get("runs")),
        "H":       _to_int(s.get("hits")),
        "doubles": _to_int(s.get("doubles")),
        "triples": _to_int(s.get("triples")),
        "HR":      _to_int(s.get("homeRuns")),
        "RBI":     _to_int(s.get("rbi")),
        "BB":      _to_int(s.get("baseOnBalls")),
        "IBB":     _to_int(s.get("intentionalWalks")),
        "SO":      _to_int(s.get("strikeOuts")),
        "SB":      _to_int(s.get("stolenBases")),
        "CS":      _to_int(s.get("caughtStealing")),
        "HBP":     _to_int(s.get("hitByPitch")),
        "SF":      _to_int(s.get("sacFlies")),
        "SH":      _to_int(s.get("sacBunts")),
        "GIDP":    _to_int(s.get("groundIntoDoublePlay")),
        "TB":      h + d2 + 2 * d3 + 3 * hr,
        "BA":      _safe_rate(s.get("avg")),
        "OBP":     _safe_rate(s.get("obp")),
        "SLG":     _safe_rate(s.get("slg")),
        "OPS":     _safe_rate(s.get("ops")),
    }
    if team_code:
        entry["team"] = team_code
    return entry


def _build_backfill_pitcher_entry(player_id: int, year: int) -> Optional[dict]:
    """Pitcher counterpart to `_build_backfill_batter_entry`."""
    try:
        data = _mlb_get_json(
            f"people/{player_id}/stats",
            {"stats": "season", "season": year, "group": "pitching"},
        )
    except Exception:
        return None
    stats_blocks = data.get("stats") or []
    if not stats_blocks:
        return None
    splits = stats_blocks[0].get("splits") or []
    if not splits:
        return None
    split = splits[0]
    s = split.get("stat") or {}
    team_id = (split.get("team") or {}).get("id")
    team_code = _MLB_TEAM_ID_TO_LAHMAN_TEAM_ID.get(team_id) if team_id else None

    entry: dict = {
        "year": year,
        "G":    _to_int(s.get("gamesPlayed")),
        "GS":   _to_int(s.get("gamesStarted")),
        "W":    _to_int(s.get("wins")),
        "L":    _to_int(s.get("losses")),
        "SV":   _to_int(s.get("saves")),
        "IP":   _ip_str_to_decimal(s.get("inningsPitched")),
        "H":    _to_int(s.get("hits")),
        "R":    _to_int(s.get("runs")),
        "ER":   _to_int(s.get("earnedRuns")),
        "HR":   _to_int(s.get("homeRuns")),
        "BB":   _to_int(s.get("baseOnBalls")),
        "IBB":  _to_int(s.get("intentionalWalks")),
        "SO":   _to_int(s.get("strikeOuts")),
        "HBP":  _to_int(s.get("hitByPitch")),
        "BK":   _to_int(s.get("balks")),
        "WP":   _to_int(s.get("wildPitches")),
        "ERA":  _safe_rate(s.get("era")),
        "WHIP": _safe_rate(s.get("whip")),
    }
    if team_code:
        entry["team"] = team_code
    return entry


def _to_int(v) -> Optional[int]:
    """Defensive int parse; returns None for blank / non-numeric."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _ip_str_to_decimal(v) -> Optional[float]:
    """Convert IP from MLB API string ('6.1' = 6⅓) to decimal innings."""
    if v is None or v == "":
        return None
    try:
        s = str(v)
        whole, _, frac = s.partition(".")
        outs = int(frac[:1]) if frac and frac[:1].isdigit() else 0
        return round((int(whole) if whole else 0) + outs / 3, 3)
    except Exception:
        return None


def _opponent_label(opp: dict) -> Optional[str]:
    """Pick the most-iOS-friendly opponent label from the API's opponent block."""
    if not opp:
        return None
    for k in ("abbreviation", "teamCode", "fileCode", "shortName", "teamName", "name"):
        v = opp.get(k)
        if v:
            return str(v)
    return None


def _parse_batting_split(split: dict) -> Optional[dict]:
    """One MLB Stats API gameLog split → batting_gamelogs row dict, or None
    if the split doesn't carry the required keys (gamePk + date)."""
    stat = split.get("stat") or {}
    game = split.get("game") or {}
    opp  = split.get("opponent") or {}
    team = split.get("team") or {}

    game_pk = game.get("gamePk") or split.get("gamePk")
    date_raw = (
        split.get("date")
        or game.get("officialDate")
        or game.get("gameDate")
    )
    if game_pk is None or not date_raw:
        return None
    try:
        game_date = datetime.date.fromisoformat(str(date_raw)[:10])
    except ValueError:
        return None

    # W/L/T from the convenience flags MLB ships at the split level.
    if   split.get("isWin"):  result = "W"
    elif split.get("isLoss"): result = "L"
    elif split.get("isTie"):  result = "T"
    else:                      result = None

    return {
        "game_id":    str(game_pk),
        "game_date":  game_date,
        "season":     _to_int(split.get("season")) or game_date.year,
        "opponent":   _opponent_label(opp),
        "home_away":  "H" if split.get("isHome") else "A",
        "result":     result,
        "team_score": _to_int(team.get("score")),
        "opp_score":  _to_int(opp.get("score")),
        "AB":         _to_int(stat.get("atBats")),
        "R":          _to_int(stat.get("runs")),
        "H":          _to_int(stat.get("hits")),
        "doubles":    _to_int(stat.get("doubles")),
        "triples":    _to_int(stat.get("triples")),
        "HR":         _to_int(stat.get("homeRuns")),
        "RBI":        _to_int(stat.get("rbi")),
        "BB":         _to_int(stat.get("baseOnBalls")),
        "IBB":        _to_int(stat.get("intentionalWalks")),
        "SO":         _to_int(stat.get("strikeOuts")),
        "SB":         _to_int(stat.get("stolenBases")),
        "CS":         _to_int(stat.get("caughtStealing")),
        "HBP":        _to_int(stat.get("hitByPitch")),
        "SF":         _to_int(stat.get("sacFlies")),
        "LOB":        _to_int(stat.get("leftOnBase")),
    }


def _parse_pitching_split(split: dict) -> Optional[dict]:
    stat = split.get("stat") or {}
    game = split.get("game") or {}
    opp  = split.get("opponent") or {}

    game_pk = game.get("gamePk") or split.get("gamePk")
    date_raw = (
        split.get("date")
        or game.get("officialDate")
        or game.get("gameDate")
    )
    if game_pk is None or not date_raw:
        return None
    try:
        game_date = datetime.date.fromisoformat(str(date_raw)[:10])
    except ValueError:
        return None

    # Decision priority: saves > holds > blown saves > wins > losses > ND.
    # (The save/hold flags only fire for relievers who got the credit.)
    if   _to_int(stat.get("saves")):      result = "S"
    elif _to_int(stat.get("holds")):      result = "H"
    elif _to_int(stat.get("blownSaves")): result = "BS"
    elif _to_int(stat.get("wins")):       result = "W"
    elif _to_int(stat.get("losses")):     result = "L"
    else:                                  result = "ND"

    return {
        "game_id":   str(game_pk),
        "game_date": game_date,
        "season":    _to_int(split.get("season")) or game_date.year,
        "opponent":  _opponent_label(opp),
        "home_away": "H" if split.get("isHome") else "A",
        "result":    result,
        "IP":        _ip_str_to_decimal(stat.get("inningsPitched")),
        "H":         _to_int(stat.get("hits")),
        "R":         _to_int(stat.get("runs")),
        "ER":        _to_int(stat.get("earnedRuns")),
        "BB":        _to_int(stat.get("baseOnBalls")),
        "SO":        _to_int(stat.get("strikeOuts")),
        "HR":        _to_int(stat.get("homeRuns")),
        "HBP":       _to_int(stat.get("hitByPitch")),
        "WP":        _to_int(stat.get("wildPitches")),
        "pitches":   _to_int(stat.get("numberOfPitches")) or _to_int(stat.get("pitchesThrown")),
        "strikes":   _to_int(stat.get("strikes")),
    }


def _fetch_mlb_gamelog(player_id: int, season: int, group: str) -> list[dict]:
    """Hit /people/{id}/stats?stats=gameLog — return list of split dicts."""
    payload = _mlb_get_json(
        f"people/{player_id}/stats",
        {
            "stats":    "gameLog",
            "season":   season,
            "group":    group,            # "hitting" | "pitching"
            "gameType": "R",
        },
    )
    out: list[dict] = []
    for stats_block in payload.get("stats") or []:
        out.extend(stats_block.get("splits") or [])
    return out


def fetch_and_save_batting_gamelogs(player_id: int, season: int) -> int:
    """Pull batting gamelog from MLB Stats API; upsert into batting_gamelogs.
    Returns the number of game rows written (de-duplicated by game_id)."""
    if not connection.db_available():
        return 0
    splits = _fetch_mlb_gamelog(player_id, season, "hitting")
    rows: dict[str, dict] = {}
    for s in splits:
        parsed = _parse_batting_split(s)
        if parsed is None:
            continue
        rows[parsed["game_id"]] = parsed
    if not rows:
        return 0
    with connection.get_session() as db:
        crud.save_batting_gamelogs(db, player_id, list(rows.values()))
    return len(rows)


def fetch_and_save_pitching_gamelogs(player_id: int, season: int) -> int:
    if not connection.db_available():
        return 0
    splits = _fetch_mlb_gamelog(player_id, season, "pitching")
    rows: dict[str, dict] = {}
    for s in splits:
        parsed = _parse_pitching_split(s)
        if parsed is None:
            continue
        rows[parsed["game_id"]] = parsed
    if not rows:
        return 0
    with connection.get_session() as db:
        crud.save_pitching_gamelogs(db, player_id, list(rows.values()))
    return len(rows)


# ---------------------------------------------------------------------------
# Splits (last 5 / 10 / 15 / 30 / season)
# ---------------------------------------------------------------------------

def _aggregate_batting_window(games: list[dict]) -> dict:
    if not games:
        return {"G": 0, "AB": 0, "R": 0, "H": 0, "HR": 0, "RBI": 0, "BB": 0,
                "SO": 0, "BA": None, "OBP": None, "SLG": None, "OPS": None}
    s = {k: sum((g.get(k) or 0) for g in games) for k in
         ("AB", "R", "H", "doubles", "triples", "HR", "RBI",
          "BB", "SO", "SB", "HBP", "SF")}
    ab, h, hr, bb, hbp, sf = s["AB"], s["H"], s["HR"], s["BB"], s["HBP"], s["SF"]
    doubles, triples = s["doubles"], s["triples"]
    singles = h - doubles - triples - hr

    ba  = round(h / ab, 3) if ab > 0 else None
    obp_den = ab + bb + hbp + sf
    obp = round((h + bb + hbp) / obp_den, 3) if obp_den > 0 else None
    slg = round((singles + 2 * doubles + 3 * triples + 4 * hr) / ab, 3) if ab > 0 else None
    ops = round(obp + slg, 3) if (obp is not None and slg is not None) else None

    return {
        "G":   len(games),
        "AB":  ab, "R": s["R"], "H": h, "HR": hr, "RBI": s["RBI"],
        "BB":  bb, "SO": s["SO"], "SB": s["SB"],
        "BA":  ba, "OBP": obp, "SLG": slg, "OPS": ops,
    }


def _aggregate_pitching_window(games: list[dict]) -> dict:
    if not games:
        return {"G": 0, "IP": 0.0, "H": 0, "R": 0, "ER": 0, "BB": 0, "SO": 0,
                "HR": 0, "ERA": None, "WHIP": None, "K_per9": None, "BB_per9": None}
    ip_dec = round(sum((g.get("IP") or 0.0) for g in games), 3)
    s = {k: sum((g.get(k) or 0) for g in games) for k in
         ("H", "R", "ER", "BB", "SO", "HR")}

    era     = round(s["ER"] * 9 / ip_dec, 2) if ip_dec > 0 else None
    whip    = round((s["BB"] + s["H"]) / ip_dec, 2) if ip_dec > 0 else None
    k_per9  = round(s["SO"] * 9 / ip_dec, 2) if ip_dec > 0 else None
    bb_per9 = round(s["BB"] * 9 / ip_dec, 2) if ip_dec > 0 else None

    return {
        "G":  len(games),
        "IP": ip_dec,
        "H":  s["H"], "R": s["R"], "ER": s["ER"],
        "BB": s["BB"], "SO": s["SO"], "HR": s["HR"],
        "ERA": era, "WHIP": whip, "K_per9": k_per9, "BB_per9": bb_per9,
    }


def _gamelog_row_to_dict(row, columns_to_exclude=("player_id",)) -> dict:
    """Materialize a gamelog row to a JSON-friendly dict (dates → ISO strings)."""
    out = {}
    for c in row.__table__.columns:
        if c.key in columns_to_exclude:
            continue
        v = getattr(row, c.key)
        if isinstance(v, datetime.date):
            v = v.isoformat()
        out[c.key] = v
    return out


def _build_batting_splits(games: list[dict]) -> dict:
    """games are dicts (already in reverse-chrono order, season-filtered)."""
    return {
        "last_5":  _aggregate_batting_window(games[:5]),
        "last_10": _aggregate_batting_window(games[:10]),
        "last_15": _aggregate_batting_window(games[:15]),
        "last_30": _aggregate_batting_window(games[:30]),
        "season":  _aggregate_batting_window(games),
    }


def _build_pitching_splits(games: list[dict]) -> dict:
    return {
        "last_5":  _aggregate_pitching_window(games[:5]),
        "last_10": _aggregate_pitching_window(games[:10]),
        "last_15": _aggregate_pitching_window(games[:15]),
        "last_30": _aggregate_pitching_window(games[:30]),
        "season":  _aggregate_pitching_window(games),
    }


def get_batting_gamelog_response(
    player_id: int,
    season: Optional[int] = None,
    last_n: Optional[int] = None,
) -> Optional[dict]:
    """Read batting gamelogs from DB (auto-fetching from MLB Stats API on
    cache miss). Returns reverse-chrono game list + splits block. None if no
    games found even after fetch attempt."""
    if not connection.db_available():
        return None
    if season is None:
        season = _current_year()

    with connection.get_session() as db:
        rows  = crud.get_batting_gamelogs(db, player_id, season=season)
        games = [_gamelog_row_to_dict(r) for r in rows]

    if not games:
        try:
            fetch_and_save_batting_gamelogs(player_id, season)
        except Exception as exc:
            log.warning("MLB API batting gamelog fetch failed (%s, %s): %s",
                        player_id, season, exc)
        with connection.get_session() as db:
            rows  = crud.get_batting_gamelogs(db, player_id, season=season)
            games = [_gamelog_row_to_dict(r) for r in rows]

    if not games:
        return None

    splits = _build_batting_splits(games)
    if last_n:
        games = games[:last_n]

    return {
        "player_id": player_id,
        "season":    season,
        "games":     games,
        "splits":    splits,
    }


def get_pitching_gamelog_response(
    player_id: int,
    season: Optional[int] = None,
    last_n: Optional[int] = None,
) -> Optional[dict]:
    if not connection.db_available():
        return None
    if season is None:
        season = _current_year()

    with connection.get_session() as db:
        rows  = crud.get_pitching_gamelogs(db, player_id, season=season)
        games = [_gamelog_row_to_dict(r) for r in rows]

    if not games:
        try:
            fetch_and_save_pitching_gamelogs(player_id, season)
        except Exception as exc:
            log.warning("MLB API pitching gamelog fetch failed (%s, %s): %s",
                        player_id, season, exc)
        with connection.get_session() as db:
            rows  = crud.get_pitching_gamelogs(db, player_id, season=season)
            games = [_gamelog_row_to_dict(r) for r in rows]

    if not games:
        return None

    splits = _build_pitching_splits(games)
    if last_n:
        games = games[:last_n]

    return {
        "player_id": player_id,
        "season":    season,
        "games":     games,
        "splits":    splits,
    }


def get_team_standings(year: int) -> list[dict]:
    if not connection.db_available():
        return []
    with connection.get_session() as db:
        rows = crud.get_team_standings(db, year)
        return [_row_to_dict(r, exclude=()) for r in rows]


def get_team_history(team_id: str) -> list[dict]:
    """Return year-by-year record for a franchise. Resolves teamID → franchID
    so relocations (e.g. MON → WSN) appear in one continuous history."""
    if not connection.db_available():
        return []
    with connection.get_session() as db:
        franch_id = crud.get_team_franchise(db, team_id)
        if franch_id is None:
            return []
        rows = crud.get_team_history_by_franchise(db, franch_id)
        return [_row_to_dict(r, exclude=()) for r in rows]


# ---------------------------------------------------------------------------
# Leaderboards — top N players for a given (stat, year, player_type)
# ---------------------------------------------------------------------------
# Stat → (DB column, sort direction, optional minimum-PA/IP filter).
# - ERA / WHIP sort ASC because lower is better; everything else DESC.
# - Rate stats (AVG / OBP / SLG / OPS / ERA / WHIP) get a pro-rated PA/IP
#   minimum so a single pinch-hit AB or a one-inning relief outing doesn't
#   crown the table.
# - API stat keys are the user-facing labels and map to the SQLAlchemy
#   column name when they differ ("AVG"→"BA", "2B"→"doubles", "3B"→"triples").
# - Note on HLD: pitcher_seasons has no `holds` column today, so the
#   leaderboard catalog can't include it without a schema migration.
_LEADERBOARD_BATTING: dict[str, tuple[str, str, Optional[str]]] = {
    "HR":  ("HR",      "desc", None),
    "AVG": ("BA",      "desc", "PA"),
    "RBI": ("RBI",     "desc", None),
    "OPS": ("OPS",     "desc", "PA"),
    "H":   ("H",       "desc", None),
    "R":   ("R",       "desc", None),
    "SB":  ("SB",      "desc", None),
    "BB":  ("BB",      "desc", None),
    "OBP": ("OBP",     "desc", "PA"),
    "SLG": ("SLG",     "desc", "PA"),
    "WAR": ("WAR",     "desc", None),
    "2B":  ("doubles", "desc", None),
    "3B":  ("triples", "desc", None),
    "SO":  ("SO",      "desc", None),
    "PA":  ("PA",      "desc", None),
    "AB":  ("AB",      "desc", None),
}
_LEADERBOARD_PITCHING: dict[str, tuple[str, str, Optional[str]]] = {
    "ERA":  ("ERA",    "asc",  "IP"),
    "SO":   ("SO",     "desc", None),
    "W":    ("W",      "desc", None),
    "WHIP": ("WHIP",   "asc",  "IP"),
    "SV":   ("SV",     "desc", None),
    "IP":   ("IP",     "desc", None),
    "H":    ("H",      "desc", None),
    "BB":   ("BB",     "desc", None),
    "HR":   ("HR",     "desc", None),
    "WAR":  ("WAR",    "desc", None),
    "CG":   ("CG",     "desc", None),
    "SHO":  ("SHO",    "desc", None),
    # K/9 — column lives at `K_per9` on PitcherSeason; key matches
    # the "SO/9" label the career-table leader catalog uses (see
    # `_LEADER_PITCHING_STATS` above), so both lookups stay aligned.
    # IP-qualified so partial-season relievers don't crowd the
    # leaderboard with tiny-sample SO/9 outliers.
    "SO/9": ("K_per9", "desc", "IP"),
}

# Canonical team key (Lahman code) → matcher that picks up every
# stored variant for that franchise across the loaders.
#
# Storage in player_seasons / pitcher_seasons varies by source:
#   • Lahman load (historical) → Lahman codes ("NYA", "LAN", "FLO", …)
#   • Nightly bref/pybaseball (current season) → city display names
#     ("New York", "Boston", "Atlanta") with the league column set
#     so two-team cities can be disambiguated.
# The leaderboard filter must accept both.
#
# `codes`        — exact-match codes (Lahman + any historical aliases).
# `cities`       — unambiguous city display names; matched directly.
# `city_league`  — (city, league) pairs for two-team cities ("Chicago",
#                  "Los Angeles", "New York") where the team column
#                  alone can't disambiguate AL vs NL.
_TEAM_FILTER_MATCHERS: dict[str, dict] = {
    "ARI": {"codes": ["ARI"],          "cities": ["Arizona"]},
    "ATL": {"codes": ["ATL"],          "cities": ["Atlanta"]},
    "BAL": {"codes": ["BAL"],          "cities": ["Baltimore"]},
    "BOS": {"codes": ["BOS"],          "cities": ["Boston"]},
    "CHA": {"codes": ["CHA"],          "city_league": [("Chicago",     "AL")]},
    "CHN": {"codes": ["CHN"],          "city_league": [("Chicago",     "NL")]},
    "CIN": {"codes": ["CIN"],          "cities": ["Cincinnati"]},
    "CLE": {"codes": ["CLE"],          "cities": ["Cleveland"]},
    "COL": {"codes": ["COL"],          "cities": ["Colorado"]},
    "DET": {"codes": ["DET"],          "cities": ["Detroit"]},
    "HOU": {"codes": ["HOU"],          "cities": ["Houston"]},
    "KCA": {"codes": ["KCA"],          "cities": ["Kansas City"]},
    "LAA": {"codes": ["LAA", "ANA", "CAL"], "city_league": [("Los Angeles", "AL")]},
    "LAN": {"codes": ["LAN", "BRO"],   "city_league": [("Los Angeles", "NL")]},
    "MIA": {"codes": ["MIA", "FLO"],   "cities": ["Miami"]},
    "MIL": {"codes": ["MIL", "ML4"],   "cities": ["Milwaukee"]},
    "MIN": {"codes": ["MIN"],          "cities": ["Minnesota"]},
    "NYA": {"codes": ["NYA"],          "city_league": [("New York",    "AL")]},
    "NYN": {"codes": ["NYN"],          "city_league": [("New York",    "NL")]},
    "OAK": {"codes": ["OAK", "ATH"],   "cities": ["Oakland", "Athletics"]},
    "PHI": {"codes": ["PHI"],          "cities": ["Philadelphia"]},
    "PIT": {"codes": ["PIT"],          "cities": ["Pittsburgh"]},
    "SDN": {"codes": ["SDN"],          "cities": ["San Diego"]},
    "SFN": {"codes": ["SFN"],          "cities": ["San Francisco"]},
    "SEA": {"codes": ["SEA"],          "cities": ["Seattle"]},
    "SLN": {"codes": ["SLN"],          "cities": ["St. Louis"]},
    "TBA": {"codes": ["TBA"],          "cities": ["Tampa Bay"]},
    "TEX": {"codes": ["TEX"],          "cities": ["Texas"]},
    "TOR": {"codes": ["TOR"],          "cities": ["Toronto"]},
    "WAS": {"codes": ["WAS", "MON"],   "cities": ["Washington"]},
}

# Backwards-compat alias the route still references for whitelist
# membership; iterating its keys is identical to iterating the matcher
# dict's keys so `team in _TEAM_FILTER_VARIANTS` still validates.
_TEAM_FILTER_VARIANTS = _TEAM_FILTER_MATCHERS


def _team_filter_clause(table, team: str):
    """Build an OR clause that matches every storage variant for a
    franchise. Returns `None` when the team key is unknown so the
    caller can fall back to a plain equality (or treat it as a no-op
    filter — current callers raise 400 before this is reached)."""
    matcher = _TEAM_FILTER_MATCHERS.get(team)
    if matcher is None:
        return None
    clauses = []
    if matcher.get("codes"):
        clauses.append(table.team.in_(matcher["codes"]))
    if matcher.get("cities"):
        clauses.append(table.team.in_(matcher["cities"]))
    for city, league_code in matcher.get("city_league", []):
        clauses.append(and_(table.team == city, table.league == league_code))
    return or_(*clauses) if clauses else None

# MLB's standard rate-stat qualifiers, applied to completed seasons
# (3.1 PA per team game and 1.0 IP per team game over a 162-game schedule).
_QUALIFIER_PA_PER_GAME = 3.1
_QUALIFIER_IP_PER_GAME = 1.0
_FULL_SEASON_GAMES     = 162

# Single-team-filter qualifier — flat low minimums when the leaderboard
# is scoped to one franchise. The MLB-wide qualifier doesn't make sense
# for a 4–5 starter rotation, and would wipe out every result early in
# the season. These thresholds only filter out single-AB pinch hits and
# one-inning relief outings, not actual rotation arms or everyday bats.
_TEAM_FILTER_MIN_PA = 10
_TEAM_FILTER_MIN_IP = 3.0


def _qualifier_thresholds(db, year: int) -> tuple[int, float]:
    """Return (min_PA, min_IP) for rate-stat eligibility in `year`,
    pro-rated by the most games played by any team that season.

    For completed historical seasons this reproduces the standard 502 PA
    / 162 IP qualifying lines (give or take the schedule length). For an
    in-progress season it scales down so mid-season leaderboards aren't
    empty — at game 25 of 162 a batter needs 78 PA, a pitcher 25 IP.

    Falls back to a full 162-game schedule when team_seasons doesn't
    have a row for `year` yet (e.g. opening day before standings are
    saved). The pro-rated math then floors at zero, so an empty year
    still produces non-negative thresholds.
    """
    rows = crud.get_team_standings(db, year)
    games = []
    for r in rows:
        w, l = r.W, r.L
        if w is not None and l is not None:
            games.append(w + l)
    max_games = max(games) if games else _FULL_SEASON_GAMES
    if max_games <= 0:
        max_games = _FULL_SEASON_GAMES
    # math.ceil keeps the threshold strictly above the typical at-bat
    # so a 200-PA hitter at game 64 (3.1 × 64 = 198.4) still qualifies.
    min_pa = int(math.ceil(_QUALIFIER_PA_PER_GAME * max_games))
    min_ip = float(_QUALIFIER_IP_PER_GAME * max_games)
    return min_pa, min_ip


# Career-mode minimum qualifiers. Looser than per-season MLB rules
# because they accumulate across a whole career — a 1000 PA floor cuts
# pinch-hit-only utility guys without excluding genuine starters who
# had short careers (Joe DiMaggio still cleared this in 13 seasons).
# 500 IP is roughly 3 full-rotation seasons; relievers who never hit
# this don't belong in the all-time ERA / WHIP conversation.
_CAREER_MIN_PA = 1000
_CAREER_MIN_IP = 500.0

# Standard (modern) full-season qualifying thresholds for the all-time
# single-season rate-stat leaderboards. Used as a flat floor — we don't
# pro-rate by year-specific schedule length because the all-time list
# is dominated by modern seasons anyway, and pre-1961 short schedules
# rarely produced rate-stat leaders below this PA / IP bar.
_ALL_TIME_MIN_PA = 502
_ALL_TIME_MIN_IP = 162.0


def get_leaderboard(
    stat: str,
    year: Optional[int],
    player_type: str,
    mode: str = "season",
    limit: int = 25,
    league: Optional[str] = None,
    team: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> Optional[dict]:
    """Top `limit` players for a given (stat, year/mode) on either
    batting or pitching seasons. Three modes:

      • `season`   — top single seasons for the given `year` (default).
      • `all_time` — top single seasons across every year; `year` is
                     ignored. Rate-stat qualifier is the flat modern
                     full-season floor (502 PA / 162 IP).
      • `career`   — aggregated career totals per player. Counting
                     stats are SUM, rate stats are computed from the
                     career totals (career AVG = SUM(H)/SUM(AB), etc.).
                     Rate-stat eligibility uses a career floor
                     (1000 PA for batters, 500 IP for pitchers).

    `league` filters at the season-row level — for career, that means
    only seasons in that league count toward the player's career
    aggregate. `team` filters likewise (expanded to all historical
    franchise variants), and remains useful even in career mode for
    "best-ever Yankees careers" type queries.

    Returns a response dict shaped like:
      {
        "stat": "HR", "mode": "season",
        "year": 2026 (null in all_time/career),
        "player_type": "batter",
        "league": "AL", "team": "NYA",
        "min_pa": 502, "min_ip": 162.0,
        "leaders": [
          {
            "rank": 1, "value": 54.0,
            "year": 2026 (null in career),
            "player": { ...PlayerSearchResult... }
          },
          ...
        ]
      }

    Returns None if the database is unreachable or the stat is unknown.
    """
    if not connection.db_available():
        return None

    is_batter = player_type == "batter"
    catalog = _LEADERBOARD_BATTING if is_batter else _LEADERBOARD_PITCHING
    if stat not in catalog:
        return None

    if mode == "career":
        return _cached_leaderboard_career(
            stat=stat, is_batter=is_batter,
            limit=limit, league=league, team=team,
            year_from=year_from, year_to=year_to,
        )
    if mode == "all_time":
        return _leaderboard_all_time(
            stat=stat, is_batter=is_batter,
            limit=limit, league=league, team=team,
            year_from=year_from, year_to=year_to,
        )
    if year is None:
        return None
    # Season mode pins to a specific year — year_from/year_to are
    # ignored on purpose (the year filter is already maximally
    # specific).
    return _leaderboard_season(
        stat=stat, year=year, is_batter=is_batter,
        limit=limit, league=league, team=team,
    )


# Read-time defense against legacy (player_id, year) duplicates.
# A pre-PK ingest could write multiple rows for the same season
# (different stints, partial vs. merged stat rows, etc.), and the
# leaderboard query would surface all of them — "Sosa 1998 HR" might
# appear three times before deduping. The PK migration in
# connection.py is the structural fix; this helper is the defensive
# read-time guarantee.
def _DEDUPE_FETCH_LIMIT(limit: int) -> int:
    """Fetch this many rows from the DB so the per-pair dedupe below
    has enough headroom even when the historical data has 5–10 copies
    of the same season row. Cap at 500 so a malformed limit can't
    blow up the query."""
    return min(max(limit * 10, 100), 500)


def _dedupe_rows_by_player_year(rows, limit: int) -> list:
    """Drop duplicate (player_id, year) rows from an already-sorted
    list, keeping the first occurrence of each pair. Since callers
    have already applied the leaderboard's ORDER BY, the first
    occurrence is the best-ranked row — exactly what we want to
    surface. Returns up to `limit` deduped rows."""
    seen: set[tuple[int, int]] = set()
    out: list = []
    for r in rows:
        key = (r.player_id, r.year)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _ranked_unique_seasons(
    *, db, table, column, direction: str,
    year: Optional[int], eligibility: Optional[str],
    league: Optional[str], team: Optional[str],
    min_pa: int, min_ip: float, limit: int,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
):
    """Run a leaderboard query that's structurally guaranteed to return
    at most one row per (player_id, year).

    Mechanism: an inner SELECT assigns a ROW_NUMBER() partitioned by
    (player_id, year) — ordered so rn=1 is the "best" row per pair
    (highest value for desc stats, lowest for asc stats, PA/IP as a
    secondary tiebreaker to prefer the most complete stat row). The
    outer SELECT filters to rn=1, sorts by the actual stat, and takes
    the top `limit`.

    This is the SQL-level fortress: even if the underlying table still
    carries (player_id, year) duplicates from a pre-PK ingest, the
    response can never repeat a player's same season — the database
    itself enforces "one per pair" before ranking. The defensive
    Python-side `_dedupe_rows_by_player_year` runs after as a final
    safety net (cost: one ~25-element pass), but the CTE alone is
    sufficient.

    Pass `year=None` to skip the per-year filter (all-time mode).
    """
    is_asc = direction == "asc"

    # Partition order — rn=1 should be the row we *want* to keep.
    # Primary: the leaderboard stat itself, in the response sort
    # direction. Secondary: PA / IP descending so a tie on the stat
    # falls to the more-complete (higher-volume) row.
    partition_order = [column.asc() if is_asc else column.desc()]
    if hasattr(table, "PA"):
        partition_order.append(table.PA.desc())
    if hasattr(table, "IP"):
        partition_order.append(table.IP.desc())

    rn = (
        func.row_number()
        .over(
            partition_by=(table.player_id, table.year),
            order_by=tuple(partition_order),
        )
        .label("rn")
    )

    inner = db.query(table, rn).filter(column.isnot(None))
    if year is not None:
        inner = inner.filter(table.year == year)
    # year_from / year_to are independent of `year` — when season mode
    # is in play (`year` set), the explicit year filter dominates.
    # All-time mode passes year=None and uses the range filters to
    # narrow the eligible seasons.
    if year_from is not None:
        inner = inner.filter(table.year >= year_from)
    if year_to is not None:
        inner = inner.filter(table.year <= year_to)
    if league:
        inner = inner.filter(table.league == league)
    if team:
        clause = _team_filter_clause(table, team)
        if clause is not None:
            inner = inner.filter(clause)
    if eligibility == "PA":
        inner = inner.filter(table.PA.isnot(None), table.PA >= min_pa)
    elif eligibility == "IP":
        inner = inner.filter(table.IP.isnot(None), table.IP >= min_ip)
    sub = inner.subquery()

    # Aliased table → outer ORM access to the deduped subquery rows.
    # `aliased(table, sub)` makes `T.column_name` resolve to the
    # subquery's column rather than the underlying table's, so the
    # outer ORDER BY hits the dedup-filtered set.
    T = aliased(table, sub)
    outer_col = getattr(T, column.key)
    outer_order = outer_col.asc() if is_asc else outer_col.desc()

    # Fetch a slightly wider window than `limit` so the defensive
    # Python-side dedupe below has headroom on the off-chance the
    # ROW_NUMBER subquery somehow produced a tie that the outer
    # ORDER BY couldn't break deterministically (shouldn't happen,
    # but the cost is one extra `.limit() * 2` rows).
    rows = (
        db.query(T)
        .filter(sub.c.rn == 1)
        .order_by(outer_order)
        .limit(max(limit * 2, 50))
        .all()
    )
    return _dedupe_rows_by_player_year(rows, limit)


def _leaderboard_season(
    stat: str, year: int, is_batter: bool, limit: int,
    league: Optional[str], team: Optional[str],
) -> dict:
    """Top single-season performances for a specific year. The mode the
    leaderboard endpoint has always served — extracted here so the
    three-mode dispatcher can call it as one branch."""
    table = _PlayerSeason if is_batter else _PitcherSeason
    catalog = _LEADERBOARD_BATTING if is_batter else _LEADERBOARD_PITCHING
    column_name, direction, eligibility = catalog[stat]
    column = getattr(table, column_name)

    with connection.get_session() as db:
        if team:
            min_pa, min_ip = _TEAM_FILTER_MIN_PA, _TEAM_FILTER_MIN_IP
        else:
            min_pa, min_ip = _qualifier_thresholds(db, year)

        rows = _ranked_unique_seasons(
            db=db, table=table, column=column, direction=direction,
            year=year, eligibility=eligibility,
            league=league, team=team,
            min_pa=min_pa, min_ip=min_ip, limit=limit,
        )

        leaders = []
        for rank, season_row in enumerate(rows, start=1):
            player_row = _resolve_player_row(db, season_row.player_id, is_batter)
            if player_row is None:
                continue
            team_code = _resolve_team_code(season_row.team, season_row.league)
            leaders.append({
                "rank":   rank,
                "value":  getattr(season_row, column_name),
                "year":   season_row.year,
                "player": _player_payload(
                    player_row, db,
                    current_team=season_row.team,
                    team_code=team_code,
                    is_pitcher=not is_batter,
                ),
            })

    return {
        "stat":        stat,
        "mode":        "season",
        "year":        year,
        "player_type": "batter" if is_batter else "pitcher",
        "league":      league,
        "team":        team,
        "min_pa":      min_pa,
        "min_ip":      min_ip,
        "leaders":     leaders,
    }


def _leaderboard_all_time(
    stat: str, is_batter: bool, limit: int,
    league: Optional[str], team: Optional[str],
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> dict:
    """Top single-season performances across every year on record. Same
    shape as the season query but with the year filter dropped. Rate
    stats use the flat modern-season qualifier (502 PA / 162 IP) — we
    don't pro-rate per-year schedules because pre-1961 leaders cleared
    that bar anyway and modeling deadball-era schedules adds complexity
    without changing the produced list."""
    table = _PlayerSeason if is_batter else _PitcherSeason
    catalog = _LEADERBOARD_BATTING if is_batter else _LEADERBOARD_PITCHING
    column_name, direction, eligibility = catalog[stat]
    column = getattr(table, column_name)

    min_pa = _TEAM_FILTER_MIN_PA if team else _ALL_TIME_MIN_PA
    min_ip = _TEAM_FILTER_MIN_IP if team else _ALL_TIME_MIN_IP

    with connection.get_session() as db:
        rows = _ranked_unique_seasons(
            db=db, table=table, column=column, direction=direction,
            year=None, eligibility=eligibility,
            league=league, team=team,
            min_pa=min_pa, min_ip=min_ip, limit=limit,
            year_from=year_from, year_to=year_to,
        )

        leaders = []
        for rank, season_row in enumerate(rows, start=1):
            player_row = _resolve_player_row(db, season_row.player_id, is_batter)
            if player_row is None:
                continue
            team_code = _resolve_team_code(season_row.team, season_row.league)
            leaders.append({
                "rank":   rank,
                "value":  getattr(season_row, column_name),
                "year":   season_row.year,
                "player": _player_payload(
                    player_row, db,
                    current_team=season_row.team,
                    team_code=team_code,
                    is_pitcher=not is_batter,
                ),
            })

    return {
        "stat":        stat,
        "mode":        "all_time",
        "year":        None,
        "year_from":   year_from,
        "year_to":     year_to,
        "player_type": "batter" if is_batter else "pitcher",
        "league":      league,
        "team":        team,
        "min_pa":      min_pa,
        "min_ip":      min_ip,
        "leaders":     leaders,
    }


# Career rate stats — these compute from aggregated counting stats
# instead of pulling a season column. Set per role so the dispatcher
# knows when to apply the career PA/IP qualifier.
_CAREER_BATTING_RATE_STATS  = {"AVG", "OBP", "SLG", "OPS"}
_CAREER_PITCHING_RATE_STATS = {"ERA", "WHIP"}


# Career leaderboard cache. The career aggregation is the most
# expensive query in the app — GROUP BY player_id across hundreds of
# thousands of season rows runs ~500ms per stat, and the result only
# moves after a nightly update. A simple in-memory dict keyed by the
# full parameter tuple wins back that latency for everyone except the
# first caller in each TTL window.
#
# Thread safety: relying on the GIL alone. Setting a dict key is a
# single bytecode op in CPython, so the worst-case race is two
# concurrent misses recomputing the same result and one overwriting
# the other on insert — harmless. Skipping a lock keeps the hot path
# free of contention overhead.
_CAREER_CACHE_TTL_SECONDS = 60 * 60  # 1 hour — aligns with the nightly job cadence.
_career_cache: dict[tuple, tuple[float, dict]] = {}


def _cached_leaderboard_career(
    *,
    stat: str, is_batter: bool, limit: int,
    league: Optional[str], team: Optional[str],
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> dict:
    """Memoizing wrapper around `_leaderboard_career`. Returns a cached
    result when one is still fresh; otherwise computes, caches, and
    returns the live result."""
    key = (
        stat,
        is_batter,
        limit,
        league,
        team,
        year_from,
        year_to,
    )
    now = time.time()
    cached = _career_cache.get(key)
    if cached is not None:
        ts, value = cached
        if now - ts < _CAREER_CACHE_TTL_SECONDS:
            return value
    # Cache miss or expired — recompute. Concurrent misses may
    # duplicate work; that's acceptable vs. introducing per-key
    # locks for what's a once-an-hour cold path per (stat, scope).
    result = _leaderboard_career(
        stat=stat, is_batter=is_batter, limit=limit,
        league=league, team=team,
        year_from=year_from, year_to=year_to,
    )
    _career_cache[key] = (now, result)
    return result


def _leaderboard_career(
    stat: str, is_batter: bool, limit: int,
    league: Optional[str], team: Optional[str],
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> dict:
    """Top career totals per player. Counting stats are SUM; rate stats
    derive from career sums of their components (career AVG =
    SUM(H)/SUM(AB), career ERA = SUM(ER)*9/SUM(IP), …). Players need at
    least 1000 PA (batters) or 500 IP (pitchers) to be eligible for
    rate-stat boards — keeps the lists meaningful without excluding
    short-career stars.

    `league` and `team` filter at the season-row level before
    aggregation: career-mode-with-league = "career stats among AL
    seasons only," which mirrors the season-mode filter semantics."""
    # Career mode only borrows the sort direction from the season
    # catalog — value formulas live in _career_value, and rate-stat
    # eligibility uses the career floor (1000 PA / 500 IP) rather than
    # the per-season catalog's PA / IP rule.
    catalog = _LEADERBOARD_BATTING if is_batter else _LEADERBOARD_PITCHING
    direction = catalog[stat][1]

    rate_stats = _CAREER_BATTING_RATE_STATS if is_batter else _CAREER_PITCHING_RATE_STATS
    is_rate = stat in rate_stats

    table = _PlayerSeason if is_batter else _PitcherSeason
    min_pa = _CAREER_MIN_PA
    min_ip = _CAREER_MIN_IP

    with connection.get_session() as db:
        # One aggregate row per player — every column we might need to
        # compute any supported career stat. Counting stats read off
        # the matching aggregate; rate stats build on multiple ones.
        if is_batter:
            agg_columns = [
                _PlayerSeason.player_id.label("player_id"),
                func.sum(_PlayerSeason.WAR).label("WAR"),
                func.sum(_PlayerSeason.HR).label("HR"),
                func.sum(_PlayerSeason.RBI).label("RBI"),
                func.sum(_PlayerSeason.H).label("H"),
                func.sum(_PlayerSeason.R).label("R"),
                func.sum(_PlayerSeason.SB).label("SB"),
                func.sum(_PlayerSeason.BB).label("BB"),
                func.sum(_PlayerSeason.SO).label("SO"),
                func.sum(_PlayerSeason.PA).label("PA"),
                func.sum(_PlayerSeason.AB).label("AB"),
                func.sum(_PlayerSeason.doubles).label("doubles"),
                func.sum(_PlayerSeason.triples).label("triples"),
                func.sum(_PlayerSeason.HBP).label("HBP"),
                func.sum(_PlayerSeason.SF).label("SF"),
                # max(year) gives us the player's most-recent season —
                # used downstream to surface a "current team" label.
                func.max(_PlayerSeason.year).label("last_year"),
            ]
        else:
            agg_columns = [
                _PitcherSeason.player_id.label("player_id"),
                func.sum(_PitcherSeason.WAR).label("WAR"),
                func.sum(_PitcherSeason.SO).label("SO"),
                func.sum(_PitcherSeason.W).label("W"),
                func.sum(_PitcherSeason.SV).label("SV"),
                func.sum(_PitcherSeason.IP).label("IP"),
                func.sum(_PitcherSeason.H).label("H"),
                func.sum(_PitcherSeason.BB).label("BB"),
                func.sum(_PitcherSeason.HR).label("HR"),
                func.sum(_PitcherSeason.CG).label("CG"),
                func.sum(_PitcherSeason.SHO).label("SHO"),
                func.sum(_PitcherSeason.ER).label("ER"),
                func.max(_PitcherSeason.year).label("last_year"),
            ]

        agg_q = db.query(*agg_columns).group_by(table.player_id)
        if league:
            agg_q = agg_q.filter(table.league == league)
        if team:
            clause = _team_filter_clause(table, team)
            if clause is not None:
                agg_q = agg_q.filter(clause)
        # year_from / year_to filter the source season rows *before*
        # the GROUP BY — so "career HR leaders 1990–1999" sums HR
        # only across each player's 1990s seasons, not their whole
        # career.
        if year_from is not None:
            agg_q = agg_q.filter(table.year >= year_from)
        if year_to is not None:
            agg_q = agg_q.filter(table.year <= year_to)
        agg_rows = agg_q.all()

        # Compute the requested stat per row, drop ineligible ones,
        # then sort + take top N. Done in Python so the rate formulas
        # stay obvious and we don't need to push them into SQL.
        valued = []
        for r in agg_rows:
            value = _career_value(r, stat, is_batter)
            if value is None:
                continue
            if is_rate:
                if is_batter:
                    if (r.PA or 0) < min_pa:
                        continue
                else:
                    if float(r.IP or 0) < min_ip:
                        continue
            valued.append((r, value))

        reverse = direction == "desc"
        valued.sort(key=lambda rv: rv[1], reverse=reverse)
        top = valued[:limit]

        # Build the leader rows + "most recent team" labels. We do one
        # extra query per top-25 row to grab the player's last-season
        # team — bounded fan-out, negligible cost vs. the aggregate
        # scan above.
        leaders = []
        for rank, (agg_row, value) in enumerate(top, start=1):
            player_row = _resolve_player_row(db, agg_row.player_id, is_batter)
            if player_row is None:
                continue
            last_team, last_team_league = _last_season_team(
                db, agg_row.player_id, agg_row.last_year, is_batter,
            )
            team_code = _resolve_team_code(last_team, last_team_league)
            leaders.append({
                "rank":   rank,
                "value":  value,
                "year":   None,
                "player": _player_payload(
                    player_row, db,
                    current_team=last_team,
                    team_code=team_code,
                    is_pitcher=not is_batter,
                ),
            })

    return {
        "stat":        stat,
        "mode":        "career",
        "year":        None,
        "year_from":   year_from,
        "year_to":     year_to,
        "player_type": "batter" if is_batter else "pitcher",
        "league":      league,
        "team":        team,
        "min_pa":      min_pa if is_batter else None,
        "min_ip":      min_ip if not is_batter else None,
        "leaders":     leaders,
    }


def _career_value(agg, stat: str, is_batter: bool) -> Optional[float]:
    """Pull or compute the requested career stat from one aggregated
    row. Returns None when the stat can't be computed (missing inputs,
    zero denominators, etc.) so the caller can drop the row."""
    if is_batter:
        if stat == "AVG":
            ab = agg.AB or 0
            return (agg.H or 0) / ab if ab else None
        if stat == "OBP":
            denom = (agg.AB or 0) + (agg.BB or 0) + (agg.HBP or 0) + (agg.SF or 0)
            num   = (agg.H or 0)  + (agg.BB or 0) + (agg.HBP or 0)
            return num / denom if denom else None
        if stat == "SLG":
            ab = agg.AB or 0
            if not ab:
                return None
            tb = ((agg.H or 0)
                  + (agg.doubles or 0)
                  + 2 * (agg.triples or 0)
                  + 3 * (agg.HR or 0))
            return tb / ab
        if stat == "OPS":
            obp = _career_value(agg, "OBP", True)
            slg = _career_value(agg, "SLG", True)
            return (obp + slg) if (obp is not None and slg is not None) else None
        # Counting stat: pull directly off the aggregated row. The
        # API stat label might not match the aggregate label (e.g.
        # "2B" vs "doubles") — translate here.
        key = {"2B": "doubles", "3B": "triples"}.get(stat, stat)
        raw = getattr(agg, key, None)
        return float(raw) if raw is not None else None

    # Pitcher
    if stat == "ERA":
        ip = float(agg.IP or 0)
        return (float(agg.ER or 0) * 9.0) / ip if ip else None
    if stat == "WHIP":
        ip = float(agg.IP or 0)
        return (float((agg.BB or 0) + (agg.H or 0))) / ip if ip else None
    raw = getattr(agg, stat, None)
    return float(raw) if raw is not None else None


def _resolve_player_row(db, player_id: int, is_batter: bool):
    """Pull the canonical player record by id. Two-way players (Ohtani-
    style) sit in both `players` and `pitchers`; fall back to the other
    table so the row is never anonymous regardless of which leaderboard
    surfaced them."""
    primary = crud.get_player if is_batter else crud.get_pitcher
    fallback = crud.get_pitcher if is_batter else crud.get_player
    row = primary(db, player_id)
    if row is None:
        row = fallback(db, player_id)
    return row


def _last_season_team(db, player_id: int, last_year: Optional[int], is_batter: bool):
    """Return (team, league) for the player's most-recent season in the
    same role table the aggregate came from. Both values may be None
    when the season row is missing — caller treats that as 'team
    unknown' and the iOS row falls back to omitting the team line."""
    if last_year is None:
        return None, None
    table = _PlayerSeason if is_batter else _PitcherSeason
    row = (
        db.query(table.team, table.league)
        .filter(table.player_id == player_id, table.year == last_year)
        .first()
    )
    if row is None:
        return None, None
    return row[0], row[1]


def _player_payload(player_row, db, *, current_team, team_code, is_pitcher) -> dict:
    """Common PlayerSearchResult-shaped block for leaderboard rows.
    Centralized here so the three mode helpers can't drift in what
    they ship to the client."""
    return {
        "player_id":       player_row.player_id,
        "name":            player_row.name,
        "bbref_id":        player_row.bbref_id,
        "mlb_debut":       player_row.mlb_debut,
        "mlb_last_season": player_row.mlb_last_season,
        "current_team":    current_team,
        "team_code":       team_code,
        "is_pitcher":      is_pitcher,
        **_bio_dict(player_row, db),
    }


# ---------------------------------------------------------------------------
# Pybaseball fetchers — used by nightly_update only (current season ingest).
# Historical seasons are loaded from Lahman; full-career pybaseball fetches
# were removed when Lahman became the primary source.
# ---------------------------------------------------------------------------

def _build_pitcher_season_entry(
    player_id: int,
    year: int,
    war_group: pd.DataFrame,
    bref_df: Optional[pd.DataFrame],
    mlb_api_stats: Optional[dict] = None,
) -> dict:
    """Build a pitcher_seasons row from one year of bwar_pitch +
    (optionally) bref + (current-season only) the MLB Stats API
    season splits.

    When `mlb_api_stats` is provided, its values take precedence
    over bref for the standard counting + rate stats (W, L, G, GS,
    SV, IP, H, R, ER, HR, BB, IBB, SO, HBP, BK, WP, ERA, WHIP).
    bref still drives WAR-adjacent computed fields (FIP, BABIP,
    K_per9, BB_per9, HR_per9) and remaining bref-only counters
    (CG, SHO, BAOpp, GF, SH, SF, GIDP, BFP). bwar_pitch always
    drives WAR / ERA_plus / WAA. The MLB Stats API path closes
    the ~24h lag bref has between game completion and its public
    stats table publishing.
    """
    # bwar_pitch slice may be empty when the caller has only MLB
    # Stats API data (no bref / no bwar yet). Guard the war-derived
    # section so the entry-build still proceeds with API + bref
    # values alone.
    has_war = war_group is not None and not war_group.empty
    ip_dec: float = 0.0
    # Start with year only — team / league get filled in iff a
    # source provides them. When no source does (bref + bwar both
    # empty, MLB API only carries stat fields), omitting them lets
    # the upsert preserve whatever team Phase 5 wrote previously
    # instead of NULLing it.
    entry: dict = {"year": year}

    if has_war:
        raw_team = str(war_group.iloc[-1]["team_ID"])
        ip_outs_total = float(war_group["IPouts"].sum()) if "IPouts" in war_group else 0.0
        ip_dec = ip_outs_total / 3 if ip_outs_total else 0.0

        # ERA_plus is rate-based; weight by IPouts to combine stints within a year.
        era_plus_vals = war_group["ERA_plus"].dropna()
        era_plus: Optional[float] = None
        if ip_outs_total > 0 and not era_plus_vals.empty:
            era_plus = float(
                (war_group["ERA_plus"].fillna(0) * war_group["IPouts"]).sum() / ip_outs_total
            )

        entry.update({
        "team":           _TEAM_DISPLAY.get(raw_team, raw_team),
        "league":         str(war_group.iloc[-1]["lg_ID"]),
        "G":              int(war_group["G"].sum()) if "G" in war_group else None,
        "GS":             int(war_group["GS"].sum()) if "GS" in war_group else None,
        "IP":             round(ip_outs_total / 3, 1) if ip_outs_total else None,
        "WAR":            round(float(war_group["WAR"].sum()), 2),
        "WAR_def":        round(float(war_group["WAR_def"].sum()), 2)
                          if "WAR_def" in war_group else None,
        "WAA":            round(float(war_group["WAA"].sum()), 2),
        "ERA_plus":       round(era_plus, 1) if era_plus is not None else None,
        "runs_above_avg": round(float(war_group["runs_above_avg"].sum()), 2),
        "runs_above_rep": round(float(war_group["runs_above_rep"].sum()), 2),
        })

    if bref_df is not None and not bref_df.empty:
        # pitching_stats_bref stores mlbID as STRING.
        player_bref = bref_df[bref_df["mlbID"] == str(player_id)]
        if not player_bref.empty:
            br = player_bref.iloc[0]

            # Aggregate counting stats if a player had multiple stint rows.
            def _sum(col):
                v = player_bref[col]
                v = pd.to_numeric(v, errors="coerce").fillna(0)
                return float(v.sum()) if not v.empty else 0.0

            so  = _sum("SO")
            bb  = _sum("BB")
            hr  = _sum("HR")
            hbp = _sum("HBP")
            bf  = _sum("BF") if "BF" in br.index else 0.0
            h   = _sum("H")
            ip_dec = _ip_to_decimal(br["IP"]) if pd.notna(br["IP"]) else ip_dec

            babip_denom = bf - so - hr - bb
            babip = round((h - hr) / babip_denom, 3) if babip_denom > 0 else None

            k_per9   = round(so * 9 / ip_dec, 2) if ip_dec else None
            bb_per9  = round(bb * 9 / ip_dec, 2) if ip_dec else None
            hr_per9  = round(hr * 9 / ip_dec, 2) if ip_dec else None

            entry["team"] = str(br["Tm"])
            entry.update({
                # W/L: coerce NaN → 0 at write time so we don't store
                # nulls for relievers / fresh callups whose bref row
                # has the column but no value yet. Lahman always
                # writes 0; this lines nightly up with that.
                "W":     int(_safe(br["W"]) or 0),
                "L":     int(_safe(br["L"]) or 0),
                "G":     _safe(br["G"]),
                "GS":    _safe(br["GS"]),
                # bref ships IP as a string in baseball notation
                # ("10.2" = 10 ⅔). _safe() would store the raw float
                # 10.2 in the DB; convert via _ip_to_decimal to the
                # true-decimal value (10.667) the rest of the app
                # assumes. Without this, the iOS overlay adds
                # today's true-decimal IP onto a baseball-notation
                # season total and the running IP drifts.
                "IP":    _ip_to_decimal(br["IP"]) if pd.notna(br["IP"]) else None,
                "SO":    int(so),
                "BB":    int(bb),
                "HR":    int(hr),
                "ERA":   _safe(br["ERA"]),
                "WHIP":  _safe(br["WHIP"]),
                "FIP":   _fip(hr, bb, hbp, so, ip_dec),
                "BABIP": babip,
                "K_per9":  k_per9,
                "BB_per9": bb_per9,
                "HR_per9": hr_per9,
                # Extended counting stats from bref. Most are present; SH/SF/
                # GIDP/SHO can be missing in older years — _safe_col handles it.
                "CG":    _safe_col(br, "CG"),
                "SHO":   _safe_col(br, "SHO"),
                "SV":    _safe_col(br, "SV"),
                "H":     int(h),
                "ER":    int(_sum("ER")) if "ER" in br.index else None,
                "R":     int(_sum("R"))  if "R"  in br.index else None,
                "BAOpp": _safe_col(br, "BAopp") or _safe_col(br, "BAOpp"),
                "IBB":   int(_sum("IBB")) if "IBB" in br.index else None,
                "WP":    int(_sum("WP"))  if "WP"  in br.index else None,
                "HBP":   int(hbp),
                "BK":    int(_sum("BK"))  if "BK"  in br.index else None,
                "BFP":   int(bf) if bf else None,
                "GF":    int(_sum("GF"))  if "GF"  in br.index else None,
                "SH":    _safe_col(br, "SH"),
                "SF":    _safe_col(br, "SF"),
                "GIDP":  _safe_col(br, "GIDP") if "GIDP" in br.index else _safe_col(br, "GDP"),
            })

    # MLB Stats API override — current-season only. Overrides the
    # standard counting + rate stats with the canonical values from
    # `/people/{id}/stats?group=pitching`. Anything not in this
    # dict stays as whatever bref / bwar provided above (FIP,
    # K_per9, BB_per9, HR_per9, BABIP, CG, SHO, BAOpp, GF, SH, SF,
    # GIDP, BFP). Iterate so None values from the API don't clobber
    # a populated bref value (e.g. holds — never set by bref).
    if mlb_api_stats:
        for key in ("G", "GS", "W", "L", "SV", "IP",
                    "H", "R", "ER", "HR",
                    "BB", "IBB", "SO", "HBP", "BK", "WP",
                    "ERA", "WHIP"):
            value = mlb_api_stats.get(key)
            if value is not None:
                entry[key] = value
        # Recompute the K/9, BB/9, HR/9, FIP, BABIP rates from the
        # MLB API counts when IP is non-zero — keeps these in sync
        # with the new (authoritative) IP / SO / BB / HR values
        # instead of leaving bref's stale derivations in place.
        ip_dec = mlb_api_stats.get("IP") or 0.0
        if ip_dec:
            so = mlb_api_stats.get("SO") or 0
            bb = mlb_api_stats.get("BB") or 0
            hr = mlb_api_stats.get("HR") or 0
            hbp = mlb_api_stats.get("HBP") or 0
            entry["K_per9"]  = round(so * 9 / ip_dec, 2)
            entry["BB_per9"] = round(bb * 9 / ip_dec, 2)
            entry["HR_per9"] = round(hr * 9 / ip_dec, 2)
            entry["FIP"]     = _fip(hr, bb, hbp, so, ip_dec)

    return entry


def init_db() -> None:
    """Create database tables if they don't exist. Called once on startup."""
    connection.init_db()
