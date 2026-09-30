#!/usr/bin/env python3
"""balldontlie postseason game logs for one season (see api/postseason_ingest.py).

Dry run by default: reads balldontlie, builds every row, prints per-round game
counts, row counts and the unmapped players (kept with player_id NULL), and
writes nothing. --write inserts and exits non-zero unless the season's stored
balldontlie rows equal what was submitted.

--compare-retro SEASON-SOURCE check, for a season Retrosheet has published:
sums both sources' rows per player (mapped players) and reports how many
agree stat for stat. Reads only.

Usage (the backend's Python 3.11 environment, DATABASE_URL + BDL_KEY set):
    python backend/scripts/bdl_postseason_ingest.py --season 2026            # dry run
    python backend/scripts/bdl_postseason_ingest.py --season 2026 --write
    python backend/scripts/bdl_postseason_ingest.py --season 2025 --compare-retro
"""
import argparse
import collections
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

import postseason_ingest                                          # noqa: E402

BAT = ("AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB", "SO", "SB", "CS", "HBP")
PIT = ("H", "R", "ER", "BB", "SO", "HR", "W", "L", "SV", "GS")


def compare_retro(c: dict) -> int:
    from sqlalchemy import text
    from database import connection
    out = 0
    for kind, rows, table, stats in (("bat", c["bat"], "postseason_batting_gamelogs", BAT),
                                     ("pit", c["pit"], "postseason_pitching_gamelogs", PIT)):
        bdl = collections.defaultdict(collections.Counter)
        for r in rows:
            if r["player_id"] is not None:
                for s in stats:
                    bdl[r["player_id"]][s] += r.get(s) or 0
                if kind == "pit":
                    bdl[r["player_id"]]["OUTS"] += round((r.get("IP") or 0) * 3)
        cols = ", ".join(f'sum("{s}")' if s not in ("doubles", "triples") else f"sum({s})" for s in stats)
        with connection.get_session() as db:
            q = db.execute(text(f"SELECT player_id, {cols}"
                                + (", sum(round(\"IP\"::numeric*3))" if kind == "pit" else "")
                                + f" FROM {table} WHERE source='retrosheet' AND season=:s "
                                  "AND player_id IS NOT NULL GROUP BY 1"), {"s": c["season"]}).fetchall()
        retro = {r[0]: collections.Counter(dict(zip(stats + (("OUTS",) if kind == "pit" else ()),
                                                    [int(v or 0) for v in r[1:]]))) for r in q}
        both = set(bdl) & set(retro)
        diff = {p: {s: (bdl[p][s], retro[p][s]) for s in set(bdl[p]) | set(retro[p]) if bdl[p][s] != retro[p][s]}
                for p in both}
        diff = {p: d for p, d in diff.items() if d}
        print(f"{kind}: {len(both)} players in both, exact {len(both) - len(diff)}, differ {len(diff)}; "
              f"only balldontlie {len(set(bdl) - set(retro))}, only Retrosheet {len(set(retro) - set(bdl))}")
        for p, d in sorted(diff.items())[:15]:
            print(f"   {p}: {d}")
        out += len(diff)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--compare-retro", action="store_true")
    args = ap.parse_args()
    c = postseason_ingest.collect(args.season)
    print(f"season {c['season']}: {c['report']}")
    print(f"rows: batting {len(c['bat'])}, pitching {len(c['pit'])}; "
          f"unmapped players {len(c['unmapped'])} (kept, player_id NULL): {c['unmapped']}")
    if c["round_withheld"]:
        print(f"⚠️ round withheld, NOT written: {c['round_withheld']}")
    if c["empty_stat_sheets"]:
        print(f"⚠️ empty stat sheets, NOT checked: {c['empty_stat_sheets']}")
    if args.compare_retro:
        compare_retro(c)
    if not args.write:
        print("DRY RUN — nothing written.")
        return 0
    r = postseason_ingest.write(c)
    for t in r["after"]:
        print(f"{t}: before {r['before'][t]}, after {r['after'][t]}, added {r['added'][t]}, "
              f"submitted {r['submitted'][t]}")
    if not r["ok"] or c["empty_stat_sheets"]:
        print("⚠️ STORED ≠ SUBMITTED, or games went unchecked — investigate.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
