#!/usr/bin/env python3
"""Pre-populate PostgreSQL with historical batting AND pitching stats since 1990.

Usage:
    backend/venv/bin/python backend/scripts/bulk_load.py
    backend/venv/bin/python backend/scripts/bulk_load.py --limit 10

Requires DATABASE_URL to be set in the environment or backend/.env.
Safe to re-run — players whose seasons are already in the database are skipped.

The --limit flag applies independently to each phase (batters, then pitchers).
"""

import argparse
import logging
import os
import sys
import time

# ---------------------------------------------------------------------------
# Path setup — allow imports from backend/api and backend/database
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

import data_service                                       # noqa: E402
from database import connection                           # noqa: E402
from database.models import PitcherSeason, PlayerSeason   # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
START_YEAR = 1990
DELAY_BETWEEN_PLAYERS = 2.0  # seconds between players to avoid bref rate-limiting

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk-load historical batting + pitching stats into PostgreSQL."
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after processing N players in each phase (useful for testing).",
    )
    return p.parse_args()


def _load_batters(limit: int | None) -> tuple[int, int, list[int]]:
    log.info("Fetching bwar_bat (one-time fetch, may take ~30 s)...")
    bwar = data_service._bwar_bat_all()

    recent = bwar[bwar["year_ID"] >= START_YEAR]
    all_ids: list[int] = sorted(
        int(pid)
        for pid in recent["mlb_ID"].dropna().unique()
        if pid > 0
    )
    log.info(f"Found {len(all_ids)} unique batters with seasons since {START_YEAR}")

    with connection.get_session() as db:
        rows = db.query(PlayerSeason.player_id).distinct().all()
    already_loaded: set[int] = {r.player_id for r in rows}
    log.info(f"{len(already_loaded)} batters already in DB — skipping them")

    to_process = [pid for pid in all_ids if pid not in already_loaded]
    if limit is not None:
        to_process = to_process[:limit]
        log.info(f"--limit {limit}: processing first {len(to_process)} batters")
    else:
        log.info(f"{len(to_process)} batters queued for processing")

    if not to_process:
        return 0, 0, []

    loaded = 0
    skipped = 0
    failed: list[int] = []
    for i, player_id in enumerate(to_process, 1):
        prefix = f"[bat {i:>5}/{len(to_process)}]"
        try:
            name = data_service.fetch_and_save_batting_career(player_id)
            if name is None:
                log.info(f"{prefix}  {player_id} — no career data found, skipping")
                skipped += 1
            else:
                log.info(f"{prefix}  {name} ({player_id}) — saved")
                loaded += 1
        except Exception as exc:
            log.error(f"{prefix}  player {player_id} FAILED: {exc}")
            failed.append(player_id)
        time.sleep(DELAY_BETWEEN_PLAYERS)

    return loaded, skipped, failed


def _load_pitchers(limit: int | None) -> tuple[int, int, list[int]]:
    log.info("Fetching bwar_pitch (one-time fetch)...")
    bwar = data_service._bwar_pitch_all()

    recent = bwar[bwar["year_ID"] >= START_YEAR]
    all_ids: list[int] = sorted(
        int(pid)
        for pid in recent["mlb_ID"].dropna().unique()
        if pid > 0
    )
    log.info(f"Found {len(all_ids)} unique pitchers with seasons since {START_YEAR}")

    with connection.get_session() as db:
        rows = db.query(PitcherSeason.player_id).distinct().all()
    already_loaded: set[int] = {r.player_id for r in rows}
    log.info(f"{len(already_loaded)} pitchers already in DB — skipping them")

    to_process = [pid for pid in all_ids if pid not in already_loaded]
    if limit is not None:
        to_process = to_process[:limit]
        log.info(f"--limit {limit}: processing first {len(to_process)} pitchers")
    else:
        log.info(f"{len(to_process)} pitchers queued for processing")

    if not to_process:
        return 0, 0, []

    loaded = 0
    skipped = 0
    failed: list[int] = []
    for i, player_id in enumerate(to_process, 1):
        prefix = f"[pit {i:>5}/{len(to_process)}]"
        try:
            name = data_service.fetch_and_save_pitching_career(player_id)
            if name is None:
                log.info(f"{prefix}  {player_id} — no career data found, skipping")
                skipped += 1
            else:
                log.info(f"{prefix}  {name} ({player_id}) — saved")
                loaded += 1
        except Exception as exc:
            log.error(f"{prefix}  pitcher {player_id} FAILED: {exc}")
            failed.append(player_id)
        time.sleep(DELAY_BETWEEN_PLAYERS)

    return loaded, skipped, failed


def main() -> None:
    args = parse_args()

    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set. Export it and re-run.")

    connection.init_db()

    # --------------------------- batters ---------------------------
    log.info("=" * 52)
    log.info("Phase 1: batters")
    log.info("=" * 52)
    bat_loaded, bat_skipped, bat_failed = _load_batters(args.limit)

    # --------------------------- pitchers ---------------------------
    log.info("=" * 52)
    log.info("Phase 2: pitchers")
    log.info("=" * 52)
    pit_loaded, pit_skipped, pit_failed = _load_pitchers(args.limit)

    # --------------------------- summary ---------------------------
    bar = "=" * 52
    print(f"\n{bar}")
    print("Bulk load complete")
    print(f"  Batters  — loaded: {bat_loaded:>5}  skipped: {bat_skipped:>5}  failed: {len(bat_failed):>5}")
    print(f"  Pitchers — loaded: {pit_loaded:>5}  skipped: {pit_skipped:>5}  failed: {len(pit_failed):>5}")
    if bat_failed:
        print(f"\n  Failed batter IDs (retry these manually):")
        for pid in bat_failed:
            print(f"    {pid}")
    if pit_failed:
        print(f"\n  Failed pitcher IDs (retry these manually):")
        for pid in pit_failed:
            print(f"    {pid}")
    print(bar)


if __name__ == "__main__":
    main()
