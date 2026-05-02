#!/usr/bin/env python3
"""Pre-populate PostgreSQL with historical batting stats for all MLB players since 1990.

Usage:
    backend/venv/bin/python backend/scripts/bulk_load.py

Requires DATABASE_URL to be set in the environment or backend/.env.
Safe to re-run — players with any seasons already in the database are skipped.
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

import data_service                       # noqa: E402
from database import connection           # noqa: E402
from database.models import PlayerSeason  # noqa: E402

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
    p = argparse.ArgumentParser(description="Bulk-load historical batting stats into PostgreSQL.")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after processing N players (useful for testing).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set. Export it and re-run.")

    connection.init_db()

    # ------------------------------------------------------------------
    # Load bwar_bat once — populates data_service's in-memory cache so
    # every subsequent get_career_stats() call hits the cache, not bref.
    # ------------------------------------------------------------------
    log.info("Fetching bwar_bat (one-time fetch, may take ~30 s)...")
    bwar = data_service._bwar_bat_all()

    recent = bwar[bwar["year_ID"] >= START_YEAR]
    all_ids: list[int] = sorted(
        int(pid)
        for pid in recent["mlb_ID"].dropna().unique()
        if pid > 0
    )
    log.info(f"Found {len(all_ids)} unique players with seasons since {START_YEAR}")

    # ------------------------------------------------------------------
    # Snapshot which players are already in the database (any season).
    # ------------------------------------------------------------------
    with connection.get_session() as db:
        rows = db.query(PlayerSeason.player_id).distinct().all()
    already_loaded: set[int] = {r.player_id for r in rows}
    log.info(
        f"{len(already_loaded)} players already in database — skipping them"
    )

    to_process = [pid for pid in all_ids if pid not in already_loaded]
    if args.limit is not None:
        to_process = to_process[: args.limit]
        log.info(f"--limit {args.limit}: processing first {len(to_process)} players")
    else:
        log.info(f"{len(to_process)} players queued for processing")

    if not to_process:
        log.info("Nothing to do.")
        return

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    current_year = data_service._current_year()
    loaded = 0
    skipped = 0
    failed: list[int] = []

    for i, player_id in enumerate(to_process, 1):
        prefix = f"[{i:>5}/{len(to_process)}]"
        try:
            result = data_service.get_career_stats(player_id)
            if result is None:
                log.info(f"{prefix}  {player_id} — no career data found, skipping")
                skipped += 1
            else:
                hist_count = sum(1 for s in result["seasons"] if s["year"] < current_year)
                log.info(
                    f"{prefix}  {result['name']} ({player_id})"
                    f" — {hist_count} historical seasons stored"
                )
                loaded += 1
        except Exception as exc:
            log.error(f"{prefix}  player {player_id} FAILED: {exc}")
            failed.append(player_id)

        time.sleep(DELAY_BETWEEN_PLAYERS)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    bar = "=" * 52
    print(f"\n{bar}")
    print("Bulk load complete")
    print(f"  Loaded:  {loaded:>5}")
    print(f"  Skipped: {skipped:>5}")
    print(f"  Failed:  {len(failed):>5}")
    if failed:
        print(f"\n  Failed player IDs (retry these manually):")
        for pid in failed:
            print(f"    {pid}")
    print(bar)


if __name__ == "__main__":
    main()
