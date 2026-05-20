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
import unicodedata
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
    # `bdl_id` is the FK iOS uses to filter BDL `/stats?game_ids[]=`
    # responses to a single player. The column is stamped per-side
    # (so `_BIO_COLUMNS` skips it — those are bio fields shared
    # across players/pitchers); read it directly off the row here.
    out["bdl_id"] = getattr(row, "bdl_id", None)
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


def get_player_by_bdl_id(bdl_id: int) -> Optional[dict]:
    """Look up one player by their BallDontLie id. BDL ids are the
    foreign key on every BDL game / stat / play / PA payload, so
    the iOS Scores tab needs this reverse-lookup whenever it has a
    BDL player id and wants to navigate to a profile (which is
    keyed on our MLBAM `player_id`).

    Two-way players (Ohtani) have the same bdl_id stamped on both
    bio tables — return the batter row first, matching
    `get_player_by_id`'s precedence."""
    if not connection.db_available() or bdl_id is None:
        return None
    from database.models import Pitcher as _Pitcher
    from database.models import Player as _Player

    with connection.get_session() as db:
        for model, is_pitcher in [(_Player, False), (_Pitcher, True)]:
            row = (
                db.query(model)
                .filter(model.bdl_id == bdl_id)
                .first()
            )
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


# Shared HTTP timeout for the BallDontLie helper. (The old MLB
# Stats API base URL + helper + team-id map were removed in the
# App-Store-compliance cleanup — BDL is now the only external
# stats data source.)
_BDL_HTTP_TIMEOUT = 30


# ---------------------------------------------------------------------------
# BallDontLie API — migration target. Independent ID space (BDL has its
# own integer ids for players/teams/games — they do NOT match MLBAM ids),
# so a one-shot bootstrap stamps the mapping onto our `bdl_id` columns
# before any production read-path traffic moves over.
# ---------------------------------------------------------------------------

_BDL_API_BASE = "https://api.balldontlie.io/mlb/v1"
# Bare rate-limit floor BDL enforces (5 req/sec on the GOAT tier).
# Sleeping slightly over 1/5s between requests stays comfortably under
# the bucket and dodges the burst-counter 429s we hit during testing.
_BDL_RATE_LIMIT_SLEEP = 0.22

# Lahman code → BallDontLie team id. Full 30-team mapping verified
# via the `/admin/bdl-teams` discovery endpoint. BDL's ids are stable
# across seasons (one id per franchise, not per season-row), so this
# constant is the authoritative source — the matching `team_seasons.
# bdl_id` column stamped by `fetch_bdl_teams` is the same value, just
# kept on the DB side for SQL joins.
_BDL_TEAM_ID_MAP: dict[str, int] = {
    "ARI":  1, "ATL":  2, "BAL":  3, "BOS":  4, "CHN":  5,
    "CHA":  6, "CIN":  7, "CLE":  8, "COL":  9, "DET": 10,
    "HOU": 11, "KCA": 12, "LAA": 13, "LAN": 14, "MIA": 15,
    "MIL": 16, "MIN": 17, "NYN": 18, "NYA": 19, "ATH": 20,
    "PHI": 21, "PIT": 22, "SDN": 23, "SFN": 24, "SEA": 25,
    "SLN": 26, "TBA": 27, "TEX": 28, "TOR": 29, "WAS": 30,
}

# Inverse mapping for the read direction — BDL ships team ids on
# every game / stat / play / standings payload, and the iOS app +
# our DB key off Lahman codes. Generated from `_BDL_TEAM_ID_MAP` so
# the two can't drift; if a future BDL team is added (expansion,
# rebrand) only the forward dict needs editing.
_BDL_TO_LAHMAN_TEAM_MAP: dict[int, str] = {
    bdl_id: lahman for lahman, bdl_id in _BDL_TEAM_ID_MAP.items()
}


def _get_bdl_key() -> str:
    """Read the BDL API key from env. Raises a clear error so callers
    fail fast at Railway boot or per-request rather than silently
    sending unauthenticated requests (which BDL 401s on every endpoint).
    """
    key = os.environ.get("BDL_KEY")
    if not key:
        raise RuntimeError(
            "BDL_KEY environment variable is not set — BallDontLie "
            "endpoints require the GOAT-tier API key. Set BDL_KEY on "
            "Railway (and locally in `.env`) before calling any "
            "`/admin/bdl-*` or `/admin/build-bdl-*` endpoint."
        )
    return key


def _bdl_get_json(path: str, params: Optional[dict] = None) -> dict:
    """GET https://api.balldontlie.io/mlb/v1/{path}?... and return
    parsed JSON. Uses urllib (stdlib only, no `requests`) and adds
    the BDL_KEY Authorization header. Param-array shape (`foo[]`)
    is preserved through `doseq=True` so callers can pass list
    values directly.

    Note: BDL's published OpenAPI lies about array params on the
    /season_stats, /plays, and /plate_appearances endpoints (live
    API rejects `seasons[]=` and `game_ids[]=`; expects singular
    `season=` / `game_id=`). Callers must use the singular form on
    those routes — this helper doesn't disguise the difference.
    """
    qs  = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{_BDL_API_BASE}/{path.lstrip('/')}"
    if qs:
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": _get_bdl_key(),
        "User-Agent":    "baseball-stats-app/1.0",
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req, timeout=_BDL_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_bdl_id_for_player(db, player_id: int, *, is_pitcher: bool) -> Optional[int]:
    """Look up `bdl_id` for a player in our DB by MLBAM player_id.
    The mapping is stamped per-side by `build_bdl_player_mapping`,
    so two-way players have an entry in both tables (Ohtani: same
    bdl_id on both sides). Returns None when the row exists but
    the mapping wasn't populated yet — caller should fall back to
    the MLB Stats API helper until the bootstrap finishes."""
    if is_pitcher:
        row = crud.get_pitcher(db, player_id)
    else:
        row = crud.get_player(db, player_id)
    return getattr(row, "bdl_id", None) if row else None


