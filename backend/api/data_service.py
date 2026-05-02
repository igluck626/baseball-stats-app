"""Handles all pybaseball data fetching and merging logic."""

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
from database.models import PlayerSeason as _PlayerSeason

_PS_COLUMNS = [c.key for c in _PlayerSeason.__table__.columns if c.key != "player_id"]

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
# Simple in-memory TTL cache keyed by string key
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
    """Call fn(), retrying up to `retries` times on failure."""
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

    # wOBA using standard linear weights (2022-24 era approximation).
    # Denominator excludes IBB and SH; uses unintentional BB only.
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
    """Convert a PlayerSeason ORM row to the season dict used by career endpoints."""
    return {k: getattr(row, k) for k in _PS_COLUMNS}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_player(name: str) -> list[dict]:
    """Look up players by name. Accepts 'Last', 'First Last', or 'First Last Jr'.

    Queries the PostgreSQL players table first (fast). Falls back to the
    Chadwick Bureau lookup and bwar_bat if the DB has no results (e.g. player
    not yet loaded, or DB unavailable). Results from the fallback are persisted
    to the players table for future queries.
    """
    # 1. Try the database first.
    if connection.db_available():
        try:
            with connection.get_session() as db:
                rows = crud.search_players_by_name(db, name)
                if rows:
                    return [
                        {
                            "player_id":      r.player_id,
                            "name":           r.name,
                            "bbref_id":       r.bbref_id,
                            "mlb_debut":      r.mlb_debut,
                            "mlb_last_season": r.mlb_last_season,
                        }
                        for r in rows
                    ]
        except Exception:
            pass

    # 2. Fall back to pybaseball (Chadwick Bureau + bwar_bat).
    parts = name.strip().split()
    last = parts[-1]
    first = parts[0] if len(parts) >= 2 else None

    try:
        lookup = (
            pybaseball.playerid_lookup(last, first)
            if first
            else pybaseball.playerid_lookup(last)
        )
    except Exception:
        lookup = pd.DataFrame()

    seen_ids: set = set()
    out: list[dict] = []

    if not lookup.empty:
        for _, row in lookup.iterrows():
            if pd.isna(row["key_mlbam"]):
                continue
            pid = int(row["key_mlbam"])
            seen_ids.add(pid)
            out.append({
                "player_id":      pid,
                "name":           f"{str(row['name_first']).title()} {str(row['name_last']).title()}",
                "bbref_id":       row["key_bbref"],
                "mlb_debut":      _safe(row["mlb_played_first"]),
                "mlb_last_season": _safe(row["mlb_played_last"]),
            })

    # bwar_bat fallback for players absent from the Chadwick table (e.g. Yordan Alvarez).
    # Require every significant word to match so "yordan alvarez" ≠ "R.J. Alvarez".
    war_df = _bwar_bat_all()
    significant = [p for p in parts if len(p) > 2]
    if significant:
        mask = pd.Series(True, index=war_df.index)
        for p in significant:
            mask &= war_df["name_common"].str.contains(p, case=False, na=False, regex=False)
        matches = war_df[mask]
        for mlb_id, group in matches.groupby("mlb_ID"):
            if pd.isna(mlb_id):
                continue
            pid = int(mlb_id)
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            years = group["year_ID"].dropna()
            out.append({
                "player_id":      pid,
                "name":           str(group.iloc[-1]["name_common"]),
                "bbref_id":       None,
                "mlb_debut":      int(years.min()) if not years.empty else None,
                "mlb_last_season": int(years.max()) if not years.empty else None,
            })

    # 3. Persist fallback results so the same search is fast next time.
    if out and connection.db_available():
        try:
            with connection.get_session() as db:
                for player in out:
                    crud.save_player(db, player)
        except Exception:
            pass

    return out


