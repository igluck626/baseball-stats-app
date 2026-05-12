"""Data service.

Public API functions (search_player, get_current_stats, get_career_stats,
get_current_pitching_stats, get_career_pitching_stats) read ONLY from
PostgreSQL. There are no pybaseball calls at request time.

The fetch_and_save_* helpers below pull from pybaseball and persist to the DB.
They are used by bulk_load.py and nightly_update.py only.
"""

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

# ---------------------------------------------------------------------------
# Simple in-memory TTL cache (used only by the fetch_and_save_* helpers).
# ---------------------------------------------------------------------------

_store: dict = {}


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
    """(team_display, team_code) from the player's most recent *_seasons row.
    `team_display` is the raw value as stored; `team_code` is normalized to
    a 2-3 char abbreviation suitable for client-side lookup. Returns
    (None, None) when the player has no season rows."""
    rows = (crud.get_pitcher_seasons(db, player_id) if pitcher
            else crud.get_player_seasons(db, player_id))
    if not rows:
        return None, None
    latest = max(rows, key=lambda r: r.year or 0)
    return latest.team, _resolve_team_code(latest.team, latest.league)


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
        "player_id": player_id,
        "season":    year,
        "bio":       bio,
        "standard":  standard,
        "advanced":  advanced,
    }


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

    seasons_with_counting = [s for s in seasons if s.get("H") is not None]
    career_totals: dict = {
        "seasons": len(seasons),
        "WAR":     round(sum((s.get("WAR")     or 0.0) for s in seasons), 1),
        "WAR_off": round(sum((s.get("WAR_off") or 0.0) for s in seasons), 1),
        "WAR_def": round(sum((s.get("WAR_def") or 0.0) for s in seasons), 1),
    }
    if seasons_with_counting:
        career_totals.update({
            "G":   int(sum(s.get("G")   or 0 for s in seasons_with_counting)),
            "H":   int(sum(s.get("H")   or 0 for s in seasons_with_counting)),
            "HR":  int(sum(s.get("HR")  or 0 for s in seasons_with_counting)),
            "RBI": int(sum(s.get("RBI") or 0 for s in seasons_with_counting)),
        })

    return {
        "player_id":     player_id,
        "name":          name,
        "bio":           bio,
        "seasons":       seasons,
        "career_totals": career_totals,
    }


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
        "player_id": player_id,
        "season":    year,
        "bio":       bio,
        "standard":  standard,
        "advanced":  advanced,
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
        "career_totals": {
            "seasons": len(seasons),
            "WAR":     round(sum((s.get("WAR") or 0.0) for s in seasons), 1),
            "IP":      round(sum((s.get("IP")  or 0.0) for s in seasons), 1),
            "SO":      int(sum(s.get("SO") or 0 for s in seasons)),
            "BB":      int(sum(s.get("BB") or 0 for s in seasons)),
            "W":       int(sum(s.get("W")  or 0 for s in seasons)),
            "L":       int(sum(s.get("L")  or 0 for s in seasons)),
        },
    }


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


def _mlb_get_json(path: str, params: dict) -> dict:
    """GET https://statsapi.mlb.com/api/v1/{path}?... and return parsed JSON.
    Uses urllib (stdlib) — no `requests` dependency."""
    qs  = urllib.parse.urlencode(params)
    url = f"{_MLB_STATS_API}/{path.lstrip('/')}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "baseball-stats-app/1.0"})
    with urllib.request.urlopen(req, timeout=_MLB_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    "ERA":  ("ERA",  "asc",  "IP"),
    "SO":   ("SO",   "desc", None),
    "W":    ("W",    "desc", None),
    "WHIP": ("WHIP", "asc",  "IP"),
    "SV":   ("SV",   "desc", None),
    "IP":   ("IP",   "desc", None),
    "H":    ("H",    "desc", None),
    "BB":   ("BB",   "desc", None),
    "HR":   ("HR",   "desc", None),
    "WAR":  ("WAR",  "desc", None),
    "CG":   ("CG",   "desc", None),
    "SHO":  ("SHO",  "desc", None),
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
        return _leaderboard_career(
            stat=stat, is_batter=is_batter,
            limit=limit, league=league, team=team,
        )
    if mode == "all_time":
        return _leaderboard_all_time(
            stat=stat, is_batter=is_batter,
            limit=limit, league=league, team=team,
        )
    if year is None:
        return None
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


def _leaderboard_career(
    stat: str, is_batter: bool, limit: int,
    league: Optional[str], team: Optional[str],
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
) -> dict:
    """Build a pitcher_seasons row from one year of bwar_pitch + (optionally) bref."""
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

    entry: dict = {
        "year":           year,
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
    }

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
                "IP":    _safe(br["IP"]),
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

    return entry


def init_db() -> None:
    """Create database tables if they don't exist. Called once on startup."""
    connection.init_db()
