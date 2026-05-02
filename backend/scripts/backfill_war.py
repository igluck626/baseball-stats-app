#!/usr/bin/env python3
"""Backfill WAR / advanced metrics on player_seasons + pitcher_seasons rows.

Pre-2008 rows loaded from Lahman have no WAR or WAR-derived columns. This
script joins each null-WAR row to bwar_bat / bwar_pitch (full history,
fetched from pybaseball in one call each) and writes the missing fields.

Idempotent: a row with WAR already populated is left untouched. Safe to
re-run after each Lahman import.

Usage:
    DATABASE_URL=... backend/venv/bin/python backend/scripts/backfill_war.py
"""

import argparse
import csv
import logging
import os
import sys
import threading
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Path setup — share data_service + database with the API
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, _SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

import data_service                                              # noqa: E402
from database import connection                                  # noqa: E402
from database.models import PitcherSeason, PlayerSeason          # noqa: E402

CHADWICK_CSV = os.path.join(_BACKEND_DIR, "data", "lahman", "chadwick_mlb.csv")
_UPDATE_BATCH = 5000     # rows per DB transaction during the update loops

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional shared state (so the /admin/backfill-war endpoint can read progress)
# ---------------------------------------------------------------------------

def _set_state(state: Optional[dict], lock: Optional[threading.Lock], **kwargs) -> None:
    if state is None:
        return
    if lock is not None:
        with lock:
            state.update(kwargs)
    else:
        state.update(kwargs)


# ---------------------------------------------------------------------------
# Chadwick bridge (only used as a fallback when bwar's mlb_ID column is NaN —
# which happens for some 19th-century players)
# ---------------------------------------------------------------------------

def _load_chadwick_bridge() -> dict[str, int]:
    bridge: dict[str, int] = {}
    with open(CHADWICK_CSV, newline="") as fh:
        for row in csv.DictReader(fh):
            bbref = row["key_bbref"]
            mlbam = row["key_mlbam"]
            if bbref and mlbam:
                bridge[bbref] = int(mlbam)
    return bridge


def _resolve_mlbam_column(df: pd.DataFrame, bridge: dict[str, int]) -> pd.Series:
    """Pick mlb_ID where bwar has it, fall back to the bridge by player_ID (bbref)."""
    resolved = df["mlb_ID"].copy()
    missing = resolved.isna()
    if missing.any() and "player_ID" in df.columns:
        resolved.loc[missing] = df.loc[missing, "player_ID"].map(bridge)
    return resolved


# ---------------------------------------------------------------------------
# Build (player_id, year) → war-fields lookups from bwar_bat / bwar_pitch
# ---------------------------------------------------------------------------

def _build_batting_lookup(bridge: dict[str, int]) -> dict[tuple[int, int], dict]:
    log.info("Fetching bwar_bat (full history) ...")
    bwar = data_service._bwar_bat_all().copy()
    log.info(f"  {len(bwar):,} bwar_bat rows")

    bwar["resolved_mlbam"] = _resolve_mlbam_column(bwar, bridge)
    bwar = bwar[bwar["resolved_mlbam"].notna() & bwar["year_ID"].notna()].copy()
    bwar["resolved_mlbam"] = bwar["resolved_mlbam"].astype(int)
    bwar["year_ID"] = bwar["year_ID"].astype(int)
    log.info(f"  {len(bwar):,} rows after resolving mlbam")

    lookup: dict[tuple[int, int], dict] = {}
    for (mlbam, year), group in bwar.groupby(["resolved_mlbam", "year_ID"]):
        total_pa = float(group["PA"].sum()) if "PA" in group else 0.0
        ops_plus_vals = group["OPS_plus"].dropna() if "OPS_plus" in group else pd.Series(dtype=float)
        ops_plus = (
            float((group["OPS_plus"] * group["PA"]).sum() / total_pa)
            if total_pa > 0 and not ops_plus_vals.empty
            else None
        )
        lookup[(int(mlbam), int(year))] = {
            "WAR":            round(float(group["WAR"].sum()), 2),
            "WAR_off":        round(float(group["WAR_off"].sum()), 2),
            "WAR_def":        round(float(group["WAR_def"].sum()), 2),
            "WAA":            round(float(group["WAA"].sum()), 2),
            "OPS_plus":       round(ops_plus, 1) if ops_plus is not None else None,
            "runs_above_avg": round(float(group["runs_above_avg"].sum()), 2),
            "runs_above_rep": round(float(group["runs_above_rep"].sum()), 2),
        }
    log.info(f"  built lookup of {len(lookup):,} (player_id, year) keys")
    return lookup


def _build_pitching_lookup(bridge: dict[str, int]) -> dict[tuple[int, int], dict]:
    log.info("Fetching bwar_pitch (full history) ...")
    bwar = data_service._bwar_pitch_all().copy()
    log.info(f"  {len(bwar):,} bwar_pitch rows")

    bwar["resolved_mlbam"] = _resolve_mlbam_column(bwar, bridge)
    bwar = bwar[bwar["resolved_mlbam"].notna() & bwar["year_ID"].notna()].copy()
    bwar["resolved_mlbam"] = bwar["resolved_mlbam"].astype(int)
    bwar["year_ID"] = bwar["year_ID"].astype(int)
    log.info(f"  {len(bwar):,} rows after resolving mlbam")

    lookup: dict[tuple[int, int], dict] = {}
    for (mlbam, year), group in bwar.groupby(["resolved_mlbam", "year_ID"]):
        # ERA+ is rate-based: weight by IPouts to combine stints within a year
        ipouts = float(group["IPouts"].sum()) if "IPouts" in group else 0.0
        era_plus_vals = group["ERA_plus"].dropna() if "ERA_plus" in group else pd.Series(dtype=float)
        era_plus = (
            float((group["ERA_plus"].fillna(0) * group["IPouts"]).sum() / ipouts)
            if ipouts > 0 and not era_plus_vals.empty
            else None
        )

        entry: dict = {
            "WAR":            round(float(group["WAR"].sum()), 2),
            "WAA":            round(float(group["WAA"].sum()), 2),
            "ERA_plus":       round(era_plus, 1) if era_plus is not None else None,
            "runs_above_avg": round(float(group["runs_above_avg"].sum()), 2),
            "runs_above_rep": round(float(group["runs_above_rep"].sum()), 2),
        }
        if "WAR_def" in group:
            entry["WAR_def"] = round(float(group["WAR_def"].sum()), 2)
        lookup[(int(mlbam), int(year))] = entry
    log.info(f"  built lookup of {len(lookup):,} (player_id, year) keys")
    return lookup


