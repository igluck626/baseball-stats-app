#!/usr/bin/env python3
"""Load Lahman historical batting/pitching stats into PostgreSQL.

Lahman data covers 1871–2021. We only load years < 2008, since
Baseball Reference (via pybaseball) provides richer data from 2008 onward.

Joins Lahman.playerID → Chadwick key_bbref → key_mlbam (our player_id).
Players whose Chadwick row lacks a key_mlbam are skipped (~81 players,
all obscure 19th-century guys).

Idempotent: rows already in the database are skipped.
"""

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Path setup — allow imports from backend/api and backend/database
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

from database import connection, crud                                     # noqa: E402
from database.models import PitcherSeason, PlayerSeason                   # noqa: E402

LAHMAN_DIR     = os.path.join(_BACKEND_DIR, "data", "lahman")
BATTING_CSV    = os.path.join(LAHMAN_DIR, "Batting.csv")
PITCHING_CSV   = os.path.join(LAHMAN_DIR, "Pitching.csv")
PEOPLE_CSV     = os.path.join(LAHMAN_DIR, "People.csv")
CHADWICK_CSV   = os.path.join(LAHMAN_DIR, "chadwick_mlb.csv")

CUTOFF_YEAR = 2008  # load Lahman data only for years STRICTLY less than this

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Chadwick bbref → mlbam bridge
# ---------------------------------------------------------------------------

def _load_chadwick_bridge() -> dict[str, int]:
    """key_bbref → key_mlbam dict, loaded from the trimmed Chadwick file."""
    bridge: dict[str, int] = {}
    with open(CHADWICK_CSV, newline="") as fh:
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
    with open(BATTING_CSV, newline="") as fh:
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
            })

            # Sum counting stats
            agg["G"]   += _i(row["G"])
            agg["AB"]  += _i(row["AB"])
            agg["R"]   += _i(row["R"])
            agg["H"]   += _i(row["H"])
            agg["2B"]  += _i(row["2B"])
            agg["3B"]  += _i(row["3B"])
            agg["HR"]  += _i(row["HR"])
            agg["RBI"] += _f(row["RBI"])
            agg["SB"]  += _f(row["SB"])
            agg["CS"]  += _f(row["CS"])
            agg["BB"]  += _i(row["BB"])
            agg["SO"]  += _f(row["SO"])
            agg["IBB"] += _f(row["IBB"])
            agg["HBP"] += _f(row["HBP"])
            agg["SH"]  += _f(row["SH"])
            agg["SF"]  += _f(row["SF"])

            # Latest stint's team is the player's "final" team that year
            if stint >= agg["stint"]:
                agg["stint"]  = stint
                agg["teamID"] = row["teamID"]
                agg["lgID"]   = row["lgID"]

    return by_key


def _read_pitching_aggregated() -> dict[tuple[str, int], dict]:
    """Returns {(playerID, yearID): aggregated_stats_dict} for years < CUTOFF_YEAR."""
    by_key: dict[tuple[str, int], dict] = {}
    with open(PITCHING_CSV, newline="") as fh:
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
                "W": 0, "L": 0, "G": 0, "GS": 0, "IPouts": 0,
                "H": 0, "ER": 0, "HR": 0, "BB": 0, "SO": 0,
                "ERA_num": 0.0,    # ER * 9 (numerator for combined ERA)
                "BFP": 0.0,
                "HBP": 0.0,
            })

            agg["W"]      += _i(row["W"])
            agg["L"]      += _i(row["L"])
            agg["G"]      += _i(row["G"])
            agg["GS"]     += _i(row["GS"])
            agg["IPouts"] += _i(row["IPouts"])
            agg["H"]      += _i(row["H"])
            agg["ER"]     += _i(row["ER"])
            agg["HR"]     += _i(row["HR"])
            agg["BB"]     += _i(row["BB"])
            agg["SO"]     += _i(row["SO"])
            agg["BFP"]    += _f(row["BFP"])
            agg["HBP"]    += _f(row["HBP"])

            if stint >= agg["stint"]:
                agg["stint"]  = stint
                agg["teamID"] = row["teamID"]
                agg["lgID"]   = row["lgID"]

    return by_key


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_batting(bridge: dict[str, int], existing_keys: set[tuple[int, int]]) -> tuple[int, int, int]:
    """Aggregate Lahman batting and upsert pre-2008 seasons. Returns (saved, skipped_existing, skipped_no_id)."""
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
            **derived,
        }
        by_player_id[mlbam].append(season)

    saved = 0
    log.info(f"  saving {sum(len(v) for v in by_player_id.values()):,} batting rows for {len(by_player_id):,} batters ...")
    with connection.get_session() as db:
        for mlbam, seasons in by_player_id.items():
            crud.save_player_seasons(db, mlbam, seasons)
            saved += len(seasons)

    log.info(f"  batting: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}, skipped (already in DB): {skipped_existing:,}")
    return saved, skipped_existing, skipped_no_id


