#!/usr/bin/env python3
"""Load Lahman historical batting/pitching stats into PostgreSQL.

Lahman is the canonical source for every completed season. We load every
year STRICTLY less than the current year — pybaseball handles the in-flight
season via nightly_update.py. After each season ends, Lahman is re-released
with the just-completed year and this loader picks it up on the next run.

Joins Lahman.playerID → Chadwick key_bbref → key_mlbam (our player_id).
Players whose Chadwick row lacks a key_mlbam are skipped (a small tail of
obscure 19th-century guys with no MLBAM ID assigned).

Idempotent: rows already in the database are skipped.
"""

import argparse
import csv
import datetime
import logging
import os
import re
import sys
import threading
from collections import defaultdict
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup — allow imports from backend/api and backend/database
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

from database import connection, crud                                     # noqa: E402
from database.models import (                                              # noqa: E402
    PitcherSeason, PlayerFielding, PlayerSeason, TeamSeason,
)

LAHMAN_DIR              = os.path.join(_BACKEND_DIR, "data", "lahman")
BATTING_CSV             = os.path.join(LAHMAN_DIR, "Batting.csv")
PITCHING_CSV            = os.path.join(LAHMAN_DIR, "Pitching.csv")
PEOPLE_CSV              = os.path.join(LAHMAN_DIR, "People.csv")
CHADWICK_CSV            = os.path.join(LAHMAN_DIR, "chadwick_mlb.csv")
FIELDING_CSV            = os.path.join(LAHMAN_DIR, "Fielding.csv")
AWARDS_CSV              = os.path.join(LAHMAN_DIR, "AwardsPlayers.csv")
ALLSTAR_CSV             = os.path.join(LAHMAN_DIR, "AllstarFull.csv")
BATTING_POST_CSV        = os.path.join(LAHMAN_DIR, "BattingPost.csv")
PITCHING_POST_CSV       = os.path.join(LAHMAN_DIR, "PitchingPost.csv")
TEAMS_CSV               = os.path.join(LAHMAN_DIR, "Teams.csv")
HOF_CSV                 = os.path.join(LAHMAN_DIR, "HallOfFame.csv")

# Load Lahman data only for years STRICTLY less than this — i.e. every
# completed season. Pybaseball owns the current season; after it ends, Lahman
# is re-released with the new year and the cutoff naturally rolls forward.
CUTOFF_YEAR = datetime.date.today().year
_SAVE_BATCH = 200    # players per DB transaction during the save loops

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional shared-state plumbing
# ---------------------------------------------------------------------------
# When run() is invoked from a background thread (e.g. main.py's
# /admin/lahman-load endpoint), the caller passes a state dict + lock so the
# status endpoint can read live progress. Standalone runs pass nothing and
# only log to stdout.

def _set_state(state: Optional[dict], lock: Optional[threading.Lock], **kwargs) -> None:
    if state is None:
        return
    if lock is not None:
        with lock:
            state.update(kwargs)
    else:
        state.update(kwargs)


# ---------------------------------------------------------------------------
# CSV-cell helpers
# ---------------------------------------------------------------------------

def _i(v) -> int:
    """Lahman CSV cell → int (blank → 0)."""
    if v is None or v == "":
        return 0
    return int(float(v))


def _f(v) -> float:
    """Lahman CSV cell → float (blank → 0.0)."""
    if v is None or v == "":
        return 0.0
    return float(v)


def _i_or_none(v):
    if v is None or v == "":
        return None
    return int(float(v))


