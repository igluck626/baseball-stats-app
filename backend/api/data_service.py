"""Handles all pybaseball data fetching and merging logic."""

import datetime
import os
import time
from typing import Optional

import pandas as pd
import pybaseball
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

pybaseball.cache.enable()

_CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "3600"))

# ---------------------------------------------------------------------------
# Simple in-memory TTL cache keyed by (function_name, *args)
# ---------------------------------------------------------------------------

_store: dict = {}


def _cached(key: str, fn, ttl: int = _CACHE_TTL):
    entry = _store.get(key)
    if entry and (time.monotonic() - entry["ts"]) < ttl:
        return entry["value"]
    result = fn()
    _store[key] = {"value": result, "ts": time.monotonic()}
    return result


def _batting_bref(year: int) -> pd.DataFrame:
    return _cached(f"batting_bref_{year}", lambda: pybaseball.batting_stats_bref(year))


def _bwar_bat_all() -> pd.DataFrame:
    return _cached("bwar_bat_all", lambda: pybaseball.bwar_bat(return_all=True))


def _pitching_bref(year: int) -> pd.DataFrame:
    return _cached(f"pitching_bref_{year}", lambda: pybaseball.pitching_stats_bref(year))


def _bwar_pitch_all() -> pd.DataFrame:
    return _cached("bwar_pitch_all", lambda: pybaseball.bwar_pitch(return_all=True))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_year() -> int:
    return datetime.date.today().year


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_player(name: str) -> list[dict]:
    """Look up players by name. Accepts 'Last', 'First Last', or 'First Last Jr'.

    Tries the Chadwick Bureau lookup table first; falls back to searching
    bwar_bat by name for players missing from that table (e.g. Yordan Alvarez).
    """
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
                "player_id": pid,
                "name": f"{str(row['name_first']).title()} {str(row['name_last']).title()}",
                "bbref_id": row["key_bbref"],
                "mlb_debut": _safe(row["mlb_played_first"]),
                "mlb_last_season": _safe(row["mlb_played_last"]),
            })

    # Fallback: search bwar_bat by name to catch players absent from lookup table.
    # Require every part to be present so "yordan alvarez" doesn't match "R.J. Alvarez".
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
                "player_id": pid,
                "name": str(group.iloc[-1]["name_common"]),
                "bbref_id": None,
                "mlb_debut": int(years.min()) if not years.empty else None,
                "mlb_last_season": int(years.max()) if not years.empty else None,
            })

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
    """Return season-by-season stats for a player's career."""
    war_df = _bwar_bat_all()
    player_df = (
        war_df[war_df["mlb_ID"] == float(player_id)]
        .sort_values("year_ID")
        .copy()
    )

    if player_df.empty:
        return None

    seasons = []
    for _, r in player_df.iterrows():
        seasons.append({
            "year":           int(r["year_ID"]),
            "team":           str(r["team_ID"]),
            "league":         str(r["lg_ID"]),
            "G":              _safe(r["G"]),
            "PA":             _safe(r["PA"]),
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
        })

    batters = player_df[player_df["pitcher"] == "N"]
    totals_df = batters if not batters.empty else player_df

    return {
        "player_id":  player_id,
        "name":       str(player_df.iloc[0]["name_common"]),
        "seasons":    seasons,
        "career_totals": {
            "seasons":   len(seasons),
            "WAR":       round(float(totals_df["WAR"].sum()), 1),
            "WAR_off":   round(float(totals_df["WAR_off"].sum()), 1),
            "WAR_def":   round(float(totals_df["WAR_def"].sum()), 1),
        },
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