def _load_pitching(bridge: dict[str, int], existing_keys: set[tuple[int, int]]) -> tuple[int, int, int]:
    """Aggregate Lahman pitching and upsert pre-2008 seasons. Returns (saved, skipped_existing, skipped_no_id)."""
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

        season = {
            "year":    year,
            "team":    agg["teamID"] or None,
            "league":  agg["lgID"] or None,
            "W":       agg["W"],
            "L":       agg["L"],
            "G":       agg["G"],
            "GS":      agg["GS"],
            "SO":      agg["SO"],
            "BB":      agg["BB"],
            "HR":      agg["HR"],
            "ERA":     era,
            **derived,
        }
        by_player_id[mlbam].append(season)

    saved = 0
    log.info(f"  saving {sum(len(v) for v in by_player_id.values()):,} pitching rows for {len(by_player_id):,} pitchers ...")
    with connection.get_session() as db:
        for mlbam, seasons in by_player_id.items():
            crud.save_pitcher_seasons(db, mlbam, seasons)
            saved += len(seasons)

    log.info(f"  pitching: saved {saved:,}, skipped (no Chadwick id): {skipped_no_id:,}, skipped (already in DB): {skipped_existing:,}")
    return saved, skipped_existing, skipped_no_id


# ---------------------------------------------------------------------------
# Player / pitcher info from People.csv merged with Chadwick
# ---------------------------------------------------------------------------

def _load_people_info(
    bridge: dict[str, int],
    batter_ids: set[int],
    pitcher_ids: set[int],
) -> tuple[int, int]:
    """Populate the players/pitchers tables with bio info from Lahman People.csv.

    Only writes rows whose mlbam IDs appear in batter_ids / pitcher_ids
    (i.e. players who actually have stats loaded in this run or already in DB).
    """
    log.info(f"Reading {PEOPLE_CSV} ...")

    by_mlbam: dict[int, dict] = {}
    with open(PEOPLE_CSV, newline="") as fh:
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
            debut_year = _i_or_none((row.get("debut") or "")[:4])
            last_year  = _i_or_none((row.get("finalGame") or "")[:4])

            by_mlbam[mlbam] = {
                "player_id":       mlbam,
                "name":            name,
                "bbref_id":        bbref,
                "mlb_debut":       debut_year,
                "mlb_last_season": last_year,
            }

    batters_written = 0
    pitchers_written = 0
    with connection.get_session() as db:
        for mlbam, info in by_mlbam.items():
            if mlbam in batter_ids:
                crud.save_player(db, info)
                batters_written += 1
            if mlbam in pitcher_ids:
                crud.save_pitcher(db, info)
                pitchers_written += 1

    log.info(f"  people: wrote {batters_written:,} player rows, {pitchers_written:,} pitcher rows")
    return batters_written, pitchers_written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load Lahman pre-2008 stats into PostgreSQL.")
    return p.parse_args()


def run() -> None:
    """Importable entry point so bulk_load.py can call this as Phase 0."""
    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set. Export it and re-run.")
    connection.init_db()

    log.info("=" * 52)
    log.info("Lahman load — pre-{} historical stats".format(CUTOFF_YEAR))
    log.info("=" * 52)

    log.info(f"Loading Chadwick bridge from {CHADWICK_CSV} ...")
    bridge = _load_chadwick_bridge()
    log.info(f"  {len(bridge):,} bbref→mlbam mappings")

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

    bat_saved, bat_skipped, bat_no_id = _load_batting(bridge, bat_existing)
    pit_saved, pit_skipped, pit_no_id = _load_pitching(bridge, pit_existing)

    # Pull final ID sets so we only write people rows for players we actually have stats for
    with connection.get_session() as db:
        batter_ids  = set(crud.get_all_player_ids(db))
        pitcher_ids = set(crud.get_all_pitcher_ids(db))

    p_batters, p_pitchers = _load_people_info(bridge, batter_ids, pitcher_ids)

    bar = "=" * 52
    print(f"\n{bar}")
    print("Lahman load complete")
    print(f"  Batting rows  saved: {bat_saved:>7,}   skipped existing: {bat_skipped:>6,}   no chadwick id: {bat_no_id:>5,}")
    print(f"  Pitching rows saved: {pit_saved:>7,}   skipped existing: {pit_skipped:>6,}   no chadwick id: {pit_no_id:>5,}")
    print(f"  Player rows written : {p_batters:>7,}")
    print(f"  Pitcher rows written: {p_pitchers:>7,}")
    print(bar)


def main() -> None:
    parse_args()
    run()


if __name__ == "__main__":
    main()
