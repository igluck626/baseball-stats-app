#!/usr/bin/env python3
"""Bulk-populate PostgreSQL with the full historical baseball dataset.

Pipeline:
  Phase 0 — Lahman load    (1871 → last completed season, local CSVs)
  Phase 1 — WAR backfill   (joins null-WAR rows to bwar_bat / bwar_pitch)
  Phase 2 — Current season (pulls in-flight season from pybaseball)

Phases 0 and 1 are local CSV / cached fetches; phase 2 hits Baseball
Reference once for the current year. Total runtime is minutes, not hours.

Usage:
    DATABASE_URL=... backend/venv/bin/python backend/scripts/bulk_load.py

Requires DATABASE_URL to be set in the environment or backend/.env.
Safe to re-run — every phase skips rows already populated.
"""

import argparse
import datetime
import logging
import os
import sys

# ---------------------------------------------------------------------------
# Path setup — allow imports from backend/api, backend/scripts, backend/
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, _SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

import backfill_war                                       # noqa: E402
import lahman_load                                        # noqa: E402
import nightly_update                                     # noqa: E402
from database import connection                           # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    return argparse.ArgumentParser(
        description="Bulk-populate PostgreSQL: Lahman + WAR backfill + current season."
    ).parse_args()


def main() -> None:
    parse_args()

    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set. Export it and re-run.")

    connection.init_db()
    current_year = datetime.date.today().year

    log.info("=" * 52)
    log.info("Phase 0: Lahman historical (through last completed season)")
    log.info("=" * 52)
    lahman_load.run()

    log.info("=" * 52)
    log.info("Phase 1: WAR backfill via bwar_bat / bwar_pitch")
    log.info("=" * 52)
    backfill_war.run()

    log.info("=" * 52)
    log.info(f"Phase 2: Current season ({current_year}) — pybaseball")
    log.info("=" * 52)
    bat_updated, bat_skipped, bat_failed = nightly_update._update_batters(current_year)
    pit_updated, pit_skipped, pit_failed = nightly_update._update_pitchers(current_year)

    bar = "=" * 52
    print(f"\n{bar}")
    print("Bulk load complete")
    print(f"  Phase 2 batters  — updated: {bat_updated:>5}  skipped: {bat_skipped:>5}  failed: {len(bat_failed):>5}")
    print(f"  Phase 2 pitchers — updated: {pit_updated:>5}  skipped: {pit_skipped:>5}  failed: {len(pit_failed):>5}")
    print(bar)


if __name__ == "__main__":
    main()
