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
                }
            # Players (batters) take priority for two-way players.
            for r in crud.search_players_by_name(db, name):
                by_id[r.player_id] = {
                    "player_id":       r.player_id,
                    "name":            r.name,
                    "bbref_id":        r.bbref_id,
                    "mlb_debut":       r.mlb_debut,
                    "mlb_last_season": r.mlb_last_season,
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
        rows = [r for r in crud.get_player_seasons(db, player_id) if r.year == year]
        player = crud.get_player(db, player_id)

    if not rows:
        return None

    season = _db_row_to_season(rows[0])
    name = player.name if player else None

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
        "SO":      season.get("SO"),
        "SB":      season.get("SB"),
        "CS":      season.get("CS"),
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
        "standard":  standard,
        "advanced":  advanced,
    }


def get_career_stats(player_id: int) -> Optional[dict]:
    """Return season-by-season batting stats for a player's career, from DB only."""
    if not connection.db_available():
        return None

    with connection.get_session() as db:
        rows = crud.get_player_seasons(db, player_id)
        player = crud.get_player(db, player_id)

    if not rows:
        return None

    seasons = [_db_row_to_season(r) for r in sorted(rows, key=lambda r: r.year)]

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
        "name":          player.name if player else None,
        "seasons":       seasons,
        "career_totals": career_totals,
    }


def get_current_pitching_stats(player_id: int) -> Optional[dict]:
    """Return current-season pitching stats for a player, from PostgreSQL only."""
    if not connection.db_available():
        return None

    year = _current_year()
    with connection.get_session() as db:
        rows = [r for r in crud.get_pitcher_seasons(db, player_id) if r.year == year]
        pitcher = crud.get_pitcher(db, player_id)

    if not rows:
        return None

    season = _db_pitcher_row_to_season(rows[0])
    name = pitcher.name if pitcher else None

    standard = {
        "name":  name,
        "team":  season.get("team"),
        "G":     season.get("G"),
        "GS":    season.get("GS"),
        "W":     season.get("W"),
        "L":     season.get("L"),
        "IP":    season.get("IP"),
        "SO":    season.get("SO"),
        "BB":    season.get("BB"),
        "HR":    season.get("HR"),
        "ERA":   season.get("ERA"),
        "WHIP":  season.get("WHIP"),
        "FIP":   season.get("FIP"),
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
        "standard":  standard,
        "advanced":  advanced,
    }


