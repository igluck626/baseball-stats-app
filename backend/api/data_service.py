"""Data service.

Public API functions (search_player, get_current_stats, get_career_stats,
get_current_pitching_stats, get_career_pitching_stats) read ONLY from
PostgreSQL. There are no pybaseball calls at request time.

The fetch_and_save_* helpers below pull from pybaseball and persist to the DB.
They are used by bulk_load.py and nightly_update.py only.
"""

import datetime
import os
import sys
import time
from typing import Optional

import pandas as pd
import pybaseball
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Allow `from database import ...` whether running from /app/api (Docker)
# or backend/api (local venv).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
                by_id[r.player_id] = {
                    "player_id":       r.player_id,
                    "name":            r.name,
                    "bbref_id":        r.bbref_id,
                    "mlb_debut":       r.mlb_debut,
                    "mlb_last_season": r.mlb_last_season,
                    **_bio_dict(r, db),
                }
            # Players (batters) take priority for two-way players.
            for r in crud.search_players_by_name(db, name):
                by_id[r.player_id] = {
                    "player_id":       r.player_id,
                    "name":            r.name,
                    "bbref_id":        r.bbref_id,
                    "mlb_debut":       r.mlb_debut,
                    "mlb_last_season": r.mlb_last_season,
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
        "W":     season.get("W"),
        "L":     season.get("L"),
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
                "W":     _safe(br["W"]),
                "L":     _safe(br["L"]),
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