# Defensive parsers for CSVs known to contain malformed cells (the AllstarFull
# data has rows where a numeric column appears as e.g. "9;9", which crashes
# float()). Take the first leading numeric token; fall back to the empty/None
# behavior of the strict parsers above.
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _clean_num(v) -> Optional[str]:
    """Extract the first leading numeric token from a possibly-dirty cell.

    Examples:
        "9;9"   → "9"
        "9.5"   → "9.5"
        "9 4 5" → "9"
        "abc9"  → None
        ""      → None
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = _NUM_RE.match(s)
    return m.group(0) if m else None


def _i_safe(v) -> int:
    n = _clean_num(v)
    return int(float(n)) if n is not None else 0


def _f_safe(v) -> float:
    n = _clean_num(v)
    return float(n) if n is not None else 0.0


def _i_or_none_safe(v):
    n = _clean_num(v)
    return int(float(n)) if n is not None else None


def _f_or_none_safe(v):
    n = _clean_num(v)
    return float(n) if n is not None else None


# ---------------------------------------------------------------------------
# Chadwick bbref → mlbam bridge
# ---------------------------------------------------------------------------

def _load_chadwick_bridge() -> dict[str, int]:
    """key_bbref → key_mlbam dict, loaded from the trimmed Chadwick file."""
    bridge: dict[str, int] = {}
    with open(CHADWICK_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            bbref = row["key_bbref"]
            mlbam = row["key_mlbam"]
            if bbref and mlbam:
                bridge[bbref] = int(mlbam)
    return bridge


# ---------------------------------------------------------------------------
# Derived-stat formulas
# ---------------------------------------------------------------------------

def _round(v, places=3):
    return round(v, places) if v is not None else None


def _batting_derived(ab: float, h: float, doubles: float, triples: float, hr: float,
                     bb: float, ibb: float, hbp: float, so: float, sf: float, sh: float) -> dict:
    """Compute PA + slash line + advanced stats from Lahman counting stats.

    PA = AB + BB + HBP + SF + SH
    Standard sabermetric formulas; when a denominator is zero, returns None.
    """
    pa = ab + bb + hbp + sf + sh

    ba   = h / ab                                      if ab > 0 else None
    obp  = (h + bb + hbp) / (ab + bb + hbp + sf)       if (ab + bb + hbp + sf) > 0 else None
    slg  = (h + doubles + 2 * triples + 3 * hr) / ab   if ab > 0 else None
    ops  = (obp + slg)                                 if (obp is not None and slg is not None) else None

    babip_denom = ab - so - hr + sf
    babip = (h - hr) / babip_denom                     if babip_denom > 0 else None

    iso = (slg - ba)                                   if (slg is not None and ba is not None) else None

    bb_pct = bb / pa                                   if pa > 0 else None
    k_pct  = so / pa                                   if pa > 0 else None

    woba_num = (0.69 * bb + 0.72 * hbp + 0.89 * h
                + 1.27 * doubles + 1.62 * triples + 2.10 * hr)
    woba_den = ab + bb - ibb + sf + hbp
    woba = woba_num / woba_den                         if woba_den > 0 else None

    return {
        "PA":     int(pa),
        "BA":     _round(ba),
        "OBP":    _round(obp),
        "SLG":    _round(slg),
        "OPS":    _round(ops),
        "BABIP":  _round(babip),
        "ISO":    _round(iso),
        "BB_pct": _round(bb_pct),
        "K_pct":  _round(k_pct),
        "wOBA":   _round(woba),
    }


def _pitching_derived(ipouts: float, h: float, hr: float, bb: float, hbp: float,
                      so: float, bfp: float) -> dict:
    """Compute IP + WHIP/FIP/per-9/BABIP from Lahman pitching counting stats.

    IP = IPouts / 3 (decimal innings, not the .1/.2 baseball notation)
    """
    ip = ipouts / 3 if ipouts > 0 else 0.0

    whip = (bb + h) / ip                if ip > 0 else None
    fip  = (13 * hr + 3 * (bb + hbp) - 2 * so) / ip + 3.10  if ip > 0 else None

    k_per9  = so * 9 / ip               if ip > 0 else None
    bb_per9 = bb * 9 / ip               if ip > 0 else None
    hr_per9 = hr * 9 / ip               if ip > 0 else None

    babip_denom = bfp - so - hr - bb
    babip = (h - hr) / babip_denom      if babip_denom > 0 else None

    return {
        "IP":      round(ip, 1) if ip > 0 else None,
        "WHIP":    _round(whip),
        "FIP":     _round(fip, 2),
        "K_per9":  _round(k_per9, 2),
        "BB_per9": _round(bb_per9, 2),
        "HR_per9": _round(hr_per9, 2),
        "BABIP":   _round(babip),
    }


# ---------------------------------------------------------------------------
# Aggregation across stints
# ---------------------------------------------------------------------------
# Lahman stores one row per (player, year, stint), where a "stint" is a span
# with one team. A traded player has multiple stints in one year. We sum
# counting stats across stints and use the LAST stint's team/league.

def _read_batting_aggregated() -> dict[tuple[str, int], dict]:
    """Returns {(playerID, yearID): aggregated_stats_dict} for years < CUTOFF_YEAR."""
    by_key: dict[tuple[str, int], dict] = {}
    with open(BATTING_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            year = int(row["yearID"])
            if year >= CUTOFF_YEAR:
                continue
            key = (row["playerID"], year)
            stint = int(row["stint"])

            agg = by_key.setdefault(key, {
                "stint":   0,
                "teamID":  "",
                "lgID":    "",
                "G": 0, "AB": 0, "R": 0, "H": 0, "2B": 0, "3B": 0, "HR": 0,
                "RBI": 0.0, "SB": 0.0, "CS": 0.0, "BB": 0, "SO": 0.0,
                "IBB": 0.0, "HBP": 0.0, "SH": 0.0, "SF": 0.0,
                "GIDP": 0.0,
            })

            # Sum counting stats
            agg["G"]    += _i(row["G"])
            agg["AB"]   += _i(row["AB"])
            agg["R"]    += _i(row["R"])
            agg["H"]    += _i(row["H"])
            agg["2B"]   += _i(row["2B"])
            agg["3B"]   += _i(row["3B"])
            agg["HR"]   += _i(row["HR"])
            agg["RBI"]  += _f(row["RBI"])
            agg["SB"]   += _f(row["SB"])
            agg["CS"]   += _f(row["CS"])
            agg["BB"]   += _i(row["BB"])
            agg["SO"]   += _f(row["SO"])
            agg["IBB"]  += _f(row["IBB"])
            agg["HBP"]  += _f(row["HBP"])
            agg["SH"]   += _f(row["SH"])
            agg["SF"]   += _f(row["SF"])
            agg["GIDP"] += _f(row.get("GIDP"))

            # Latest stint's team is the player's "final" team that year
            if stint >= agg["stint"]:
                agg["stint"]  = stint
                agg["teamID"] = row["teamID"]
                agg["lgID"]   = row["lgID"]

    return by_key


def _read_pitching_aggregated() -> dict[tuple[str, int], dict]:
    """Returns {(playerID, yearID): aggregated_stats_dict} for years < CUTOFF_YEAR."""
    by_key: dict[tuple[str, int], dict] = {}
    with open(PITCHING_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            year = int(row["yearID"])
            if year >= CUTOFF_YEAR:
                continue
            key = (row["playerID"], year)
            stint = int(row["stint"])

            agg = by_key.setdefault(key, {
                "stint":  0,
                "teamID": "",
                "lgID":   "",
                "W": 0, "L": 0, "G": 0, "GS": 0,
                "CG": 0, "SHO": 0, "SV": 0,
                "IPouts": 0,
                "H": 0, "ER": 0, "R": 0, "HR": 0,
                "BB": 0, "SO": 0,
                "ERA_num": 0.0,    # ER * 9 (numerator for combined ERA)
                "IBB": 0.0,
                "WP": 0,
                "BFP": 0.0,
                "HBP": 0.0,
                "BK": 0,
                "GF": 0,
                "SH": 0.0, "SF": 0.0, "GIDP": 0.0,
            })

            agg["W"]      += _i(row["W"])
            agg["L"]      += _i(row["L"])
            agg["G"]      += _i(row["G"])
            agg["GS"]     += _i(row["GS"])
            agg["CG"]     += _i(row.get("CG"))
            agg["SHO"]    += _i(row.get("SHO"))
            agg["SV"]     += _i(row.get("SV"))
            agg["IPouts"] += _i(row["IPouts"])
            agg["H"]      += _i(row["H"])
            agg["ER"]     += _i(row["ER"])
            agg["R"]      += _i(row.get("R"))
            agg["HR"]     += _i(row["HR"])
            agg["BB"]     += _i(row["BB"])
            agg["SO"]     += _i(row["SO"])
            agg["IBB"]    += _f(row.get("IBB"))
            agg["WP"]     += _i(row.get("WP"))
            agg["BFP"]    += _f(row["BFP"])
            agg["HBP"]    += _f(row["HBP"])
            agg["BK"]     += _i(row.get("BK"))
            agg["GF"]     += _i(row.get("GF"))
            agg["SH"]     += _f(row.get("SH"))
            agg["SF"]     += _f(row.get("SF"))
            agg["GIDP"]   += _f(row.get("GIDP"))

            if stint >= agg["stint"]:
                agg["stint"]  = stint
                agg["teamID"] = row["teamID"]
                agg["lgID"]   = row["lgID"]

    return by_key


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_batting(
    bridge: dict[str, int],
    existing_keys: set[tuple[int, int]],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> tuple[int, int, int]:
    """Aggregate Lahman batting and upsert pre-2008 seasons.

    Returns (saved, skipped_existing, skipped_no_id).
    """
    _set_state(state, lock, phase="batting")
    log.info(f"Reading {BATTING_CSV} ...")
    aggregated = _read_batting_aggregated()
    log.info(f"  {len(aggregated):,} (player, year) entries pre-{CUTOFF_YEAR}")

    by_player_id: dict[int, list[dict]] = defaultdict(list)
    skipped_no_id = 0
    skipped_existing = 0

    for (lahman_id, year), agg in aggregated.items():
        mlbam = bridge.get(lahman_id)
        if mlbam is None:
            skipped_no_id += 1
            continue
        if (mlbam, year) in existing_keys:
            skipped_existing += 1
            continue

        derived = _batting_derived(
            ab=agg["AB"], h=agg["H"],
            doubles=agg["2B"], triples=agg["3B"], hr=agg["HR"],
            bb=agg["BB"], ibb=agg["IBB"], hbp=agg["HBP"],
            so=agg["SO"], sf=agg["SF"], sh=agg["SH"],
        )

        season = {
            "year":    year,
            "team":    agg["teamID"] or None,
            "league":  agg["lgID"] or None,
            "G":       agg["G"],
            "AB":      agg["AB"],
            "R":       agg["R"],
            "H":       agg["H"],
            "doubles": agg["2B"],
            "triples": agg["3B"],
            "HR":      agg["HR"],
            "RBI":     int(agg["RBI"]),
            "SB":      int(agg["SB"]),
            "CS":      int(agg["CS"]),
            "BB":      agg["BB"],
            "SO":      int(agg["SO"]),
            "IBB":     int(agg["IBB"]),
            "HBP":     int(agg["HBP"]),
            "SH":      int(agg["SH"]),
            "SF":      int(agg["SF"]),
            "GIDP":    int(agg["GIDP"]),
            **derived,
        }
        by_player_id[mlbam].append(season)

    total_rows = sum(len(v) for v in by_player_id.values())
    log.info(f"  saving {total_rows:,} batting rows for {len(by_player_id):,} batters ...")
    _set_state(
        state, lock,
        batting_rows_total=total_rows,
        batting_skipped_existing=skipped_existing,
        batting_skipped_no_id=skipped_no_id,
    )

    saved = 0
    pids = list(by_player_id.keys())
    for chunk_start in range(0, len(pids), _SAVE_BATCH):
        chunk = pids[chunk_start:chunk_start + _SAVE_BATCH]
        with connection.get_session() as db:
            for mlbam in chunk:
                crud.save_player_seasons(db, mlbam, by_player_id[mlbam])
                saved += len(by_player_id[mlbam])
        _set_state(state, lock, batting_loaded=saved)

    log.info(f"  batting: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}, skipped (already in DB): {skipped_existing:,}")
    return saved, skipped_existing, skipped_no_id


def _load_pitching(
    bridge: dict[str, int],
    existing_keys: set[tuple[int, int]],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> tuple[int, int, int]:
    """Aggregate Lahman pitching and upsert pre-2008 seasons.

    Returns (saved, skipped_existing, skipped_no_id).
    """
    _set_state(state, lock, phase="pitching")
    log.info(f"Reading {PITCHING_CSV} ...")
    aggregated = _read_pitching_aggregated()
    log.info(f"  {len(aggregated):,} (pitcher, year) entries pre-{CUTOFF_YEAR}")

    by_player_id: dict[int, list[dict]] = defaultdict(list)
    skipped_no_id = 0
    skipped_existing = 0

    for (lahman_id, year), agg in aggregated.items():
        mlbam = bridge.get(lahman_id)
        if mlbam is None:
            skipped_no_id += 1
            continue
        if (mlbam, year) in existing_keys:
            skipped_existing += 1
            continue

        derived = _pitching_derived(
            ipouts=agg["IPouts"], h=agg["H"], hr=agg["HR"],
            bb=agg["BB"], hbp=agg["HBP"], so=agg["SO"], bfp=agg["BFP"],
        )

        # Lahman ERA is per-stint; for multi-stint years, recompute as ER*9/IP
        ip_dec = agg["IPouts"] / 3 if agg["IPouts"] > 0 else 0.0
        era = round(agg["ER"] * 9 / ip_dec, 2) if ip_dec > 0 else None

        # BAOpp = H / (BFP - BB - HBP - SH - SF) — recompute for multi-stint
        ab_faced = agg["BFP"] - agg["BB"] - agg["HBP"] - agg["SH"] - agg["SF"]
        baopp = round(agg["H"] / ab_faced, 3) if ab_faced > 0 else None

        season = {
            "year":    year,
            "team":    agg["teamID"] or None,
            "league":  agg["lgID"] or None,
            "W":       agg["W"],
            "L":       agg["L"],
            "G":       agg["G"],
            "GS":      agg["GS"],
            "CG":      agg["CG"],
            "SHO":     agg["SHO"],
            "SV":      agg["SV"],
            "GF":      agg["GF"],
            "H":       agg["H"],
            "ER":      agg["ER"],
            "R":       agg["R"],
            "HR":      agg["HR"],
            "BB":      agg["BB"],
            "IBB":     int(agg["IBB"]),
            "SO":      agg["SO"],
            "HBP":     int(agg["HBP"]),
            "WP":      agg["WP"],
            "BK":      agg["BK"],
            "BFP":     int(agg["BFP"]),
            "SH":      int(agg["SH"]),
            "SF":      int(agg["SF"]),
            "GIDP":    int(agg["GIDP"]),
            "ERA":     era,
            "BAOpp":   baopp,
            **derived,
        }
        by_player_id[mlbam].append(season)

    total_rows = sum(len(v) for v in by_player_id.values())
    log.info(f"  saving {total_rows:,} pitching rows for {len(by_player_id):,} pitchers ...")
    _set_state(
        state, lock,
        pitching_rows_total=total_rows,
        pitching_skipped_existing=skipped_existing,
        pitching_skipped_no_id=skipped_no_id,
    )

    saved = 0
    pids = list(by_player_id.keys())
    for chunk_start in range(0, len(pids), _SAVE_BATCH):
        chunk = pids[chunk_start:chunk_start + _SAVE_BATCH]
        with connection.get_session() as db:
            for mlbam in chunk:
                crud.save_pitcher_seasons(db, mlbam, by_player_id[mlbam])
                saved += len(by_player_id[mlbam])
        _set_state(state, lock, pitching_loaded=saved)

    log.info(f"  pitching: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}, skipped (already in DB): {skipped_existing:,}")
    return saved, skipped_existing, skipped_no_id


# ---------------------------------------------------------------------------
# Player / pitcher info from People.csv merged with Chadwick
# ---------------------------------------------------------------------------

def _load_people_info(
    bridge: dict[str, int],
    batter_ids: set[int],
    pitcher_ids: set[int],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> tuple[int, int]:
    """Populate the players/pitchers tables with bio info from Lahman People.csv.

    Only writes rows whose mlbam IDs appear in batter_ids / pitcher_ids
    (i.e. players who actually have stats loaded in this run or already in DB).
    """
    _set_state(state, lock, phase="people")
    log.info(f"Reading {PEOPLE_CSV} ...")

    by_mlbam: dict[int, dict] = {}
    with open(PEOPLE_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            bbref = row.get("bbrefID")
            if not bbref:
                continue
            mlbam = bridge.get(bbref)
            if mlbam is None:
                continue
            if mlbam not in batter_ids and mlbam not in pitcher_ids:
                continue

            first = (row.get("nameFirst") or "").strip()
            last  = (row.get("nameLast")  or "").strip()
            name  = f"{first} {last}".strip()
            if not name:
                continue

            # Lahman's debut/finalGame are date strings like "1914-07-11"
            debut      = (row.get("debut") or "").strip() or None
            final_game = (row.get("finalGame") or "").strip() or None
            debut_year = _i_or_none(debut[:4]) if debut else None
            last_year  = _i_or_none(final_game[:4]) if final_game else None

            def _str_or_none(v):
                v = (v or "").strip()
                return v or None

            by_mlbam[mlbam] = {
                "player_id":       mlbam,
                "name":            name,
                "bbref_id":        bbref,
                "mlb_debut":       debut_year,
                "mlb_last_season": last_year,
                "bats":            _str_or_none(row.get("bats")),
                "throws":          _str_or_none(row.get("throws")),
                "height":          _i_or_none(row.get("height")),
                "weight":          _i_or_none(row.get("weight")),
                "birth_year":      _i_or_none(row.get("birthYear")),
                "birth_month":     _i_or_none(row.get("birthMonth")),
                "birth_day":       _i_or_none(row.get("birthDay")),
                "birth_city":      _str_or_none(row.get("birthCity")),
                "birth_state":     _str_or_none(row.get("birthState")),
                "birth_country":   _str_or_none(row.get("birthCountry")),
                "debut":           debut,
                "final_game":      final_game,
            }

    batters_written = 0
    pitchers_written = 0
    items = list(by_mlbam.items())
    for chunk_start in range(0, len(items), _SAVE_BATCH):
        chunk = items[chunk_start:chunk_start + _SAVE_BATCH]
        with connection.get_session() as db:
            for mlbam, info in chunk:
                if mlbam in batter_ids:
                    # Player.position is filled in by _compute_primary_positions
                    # after fielding has been loaded.
                    crud.save_player(db, info)
                    batters_written += 1
                if mlbam in pitcher_ids:
                    # Pitcher.position is always "P".
                    crud.save_pitcher(db, {**info, "position": "P"})
                    pitchers_written += 1
        _set_state(state, lock,
                   players_written=batters_written,
                   pitchers_written=pitchers_written)

    log.info(f"  people: wrote {batters_written:,} player rows, {pitchers_written:,} pitcher rows")
    return batters_written, pitchers_written


# ---------------------------------------------------------------------------
# Fielding (Fielding.csv) — aggregate stints per (player, year, position)
# ---------------------------------------------------------------------------

def _load_fielding(
    bridge: dict[str, int],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> int:
    _set_state(state, lock, phase="fielding")
    log.info(f"Reading {FIELDING_CSV} ...")

    # Aggregate counting stats across stints per (player, year, position)
    by_key: dict[tuple[str, int, str], dict] = {}
    with open(FIELDING_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            year = int(row["yearID"])
            if year >= CUTOFF_YEAR:
                continue
            pos = row.get("POS") or ""
            if not pos:
                continue
            key = (row["playerID"], year, pos)
            agg = by_key.setdefault(key, {
                "stint": 0, "teamID": "",
                "G": 0, "GS": 0, "InnOuts": 0, "PO": 0, "A": 0, "E": 0, "DP": 0,
            })
            stint = _i(row.get("stint"))
            agg["G"]       += _i(row["G"])
            agg["GS"]      += _i(row["GS"])
            agg["InnOuts"] += _i(row["InnOuts"])
            agg["PO"]      += _i(row["PO"])
            agg["A"]       += _i(row["A"])
            agg["E"]       += _i(row["E"])
            agg["DP"]      += _i(row["DP"])
            if stint >= agg["stint"]:
                agg["stint"]  = stint
                agg["teamID"] = row["teamID"]

    # Build rows keyed by mlbam player_id
    by_player: dict[int, list[dict]] = defaultdict(list)
    skipped_no_id = 0
    for (lahman_id, year, pos), agg in by_key.items():
        mlbam = bridge.get(lahman_id)
        if mlbam is None:
            skipped_no_id += 1
            continue
        po, a, e = agg["PO"], agg["A"], agg["E"]
        chances  = po + a + e
        innings  = agg["InnOuts"] / 3 if agg["InnOuts"] > 0 else 0.0
        by_player[mlbam].append({
            "year":         year,
            "position":     pos,
            "team":         agg["teamID"] or None,
            "G":            agg["G"],
            "GS":           agg["GS"],
            "innings_outs": agg["InnOuts"],
            "PO":           po,
            "A":            a,
            "E":            e,
            "DP":           agg["DP"],
            "fielding_pct": round((po + a) / chances, 3) if chances > 0 else None,
            "RF_per9":      round((po + a) * 9 / innings, 2) if innings > 0 else None,
        })

    total_rows = sum(len(v) for v in by_player.values())
    log.info(f"  saving {total_rows:,} fielding rows for {len(by_player):,} players ...")
    _set_state(state, lock, fielding_rows_total=total_rows,
               fielding_skipped_no_id=skipped_no_id)

    saved = 0
    pids = list(by_player.keys())
    for chunk_start in range(0, len(pids), _SAVE_BATCH):
        chunk = pids[chunk_start:chunk_start + _SAVE_BATCH]
        with connection.get_session() as db:
            for mlbam in chunk:
                crud.save_player_fielding(db, mlbam, by_player[mlbam])
                saved += len(by_player[mlbam])
        _set_state(state, lock, fielding_loaded=saved)

    log.info(f"  fielding: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}")
    return saved


def _compute_primary_positions(
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> int:
    """For every Player row, set position to whichever non-pitcher fielding
    position the player accumulated the most games at. Falls back to "P" if
    they only ever pitched. Pitcher.position is set to "P" at people-load
    time so this only updates Player rows.
    """
    _set_state(state, lock, phase="primary_positions")
    log.info("Computing primary fielding position per Player ...")

    from sqlalchemy import func
    with connection.get_session() as db:
        rows = (
            db.query(
                PlayerFielding.player_id,
                PlayerFielding.position,
                func.sum(PlayerFielding.G).label("g"),
            )
            .group_by(PlayerFielding.player_id, PlayerFielding.position)
            .all()
        )

    positions_by_player: dict[int, dict[str, int]] = defaultdict(dict)
    for r in rows:
        positions_by_player[r.player_id][r.position] = int(r.g or 0)

    # Decide each player's primary: prefer non-P, otherwise P (player never
    # appears in the field except as pitcher).
    primary: dict[int, str] = {}
    for pid, by_pos in positions_by_player.items():
        non_p = {p: g for p, g in by_pos.items() if p != "P"}
        primary[pid] = max(non_p, key=non_p.get) if non_p else "P"

    updated = 0
    items = list(primary.items())
    for chunk_start in range(0, len(items), _SAVE_BATCH):
        chunk = items[chunk_start:chunk_start + _SAVE_BATCH]
        with connection.get_session() as db:
            for pid, pos in chunk:
                p = crud.get_player(db, pid)
                if p is not None:
                    p.position = pos
                    updated += 1
        _set_state(state, lock, positions_set=updated)

    log.info(f"  positions set on {updated:,} player rows")
    return updated


# ---------------------------------------------------------------------------
# Awards (AwardsPlayers.csv) and All-Star (AllstarFull.csv)
# ---------------------------------------------------------------------------

def _load_awards(
    bridge: dict[str, int],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> int:
    _set_state(state, lock, phase="awards")
    log.info(f"Reading {AWARDS_CSV} ...")

    rows: list[dict] = []
    skipped_no_id = 0
    with open(AWARDS_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            year = int(row["yearID"])
            if year >= CUTOFF_YEAR:
                continue
            mlbam = bridge.get(row["playerID"])
            if mlbam is None:
                skipped_no_id += 1
                continue
            rows.append({
                "player_id":  mlbam,
                "year":       year,
                "award_name": row["awardID"],
                "league":     row.get("lgID") or "",
                "tie":        (row.get("tie") or "").strip() or None,
                "notes":      (row.get("notes") or "").strip() or None,
            })

    log.info(f"  saving {len(rows):,} award rows ...")
    _set_state(state, lock, awards_rows_total=len(rows),
               awards_skipped_no_id=skipped_no_id)

    saved = 0
    BATCH = 5000
    for chunk_start in range(0, len(rows), BATCH):
        chunk = rows[chunk_start:chunk_start + BATCH]
        with connection.get_session() as db:
            crud.save_player_awards(db, chunk)
            saved += len(chunk)
        _set_state(state, lock, awards_loaded=saved)

    log.info(f"  awards: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}")
    return saved


def _load_allstar(
    bridge: dict[str, int],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> int:
    _set_state(state, lock, phase="allstar")
    log.info(f"Reading {ALLSTAR_CSV} ...")

    rows: list[dict] = []
    skipped_no_id = 0
    with open(ALLSTAR_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            year = int(row["yearID"])
            if year >= CUTOFF_YEAR:
                continue
            mlbam = bridge.get(row["playerID"])
            if mlbam is None:
                skipped_no_id += 1
                continue
            rows.append({
                "player_id":    mlbam,
                "year":         year,
                "game_num":     _i_safe(row.get("gameNum")),
                "team":         row.get("teamID") or None,
                "league":       row.get("lgID") or None,
                "GP":           _i_or_none_safe(row.get("GP")),
                "starting_pos": _i_or_none_safe(row.get("startingPos")),
            })

    log.info(f"  saving {len(rows):,} all-star rows ...")
    _set_state(state, lock, allstar_rows_total=len(rows),
               allstar_skipped_no_id=skipped_no_id)

    saved = 0
    BATCH = 5000
    for chunk_start in range(0, len(rows), BATCH):
        chunk = rows[chunk_start:chunk_start + BATCH]
        with connection.get_session() as db:
            crud.save_player_allstar(db, chunk)
            saved += len(chunk)
        _set_state(state, lock, allstar_loaded=saved)

    log.info(f"  allstar: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}")
    return saved


# ---------------------------------------------------------------------------
# Postseason batting & pitching
# ---------------------------------------------------------------------------

def _load_postseason_batting(
    bridge: dict[str, int],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> int:
    _set_state(state, lock, phase="postseason_batting")
    log.info(f"Reading {BATTING_POST_CSV} ...")

    rows: list[dict] = []
    skipped_no_id = 0
    with open(BATTING_POST_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            year = int(row["yearID"])
            if year >= CUTOFF_YEAR:
                continue
            mlbam = bridge.get(row["playerID"])
            if mlbam is None:
                skipped_no_id += 1
                continue

            ab = _i_safe(row["AB"]); h = _i_safe(row["H"])
            doubles = _i_safe(row["2B"]); triples = _i_safe(row["3B"]); hr = _i_safe(row["HR"])
            bb = _i_safe(row["BB"]); hbp = _f_safe(row.get("HBP")); sf = _f_safe(row.get("SF"))
            ba  = round(h / ab, 3) if ab > 0 else None
            obp_den = ab + bb + hbp + sf
            obp = round((h + bb + hbp) / obp_den, 3) if obp_den > 0 else None
            slg = round((h + doubles + 2 * triples + 3 * hr) / ab, 3) if ab > 0 else None
            ops = round(obp + slg, 3) if (obp is not None and slg is not None) else None

            rows.append({
                "player_id": mlbam,
                "year":      year,
                "round":     row["round"],
                "team":      row.get("teamID") or None,
                "league":    row.get("lgID") or None,
                "G":         _i_safe(row["G"]),
                "AB":        ab,
                "R":         _i_safe(row["R"]),
                "H":         h,
                "doubles":   doubles,
                "triples":   triples,
                "HR":        hr,
                "RBI":       _i_or_none_safe(row.get("RBI")),
                "BB":        bb,
                "SO":        _i_or_none_safe(row.get("SO")),
                "SB":        _i_or_none_safe(row.get("SB")),
                "CS":        _i_or_none_safe(row.get("CS")),
                "BA":        ba,
                "OBP":       obp,
                "SLG":       slg,
                "OPS":       ops,
            })

    log.info(f"  saving {len(rows):,} postseason batting rows ...")
    _set_state(state, lock, postseason_batting_rows_total=len(rows),
               postseason_batting_skipped_no_id=skipped_no_id)

    saved = 0
    BATCH = 5000
    for chunk_start in range(0, len(rows), BATCH):
        chunk = rows[chunk_start:chunk_start + BATCH]
        with connection.get_session() as db:
            crud.save_player_postseason_batting(db, chunk)
            saved += len(chunk)
        _set_state(state, lock, postseason_batting_loaded=saved)

    log.info(f"  postseason batting: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}")
    return saved


def _load_postseason_pitching(
    bridge: dict[str, int],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> int:
    _set_state(state, lock, phase="postseason_pitching")
    log.info(f"Reading {PITCHING_POST_CSV} ...")

    rows: list[dict] = []
    skipped_no_id = 0
    with open(PITCHING_POST_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            year = int(row["yearID"])
            if year >= CUTOFF_YEAR:
                continue
            mlbam = bridge.get(row["playerID"])
            if mlbam is None:
                skipped_no_id += 1
                continue

            ipouts = _i_safe(row["IPouts"])
            ip = round(ipouts / 3, 1) if ipouts > 0 else None
            ip_dec = ipouts / 3 if ipouts > 0 else 0.0
            h  = _i_safe(row["H"])
            bb = _i_safe(row["BB"])
            whip = round((bb + h) / ip_dec, 2) if ip_dec > 0 else None

            rows.append({
                "player_id": mlbam,
                "year":      year,
                "round":     row["round"],
                "team":      row.get("teamID") or None,
                "league":    row.get("lgID") or None,
                "W":         _i_safe(row["W"]),
                "L":         _i_safe(row["L"]),
                "G":         _i_safe(row["G"]),
                "GS":        _i_safe(row["GS"]),
                "SV":        _i_or_none_safe(row.get("SV")),
                "IP":        ip,
                "H":         h,
                "ER":        _i_safe(row["ER"]),
                "HR":        _i_safe(row["HR"]),
                "BB":        bb,
                "SO":        _i_safe(row["SO"]),
                "ERA":       _f_or_none_safe(row.get("ERA")),
                "WHIP":      whip,
            })

    log.info(f"  saving {len(rows):,} postseason pitching rows ...")
    _set_state(state, lock, postseason_pitching_rows_total=len(rows),
               postseason_pitching_skipped_no_id=skipped_no_id)

    saved = 0
    BATCH = 5000
    for chunk_start in range(0, len(rows), BATCH):
        chunk = rows[chunk_start:chunk_start + BATCH]
        with connection.get_session() as db:
            crud.save_player_postseason_pitching(db, chunk)
            saved += len(chunk)
        _set_state(state, lock, postseason_pitching_loaded=saved)

    log.info(f"  postseason pitching: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}")
    return saved


# ---------------------------------------------------------------------------
# Team standings (Teams.csv)
# ---------------------------------------------------------------------------

def _load_teams(
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> int:
    _set_state(state, lock, phase="teams")
    log.info(f"Reading {TEAMS_CSV} ...")

    rows: list[dict] = []
    with open(TEAMS_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            year = int(row["yearID"])
            if year >= CUTOFF_YEAR:
                continue
            w = _i_safe(row["W"]); l = _i_safe(row["L"])
            win_pct = round(w / (w + l), 3) if (w + l) > 0 else None
            rows.append({
                "year":         year,
                "team_id":      row["teamID"],
                "franch_id":    row.get("franchID") or row["teamID"],
                "team_name":    row.get("name") or None,
                "league":       row.get("lgID") or None,
                "division":     row.get("divID") or None,
                "rank":         _i_or_none_safe(row.get("Rank")),
                "G":            _i_safe(row["G"]),
                "W":            w,
                "L":            l,
                "win_pct":      win_pct,
                "runs_scored":  _i_safe(row["R"]),
                "runs_allowed": _i_safe(row["RA"]),
                "HR":           _i_safe(row["HR"]),
                "ERA":          _f_or_none_safe(row.get("ERA")),
                "attendance":   _i_or_none_safe(row.get("attendance")),
                "park_name":    row.get("park") or None,
            })

    log.info(f"  saving {len(rows):,} team-season rows ...")
    _set_state(state, lock, teams_rows_total=len(rows))

    saved = 0
    BATCH = 1000
    for chunk_start in range(0, len(rows), BATCH):
        chunk = rows[chunk_start:chunk_start + BATCH]
        with connection.get_session() as db:
            crud.save_team_seasons(db, chunk)
            saved += len(chunk)
        _set_state(state, lock, teams_loaded=saved)

    log.info(f"  teams: saved {saved:,}")
    return saved


# ---------------------------------------------------------------------------
# Hall of Fame (HallOfFame.csv)
# ---------------------------------------------------------------------------

def _load_hof(
    bridge: dict[str, int],
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> int:
    """Load every Hall of Fame ballot row keyed by mlbam player_id. Stores the
    full voting history (one row per player+year+voting-body); the API
    surfaces is_hof / hof_year by checking for any inducted=True row."""
    _set_state(state, lock, phase="hof")
    log.info(f"Reading {HOF_CSV} ...")

    rows: list[dict] = []
    skipped_no_id = 0
    with open(HOF_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            mlbam = bridge.get(row["playerID"])
            if mlbam is None:
                # Many HOF entries are managers / executives / pioneers without
                # an MLBAM player ID. Expected; just count and skip.
                skipped_no_id += 1
                continue
            inducted_raw = (row.get("inducted") or "").strip().upper()
            inducted = True if inducted_raw == "Y" else False if inducted_raw == "N" else None
            rows.append({
                "player_id":     mlbam,
                "year_inducted": int(row["yearid"]),
                "voted_by":      (row.get("votedBy") or "").strip() or "",
                "category":      (row.get("category") or "").strip() or None,
                "needed":        _i_or_none(row.get("needed")),
                "votes":         _i_or_none(row.get("votes")),
                "inducted":      inducted,
            })

    log.info(f"  saving {len(rows):,} HOF ballot rows ...")
    _set_state(state, lock, hof_rows_total=len(rows), hof_skipped_no_id=skipped_no_id)

    saved = 0
    BATCH = 5000
    for chunk_start in range(0, len(rows), BATCH):
        chunk = rows[chunk_start:chunk_start + BATCH]
        with connection.get_session() as db:
            crud.save_player_hof(db, chunk)
            saved += len(chunk)
        _set_state(state, lock, hof_loaded=saved)

    log.info(f"  hof: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}")
    return saved


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load Lahman historical stats into PostgreSQL.")
    return p.parse_args()


def run(
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> None:
    """Run the full Lahman load.

    If `state` and `lock` are provided, progress is written to the dict so a
    status endpoint can read it from another thread. Without them the loader
    runs standalone and only logs to stdout.
    """
    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set. Export it and re-run.")
    connection.init_db()

    log.info("=" * 52)
    log.info("Lahman load — pre-{} historical stats".format(CUTOFF_YEAR))
    log.info("=" * 52)

    _set_state(state, lock, phase="bridge")
    log.info(f"Loading Chadwick bridge from {CHADWICK_CSV} ...")
    bridge = _load_chadwick_bridge()
    log.info(f"  {len(bridge):,} bbref→mlbam mappings")

    _set_state(state, lock, phase="snapshot")
    log.info("Snapshotting existing (player_id, year) keys to skip ...")
    with connection.get_session() as db:
        bat_existing = {
            (row.player_id, row.year)
            for row in db.query(PlayerSeason.player_id, PlayerSeason.year).all()
        }
        pit_existing = {
            (row.player_id, row.year)
            for row in db.query(PitcherSeason.player_id, PitcherSeason.year).all()
        }
    log.info(f"  player_seasons: {len(bat_existing):,} rows, pitcher_seasons: {len(pit_existing):,} rows")

    bat_saved, bat_skipped, bat_no_id = _load_batting(bridge, bat_existing, state, lock)
    pit_saved, pit_skipped, pit_no_id = _load_pitching(bridge, pit_existing, state, lock)

    # Pull final ID sets so we only write people rows for players we actually have stats for
    with connection.get_session() as db:
        batter_ids  = set(crud.get_all_player_ids(db))
        pitcher_ids = set(crud.get_all_pitcher_ids(db))

    p_batters, p_pitchers = _load_people_info(bridge, batter_ids, pitcher_ids, state, lock)

    fielding_saved   = _load_fielding(bridge, state, lock)
    positions_set    = _compute_primary_positions(state, lock)
    awards_saved     = _load_awards(bridge, state, lock)
    allstar_saved    = _load_allstar(bridge, state, lock)
    post_bat_saved   = _load_postseason_batting(bridge, state, lock)
    post_pit_saved   = _load_postseason_pitching(bridge, state, lock)
    teams_saved      = _load_teams(state, lock)
    hof_saved        = _load_hof(bridge, state, lock)

    _set_state(state, lock, phase="done")

    bar = "=" * 52
    print(f"\n{bar}")
    print("Lahman load complete")
    print(f"  Batting rows           saved: {bat_saved:>7,}   skipped existing: {bat_skipped:>6,}   no chadwick id: {bat_no_id:>5,}")
    print(f"  Pitching rows          saved: {pit_saved:>7,}   skipped existing: {pit_skipped:>6,}   no chadwick id: {pit_no_id:>5,}")
    print(f"  Player rows written          : {p_batters:>7,}")
    print(f"  Pitcher rows written         : {p_pitchers:>7,}")
    print(f"  Fielding rows saved          : {fielding_saved:>7,}")
    print(f"  Primary positions set        : {positions_set:>7,}")
    print(f"  Award rows saved             : {awards_saved:>7,}")
    print(f"  All-Star rows saved          : {allstar_saved:>7,}")
    print(f"  Postseason batting saved     : {post_bat_saved:>7,}")
    print(f"  Postseason pitching saved    : {post_pit_saved:>7,}")
    print(f"  Team-season rows saved       : {teams_saved:>7,}")
    print(f"  Hall of Fame ballots saved   : {hof_saved:>7,}")
    print(bar)


def main() -> None:
    parse_args()
    run()


if __name__ == "__main__":
    main()
