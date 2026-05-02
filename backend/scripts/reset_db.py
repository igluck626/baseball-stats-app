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
from database.models import Pitcher, PitcherSeason, Player, PlayerSeason   # noqa: E402

# Order: season tables before parent tables — even though there's no FK
# constraint today, this matches the conceptual ownership.
TABLES = [
    ("player_seasons",  PlayerSeason),
    ("pitcher_seasons", PitcherSeason),
    ("players",         Player),
    ("pitchers",        Pitcher),
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


def main() -> None:
    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set. Export it and re-run.")

    log.info(f"Target database: {_safe_target()}")

    deleted: dict[str, int] = {}
    with connection.get_session() as db:
        for name, model in TABLES:
            before = db.query(model).count()
            n = db.query(model).delete(synchronize_session=False)
            deleted[name] = n
            log.info(f"  {name:<20}  rows before: {before:>9,}  deleted: {n:>9,}")

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