def get_current_stats(player_id: int) -> Optional[dict]:
    """Return standard + advanced stats for a player for the current season."""
    year = _current_year()
    batting = _batting_bref(year)
    war_df = _bwar_bat_all()

    std_row = batting[batting["mlbID"] == player_id]
    adv_row = war_df[
        (war_df["mlb_ID"] == float(player_id)) & (war_df["year_ID"] == year)
    ]

    if std_row.empty and adv_row.empty:
        return None

    result: dict = {"player_id": player_id, "season": year}

    if not std_row.empty:
        r = std_row.iloc[0]
        result["standard"] = {
            "name":    str(r["Name"]),
            "team":    str(r["Tm"]),
            "G":       _safe(r["G"]),
            "PA":      _safe(r["PA"]),
            "AB":      _safe(r["AB"]),
            "R":       _safe(r["R"]),
            "H":       _safe(r["H"]),
            "doubles": _safe(r["2B"]),
            "triples": _safe(r["3B"]),
            "HR":      _safe(r["HR"]),
            "RBI":     _safe(r["RBI"]),
            "BB":      _safe(r["BB"]),
            "IBB":     _safe(r["IBB"]),
            "SO":      _safe(r["SO"]),
            "HBP":     _safe(r["HBP"]),
            "SB":      _safe(r["SB"]),
            "CS":      _safe(r["CS"]),
            "BA":      _safe(r["BA"]),
            "OBP":     _safe(r["OBP"]),
            "SLG":     _safe(r["SLG"]),
            "OPS":     _safe(r["OPS"]),
            **_batting_derived(r),
        }

    if not adv_row.empty:
        r = adv_row.iloc[0]
        result["advanced"] = {
            "WAR":            _safe(r["WAR"]),
            "WAR_off":        _safe(r["WAR_off"]),
            "WAR_def":        _safe(r["WAR_def"]),
            "WAA":            _safe(r["WAA"]),
            "OPS_plus":       _safe(r["OPS_plus"]),
            "runs_bat":       _safe(r["runs_bat"]),
            "runs_baserunning": _safe(r["runs_br"]),
            "runs_defense":   _safe(r["runs_defense"]),
            "runs_above_avg": _safe(r["runs_above_avg"]),
            "runs_above_rep": _safe(r["runs_above_rep"]),
            "runs_position":  _safe(r["runs_position"]),
        }

    return result


def get_career_stats(player_id: int) -> Optional[dict]:
    """Return season-by-season batting stats and WAR for a player's career.

    Historical seasons (year < current) are stored in PostgreSQL after first
    fetch so subsequent calls skip the Baseball Reference requests entirely.
    """
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

    # Pull already-stored historical seasons from PostgreSQL.
    db_seasons: dict[int, dict] = {}
    if connection.db_available():
        try:
            with connection.get_session() as db:
                for row in crud.get_player_seasons(db, player_id):
                    db_seasons[row.year] = _db_row_to_season(row)
        except Exception:
            pass

    # Only fetch from Baseball Reference for years not yet in DB, plus the
    # current season (which is never stored and always re-fetched).
    years_to_fetch = [
        y for y in career_years
        if y == current or (y < current and y not in db_seasons)
    ]
    bref_by_year: dict = {}
    for i, y in enumerate(sorted(years_to_fetch)):
        try:
            bref_by_year[y] = _batting_bref(y)
        except Exception:
            pass
        if i < len(years_to_fetch) - 1:
            time.sleep(0.3)

    seasons: list[dict] = []
    new_historical: list[dict] = []  # newly fetched historical seasons to persist

    for year_id, group in player_war.groupby("year_ID"):
        year = int(year_id)

        # Historical season already in DB — use it directly, no bref merge needed.
        if year < current and year in db_seasons:
            seasons.append(db_seasons[year])
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

        seasons.append(entry)
        if year < current:
            new_historical.append(entry)

    # Persist newly fetched historical seasons to PostgreSQL.
    if new_historical and connection.db_available():
        try:
            with connection.get_session() as db:
                crud.save_player_seasons(db, player_id, new_historical)
        except Exception:
            pass

    # Career totals: WAR from bwar_bat, counting stats summed across all seasons.
    batters = player_war[player_war["pitcher"] == "N"]
    war_totals = batters if not batters.empty else player_war

    career_totals: dict = {
        "seasons": len(seasons),
        "WAR":     round(float(war_totals["WAR"].sum()), 1),
        "WAR_off": round(float(war_totals["WAR_off"].sum()), 1),
        "WAR_def": round(float(war_totals["WAR_def"].sum()), 1),
    }
    seasons_with_counting = [s for s in seasons if s.get("H") is not None]
    if seasons_with_counting:
        career_totals.update({
            "G":   int(sum(s.get("G") or 0 for s in seasons_with_counting)),
            "H":   int(sum(s.get("H") or 0 for s in seasons_with_counting)),
            "HR":  int(sum(s.get("HR") or 0 for s in seasons_with_counting)),
            "RBI": int(sum(s.get("RBI") or 0 for s in seasons_with_counting)),
        })

    # Persist basic player info so future name searches are served from DB.
    if connection.db_available():
        try:
            with connection.get_session() as db:
                crud.save_player(db, {
                    "player_id":      player_id,
                    "name":           str(player_war.iloc[0]["name_common"]),
                    "bbref_id":       None,
                    "mlb_debut":      min(s["year"] for s in seasons) if seasons else None,
                    "mlb_last_season": max(s["year"] for s in seasons) if seasons else None,
                })
        except Exception:
            pass

    return {
        "player_id": player_id,
        "name":      str(player_war.iloc[0]["name_common"]),
        "seasons":   seasons,
        "career_totals": career_totals,
    }