def _fetch_bdl_batch_stats(
    bdl_ids: list[int],
    year: int,
    batch_size: int = 50,
) -> dict[int, dict]:
    """Bulk-fetch season_stats for many players in one go. Splits
    `bdl_ids` into chunks of `batch_size` and issues one HTTP
    request per chunk (`player_ids[]=A&player_ids[]=B&...`),
    paginating each chunk's response if BDL ships a next_cursor.
    Returns `{bdl_id: raw_bdl_row}` — missing players (no current-
    season activity) are simply absent from the dict.

    Rate-limit: one `_BDL_RATE_LIMIT_SLEEP` between chunks instead
    of between players. A nightly walk of ~2,200 players collapses
    from ~8 minutes of sleep to ~10 seconds.

    Note on the API: BDL's live endpoint accepts `player_ids[]` as
    a repeated query param plus a singular `season=` (despite the
    OpenAPI spec advertising `seasons[]`)."""
    out: dict[int, dict] = {}
    if not bdl_ids:
        return out

    # Deduplicate while preserving order; the same bdl_id appearing
    # twice would waste a slot in the chunk.
    seen: set[int] = set()
    unique_ids: list[int] = []
    for b in bdl_ids:
        if b is None or b in seen:
            continue
        seen.add(b)
        unique_ids.append(b)

    for start in range(0, len(unique_ids), batch_size):
        chunk = unique_ids[start:start + batch_size]
        cursor: Optional[str] = None
        while True:
            params: dict = {
                "player_ids[]": chunk,    # urlencoded with doseq=True
                "season":       year,
                "per_page":     100,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                data = _bdl_get_json("season_stats", params)
            except Exception as exc:
                log.warning(
                    "BDL batch fetch failed for chunk starting at "
                    "%d (size %d): %s", start, len(chunk), exc,
                )
                break
            for row in data.get("data") or []:
                pid = (row.get("player") or {}).get("id")
                if pid is None or pid in out:
                    continue
                try:
                    out[int(pid)] = row
                except (TypeError, ValueError):
                    continue
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        # Sleep between chunks, not between players.
        if start + batch_size < len(unique_ids):
            time.sleep(_BDL_RATE_LIMIT_SLEEP)

    return out


def _parse_bdl_batter_row(s: dict) -> dict:
    """Normalize one BDL season_stats row to the `player_seasons`
    field shape. Keys BDL doesn't carry (PA, CS, IBB, HBP, SF, SH,
    GIDP) are omitted so the upsert path doesn't NULL them out.
    TB is recomputed locally from H/2B/3B/HR for consistency with
    the rest of the pipeline."""
    h  = _to_int(s.get("batting_h"))     or 0
    d2 = _to_int(s.get("batting_2b"))    or 0
    d3 = _to_int(s.get("batting_3b"))    or 0
    hr = _to_int(s.get("batting_hr"))    or 0
    return {
        "G":       _to_int(s.get("batting_gp")),
        "AB":      _to_int(s.get("batting_ab")),
        "R":       _to_int(s.get("batting_r")),
        "H":       _to_int(s.get("batting_h")),
        "doubles": _to_int(s.get("batting_2b")),
        "triples": _to_int(s.get("batting_3b")),
        "HR":      _to_int(s.get("batting_hr")),
        "RBI":     _to_int(s.get("batting_rbi")),
        "BB":      _to_int(s.get("batting_bb")),
        "SO":      _to_int(s.get("batting_so")),
        "SB":      _to_int(s.get("batting_sb")),
        "TB":      h + d2 + 2 * d3 + 3 * hr,
        "BA":      _safe_rate(s.get("batting_avg")),
        "OBP":     _safe_rate(s.get("batting_obp")),
        "SLG":     _safe_rate(s.get("batting_slg")),
        "OPS":     _safe_rate(s.get("batting_ops")),
        "WAR":     _safe_rate(s.get("batting_war")),
    }


def _parse_bdl_pitcher_row(s: dict) -> dict:
    """Normalize one BDL season_stats row to the `pitcher_seasons`
    field shape. ERA / WHIP / K/9 are rounded to 2 dp at this
    boundary — BDL ships those with 4-decimal precision. FIP is
    NOT taken from BDL (their `fielding_fip` field is a fielding
    metric, not pitching FIP) — `_build_pitcher_season_entry`
    derives FIP from the HR/BB/SO components instead."""
    return {
        "G":      _to_int(s.get("pitching_gp")),
        "GS":     _to_int(s.get("pitching_gs")),
        "W":      _to_int(s.get("pitching_w")),
        "L":      _to_int(s.get("pitching_l")),
        "SV":     _to_int(s.get("pitching_sv")),
        "HLD":    _to_int(s.get("pitching_hld")),
        "IP":     _safe_rate(s.get("pitching_ip")),
        "H":      _to_int(s.get("pitching_h")),
        "ER":     _to_int(s.get("pitching_er")),
        "HR":     _to_int(s.get("pitching_hr")),
        "BB":     _to_int(s.get("pitching_bb")),
        "SO":     _to_int(s.get("pitching_k")),
        "ERA":    _round_or_none(_safe_rate(s.get("pitching_era")), 2),
        "WHIP":   _round_or_none(_safe_rate(s.get("pitching_whip")), 2),
        "K_per9": _round_or_none(_safe_rate(s.get("pitching_k_per_9")), 2),
        "WAR":    _safe_rate(s.get("pitching_war")),
    }


def _round_or_none(v: Optional[float], digits: int) -> Optional[float]:
    """Round a numeric value to `digits` decimal places. None passes
    through so callers can chain `_round_or_none(_safe_rate(...), 2)`
    without losing the missing-data sentinel."""
    return round(v, digits) if v is not None else None


def fetch_bdl_player_bio(bdl_id: int) -> Optional[dict]:
    """`/players/{bdl_id}` → bio dict in the `crud.save_player` /
    `crud.save_pitcher` shape, EXCEPT for `player_id`. BDL ids are
    not MLBAM ids — the caller is responsible for resolving the
    MLBAM PK separately (typically by name search against MLB
    Stats API) before insert.

    Returns None on 404 / network failure. The returned dict
    carries `bdl_id` so the caller can stamp it on the bio row
    in the same insert.
    """
    if bdl_id is None:
        return None
    try:
        data = _bdl_get_json(f"players/{bdl_id}", {})
    except Exception:
        return None
    # BDL's `/players/{id}` returns the player object under `data`,
    # not in a list (unlike the listing endpoints).
    p = data.get("data") if isinstance(data.get("data"), dict) else None
    if p is None:
        # Older docs imply the object may sometimes come back at the
        # top level. Tolerate that shape too.
        if isinstance(data, dict) and data.get("id"):
            p = data
        else:
            return None
    return _parse_bdl_player_bio(p)


def _parse_bdl_player_bio(p: dict) -> dict:
    """Normalize a BDL player dict (from either `/players/{id}` or
    `/players/active`) into the bio-row shape `crud.save_player` /
    `crud.save_pitcher` expects. `player_id` is NOT included —
    caller fills it in once MLBAM is resolved.

    The returned dict carries `bdl_id` (so callers can stamp the
    column in the same insert) plus `_team_code` (Lahman code for
    the player's current team — caller uses it to write the
    current-year season-row team, then strips before insert)."""
    name = p.get("full_name") or f"Player {p.get('id')}"
    bats, throws = _parse_bdl_bats_throws(p.get("bats_throws"))
    height = _parse_bdl_height(p.get("height"))
    weight = _parse_bdl_weight(p.get("weight"))
    by, bm, bd = _parse_bdl_dob(p.get("dob"))
    city, state, country = _parse_bdl_birth_place(p.get("birth_place"))
    team_bdl_id = (p.get("team") or {}).get("id")
    team_code = _BDL_TO_LAHMAN_TEAM_MAP.get(team_bdl_id) if team_bdl_id else None

    return {
        "name":            name,
        "bbref_id":        None,
        "mlb_debut":       _to_int(p.get("debut_year")),
        "mlb_last_season": None,
        "position":        p.get("position"),
        "bats":            bats,
        "throws":          throws,
        "height":          height,
        "weight":          weight,
        "birth_year":      by,
        "birth_month":     bm,
        "birth_day":       bd,
        "birth_city":      city,
        "birth_state":     state,
        "birth_country":   country,
        # BDL only ships year for debut; the column historically
        # stored a full ISO date from MLB Stats API. Leave nil and
        # rely on `mlb_debut` for the integer year.
        "debut":           None,
        "final_game":      None,
        "bdl_id":          p.get("id"),
        "_team_code":      team_code,
    }


def _parse_bdl_bats_throws(s: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """\"R/R\" → (\"R\", \"R\"). Tolerant of whitespace and missing
    halves; returns (None, None) on unparseable input."""
    if not s or "/" not in s:
        return None, None
    parts = s.split("/", 1)
    a = parts[0].strip()
    b = parts[1].strip() if len(parts) > 1 else ""
    return (a or None, b or None)


def _parse_bdl_height(s: Optional[str]) -> Optional[int]:
    """`"6' 1\""` → 73 inches. None on unparseable."""
    if not s:
        return None
    cleaned = s.replace('"', "").strip()
    if "'" not in cleaned:
        return None
    feet_s, inches_s = cleaned.split("'", 1)
    try:
        feet = int(feet_s.strip())
        rest = inches_s.strip()
        inches = int(rest) if rest else 0
        return feet * 12 + inches
    except ValueError:
        return None


def _parse_bdl_weight(s: Optional[str]) -> Optional[int]:
    """`"235 lbs"` → 235. None on unparseable."""
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _parse_bdl_dob(s: Optional[str]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """`"08/07/91"` → (1991, 8, 7). BDL ships MM/DD/YY with a
    2-digit year; the Y2K window split is "YY > 25 → 19xx, else
    20xx" so active-player DOBs (peak window 1980-2005) all land
    in the right century."""
    if not s or "/" not in s:
        return None, None, None
    parts = s.split("/")
    if len(parts) != 3:
        return None, None, None
    try:
        m  = int(parts[0])
        d  = int(parts[1])
        yy = int(parts[2])
        year = (1900 + yy) if yy > 25 else (2000 + yy)
        return year, m, d
    except ValueError:
        return None, None, None


def _parse_bdl_birth_place(
    s: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """`"Vineland, NJ"` → (\"Vineland\", \"NJ\", \"USA\").
       `"Santo Domingo, Dominican Republic"` → (\"Santo Domingo\", None, \"Dominican Republic\").
       `"San Francisco de Macoris, Distrito Nacional, Dominican Republic"` → all three.
    Heuristic on the 2-token case: a 2-letter all-caps second
    token is a US state code; otherwise treat it as a country."""
    if not s:
        return None, None, None
    parts = [t.strip() for t in s.split(",") if t.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        city, second = parts
        if len(second) == 2 and second.isupper() and second.isalpha():
            return city, second, "USA"
        return city, None, second
    if len(parts) == 1:
        return parts[0], None, None
    return None, None, None


def _stamp_bdl_id_by_name(
    db, full_name: str, bdl_id: int, is_pitcher_hint: bool,
) -> Optional[str]:
    """Find an existing DB row whose normalized name matches
    `full_name` AND whose `bdl_id` is still null, and stamp the
    given `bdl_id` on it. Returns "pitcher" / "batter" identifying
    which side was stamped, or None when no candidate was found.

    Uses the same normalization (`_normalize_bdl_name`) as the
    bootstrap mapping endpoint — strips accents, periods, and
    suffix tokens. Prefers the position-side hinted by the BDL
    entry (pitchers → `pitchers` table first); falls back to the
    other side when the primary side has no candidate."""
    from database.models import Pitcher as _Pitcher
    from database.models import Player as _Player

    target = _normalize_bdl_name(full_name)
    if not target:
        return None
    tokens = full_name.strip().split() if full_name else []
    last = tokens[-1] if tokens else ""
    if not last:
        return None

    # Position-side priority: hint-first, then the other side.
    sides = [(_Pitcher, "pitcher"), (_Player, "batter")]
    if not is_pitcher_hint:
        sides.reverse()

    for model, side_label in sides:
        candidates = (
            db.query(model)
            .filter(model.bdl_id.is_(None))
            .filter(model.name.ilike(f"%{last}%"))
            .all()
        )
        for row in candidates:
            if _normalize_bdl_name(row.name) == target:
                row.bdl_id = bdl_id
                return side_label
    return None


def _fetch_bdl_batter_stats(bdl_id: int, year: int) -> Optional[dict]:
    """One-player convenience wrapper around `_fetch_bdl_batch_stats`
    + `_parse_bdl_batter_row`. Preserved for ad-hoc callers that
    only need a single player — the nightly pipeline uses the
    batch path directly to amortize the HTTP overhead."""
    if bdl_id is None:
        return None
    batch = _fetch_bdl_batch_stats([bdl_id], year)
    row = batch.get(int(bdl_id))
    return _parse_bdl_batter_row(row) if row else None


def _fetch_bdl_pitcher_stats(bdl_id: int, year: int) -> Optional[dict]:
    """Pitcher counterpart to `_fetch_bdl_batter_stats`."""
    if bdl_id is None:
        return None
    batch = _fetch_bdl_batch_stats([bdl_id], year)
    row = batch.get(int(bdl_id))
    return _parse_bdl_pitcher_row(row) if row else None


def fetch_bdl_teams() -> dict:
    """Walk BDL's /teams once and return the full list. Output shape
    is intended for human inspection — the caller hand-pastes the
    rows into `_BDL_TEAM_ID_MAP` after verifying the mapping against
    our Lahman codes. Also stamps `bdl_id` on every current-year row
    in `team_seasons` so the migration code path can resolve via DB
    lookup once the constant is filled in.

    The endpoint is paginated (default 25 per page); since there are
    only 30 teams, requesting per_page=50 gets them in one shot.
    """
    data = _bdl_get_json("teams", {"per_page": 50})
    teams = data.get("data") or []
    # Build a display list and (when possible) a Lahman-code suggestion.
    # Lahman uses bbref-style codes that differ from BDL's abbreviation
    # for ~6 franchises (BDL uses NYY/CHC/LAD/SDP/SFG/STL etc.; our DB
    # uses NYA/CHN/LAN/SDN/SFN/SLN). The reverse map below covers the
    # ones we know about; unknown abbreviations are echoed back so the
    # human reviewer can decide.
    _BDL_ABBR_TO_LAHMAN: dict[str, str] = {
        "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BOS": "BOS",
        "CHC": "CHN", "CHW": "CHA", "CIN": "CIN", "CLE": "CLE",
        "COL": "COL", "DET": "DET", "HOU": "HOU", "KCR": "KCA",
        "LAA": "LAA", "LAD": "LAN", "MIA": "MIA", "MIL": "MIL",
        "MIN": "MIN", "NYM": "NYN", "NYY": "NYA", "OAK": "ATH",
        "ATH": "ATH", "PHI": "PHI", "PIT": "PIT", "SDP": "SDN",
        "SEA": "SEA", "SFG": "SFN", "STL": "SLN", "TBR": "TBA",
        "TEX": "TEX", "TOR": "TOR", "WSN": "WAS", "WSH": "WAS",
    }

    out: list[dict] = []
    for t in teams:
        abbr = t.get("abbreviation") or ""
        out.append({
            "bdl_id":         t.get("id"),
            "abbreviation":   abbr,
            "name":           t.get("name"),
            "display_name":   t.get("display_name"),
            "league":         t.get("league"),
            "division":       t.get("division"),
            "lahman_suggested": _BDL_ABBR_TO_LAHMAN.get(abbr.upper()),
        })
    out.sort(key=lambda x: x.get("abbreviation") or "")

    # Stamp current-year team_seasons rows so the runtime lookup also
    # works without waiting on a `_BDL_TEAM_ID_MAP` constant update.
    stamped = 0
    if connection.db_available():
        from database.models import TeamSeason as _TeamSeason
        year = _current_year()
        with connection.get_session() as db:
            for row in out:
                lahman = row.get("lahman_suggested")
                bdl_id = row.get("bdl_id")
                if not lahman or bdl_id is None:
                    continue
                # Stamp every season's row for this franchise — the
                # BDL team id is stable across years, so we don't
                # need to scope by `year`.
                updated = (
                    db.query(_TeamSeason)
                    .filter(_TeamSeason.team_id == lahman)
                    .update({_TeamSeason.bdl_id: bdl_id},
                            synchronize_session=False)
                )
                stamped += updated or 0
            db.commit()
        _ = year

    return {
        "count":    len(out),
        "teams":    out,
        "stamped":  stamped,
        "hint": (
            "Paste the (lahman_suggested → bdl_id) pairs into "
            "_BDL_TEAM_ID_MAP in data_service.py. Rows with "
            "`lahman_suggested=null` need manual mapping (rebrand or "
            "abbreviation BDL ships that we haven't seen before)."
        ),
    }


def build_bdl_player_mapping(since_year: int = 2010,
                             limit: Optional[int] = None) -> dict:
    """One-shot bootstrap: walk every player/pitcher in our DB whose
    `bdl_id` is still NULL (and whose career touches `since_year` or
    later), search BDL for their name, and stamp the matched BDL id.

    Match rule: exact full-name match (case-insensitive), with a
    position-side filter — batters match against BDL rows whose
    position is NOT pitcher-type (P / SP / RP / CL); pitchers match
    against rows whose position IS pitcher-type. Resolves the
    common "Will Smith catcher vs. Will Smith reliever" two-row case.

    Ambiguous matches (multiple BDL rows pass the side filter) are
    logged and skipped — the response includes them under
    `ambiguous` so a human can hand-resolve. Unmatched rows (zero
    BDL hits) appear under `unmatched`.

    Rate-limited at 1 request every `_BDL_RATE_LIMIT_SLEEP` seconds
    (≈4.5 req/sec, just below BDL's 5/sec ceiling). `limit` caps the
    number of DB rows processed per invocation so a single Railway
    request stays under the 5-minute timeout — caller can re-invoke
    to resume (the WHERE clause filters out already-stamped rows).
    """
    if not connection.db_available():
        return {"status": "no_db"}

    # Pre-flight the env var so we fail fast with a clear error
    # before scanning thousands of DB rows.
    _get_bdl_key()

    from database.models import Pitcher as _Pitcher
    from database.models import Player as _Player

    counts = {
        "batters_matched":   0,
        "pitchers_matched":  0,
        "batters_unmatched": 0,
        "pitchers_unmatched": 0,
        "batters_ambiguous": 0,
        "pitchers_ambiguous": 0,
        "bdl_lookups":       0,
    }
    ambiguous: list[dict] = []
    unmatched: list[dict] = []
    processed = 0

    with connection.get_session() as db:
        batter_rows = (
            db.query(_Player)
            .filter(_Player.bdl_id.is_(None))
            .filter(_Player.mlb_debut.isnot(None))
            .filter(_Player.mlb_debut >= since_year)
            .order_by(_Player.player_id)
            .all()
        )
        pitcher_rows = (
            db.query(_Pitcher)
            .filter(_Pitcher.bdl_id.is_(None))
            .filter(_Pitcher.mlb_debut.isnot(None))
            .filter(_Pitcher.mlb_debut >= since_year)
            .order_by(_Pitcher.player_id)
            .all()
        )

        for row, side in [(r, "batter")  for r in batter_rows] + \
                         [(r, "pitcher") for r in pitcher_rows]:
            if limit is not None and processed >= limit:
                break
            processed += 1

            bdl_id, status, candidates = _bdl_match_one_player(
                full_name=row.name,
                side=side,
                mlb_debut=row.mlb_debut,
            )
            counts["bdl_lookups"] += 1

            if status == "matched":
                row.bdl_id = bdl_id
                counts[f"{'batters' if side == 'batter' else 'pitchers'}_matched"] += 1
            elif status == "ambiguous":
                counts[f"{'batters' if side == 'batter' else 'pitchers'}_ambiguous"] += 1
                ambiguous.append({
                    "player_id":  row.player_id,
                    "name":       row.name,
                    "side":       side,
                    "candidates": candidates,
                })
            else:  # unmatched
                counts[f"{'batters' if side == 'batter' else 'pitchers'}_unmatched"] += 1
                if len(unmatched) < 100:
                    unmatched.append({
                        "player_id": row.player_id,
                        "name":      row.name,
                        "side":      side,
                    })

            time.sleep(_BDL_RATE_LIMIT_SLEEP)

        db.commit()

    return {
        "status":     "ok",
        "since_year": since_year,
        "processed":  processed,
        "remaining_batters_estimate":  max(0, len(batter_rows)  - processed),
        "remaining_pitchers_estimate": max(0, len(pitcher_rows) - max(0, processed - len(batter_rows))),
        "counts":     counts,
        # Cap the response payload — we only need a sample to debug
        # the run; the full unmatched list would otherwise balloon
        # for first-time runs across thousands of historical players.
        "ambiguous":  ambiguous[:200],
        "unmatched":  unmatched,
    }


def get_bdl_mapping_status(since_year: int = 2002) -> dict:
    """Reporting view of `players.bdl_id` / `pitchers.bdl_id`
    coverage. Used between bootstrap calls to see how much of the
    mapping is done and whether the matched rows look right.

    Sections:
      `coverage`  — total / mapped / unmapped per table for rows
                    whose `mlb_debut >= since_year`, plus a percent.
      `spot_checks` — hand-picked active stars with their expected
                      BDL ids when known. Compares stamped vs
                      expected and flags mismatches.
      `recent_sample` — 10 random-ish rows from debut >= 2020 so
                        the operator can eyeball that real names
                        line up with sane BDL ids.
    """
    if not connection.db_available():
        return {"status": "no_db"}

    from database.models import Pitcher as _Pitcher
    from database.models import Player as _Player

    coverage: dict[str, dict] = {}
    spot_checks: list[dict] = []
    recent_sample: list[dict] = []

    # Spot-check targets — flagging an expected BDL id where we know
    # it (Trout was verified by the migration audit). The rest are
    # left as "expected: null" so we can fill them in after the
    # mapping run confirms them.
    targets: list[dict] = [
        {"mlbam_id": 545361, "name": "Mike Trout",            "expected_bdl_id": 3403},
        {"mlbam_id": 660271, "name": "Shohei Ohtani",         "expected_bdl_id": None},
        {"mlbam_id": 518692, "name": "Freddie Freeman",       "expected_bdl_id": None},
        {"mlbam_id": 665489, "name": "Vladimir Guerrero Jr.", "expected_bdl_id": None},
        {"mlbam_id": 677951, "name": "Bobby Witt Jr.",        "expected_bdl_id": None},
    ]

    with connection.get_session() as db:
        for model, label in [(_Player, "players"), (_Pitcher, "pitchers")]:
            total = (
                db.query(func.count(model.player_id))
                .filter(model.mlb_debut.isnot(None))
                .filter(model.mlb_debut >= since_year)
                .scalar() or 0
            )
            mapped = (
                db.query(func.count(model.player_id))
                .filter(model.mlb_debut.isnot(None))
                .filter(model.mlb_debut >= since_year)
                .filter(model.bdl_id.isnot(None))
                .scalar() or 0
            )
            coverage[label] = {
                "total":    total,
                "mapped":   mapped,
                "unmapped": total - mapped,
                "match_pct": (
                    round(100.0 * mapped / total, 1) if total else None
                ),
            }

        # Spot-checks read whichever table the player lives in — try
        # batter side first, fall back to pitcher side. Two-way
        # players (Ohtani) hit both; report both bdl_ids so a
        # mismatch between sides is visible.
        for t in targets:
            pid = t["mlbam_id"]
            bat = db.get(_Player,  pid)
            pit = db.get(_Pitcher, pid)
            row = {
                "mlbam_id":         pid,
                "name":             t["name"],
                "expected_bdl_id":  t["expected_bdl_id"],
                "in_players":       bat is not None,
                "in_pitchers":      pit is not None,
                "batter_bdl_id":    getattr(bat, "bdl_id", None) if bat else None,
                "pitcher_bdl_id":   getattr(pit, "bdl_id", None) if pit else None,
                "db_name_batter":   bat.name if bat else None,
                "db_name_pitcher":  pit.name if pit else None,
            }
            actual = row["batter_bdl_id"] or row["pitcher_bdl_id"]
            row["status"] = (
                "missing_from_db" if bat is None and pit is None else
                "unmapped"        if actual is None else
                "match"           if (t["expected_bdl_id"] is None
                                       or actual == t["expected_bdl_id"]) else
                "mismatch"
            )
            spot_checks.append(row)

        # Recent sample — 10 modern (debut >= 2020) batters in
        # ascending player_id so the response is deterministic
        # across calls. Picking ascending rather than random keeps
        # the diff between successive status calls readable.
        sample_rows = (
            db.query(_Player.player_id, _Player.name,
                     _Player.bdl_id, _Player.mlb_debut)
            .filter(_Player.mlb_debut.isnot(None))
            .filter(_Player.mlb_debut >= 2020)
            .order_by(_Player.player_id)
            .limit(10)
            .all()
        )
        for r in sample_rows:
            recent_sample.append({
                "mlbam_id":  r.player_id,
                "name":      r.name,
                "bdl_id":    r.bdl_id,
                "mlb_debut": r.mlb_debut,
            })

    return {
        "status":        "ok",
        "since_year":    since_year,
        "coverage":      coverage,
        "spot_checks":   spot_checks,
        "recent_sample": recent_sample,
    }


# Lower-case position codes BDL ships that mean "pitcher". Everything
# else is treated as a position-player slot.
_BDL_PITCHER_POSITIONS: set[str] = {"p", "sp", "rp", "cl"}

# Suffix tokens stripped during name normalization. BDL is inconsistent
# about including "Jr."/"Sr." — sometimes the canonical name carries
# the suffix (Bobby Witt Jr.), sometimes it doesn't. Stripping on both
# sides of the comparison sidesteps the difference.
_BDL_NAME_SUFFIXES: set[str] = {"jr", "sr", "ii", "iii", "iv", "v"}


def _normalize_bdl_name(s: str) -> str:
    """Aggressive normalization for BDL name matching. Strips accents
    (Peña → Pena), drops periods and apostrophes (J.J. → JJ,
    O'Brien → OBrien), removes trailing suffix tokens (Jr / II / III),
    lowercases, and collapses whitespace. Returns "" for empty input
    or for a name that's nothing but a suffix marker."""
    if not s:
        return ""
    nfd = unicodedata.normalize("NFD", s)
    s = "".join(c for c in nfd if not unicodedata.combining(c))
    s = s.replace(".", "").replace("'", "")
    tokens = [t for t in s.lower().split() if t not in _BDL_NAME_SUFFIXES]
    return " ".join(tokens)


def _bdl_search_token(full_name: str) -> str:
    """Pick a normalized last-name token for BDL's `search` param.
    Walks tokens right-to-left and returns the first one that
    survives suffix-stripping — so "Bobby Witt Jr." searches for
    "witt", not "jr". Falls back to the raw last token if every
    token is a suffix (shouldn't happen)."""
    tokens = full_name.strip().split() if full_name else []
    for t in reversed(tokens):
        normalized = _normalize_bdl_name(t)
        if normalized:
            return normalized
    return tokens[-1].lower() if tokens else ""


def _bdl_candidate_dict(p: dict) -> dict:
    """Shape one BDL player dict into the small inspection payload
    we surface in `ambiguous` / debug responses."""
    return {
        "bdl_id":    p.get("id"),
        "full_name": p.get("full_name"),
        "position":  p.get("position"),
        "team":      (p.get("team") or {}).get("abbreviation"),
        "debut":     p.get("debut_year"),
    }


def _bdl_is_pitcher(p: dict) -> bool:
    return (p.get("position") or "").strip().lower() in _BDL_PITCHER_POSITIONS


def _bdl_match_one_player(
    full_name: str,
    side: str,
    mlb_debut: Optional[int] = None,
) -> tuple[Optional[int], str, list[dict]]:
    """Returns (bdl_id, status, candidates) for a single name lookup.
    `status` is "matched" / "ambiguous" / "unmatched". `candidates`
    is the filtered list when ambiguous; otherwise empty.

    Strategy ladder — each tier is tried only if the previous didn't
    produce exactly one match:
      1. Exact case-insensitive `full_name` equality.
      2. Normalized equality — strips accents, periods, and suffix
         tokens on BOTH sides. Catches "Bobby Witt" vs "Bobby Witt
         Jr.", "Wily Mo Peña" vs "Wily Mo Pena", "J.J. Davis" vs
         "JJ Davis".
      3. Debut-year filter — when (2) still has multiple hits, keep
         only BDL rows whose `debut_year` is within ±2 of our
         `mlb_debut`. BDL's debut_year is off-by-one for some
         players (Trout: BDL 2010, actual 2011), so ±2 buys a year
         of slack on each side.

    Side filter (batter vs. pitcher) is applied AFTER strategies
    1–3 select candidates by name; this preserves the existing
    behavior of disambiguating same-name pairs (Will Smith C vs
    Will Smith RP) while letting the normalized search still work
    when a player's position-side is misclassified in BDL.
    """
    if not full_name:
        return None, "unmatched", []
    search_token = _bdl_search_token(full_name)
    if not search_token:
        return None, "unmatched", []

    try:
        data = _bdl_get_json("players", {
            "search":   search_token,
            "per_page": 100,
        })
    except Exception as exc:
        log.warning("BDL search failed for %r: %s", full_name, exc)
        return None, "unmatched", []

    results = data.get("data") or []
    if not results:
        return None, "unmatched", []

    target_exact = full_name.strip().lower()
    target_norm  = _normalize_bdl_name(full_name)

    # Strategy 1: exact name match.
    exact = [
        p for p in results
        if (p.get("full_name") or "").strip().lower() == target_exact
    ]

    # Strategy 2: normalized name match. Use as the canonical
    # candidate pool when the exact tier didn't already nail one.
    norm = [
        p for p in results
        if _normalize_bdl_name(p.get("full_name") or "") == target_norm
    ]
    # Union of the two — exact wins on duplicates, but in practice
    # exact ⊆ norm (anything exactly equal is also equal after
    # normalization), so `norm` is the canonical pool.
    candidates = norm if norm else exact

    if not candidates:
        return None, "unmatched", []

    # Side filter — batters take non-pitcher rows, pitchers take
    # pitcher rows. Falls back to the unfiltered set if the side
    # filter eliminated everything (BDL sometimes mislabels the
    # position on debut-season rookies).
    if side == "pitcher":
        side_matches = [p for p in candidates if _bdl_is_pitcher(p)]
    else:
        side_matches = [p for p in candidates if not _bdl_is_pitcher(p)]
    if not side_matches:
        side_matches = candidates

    if len(side_matches) == 1:
        return side_matches[0].get("id"), "matched", []

    # Strategy 3: debut-year filter. Only kicks in when we still
    # have multiple post-side-filter candidates and our DB carries
    # an mlb_debut for the row.
    if mlb_debut is not None and len(side_matches) > 1:
        year_matches = [
            p for p in side_matches
            if p.get("debut_year") is not None
            and abs(int(p["debut_year"]) - int(mlb_debut)) <= 2
        ]
        if len(year_matches) == 1:
            return year_matches[0].get("id"), "matched", []
        if year_matches:
            side_matches = year_matches  # narrow the ambiguous report

    return None, "ambiguous", [_bdl_candidate_dict(p) for p in side_matches]


def _safe_rate(v) -> Optional[float]:
    """Parse a rate stat string. Handles ".293" (leading-dot rates),
    "2.11" (ERA), "---" (zero-AB sentinel) — all formats either
    MLB Stats API or BallDontLie might ship. Returns None for
    anything unparseable."""
    if v is None or v == "" or v == "---":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sync_player_current_team(player_id: int) -> dict:
    """Reconcile `team` on the player's current-year season rows
    against BDL `/players/{bdl_id}`. Requires that the row has a
    `bdl_id` stamped — players without a BDL mapping are skipped
    with a `no_bdl_mapping` status. The bootstrap endpoint
    (`/admin/build-bdl-player-mapping`) covers the historical
    tail; this admin endpoint is for one-shot reconciliation of
    already-mapped active players.

    For newly-discovered MLBAM ids (e.g. a freshly-debuted
    rookie not yet in either bio table): there's no path here to
    bootstrap them without an MLB Stats API hop. The Phase 5
    roster-walk handles new BDL-side rookies; manual SQL or a
    future BDL-search endpoint covers MLBAM-side bootstraps.
    """
    year = _current_year()

    with connection.get_session() as db:
        existing_pit = crud.get_pitcher(db, player_id)
        existing_bat = crud.get_player(db, player_id)
    bdl_id = (getattr(existing_pit, "bdl_id", None)
              or getattr(existing_bat, "bdl_id", None))

    if bdl_id is None:
        log.warning(
            "sync_player_current_team: skipping player_id=%s — no bdl_id mapped",
            player_id,
        )
        return {
            "player_id": player_id,
            "status":    "no_bdl_mapping",
        }

    try:
        bdl_data = _bdl_get_json(f"players/{bdl_id}", {})
    except Exception as exc:
        return {"player_id": player_id, "status": f"bdl_fetch_failed: {exc}"}
    p = bdl_data.get("data") if isinstance(bdl_data.get("data"), dict) else bdl_data
    if not p or not p.get("id"):
        return {"player_id": player_id, "status": "not_found_in_bdl"}

    name = p.get("full_name")
    team_bdl_id = (p.get("team") or {}).get("id")
    lahman_code = _BDL_TO_LAHMAN_TEAM_MAP.get(team_bdl_id) if team_bdl_id else None
    if not lahman_code:
        return {
            "player_id": player_id,
            "status":    "no_current_team",
            "fullName":  name,
        }

    bio_created: Optional[str] = None
    cleared_last_season = False
    with connection.get_session() as db:
        in_pitchers = crud.get_pitcher(db, player_id) is not None
        in_players  = crud.get_player(db, player_id)  is not None

        # Bootstrap a missing bio from the BDL payload — both
        # existing bio tables had no row for this player_id, but
        # the BDL fetch confirms they're an active MLB player.
        if not in_pitchers and not in_players:
            bio = _parse_bdl_player_bio(p)
            bio.pop("_team_code", None)
            if bio.get("mlb_debut") is None:
                bio["mlb_debut"] = year
            bio["player_id"] = player_id
            position = (p.get("position") or "").strip().lower()
            if position in _BDL_PITCHER_POSITIONS:
                crud.save_pitcher(db, bio)
                in_pitchers = True
                bio_created = "pitcher"
            else:
                crud.save_player(db, bio)
                in_players = True
                bio_created = "batter"

        # Clear stale mlb_last_season — the player is on an active
        # roster right now, so any "retired in YYYY" value from
        # Lahman's `finalGame` is wrong.
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
        "fullName":            name,
        "new_team":            lahman_code,
        "team_name":           lahman_code,
        "source":              "bdl",
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
    """Walk all 30 BDL active rosters and reconcile the `team`
    column on each player's current-year `pitcher_seasons` /
    `player_seasons` row. Also: stamps `bdl_id` on any in-DB
    player that didn't get one from the mapping bootstrap (by
    name match), clears `mlb_last_season` on returning players,
    and discovers brand-new players via BDL bio + MLB Stats API
    name search (we need the MLBAM PK).

    30 BDL API calls (one per team) instead of one-per-player —
    same correctness bar as `sync_player_current_team` for
    everyone currently on a 40-man, at one round trip per team.
    Rate-limited: `_BDL_RATE_LIMIT_SLEEP` between team calls.
    """
    _get_bdl_key()

    from database.models import Pitcher as _Pitcher
    from database.models import Player as _Player

    counts: dict[str, int] = {
        "pitcher_seasons:updated":  0,
        "pitcher_seasons:created":  0,
        "player_seasons:updated":   0,
        "player_seasons:created":   0,
        # Fresh-rookie discovery — BDL has them but our DB doesn't.
        # Bio comes from BDL; MLBAM PK from an MLB Stats API name
        # search (we can't insert without the PK).
        "pitchers_bio:created":     0,
        "players_bio:created":      0,
        # `mlb_last_season` cleared on existing bios when the player
        # shows up on an active roster — Lahman's `finalGame` year
        # sticks around even after a player returns from a gap year.
        "pitchers_bio:reactivated": 0,
        "players_bio:reactivated":  0,
        # Stamped via name match — for rows in DB by MLBAM that
        # didn't get a bdl_id during the bootstrap mapping pass
        # (or new BDL ids the bootstrap never ran on).
        "bdl_id:stamped":           0,
    }
    bio_failed: list[int] = []
    failed_teams: list[str] = []
    unresolved: list[dict] = []

    with connection.get_session() as db:
        for lahman_code, bdl_team_id in _BDL_TEAM_ID_MAP.items():
            try:
                roster_resp = _bdl_get_json(
                    "players/active",
                    {"team_ids[]": bdl_team_id, "per_page": 100},
                )
            except Exception:
                failed_teams.append(lahman_code)
                continue

            for entry in roster_resp.get("data") or []:
                bdl_player_id = entry.get("id")
                if not bdl_player_id:
                    continue
                position = (entry.get("position") or "").strip().lower()
                is_pitcher_hint = position in _BDL_PITCHER_POSITIONS
                full_name = entry.get("full_name")

                # 1. Find existing rows by bdl_id (post-mapping
                #    primary key for BDL-keyed lookups).
                pit_row = (
                    db.query(_Pitcher)
                    .filter(_Pitcher.bdl_id == bdl_player_id)
                    .first()
                )
                bat_row = (
                    db.query(_Player)
                    .filter(_Player.bdl_id == bdl_player_id)
                    .first()
                )

                # 2. No bdl_id match — try a name-match against
                #    bdl_id-less rows and stamp the bdl_id when
                #    found. Catches anyone the mapping bootstrap
                #    didn't reach plus brand-new BDL ids whose
                #    name already exists in our DB under MLBAM.
                if pit_row is None and bat_row is None and full_name:
                    stamped_side = _stamp_bdl_id_by_name(
                        db, full_name, bdl_player_id, is_pitcher_hint,
                    )
                    if stamped_side == "pitcher":
                        pit_row = (
                            db.query(_Pitcher)
                            .filter(_Pitcher.bdl_id == bdl_player_id)
                            .first()
                        )
                        counts["bdl_id:stamped"] += 1
                    elif stamped_side == "batter":
                        bat_row = (
                            db.query(_Player)
                            .filter(_Player.bdl_id == bdl_player_id)
                            .first()
                        )
                        counts["bdl_id:stamped"] += 1

                # 3. Still no match — genuinely new BDL player not
                #    cross-referenced to our MLBAM-keyed schema.
                #    Without an MLBAM, we can't insert (our PK).
                #    Record them so the operator can hand-curate the
                #    entry if needed; skip otherwise.
                if pit_row is None and bat_row is None:
                    unresolved.append({
                        "bdl_id":    bdl_player_id,
                        "full_name": full_name,
                        "team":      lahman_code,
                    })
                    continue

                # 4. Clear stale mlb_last_season — the player is on
                #    an active roster, so any retired-year stamp
                #    Lahman left behind is wrong.
                if pit_row is not None and pit_row.mlb_last_season is not None:
                    pit_row.mlb_last_season = None
                    counts["pitchers_bio:reactivated"] += 1
                if bat_row is not None and bat_row.mlb_last_season is not None:
                    bat_row.mlb_last_season = None
                    counts["players_bio:reactivated"] += 1

                # 5. Apply team to the current-year season rows.
                resolved_player_id = (
                    (pit_row.player_id if pit_row else None)
                    or (bat_row.player_id if bat_row else None)
                )
                if resolved_player_id is not None:
                    actions = _apply_team_to_season_rows(
                        db,
                        player_id=resolved_player_id,
                        year=current_year,
                        abbr=lahman_code,
                        create_pitcher=pit_row is not None,
                        create_batter=bat_row  is not None,
                    )
                    for a in actions:
                        counts[a] = counts.get(a, 0) + 1

            # Rate-limit between team calls (1 BDL request per
            # team, 30 teams → ~30 calls. Under-budget for the
            # 5/sec ceiling with `_BDL_RATE_LIMIT_SLEEP` spacing.)
            time.sleep(_BDL_RATE_LIMIT_SLEEP)

        db.commit()

    total = sum(counts.values())
    return {
        "status":       "ok",
        "total":        total,
        "counts":       counts,
        "failed_teams": failed_teams,
        # Players BDL knows about that we couldn't insert because
        # the MLB Stats API name search didn't surface a single
        # MLBAM match. Manual hand-mapping required for these
        # (rare — minor-league call-ups with name collisions).
        "unresolved":   unresolved,
        "bio_failed":   bio_failed,
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
    pit_no_bdl   = 0
    bat_no_bdl   = 0

    from database.models import Pitcher as _PitBio
    from database.models import Player as _PlrBio

    with connection.get_session() as db:
        # Pitcher side — bulk-fetch BDL stats for every null-row
        # pitcher in one batched call (or several, paginated).
        pit_rows = (
            db.query(_PitcherSeason)
            .filter(_PitcherSeason.year == current_year,
                    _PitcherSeason.last_updated.is_(None))
            .all()
        )
        # Map pid → bdl_id, dropping any without a mapping.
        pit_pid_to_bdl: dict[int, int] = {}
        for r in pit_rows:
            bio = db.get(_PitBio, r.player_id)
            if bio is None or bio.bdl_id is None:
                pit_no_bdl += 1
                continue
            pit_pid_to_bdl[r.player_id] = int(bio.bdl_id)
        raw_pit = _fetch_bdl_batch_stats(
            list(pit_pid_to_bdl.values()), current_year,
        ) if pit_pid_to_bdl else {}
        # Reverse-map bdl_id → parsed pitcher stats.
        pit_stats_by_pid: dict[int, dict] = {}
        for pid, bdl_id in pit_pid_to_bdl.items():
            row = raw_pit.get(bdl_id)
            if row is None:
                continue
            pit_stats_by_pid[pid] = _parse_bdl_pitcher_row(row)

        for r in pit_rows:
            if r.player_id not in pit_pid_to_bdl:
                continue
            stats = pit_stats_by_pid.get(r.player_id)
            if stats is None:
                pit_no_data += 1
                continue
            for key in ("G", "GS", "W", "L", "SV", "IP",
                        "H", "ER", "HR", "BB", "SO",
                        "ERA", "WHIP", "K_per9"):
                value = stats.get(key)
                if value is not None:
                    setattr(r, key, value)
            # Derive BB/9 + HR/9 from the merged components since
            # BDL only ships K/9. FIP is filled by the nightly's
            # entry-build path; not re-derived here to keep this
            # repair endpoint scoped to overlay refresh.
            ip = stats.get("IP") or 0.0
            if ip:
                r.BB_per9 = round((stats.get("BB") or 0) * 9 / ip, 2)
                r.HR_per9 = round((stats.get("HR") or 0) * 9 / ip, 2)
            r.last_updated = now
            pit_repaired += 1

        # Batter side — same shape.
        bat_rows = (
            db.query(_PlayerSeason)
            .filter(_PlayerSeason.year == current_year,
                    _PlayerSeason.last_updated.is_(None))
            .all()
        )
        bat_pid_to_bdl: dict[int, int] = {}
        for r in bat_rows:
            bio = db.get(_PlrBio, r.player_id)
            if bio is None or bio.bdl_id is None:
                bat_no_bdl += 1
                continue
            bat_pid_to_bdl[r.player_id] = int(bio.bdl_id)
        raw_bat = _fetch_bdl_batch_stats(
            list(bat_pid_to_bdl.values()), current_year,
        ) if bat_pid_to_bdl else {}
        bat_stats_by_pid: dict[int, dict] = {}
        for pid, bdl_id in bat_pid_to_bdl.items():
            row = raw_bat.get(bdl_id)
            if row is None:
                continue
            bat_stats_by_pid[pid] = _parse_bdl_batter_row(row)

        for r in bat_rows:
            if r.player_id not in bat_pid_to_bdl:
                continue
            stats = bat_stats_by_pid.get(r.player_id)
            if stats is None:
                bat_no_data += 1
                continue
            for key in ("G", "AB", "R", "H", "doubles", "triples", "HR",
                        "RBI", "BB", "SO", "SB", "TB",
                        "BA", "OBP", "SLG", "OPS"):
                value = stats.get(key)
                if value is not None:
                    setattr(r, key, value)
            r.last_updated = now
            bat_repaired += 1

        db.commit()

    return {
        "status":           "ok",
        "year":             current_year,
        "pitcher_repaired": pit_repaired,
        "pitcher_no_data":  pit_no_data,
        "pitcher_no_bdl":   pit_no_bdl,
        "batter_repaired":  bat_repaired,
        "batter_no_data":   bat_no_data,
        "batter_no_bdl":    bat_no_bdl,
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
        existing_pit = crud.get_pitcher(db, player_id)
        existing_bat = crud.get_player(db, player_id)
        in_pitchers = existing_pit is not None
        in_players  = existing_bat is not None
        bdl_id = (getattr(existing_pit, "bdl_id", None)
                  or getattr(existing_bat, "bdl_id", None))

        # New-bio bootstrap path was removed with the MLB Stats
        # API helpers — we can't insert without an MLBAM PK, and
        # BDL doesn't carry the MLBAM cross-reference. Skip if the
        # player isn't already in our DB.
        if not in_pitchers and not in_players:
            return {
                "status":    "player_not_in_db",
                "player_id": player_id,
                "hint":      "Add the player to `players` or `pitchers` first "
                             "(e.g. via Lahman load or hand-curated insert), "
                             "then re-run this backfill.",
            }

        # Populate bbref_id from the Chadwick bridge if missing.
        # Without it, the Lahman loader can't link historical rows
        # on the next archive drop. The bwar overlay below keys
        # off mlb_ID, not bbref_id, so it still works regardless.
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
            entry = _build_backfill_batter_entry(bdl_id, year)
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
            entry = _build_backfill_pitcher_entry(bdl_id, year)
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


# BDL ships season_stats rows with `team_name` (e.g., "Angels"),
# not a numeric id. Map short franchise names → Lahman codes so
# the backfill rows can stamp `team` correctly. Kept inline since
# `_BDL_TEAM_ID_MAP` only covers numeric ids and BDL doesn't
# expose the per-season team id on season_stats responses.
_BDL_TEAM_NAME_TO_LAHMAN: dict[str, str] = {
    "Diamondbacks": "ARI", "Braves":  "ATL", "Orioles": "BAL", "Red Sox":  "BOS",
    "Cubs":         "CHN", "White Sox": "CHA", "Reds":   "CIN", "Guardians": "CLE",
    "Rockies":      "COL", "Tigers":  "DET", "Astros":  "HOU", "Royals":    "KCA",
    "Angels":       "LAA", "Dodgers": "LAN", "Marlins": "MIA", "Brewers":   "MIL",
    "Twins":        "MIN", "Mets":    "NYN", "Yankees": "NYA", "Athletics": "ATH",
    "Phillies":     "PHI", "Pirates": "PIT", "Padres":  "SDN", "Giants":    "SFN",
    "Mariners":     "SEA", "Cardinals": "SLN", "Rays":   "TBA", "Rangers":   "TEX",
    "Blue Jays":    "TOR", "Nationals": "WAS",
    # Pre-rebrand aliases — historical seasons may surface these.
    "Indians":      "CLE",  # → Guardians 2022
}


def _build_backfill_batter_entry(bdl_id: Optional[int], year: int) -> Optional[dict]:
    """Build a `player_seasons` row from BDL `/season_stats` for
    one historical year. Returns None when:
      • the player isn't BDL-mapped (`bdl_id is None`)
      • BDL has no batting activity for that year
      • the row is a pitcher's pitching-only line (no AB / PA / BB)

    BDL doesn't carry HBP / SF / SH / GIDP / IBB / CS on the
    season_stats endpoint — those columns stay nil for current-
    season backfill rows. The bwar overlay (called separately
    in `backfill_player_seasons`) still drops WAR / OPS+ on top
    from the bref-sourced full-history CSV."""
    if bdl_id is None:
        return None
    try:
        data = _bdl_get_json("season_stats", {
            "player_ids[]": bdl_id,
            "season":       year,
            "per_page":     100,
        })
    except Exception:
        return None
    rows = data.get("data") or []
    if not rows:
        return None
    # BDL ships one row per (player, side); for two-way players
    # the batting row is the one with AB / PA. Pick the row with
    # non-null at_bats; fall back to first if none match.
    s = next((r for r in rows if r.get("batting_ab") is not None), rows[0])

    h  = _to_int(s.get("batting_h"))     or 0
    d2 = _to_int(s.get("batting_2b"))    or 0
    d3 = _to_int(s.get("batting_3b"))    or 0
    hr = _to_int(s.get("batting_hr"))    or 0
    team_code = _BDL_TEAM_NAME_TO_LAHMAN.get(s.get("team_name") or "")

    entry: dict = {
        "year":    year,
        "G":       _to_int(s.get("batting_gp")),
        "AB":      _to_int(s.get("batting_ab")),
        "R":       _to_int(s.get("batting_r")),
        "H":       h or None,
        "doubles": _to_int(s.get("batting_2b")),
        "triples": _to_int(s.get("batting_3b")),
        "HR":      _to_int(s.get("batting_hr")),
        "RBI":     _to_int(s.get("batting_rbi")),
        "BB":      _to_int(s.get("batting_bb")),
        "SO":      _to_int(s.get("batting_so")),
        "SB":      _to_int(s.get("batting_sb")),
        "TB":      h + d2 + 2 * d3 + 3 * hr,
        "BA":      _safe_rate(s.get("batting_avg")),
        "OBP":     _safe_rate(s.get("batting_obp")),
        "SLG":     _safe_rate(s.get("batting_slg")),
        "OPS":     _safe_rate(s.get("batting_ops")),
    }
    if team_code:
        entry["team"] = team_code
    return entry


def _build_backfill_pitcher_entry(bdl_id: Optional[int], year: int) -> Optional[dict]:
    """Pitcher counterpart to `_build_backfill_batter_entry`. BDL
    doesn't ship per-9 rates other than K/9 on season_stats; we
    round ERA / WHIP / K/9 to 2 dp to match the nightly entry
    builder's precision. FIP is derived elsewhere from components."""
    if bdl_id is None:
        return None
    try:
        data = _bdl_get_json("season_stats", {
            "player_ids[]": bdl_id,
            "season":       year,
            "per_page":     100,
        })
    except Exception:
        return None
    rows = data.get("data") or []
    if not rows:
        return None
    s = next((r for r in rows if r.get("pitching_ip") is not None), rows[0])
    if s.get("pitching_ip") is None:
        return None
    team_code = _BDL_TEAM_NAME_TO_LAHMAN.get(s.get("team_name") or "")

    entry: dict = {
        "year":   year,
        "G":      _to_int(s.get("pitching_gp")),
        "GS":     _to_int(s.get("pitching_gs")),
        "W":      _to_int(s.get("pitching_w")),
        "L":      _to_int(s.get("pitching_l")),
        "SV":     _to_int(s.get("pitching_sv")),
        "IP":     _safe_rate(s.get("pitching_ip")),
        "H":      _to_int(s.get("pitching_h")),
        "ER":     _to_int(s.get("pitching_er")),
        "HR":     _to_int(s.get("pitching_hr")),
        "BB":     _to_int(s.get("pitching_bb")),
        "SO":     _to_int(s.get("pitching_k")),
        "ERA":    _round_or_none(_safe_rate(s.get("pitching_era")), 2),
        "WHIP":   _round_or_none(_safe_rate(s.get("pitching_whip")), 2),
        "K_per9": _round_or_none(_safe_rate(s.get("pitching_k_per_9")), 2),
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


# ---------------------------------------------------------------------------
# BallDontLie game-centric gamelogs
# ---------------------------------------------------------------------------
#
# The MLB Stats API path above is player-centric: one call per player
# per season. The nightly Phase 4 ran ~2,400 calls per night at that
# shape. BDL's `/stats?game_id={id}` gives us every player's line for
# one game in a single call, so the same coverage drops to ~15 calls
# per day (one per game).
#
# Discovery / mapping notes:
#   • BDL ships its own player ids — we reverse-lookup our MLBAM PK
#     via `players.bdl_id` / `pitchers.bdl_id` before insert. Stat
#     rows whose bdl_id isn't in our DB are skipped (typical for
#     minor-league callups that BDL knows about but our mapping
#     bootstrap hasn't reached yet — the next bootstrap pass will
#     catch them).
#   • Each player gets up to two rows per game in BDL's response —
#     one batting (ip is null) and one pitching (ip is not null).
#     Two-way players (Ohtani) get one of each, both keyed to the
#     same bdl_id; the parser routes them to the right table.

def fetch_bdl_games_for_date(date_str: str,
                             finals_only: bool = True) -> list[dict]:
    """Return BDL games for one local-date (`yyyy-MM-dd`). Filters
    to finals by default — gamelogs are an end-of-game artifact, so
    fetching in-progress games would write incomplete lines. Pass
    `finals_only=False` to include all statuses for debugging."""
    try:
        data = _bdl_get_json("games", {
            "dates[]":  date_str,
            "per_page": 100,
        })
    except Exception as exc:
        # Previously this silently returned [] — that masked any
        # rate-limit / auth / network failure as "0 games today",
        # which is indistinguishable from a quiet date. Log the
        # full traceback so Railway shows what blew up.
        log.exception(
            "fetch_bdl_games_for_date(%s): /games request failed: %s",
            date_str, exc,
        )
        return []
    all_games = data.get("data") or []
    if finals_only:
        games = [g for g in all_games if g.get("status") == "STATUS_FINAL"]
    else:
        games = all_games
    log.info(
        "fetch_bdl_games_for_date(%s): %d games returned (%d final, %d other)",
        date_str, len(all_games), len(games), len(all_games) - len(games),
    )
    return games


def debug_game_stats_mapping(game_id: int) -> dict:
    """Diagnostic for the BDL gamelog mapping. Fetches stats for one
    game, checks whether each row's BDL player id is in our local
    `_bdl_to_mlbam_map`, and (when missing) name-searches the bio
    tables to see what BDL id we stored under that name. Surfaces
    the gap between the BDL id space returned by `/stats` and the
    one we populated via `/players?search=` during the mapping
    bootstrap."""
    if not connection.db_available():
        return {"status": "no_db"}
    _get_bdl_key()

    from database.models import Pitcher as _Pitcher
    from database.models import Player as _Player

    # 1. Fetch one page of stats for this game.
    try:
        data = _bdl_get_json("stats", {
            "game_ids[]": [game_id],
            "per_page":   100,
        })
    except Exception as exc:
        return {"status": f"bdl_fetch_failed: {exc}", "game_id": game_id}
    rows = data.get("data") or []

    # 2. Pre-load our BDL→MLBAM map + sample five entries so the
    # operator can eyeball the id-space we stored.
    with connection.get_session() as db:
        bdl_to_mlbam = _bdl_to_mlbam_map(db)
        map_sample: list[dict] = []
        # Pull names for the first 5 map entries so the sample is
        # actually informative (just "bdl_id 100 → mlbam 660271"
        # doesn't tell you who that is).
        for bdl_id, mlbam in list(bdl_to_mlbam.items())[:5]:
            row = (
                db.query(_Player).filter(_Player.bdl_id == bdl_id).first()
                or db.query(_Pitcher).filter(_Pitcher.bdl_id == bdl_id).first()
            )
            map_sample.append({
                "bdl_id":   bdl_id,
                "mlbam_id": mlbam,
                "name":     getattr(row, "name", None),
            })

        # 3. Walk first 5 stat rows, classify by mapping presence,
        # and for misses do a name-match search to see what BDL id
        # WE stored for that player (if any).
        diagnostics: list[dict] = []
        for stat in rows[:5]:
            p = stat.get("player") or {}
            bdl_pid = p.get("id")
            full_name = p.get("full_name")
            if bdl_pid is None:
                diagnostics.append({"row": stat, "issue": "no player.id on stat row"})
                continue
            mapped_mlbam = bdl_to_mlbam.get(int(bdl_pid))
            entry: dict = {
                "bdl_id_from_stat":  bdl_pid,
                "name":              full_name,
                "team_name":         stat.get("team_name"),
                "position":          p.get("position"),
                "in_our_map":        mapped_mlbam is not None,
                "mapped_mlbam":      mapped_mlbam,
            }
            if mapped_mlbam is None and full_name:
                # Name-search both bio tables (case-insensitive).
                # Report whatever bdl_id we DO have stored for that
                # name — that's the proof of an id-space drift.
                name_hits: list[dict] = []
                for model, side in [(_Player, "batter"), (_Pitcher, "pitcher")]:
                    hit = (
                        db.query(model)
                        .filter(model.name.ilike(full_name))
                        .first()
                    )
                    if hit is not None:
                        name_hits.append({
                            "side":               side,
                            "mlbam_id":           hit.player_id,
                            "name_in_db":         hit.name,
                            "stored_bdl_id":      hit.bdl_id,
                            "matches_stat_bdl":   hit.bdl_id == bdl_pid,
                        })
                entry["name_search"] = name_hits
            diagnostics.append(entry)

    return {
        "status":            "ok",
        "game_id":           game_id,
        "stat_rows_total":   len(rows),
        "stat_rows_sampled": min(5, len(rows)),
        "diagnostics":       diagnostics,
        "map_total_entries": len(bdl_to_mlbam),
        "map_sample":        map_sample,
    }


def _bdl_to_mlbam_map(db) -> dict[int, int]:
    """{bdl_id: mlbam_player_id} from both bio tables. Two-way
    players (Ohtani: same bdl_id stamped on both tables) end up
    keying to the same MLBAM id from either side — the merge is
    a no-op."""
    from database.models import Pitcher as _Pitcher
    from database.models import Player as _Player

    out: dict[int, int] = {}
    for row in (
        db.query(_Player.player_id, _Player.bdl_id)
        .filter(_Player.bdl_id.isnot(None))
        .all()
    ):
        if row.bdl_id is not None:
            out[int(row.bdl_id)] = int(row.player_id)
    for row in (
        db.query(_Pitcher.player_id, _Pitcher.bdl_id)
        .filter(_Pitcher.bdl_id.isnot(None))
        .all()
    ):
        if row.bdl_id is not None:
            # Don't clobber a batter mapping (preserves two-way
            # player precedence matching `_latest_team_info`).
            out.setdefault(int(row.bdl_id), int(row.player_id))
    return out


def _bdl_game_ctx(game: dict, fallback_date: Optional[str] = None) -> dict:
    """Pre-compute the game-level context the row parsers need:
    game_id (string), game_date (date), season (int), home and
    away team names + final-runs counts. `fallback_date` is the
    local-date string the caller used to find the game; we prefer
    it over BDL's UTC `date` when present to avoid attributing
    late West Coast games to the next UTC day."""
    raw_date = fallback_date or (game.get("date") or "")[:10]
    try:
        game_date = datetime.date.fromisoformat(raw_date)
    except ValueError:
        game_date = None
    home = game.get("home_team") or {}
    away = game.get("away_team") or {}
    home_data = game.get("home_team_data") or {}
    away_data = game.get("away_team_data") or {}
    return {
        "game_id":         str(game.get("id")),
        "game_date":       game_date,
        "season":          _to_int(game.get("season")) or (game_date.year if game_date else None),
        "home_team_id":    home.get("id"),
        "away_team_id":    away.get("id"),
        # BDL ships THREE different "name" shapes for a team:
        # `name` is the short franchise tag ("Rangers"), `display_name`
        # is the full city + nickname ("Texas Rangers"), and stat rows
        # use a fourth field — `team_name` — which carries the DISPLAY
        # name, not the short name. `_resolve_side` checks all three
        # to absorb the inconsistency.
        "home_team_name":          home.get("name"),
        "away_team_name":          away.get("name"),
        "home_team_display_name":  home.get("display_name"),
        "away_team_display_name":  away.get("display_name"),
        "home_team_abbr":  home.get("abbreviation"),
        "away_team_abbr":  away.get("abbreviation"),
        "home_runs":       _to_int(home_data.get("runs")),
        "away_runs":       _to_int(away_data.get("runs")),
    }


def _resolve_side(stat: dict, ctx: dict) -> Optional[str]:
    """Determine which side (home/away) the player played for in
    this game. The stat row's `team_name` field carries BDL's DISPLAY
    name ("Texas Rangers"), not the short franchise tag ("Rangers"),
    so we match against both `home_team_name` (short) AND
    `home_team_display_name` (long) to absorb the inconsistency.
    Returns "home" / "away" or None when unresolvable."""
    team_name = (stat.get("team_name") or "").strip()
    if not team_name:
        # Fall back to the nested team id on `player.team`. Note:
        # this is the player's CURRENT team, not necessarily the
        # team they played for in this game (for mid-season trades),
        # so it's a best-effort fallback.
        team_id = (stat.get("player") or {}).get("team") or {}
        tid = team_id.get("id")
        if tid is None:
            return None
        if tid == ctx.get("home_team_id"): return "home"
        if tid == ctx.get("away_team_id"): return "away"
        return None
    # Try both name shapes — BDL is inconsistent (see `_bdl_game_ctx`).
    home_names = {ctx.get("home_team_name"), ctx.get("home_team_display_name")}
    away_names = {ctx.get("away_team_name"), ctx.get("away_team_display_name")}
    if team_name in home_names: return "home"
    if team_name in away_names: return "away"
    log.warning(
        "_resolve_side: stat team_name=%r matched neither home=%r "
        "(display=%r) nor away=%r (display=%r) for game %s",
        team_name,
        ctx.get("home_team_name"), ctx.get("home_team_display_name"),
        ctx.get("away_team_name"), ctx.get("away_team_display_name"),
        ctx.get("game_id"),
    )
    return None


def _parse_bdl_batting_gamelog(stat: dict, ctx: dict) -> Optional[dict]:
    """One BDL `/stats` row → `batting_gamelogs` row dict. Returns
    None when the row carries no batting activity (the row's `ip`
    side, or a row where the player didn't bat). Caller stamps
    `player_id` from the bdl→mlbam map; this function leaves
    that key out."""
    # Skip pure pitching rows — they're identified by a non-null
    # IP. The batter side of a two-way appearance will be in a
    # separate row in the same response.
    if stat.get("ip") is not None and (stat.get("at_bats") is None
                                       and stat.get("plate_appearances") is None):
        return None
    pa = _to_int(stat.get("plate_appearances")) or 0
    ab = _to_int(stat.get("at_bats")) or 0
    bb = _to_int(stat.get("bb")) or 0
    if pa == 0 and ab == 0 and bb == 0:
        # No batting activity at all — pitcher's defensive-only line.
        return None

    side = _resolve_side(stat, ctx)
    if side is None or ctx.get("game_date") is None:
        return None
    is_home = side == "home"
    team_score = ctx["home_runs"] if is_home else ctx["away_runs"]
    opp_score  = ctx["away_runs"] if is_home else ctx["home_runs"]
    opp_abbr   = ctx["away_team_abbr"] if is_home else ctx["home_team_abbr"]

    result: Optional[str]
    if team_score is None or opp_score is None:
        result = None
    elif team_score > opp_score: result = "W"
    elif team_score < opp_score: result = "L"
    else:                         result = "T"

    return {
        "game_id":    ctx["game_id"],
        "game_date":  ctx["game_date"],
        "season":     ctx["season"],
        "opponent":   opp_abbr,
        "home_away":  "H" if is_home else "A",
        "result":     result,
        "team_score": team_score,
        "opp_score":  opp_score,
        "AB":         _to_int(stat.get("at_bats")),
        "R":          _to_int(stat.get("runs")),
        "H":          _to_int(stat.get("hits")),
        "doubles":    _to_int(stat.get("doubles")),
        "triples":    _to_int(stat.get("triples")),
        "HR":         _to_int(stat.get("hr")),
        "RBI":        _to_int(stat.get("rbi")),
        "BB":         _to_int(stat.get("bb")),
        # BDL doesn't ship IBB on /stats — column stays null.
        "IBB":        None,
        "SO":         _to_int(stat.get("k")),
        "SB":         _to_int(stat.get("stolen_bases")),
        "CS":         _to_int(stat.get("caught_stealing")),
        "HBP":        _to_int(stat.get("hit_by_pitch")),
        "SF":         _to_int(stat.get("sac_flies")),
        # BDL doesn't ship LOB on /stats — stays null.
        "LOB":        None,
    }


def _parse_bdl_pitching_gamelog(stat: dict, ctx: dict) -> Optional[dict]:
    """BDL `/stats` row → `pitching_gamelogs` row dict. Returns
    None when the row has no pitching activity (IP is null)."""
    ip = stat.get("ip")
    if ip is None:
        return None
    try:
        ip_dec = float(ip)
    except (TypeError, ValueError):
        return None
    if ip_dec <= 0 and (_to_int(stat.get("p_k")) or 0) == 0:
        return None

    side = _resolve_side(stat, ctx)
    if side is None or ctx.get("game_date") is None:
        return None
    is_home = side == "home"
    opp_abbr = ctx["away_team_abbr"] if is_home else ctx["home_team_abbr"]

    # Decision priority — same shape the MLB Stats API parser used.
    # BS (blown save) is not derivable from BDL; falls through to ND.
    if   _to_int(stat.get("saves")):   result = "S"
    elif _to_int(stat.get("holds")):   result = "H"
    elif _to_int(stat.get("wins")):    result = "W"
    elif _to_int(stat.get("losses")):  result = "L"
    else:                                result = "ND"

    return {
        "game_id":   ctx["game_id"],
        "game_date": ctx["game_date"],
        "season":    ctx["season"],
        "opponent":  opp_abbr,
        "home_away": "H" if is_home else "A",
        "result":    result,
        # BDL ships IP as true decimal already (no `_ip_str_to_decimal`
        # baseball-notation conversion needed here).
        "IP":        ip_dec,
        "H":         _to_int(stat.get("p_hits")),
        "R":         _to_int(stat.get("p_runs")),
        "ER":        _to_int(stat.get("er")),
        "BB":        _to_int(stat.get("p_bb")),
        "SO":        _to_int(stat.get("p_k")),
        "HR":        _to_int(stat.get("p_hr")),
        # BDL doesn't ship HBP or WP on the pitching side of /stats.
        "HBP":       None,
        "WP":        None,
        "pitches":   _to_int(stat.get("pitch_count")),
        # BDL doesn't ship strikes-thrown either.
        "strikes":   None,
    }


def fetch_bdl_game_stats(
    game_id: int, bdl_to_mlbam: dict[int, int], ctx: dict,
) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """Fetch all per-player stat lines for one BDL game and bucket
    them by MLBAM player_id. Returns (batting_by_pid, pitching_by_pid).
    Rows whose `player.id` isn't in `bdl_to_mlbam` are silently
    skipped — they're real BDL players we just haven't mapped yet.

    Paginates via `meta.next_cursor`; per_page=100 is enough for
    typical 25v25 games, but two-way players push some games over."""
    bat_by_pid: dict[int, list[dict]] = {}
    pit_by_pid: dict[int, list[dict]] = {}
    cursor: Optional[int] = None
    total_rows = 0
    page = 0
    while True:
        page += 1
        # BDL silently ignores `game_id` (singular) on /stats — the
        # endpoint paginates the global firehose when sent that way,
        # which is how we saw cursor reach 64,000+ for a single game.
        # The plural array form is the one that actually filters
        # (verified by curl test). `_bdl_get_json` urlencodes with
        # doseq=True, so the list value expands to `game_ids[]=X`.
        params: dict = {"game_ids[]": [game_id], "per_page": 100}
        if cursor is not None:
            params["cursor"] = cursor
        log.info(
            "fetch_bdl_game_stats(%d): fetching page %d (cursor=%s)",
            game_id, page, cursor,
        )
        try:
            data = _bdl_get_json("stats", params)
        except Exception as exc:
            # Full traceback so a transient network / auth / rate-
            # limit issue shows the failure point. Previously this
            # was a one-line warning that just said "fetch failed"
            # without the cause.
            log.exception(
                "BDL /stats fetch failed for game %d page %d: %s",
                game_id, page, exc,
            )
            break
        rows = data.get("data") or []
        total_rows += len(rows)
        for stat in rows:
            bdl_pid = (stat.get("player") or {}).get("id")
            if bdl_pid is None:
                continue
            mlbam = bdl_to_mlbam.get(int(bdl_pid))
            if mlbam is None:
                continue
            bat = _parse_bdl_batting_gamelog(stat, ctx)
            if bat is not None:
                bat_by_pid.setdefault(mlbam, []).append(bat)
            pit = _parse_bdl_pitching_gamelog(stat, ctx)
            if pit is not None:
                pit_by_pid.setdefault(mlbam, []).append(pit)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if cursor is None:
            break
    log.info(
        "fetch_bdl_game_stats(%d): %d total stat rows across %d page(s) "
        "→ %d mapped batter rows, %d mapped pitcher rows",
        game_id, total_rows, page,
        sum(len(v) for v in bat_by_pid.values()),
        sum(len(v) for v in pit_by_pid.values()),
    )
    return bat_by_pid, pit_by_pid


def save_bdl_gamelogs_for_date(date_str: str) -> dict:
    """Walk every BDL final game on `date_str` (yyyy-mm-dd, local),
    fetch each game's full stat sheet, and upsert into
    `batting_gamelogs` / `pitching_gamelogs`. Rate-limited via
    `_BDL_RATE_LIMIT_SLEEP` between game calls.

    Idempotent — the row PK (player_id, game_id) means re-running
    on a date that's already loaded is a no-op upsert."""
    log.info("save_bdl_gamelogs_for_date: starting game log fetch for %s", date_str)
    if not connection.db_available():
        log.warning("save_bdl_gamelogs_for_date(%s): DATABASE_URL not configured", date_str)
        return {"status": "no_db"}
    _get_bdl_key()

    games = fetch_bdl_games_for_date(date_str, finals_only=True)
    if not games:
        log.info("save_bdl_gamelogs_for_date(%s): no final games to ingest", date_str)
        return {
            "status":      "ok",
            "date":        date_str,
            "games":       0,
            "bat_rows":    0,
            "pit_rows":    0,
            "skipped_unmapped_players": 0,
        }

    game_ids = [g.get("id") for g in games]
    log.info(
        "save_bdl_gamelogs_for_date(%s): %d final games: %s",
        date_str, len(games), game_ids,
    )

    with connection.get_session() as db:
        bdl_to_mlbam = _bdl_to_mlbam_map(db)
    log.info(
        "save_bdl_gamelogs_for_date(%s): %d BDL→MLBAM mappings loaded",
        date_str, len(bdl_to_mlbam),
    )

    total_bat = 0
    total_pit = 0
    skipped: int = 0
    for i, g in enumerate(games):
        game_id = g.get("id")
        if game_id is None:
            continue
        log.info(
            "save_bdl_gamelogs_for_date(%s): game %d/%d — fetching stats for game %s",
            date_str, i + 1, len(games), game_id,
        )
        ctx = _bdl_game_ctx(g, fallback_date=date_str)
        try:
            bat_by_pid, pit_by_pid = fetch_bdl_game_stats(
                int(game_id), bdl_to_mlbam, ctx,
            )
        except Exception as exc:
            log.exception(
                "save_bdl_gamelogs_for_date(%s): game %s stat fetch raised: %s",
                date_str, game_id, exc,
            )
            continue
        log.info(
            "save_bdl_gamelogs_for_date(%s): game %s yielded %d batter rows, "
            "%d pitcher rows",
            date_str, game_id,
            sum(len(v) for v in bat_by_pid.values()),
            sum(len(v) for v in pit_by_pid.values()),
        )
        if bat_by_pid or pit_by_pid:
            try:
                with connection.get_session() as db:
                    for pid, rows in bat_by_pid.items():
                        crud.save_batting_gamelogs(db, pid, rows)
                        total_bat += len(rows)
                    for pid, rows in pit_by_pid.items():
                        crud.save_pitching_gamelogs(db, pid, rows)
                        total_pit += len(rows)
            except Exception as exc:
                log.exception(
                    "save_bdl_gamelogs_for_date(%s): DB upsert for game %s "
                    "raised: %s",
                    date_str, game_id, exc,
                )
        else:
            # Count games where every player was unmapped (or BDL
            # returned an empty stat sheet) so the operator sees
            # the coverage gap.
            skipped += 1

        if i < len(games) - 1:
            time.sleep(_BDL_RATE_LIMIT_SLEEP)

    return {
        "status":                    "ok",
        "date":                      date_str,
        "games":                     len(games),
        "bat_rows":                  total_bat,
        "pit_rows":                  total_pit,
        "skipped_unmapped_players":  skipped,
    }


def backfill_bdl_gamelogs(start_date: str, end_date: str) -> dict:
    """Walk dates from `start_date` through `end_date` (inclusive,
    `yyyy-mm-dd`) and call `save_bdl_gamelogs_for_date` for each.
    Resumable — already-loaded games are no-op upserts. Returns
    aggregated counts plus a per-date breakdown so the caller can
    spot failure days."""
    try:
        start = datetime.date.fromisoformat(start_date)
        end   = datetime.date.fromisoformat(end_date)
    except ValueError:
        return {"status": "bad_date_format"}
    if end < start:
        return {"status": "end_before_start"}

    per_day: list[dict] = []
    total_games = 0
    total_bat   = 0
    total_pit   = 0
    cur = start
    while cur <= end:
        result = save_bdl_gamelogs_for_date(cur.isoformat())
        per_day.append(result)
        total_games += int(result.get("games") or 0)
        total_bat   += int(result.get("bat_rows") or 0)
        total_pit   += int(result.get("pit_rows") or 0)
        cur += datetime.timedelta(days=1)

    return {
        "status":      "ok",
        "start_date":  start_date,
        "end_date":    end_date,
        "total_games": total_games,
        "total_bat_rows": total_bat,
        "total_pit_rows": total_pit,
        "per_day":     per_day,
    }




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
    """Read batting gamelogs from DB. Cache-miss fetch was removed in
    the App-Store-compliance cleanup — gamelogs only land via the
    nightly Phase 4 BDL-game-centric pipeline or the
    `/admin/backfill-bdl-gamelogs` one-shot. Returns reverse-chrono
    game list + splits block; None if no games found in DB."""
    if not connection.db_available():
        return None
    if season is None:
        season = _current_year()

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

    # Stats-source override — current-season only. The override
    # dict can come from BDL (`_fetch_bdl_pitcher_stats` — primary
    # now that the bdl_id mapping is populated) or from the MLB
    # Stats API helper (still used as a fallback for rows missing
    # a bdl_id). Both helpers normalize to the same key shape, so
    # the consumer here doesn't care which fed it.
    #
    # Iterate so None values from the source don't clobber the
    # values bref / bwar wrote above (e.g. holds — bref doesn't
    # carry it). BDL ships FIP under `fielding_fip` which the
    # fetch helper already lifts into the `FIP` key — that's why
    # we accept FIP / K_per9 / WAR directly from the override.
    if mlb_api_stats:
        for key in ("G", "GS", "W", "L", "SV", "IP",
                    "H", "R", "ER", "HR",
                    "BB", "IBB", "SO", "HBP", "BK", "WP",
                    "ERA", "WHIP", "FIP", "K_per9"):
            value = mlb_api_stats.get(key)
            if value is not None:
                entry[key] = value
        # WAR only comes off the override when bwar didn't populate
        # one above — bwar is the canonical source.
        if "WAR" not in entry:
            war = mlb_api_stats.get("WAR")
            if war is not None:
                entry["WAR"] = war
        # Recompute BB/9 and HR/9 from the override counts when IP
        # is non-zero — BDL only ships K/9, so the other per-9 rates
        # need on-the-fly derivation. If the override didn't carry
        # FIP either (MLB Stats API path), reconstruct it from the
        # FIP-component fields.
        ip_dec = mlb_api_stats.get("IP") or 0.0
        if ip_dec:
            so  = mlb_api_stats.get("SO") or 0
            bb  = mlb_api_stats.get("BB") or 0
            hr  = mlb_api_stats.get("HR") or 0
            hbp = mlb_api_stats.get("HBP") or 0
            if "K_per9" not in entry:
                entry["K_per9"] = round(so * 9 / ip_dec, 2)
            entry["BB_per9"] = round(bb * 9 / ip_dec, 2)
            entry["HR_per9"] = round(hr * 9 / ip_dec, 2)
            if "FIP" not in entry:
                entry["FIP"] = _fip(hr, bb, hbp, so, ip_dec)

    return entry


def init_db() -> None:
    """Create database tables if they don't exist. Called once on startup."""
    connection.init_db()
