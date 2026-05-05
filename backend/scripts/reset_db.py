#!/usr/bin/env python3
"""Clear every row from player_seasons, pitcher_seasons, players, pitchers.

The tables (and their indexes) are left in place — only the data is wiped.
Re-run bulk_load.py afterward to repopulate.

Usage:
    DATABASE_URL=... backend/venv/bin/python backend/scripts/reset_db.py
"""

import logging
import os
import sys
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, _SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

from database import connection                                      # noqa: E402
from database.models import (                                          # noqa: E402
    BattingGameLog, Pitcher, PitcherSeason, PitchingGameLog, Player,
    PlayerAllstar, PlayerAward, PlayerFielding, PlayerHof,
    PlayerPostseasonBatting, PlayerPostseasonPitching,
    PlayerSeason, TeamSeason,
)

# Order: child/data tables before parent player/pitcher tables — no FK
# constraints today but this matches the conceptual ownership.
TABLES = [
    ("player_seasons",             PlayerSeason),
    ("pitcher_seasons",            PitcherSeason),
    ("player_fielding",            PlayerFielding),
    ("player_awards",              PlayerAward),
    ("player_allstar",             PlayerAllstar),
    ("player_postseason_batting",  PlayerPostseasonBatting),
    ("player_postseason_pitching", PlayerPostseasonPitching),
    ("player_hof",                 PlayerHof),
    ("batting_gamelogs",           BattingGameLog),
    ("pitching_gamelogs",          PitchingGameLog),
    ("team_seasons",               TeamSeason),
    ("players",                    Player),
    ("pitchers",                   Pitcher),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _safe_target() -> str:
    """Return the DB host (no credentials) so we log what's getting wiped."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return "<unset>"
    p = urlparse(url)
    host = p.hostname or "?"
    db = (p.path or "/?").lstrip("/")
    return f"{host}/{db}"


def clear_all() -> dict[str, int]:
    """Delete every row from the four stats tables. Returns {table_name: rows_deleted}.

    Used by both the CLI script and the POST /admin/reset-db endpoint, so there
    is exactly one place that knows how to wipe the data.
    """
    deleted: dict[str, int] = {}
    with connection.get_session() as db:
        for name, model in TABLES:
            n = db.query(model).delete(synchronize_session=False)
            deleted[name] = n
    return deleted


def main() -> None:
    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set. Export it and re-run.")

    log.info(f"Target database: {_safe_target()}")

    # CLI variant: also log the before-count for visibility.
    with connection.get_session() as db:
        before = {name: db.query(model).count() for name, model in TABLES}
    for name, _ in TABLES:
        log.info(f"  {name:<20}  rows before: {before[name]:>9,}")

    deleted = clear_all()
    for name, _ in TABLES:
        log.info(f"  {name:<20}  deleted:     {deleted[name]:>9,}")

    bar = "=" * 52
    print(f"\n{bar}")
    print("Reset complete — all four tables are now empty.")
    print("Schema and indexes are intact. Run bulk_load.py to repopulate.")
    print(bar)
    for name, _ in TABLES:
        print(f"  {name:<20} {deleted[name]:>10,} rows deleted")
    print(bar)


if __name__ == "__main__":
    main()