def get_current_pitching_stats(player_id: int) -> Optional[dict]:
    """Return standard + advanced pitching stats for the current season."""
    year = _current_year()
    pitching = _pitching_bref(year)
    war_df = _bwar_pitch_all()

    # pitching_stats_bref stores mlbID as string
    std_row = pitching[pitching["mlbID"] == str(player_id)]
    adv_row = war_df[
        (war_df["mlb_ID"] == float(player_id)) & (war_df["year_ID"] == year)
    ]

    if std_row.empty and adv_row.empty:
        return None

    result: dict = {"player_id": player_id, "season": year}

    if not std_row.empty:
        r = std_row.iloc[0]
        ip_dec = _ip_to_decimal(r["IP"])
        result["standard"] = {
            "name": str(r["Name"]),
            "team": str(r["Tm"]),
            "G":    _safe(r["G"]),
            "GS":   _safe(r["GS"]),
            "W":    _safe(r["W"]),
            "L":    _safe(r["L"]),
            "SV":   _safe(r["SV"]),
            "IP":   _safe(r["IP"]),
            "SO":   _safe(r["SO"]),
            "BB":   _safe(r["BB"]),
            "HR":   _safe(r["HR"]),
            "HBP":  _safe(r["HBP"]),
            "ERA":  _safe(r["ERA"]),
            "WHIP": _safe(r["WHIP"]),
            "FIP":  _fip(
                float(r["HR"])  if pd.notna(r["HR"])  else 0.0,
                float(r["BB"])  if pd.notna(r["BB"])  else 0.0,
                float(r["HBP"]) if pd.notna(r["HBP"]) else 0.0,
                float(r["SO"])  if pd.notna(r["SO"])  else 0.0,
                ip_dec,
            ),
        }

    if not adv_row.empty:
        r = adv_row.iloc[0]
        result["advanced"] = {
            "WAR":            _safe(r["WAR"]),
            "WAA":            _safe(r["WAA"]),
            "ERA_plus":       _safe(r["ERA_plus"]),
            "runs_above_avg": _safe(r["runs_above_avg"]),
            "runs_above_rep": _safe(r["runs_above_rep"]),
            "xRA":            _safe(r["xRA"]),
            "xRA_final":      _safe(r["xRA_final"]),
        }

    return result


def init_db() -> None:
    """Create database tables if they don't exist. Called once on startup."""
    connection.init_db()


def get_career_pitching_stats(player_id: int) -> Optional[dict]:
    """Return season-by-season pitching WAR and stats for a player's career."""
    war_df = _bwar_pitch_all()
    player_df = (
        war_df[war_df["mlb_ID"] == float(player_id)]
        .sort_values("year_ID")
        .copy()
    )

    if player_df.empty:
        return None

    seasons = []
    for _, r in player_df.iterrows():
        ip = round(float(r["IPouts"]) / 3, 1) if pd.notna(r["IPouts"]) else None
        seasons.append({
            "year":           int(r["year_ID"]),
            "team":           str(r["team_ID"]),
            "league":         str(r["lg_ID"]),
            "G":              _safe(r["G"]),
            "GS":             _safe(r["GS"]),
            "IP":             ip,
            "WAR":            _safe(r["WAR"]),
            "WAA":            _safe(r["WAA"]),
            "ERA_plus":       _safe(r["ERA_plus"]),
            "runs_above_avg": _safe(r["runs_above_avg"]),
            "runs_above_rep": _safe(r["runs_above_rep"]),
        })

    return {
        "player_id": player_id,
        "name":      str(player_df.iloc[0]["name_common"]),
        "seasons":   seasons,
        "career_totals": {
            "seasons": len(seasons),
            "WAR":     round(float(player_df["WAR"].sum()), 1),
            "IP":      round(float(player_df["IPouts"].sum()) / 3, 1),
        },
    }