def get_career_pitching_stats(player_id: int) -> Optional[dict]:
    """Return season-by-season pitching stats for a player's career, from DB only."""
    if not connection.db_available():
        return None

    with connection.get_session() as db:
        rows = crud.get_pitcher_seasons(db, player_id)
        pitcher = crud.get_pitcher(db, player_id)

    if not rows:
        return None

    seasons = [_db_pitcher_row_to_season(r) for r in sorted(rows, key=lambda r: r.year)]

    return {
        "player_id":     player_id,
        "name":          pitcher.name if pitcher else None,
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
# Pybaseball fetchers — used by bulk_load and nightly_update only
# ---------------------------------------------------------------------------

def fetch_and_save_batting_career(player_id: int) -> Optional[str]:
    """Fetch a batter's full career from pybaseball and persist to PostgreSQL.

    Returns the player's name if data was found, else None. The returned name
    is taken from bwar_bat's name_common field.
    """
    if not connection.db_available():
        return None

    current = _current_year()
    war_df = _bwar_bat_all()
    player_war = (
        war_df[war_df["mlb_ID"] == float(player_id)]
        .sort_values(["year_ID", "stint_ID"])
        .copy()
    )

    if player_war.empty:
        return None

    career_years = sorted(int(y) for y in player_war["year_ID"].dropna().unique())

    # Skip years already in the DB (re-runs are cheap).
    with connection.get_session() as db:
        existing_years = {
            r.year for r in crud.get_player_seasons(db, player_id)
        }

    years_to_fetch = [
        y for y in career_years
        if y == current or (y < current and y not in existing_years)
    ]

    bref_by_year: dict = {}
    for i, y in enumerate(sorted(years_to_fetch)):
        if y < 2008:
            # batting_stats_bref only supports 2008+; older years store WAR-only.
            continue
        try:
            bref_by_year[y] = _batting_bref(y)
        except Exception:
            pass
        if i < len(years_to_fetch) - 1:
            time.sleep(0.3)

    new_seasons: list[dict] = []
    for year_id, group in player_war.groupby("year_ID"):
        year = int(year_id)
        if year in existing_years and year != current:
            continue

        total_pa = group["PA"].sum()
        ops_plus_vals = group["OPS_plus"].dropna()
        ops_plus = (
            float((group["OPS_plus"] * group["PA"]).sum() / total_pa)
            if total_pa > 0 and not ops_plus_vals.empty
            else None
        )

        raw_team = str(group.iloc[-1]["team_ID"])
        entry: dict = {
            "year":           year,
            "team":           _TEAM_DISPLAY.get(raw_team, raw_team),
            "league":         str(group.iloc[-1]["lg_ID"]),
            "WAR":            round(float(group["WAR"].sum()), 2),
            "WAR_off":        round(float(group["WAR_off"].sum()), 2),
            "WAR_def":        round(float(group["WAR_def"].sum()), 2),
            "WAA":            round(float(group["WAA"].sum()), 2),
            "OPS_plus":       round(ops_plus, 1) if ops_plus is not None else None,
            "runs_above_avg": round(float(group["runs_above_avg"].sum()), 2),
            "runs_above_rep": round(float(group["runs_above_rep"].sum()), 2),
        }

        bref_df = bref_by_year.get(year)
        if bref_df is not None and not bref_df.empty:
            player_bref = bref_df[bref_df["mlbID"] == player_id]
            if not player_bref.empty:
                br = player_bref.iloc[0]
                entry["team"] = str(br["Tm"])
                entry.update({
                    "G":       _safe(br["G"]),
                    "PA":      _safe(br["PA"]),
                    "AB":      _safe(br["AB"]),
                    "R":       _safe(br["R"]),
                    "H":       _safe(br["H"]),
                    "doubles": _safe(br["2B"]),
                    "triples": _safe(br["3B"]),
                    "HR":      _safe(br["HR"]),
                    "RBI":     _safe(br["RBI"]),
                    "BB":      _safe(br["BB"]),
                    "SO":      _safe(br["SO"]),
                    "SB":      _safe(br["SB"]),
                    "CS":      _safe(br["CS"]),
                    "BA":      _safe(br["BA"]),
                    "OBP":     _safe(br["OBP"]),
                    "SLG":     _safe(br["SLG"]),
                    "OPS":     _safe(br["OPS"]),
                    **_batting_derived(br),
                })

        new_seasons.append(entry)

    name = str(player_war.iloc[0]["name_common"])

    with connection.get_session() as db:
        if new_seasons:
            crud.save_player_seasons(db, player_id, new_seasons)
        crud.save_player(db, {
            "player_id":       player_id,
            "name":            name,
            "bbref_id":        None,
            "mlb_debut":       int(min(career_years)),
            "mlb_last_season": int(max(career_years)),
        })

    return name


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
            })

    return entry


def fetch_and_save_pitching_career(player_id: int) -> Optional[str]:
    """Fetch a pitcher's full career from pybaseball and persist to PostgreSQL.

    Returns the pitcher's name if data was found, else None.
    """
    if not connection.db_available():
        return None

    current = _current_year()
    war_df = _bwar_pitch_all()
    pitcher_war = (
        war_df[war_df["mlb_ID"] == float(player_id)]
        .sort_values(["year_ID", "stint_ID"] if "stint_ID" in war_df.columns else "year_ID")
        .copy()
    )

    if pitcher_war.empty:
        return None

    career_years = sorted(int(y) for y in pitcher_war["year_ID"].dropna().unique())

    with connection.get_session() as db:
        existing_years = {
            r.year for r in crud.get_pitcher_seasons(db, player_id)
        }

    years_to_fetch = [
        y for y in career_years
        if y == current or (y < current and y not in existing_years)
    ]

    bref_by_year: dict = {}
    for i, y in enumerate(sorted(years_to_fetch)):
        if y < 2008:
            continue
        try:
            bref_by_year[y] = _pitching_bref(y)
        except Exception:
            pass
        if i < len(years_to_fetch) - 1:
            time.sleep(0.3)

    new_seasons: list[dict] = []
    for year_id, group in pitcher_war.groupby("year_ID"):
        year = int(year_id)
        if year in existing_years and year != current:
            continue
        new_seasons.append(
            _build_pitcher_season_entry(player_id, year, group, bref_by_year.get(year))
        )

    name = str(pitcher_war.iloc[0]["name_common"])

    with connection.get_session() as db:
        if new_seasons:
            crud.save_pitcher_seasons(db, player_id, new_seasons)
        crud.save_pitcher(db, {
            "player_id":       player_id,
            "name":            name,
            "bbref_id":        None,
            "mlb_debut":       int(min(career_years)),
            "mlb_last_season": int(max(career_years)),
        })

    return name


def init_db() -> None:
    """Create database tables if they don't exist. Called once on startup."""
    connection.init_db()