# ---------------------------------------------------------------------------
# Apply the lookup to null-WAR rows in the database
# ---------------------------------------------------------------------------

def _apply_batting_updates(
    lookup: dict[tuple[int, int], dict],
    state: Optional[dict],
    lock: Optional[threading.Lock],
) -> tuple[int, int]:
    log.info("Selecting player_seasons rows where WAR IS NULL ...")
    with connection.get_session() as db:
        null_keys = [
            (r.player_id, r.year)
            for r in db.query(PlayerSeason.player_id, PlayerSeason.year)
                       .filter(PlayerSeason.WAR.is_(None)).all()
        ]
    log.info(f"  {len(null_keys):,} rows to consider")
    _set_state(state, lock, batting_total=len(null_keys))

    to_update = [k for k in null_keys if k in lookup]
    no_match = len(null_keys) - len(to_update)
    log.info(f"  {len(to_update):,} have matching bwar_bat data, {no_match:,} have no match")
    _set_state(state, lock, batting_no_match=no_match)

    updated = 0
    for chunk_start in range(0, len(to_update), _UPDATE_BATCH):
        chunk = to_update[chunk_start:chunk_start + _UPDATE_BATCH]
        with connection.get_session() as db:
            for (pid, yr) in chunk:
                row = db.get(PlayerSeason, (pid, yr))
                if row is None:
                    continue
                for col, val in lookup[(pid, yr)].items():
                    setattr(row, col, val)
                updated += 1
        _set_state(state, lock, batting_updated=updated)
        log.info(f"  batting: updated {updated:,}/{len(to_update):,}")

    return updated, no_match


def _apply_pitching_updates(
    lookup: dict[tuple[int, int], dict],
    state: Optional[dict],
    lock: Optional[threading.Lock],
) -> tuple[int, int]:
    log.info("Selecting pitcher_seasons rows where WAR IS NULL ...")
    with connection.get_session() as db:
        null_keys = [
            (r.player_id, r.year)
            for r in db.query(PitcherSeason.player_id, PitcherSeason.year)
                       .filter(PitcherSeason.WAR.is_(None)).all()
        ]
    log.info(f"  {len(null_keys):,} rows to consider")
    _set_state(state, lock, pitching_total=len(null_keys))

    to_update = [k for k in null_keys if k in lookup]
    no_match = len(null_keys) - len(to_update)
    log.info(f"  {len(to_update):,} have matching bwar_pitch data, {no_match:,} have no match")
    _set_state(state, lock, pitching_no_match=no_match)

    updated = 0
    for chunk_start in range(0, len(to_update), _UPDATE_BATCH):
        chunk = to_update[chunk_start:chunk_start + _UPDATE_BATCH]
        with connection.get_session() as db:
            for (pid, yr) in chunk:
                row = db.get(PitcherSeason, (pid, yr))
                if row is None:
                    continue
                for col, val in lookup[(pid, yr)].items():
                    setattr(row, col, val)
                updated += 1
        _set_state(state, lock, pitching_updated=updated)
        log.info(f"  pitching: updated {updated:,}/{len(to_update):,}")

    return updated, no_match


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run(
    state: Optional[dict] = None,
    lock: Optional[threading.Lock] = None,
) -> None:
    """Run the full WAR backfill. Threadsafe progress via optional state/lock."""
    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set.")
    connection.init_db()

    log.info("=" * 52)
    log.info("WAR backfill — pre-2008 rows joined to bwar_bat / bwar_pitch")
    log.info("=" * 52)

    _set_state(state, lock, phase="bridge")
    log.info(f"Loading Chadwick bridge from {CHADWICK_CSV} ...")
    bridge = _load_chadwick_bridge()
    log.info(f"  {len(bridge):,} bbref→mlbam mappings")

    _set_state(state, lock, phase="batting_lookup")
    bat_lookup = _build_batting_lookup(bridge)

    _set_state(state, lock, phase="batting")
    bat_updated, bat_no_match = _apply_batting_updates(bat_lookup, state, lock)

    _set_state(state, lock, phase="pitching_lookup")
    pit_lookup = _build_pitching_lookup(bridge)

    _set_state(state, lock, phase="pitching")
    pit_updated, pit_no_match = _apply_pitching_updates(pit_lookup, state, lock)

    _set_state(state, lock, phase="done")

    bar = "=" * 52
    print(f"\n{bar}")
    print("WAR backfill complete")
    print(f"  Batting  rows updated: {bat_updated:>7,}   no bwar match: {bat_no_match:>5,}")
    print(f"  Pitching rows updated: {pit_updated:>7,}   no bwar match: {pit_no_match:>5,}")
    print(bar)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill WAR on null-WAR rows in player_seasons / pitcher_seasons.")
    return p.parse_args()


def main() -> None:
    parse_args()
    run()


if __name__ == "__main__":
    main()
