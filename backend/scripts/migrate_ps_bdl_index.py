#!/usr/bin/env python3
"""Replace the postseason tables' partial balldontlie unique index.

The first version, `uq_ps_{bat,pit}_game_bdl` on (game_id, bdl_player_id)
WHERE player_id IS NULL, only covered UNMAPPED rows: once a player was mapped,
re-ingesting a game gave his row a player_id and neither index saw the earlier
row, so the same appearance was stored twice. The replacement,
`uq_ps_{bat,pit}_game_bdl_any`, holds whether or not player_id is set
(WHERE bdl_player_id IS NOT NULL) — the same shape as the Retrosheet index.

GUARDED: lists both tables' indexes before and after, and refuses to run if
any balldontlie row already exists (a unique index built over rows that
already duplicate would fail, and a drop would be a behaviour change on live
data). Dry run by default; --apply creates the new index, then drops the old,
in one transaction.

Boot will not bring the old index back: `init_db` only re-adds missing indexes
on the five core tables, never these.

Usage: python backend/scripts/migrate_ps_bdl_index.py [--apply]
"""
import argparse
import os
import sys

import psycopg2

TABLES = {"postseason_batting_gamelogs": "bat", "postseason_pitching_gamelogs": "pit"}


def indexes(cur) -> list[tuple]:
    cur.execute("SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE tablename = ANY(%s) ORDER BY 1, 2", (list(TABLES),))
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = con.cursor()
    print("BEFORE:")
    for row in indexes(cur):
        print("  ", *row)
    for table in TABLES:
        cur.execute(f"SELECT count(*) FROM {table} WHERE bdl_player_id IS NOT NULL OR source = 'bdl'")
        n = cur.fetchone()[0]
        print(f"{table}: {n} balldontlie rows")
        if n:
            print("REFUSED — balldontlie rows already exist; resolve before changing the index.")
            return 2
    if not args.apply:
        print("DRY RUN — nothing changed (add --apply).")
        return 0
    for table, tag in TABLES.items():
        cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_ps_{tag}_game_bdl_any "
                    f"ON {table} (game_id, bdl_player_id) WHERE bdl_player_id IS NOT NULL")
        cur.execute(f"DROP INDEX IF EXISTS uq_ps_{tag}_game_bdl")
    con.commit()
    print("AFTER:")
    after = indexes(cur)
    for row in after:
        print("  ", *row)
    names = {r[1] for r in after}
    ok = all(f"uq_ps_{t}_game_bdl_any" in names and f"uq_ps_{t}_game_bdl" not in names
             for t in TABLES.values())
    print("OK" if ok else "⚠️ the index set is not what was intended")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
