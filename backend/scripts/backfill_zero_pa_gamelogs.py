#!/usr/bin/env python3
"""Backfill the game-log rows the old zero-stat filters dropped.

Until commit 2 the BDL parsers dropped two kinds of real appearance:
  • batting lines with PA == AB == BB == 0 and no pitching line — defensive
    substitutes and pinch runners (Seager 06-30, Foscue 05-29);
  • pitching lines with IP 0 and no strikeout that still faced a batter
    (Milner 06-25: IP 0, BF 3).
The parsers keep both now. This script re-reads BDL `/stats` for every final
from `--start` on and writes the rows the new parsers produce that the old
ones did not — and nothing else.

WHAT IT WRITES, and why so narrowly:
  • ONLY rows admitted by the changed rules. A row that is missing for some
    other reason (a player mapped late, a game never ingested) is counted and
    reported but left alone — that is the mapping catch-up's job, and folding
    it in here would hide how big each gap is.
  • INSERT ... ON CONFLICT DO NOTHING on (player_id, game_id). An existing row
    is never rewritten, so no scorer's revision or repair is undone.
  • No deletions, and no new dedupe key.

⚠️ SCOPED TO 2026-05-19 ON, the BDL ingest cutover (see
`data_service._BDL_INGEST_CUTOVER`). Before it the MLB path owns the season
under bare gamePks, and a BDL row there is a second copy the (player_id,
game_id) key cannot see. For the same reason any candidate row whose player
already has a bare-gamePk row on the same ET date is EXCLUDED and reported
as a collision — the 05-19 Braves–Marlins game is held only under gamePk
823865, so its BDL twin would be exactly that duplicate.

Dry run by default: reads BDL and the DB, writes nothing, prints per-date
totals. `--write` performs the inserts.

Usage (the backend's Python 3.11 environment, DATABASE_URL + BDL_KEY set):
    python backend/scripts/backfill_zero_pa_gamelogs.py               # dry run
    python backend/scripts/backfill_zero_pa_gamelogs.py --write       # insert
    python backend/scripts/backfill_zero_pa_gamelogs.py --end 2026-06-01
"""
import argparse
import collections
import datetime
import json
import logging
import os
import sys
import time

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

import data_service                                              # noqa: E402
from database import connection, crud                            # noqa: E402
from database.models import BattingGameLog, PitchingGameLog      # noqa: E402
from sqlalchemy import text                                      # noqa: E402

log = logging.getLogger("backfill_zero_pa")


def _old_rules_dropped_batting(row: dict) -> bool:
    """The pre-commit-2 batting filter: dropped when PA, AB and BB were all 0.
    The new parser only returns such a row when it has no pitching line."""
    return not any((row.get(k) or 0) > 0 for k in ("PA", "AB", "BB"))


def _old_rules_dropped_pitching(row: dict) -> bool:
    """The pre-commit-2 pitching filter: dropped when IP <= 0 and K == 0. The
    new parser only returns such a row when batters_faced > 0."""
    return (row.get("IP") or 0) <= 0 and (row.get("SO") or 0) == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default=data_service._BDL_INGEST_CUTOVER.isoformat())
    ap.add_argument("--end", default=None, help="Inclusive ET date; default today (ET).")
    ap.add_argument("--write", action="store_true", help="Insert. Without it: dry run.")
    ap.add_argument("--json", default=None, help="Also write the per-date report here.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    start = datetime.date.fromisoformat(args.start)
    if data_service._before_bdl_cutover(start):
        print(f"refused: --start {start} is before the {data_service._BDL_INGEST_CUTOVER} "
              "BDL ingest cutover", file=sys.stderr)
        return 2
    end = (datetime.date.fromisoformat(args.end) if args.end
           else datetime.datetime.now(data_service._MLB_LOCAL_TZ).date())
    mode = "WRITE" if args.write else "DRY RUN"
    print(f"{mode}: ET dates {start}..{end}", file=sys.stderr)

    with connection.get_session() as db:
        if not args.write:
            db.execute(text("SET TRANSACTION READ ONLY"))
        bdl_to_mlbam = data_service._bdl_to_mlbam_map(db)
        have_bat = {(r[0], r[1]) for r in db.execute(text(
            "SELECT player_id, game_id FROM batting_gamelogs WHERE game_date >= :s"),
            {"s": start})}
        have_pit = {(r[0], r[1]) for r in db.execute(text(
            "SELECT player_id, game_id FROM pitching_gamelogs WHERE game_date >= :s"),
            {"s": start})}
        # Bare six-digit gamePks: the MLB path's vocabulary.
        bare_bat = {(r[0], r[1]) for r in db.execute(text(
            "SELECT player_id, game_date FROM batting_gamelogs "
            "WHERE game_date >= :s AND game_id ~ '^[0-9]{6}$'"), {"s": start})}
        bare_pit = {(r[0], r[1]) for r in db.execute(text(
            "SELECT player_id, game_date FROM pitching_gamelogs "
            "WHERE game_date >= :s AND game_id ~ '^[0-9]{6}$'"), {"s": start})}

    # BDL's `dates[]` is a UTC bucket and every game belongs to the ET date it
    # started on, so walk one bucket past `end` and keep games by ET date.
    per_date: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    to_insert_bat: list[dict] = []
    to_insert_pit: list[dict] = []
    collisions: list[dict] = []
    seen_games: set[str] = set()
    failed_buckets: list[str] = []
    unchecked: list[str] = []

    cur = start
    while cur <= end + datetime.timedelta(days=1):
        bucket = cur.isoformat()
        cur += datetime.timedelta(days=1)
        try:
            games = data_service.fetch_bdl_games_for_date(bucket, finals_only=True)
        except Exception as exc:
            log.warning("bucket %s: /games failed: %s", bucket, exc)
            failed_buckets.append(bucket)
            continue
        for g in games:
            ctx = data_service._bdl_game_ctx(g, fallback_date=bucket)
            gd = ctx.get("game_date")
            if gd is None or gd < start or gd > end or ctx["game_id"] in seen_games:
                continue
            seen_games.add(ctx["game_id"])
            day = per_date[gd.isoformat()]
            day["games"] += 1
            bat_by_pid, pit_by_pid = data_service.fetch_bdl_game_stats(
                int(ctx["game_id"]), bdl_to_mlbam, ctx)
            time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
            if not bat_by_pid and not pit_by_pid:
                # fetch_bdl_game_stats logs and swallows a failed fetch, so an
                # empty sheet is either a failure or a fully unmapped game —
                # either way this game was NOT checked, and says so.
                day["games_unchecked"] += 1
                unchecked.append(ctx["game_id"])
                continue

            for kind, by_pid, have, bare, dropped_before, sink in (
                ("bat", bat_by_pid, have_bat, bare_bat, _old_rules_dropped_batting, to_insert_bat),
                ("pit", pit_by_pid, have_pit, bare_pit, _old_rules_dropped_pitching, to_insert_pit),
            ):
                for pid, rows in by_pid.items():
                    for row in rows:
                        if (pid, row["game_id"]) in have:
                            day[f"{kind}_present"] += 1
                            continue
                        if not dropped_before(row):
                            # Missing for a reason these rules don't explain.
                            day[f"{kind}_missing_other"] += 1
                            continue
                        if (pid, row["game_date"]) in bare:
                            day[f"{kind}_collision"] += 1
                            collisions.append({"kind": kind, "player_id": pid,
                                               "game_id": row["game_id"],
                                               "game_date": row["game_date"].isoformat()})
                            continue
                        day[f"{kind}_new"] += 1
                        sink.append({**row, "player_id": pid})

    cols = ["games", "games_unchecked", "bat_new", "pit_new", "bat_collision", "pit_collision",
            "bat_missing_other", "pit_missing_other", "bat_present", "pit_present"]
    totals = collections.Counter()
    print("date\t" + "\t".join(cols))
    for d in sorted(per_date):
        totals.update(per_date[d])
        print(d + "\t" + "\t".join(str(per_date[d][c]) for c in cols))
    print("TOTAL\t" + "\t".join(str(totals[c]) for c in cols))
    if collisions:
        print(f"\ncollisions excluded ({len(collisions)}):")
        for c in collisions:
            print(f"  {c}")
    if unchecked:
        print(f"\n⚠️ {len(unchecked)} game(s) returned an empty stat sheet and were NOT "
              f"checked: {unchecked}")
    if failed_buckets:
        print(f"\n⚠️ /games failed for {len(failed_buckets)} UTC bucket(s): {failed_buckets}"
              " — those dates are NOT covered")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"mode": mode, "start": start.isoformat(), "end": end.isoformat(),
                       "per_date": {d: dict(c) for d, c in per_date.items()},
                       "totals": dict(totals), "collisions": collisions, "unchecked": unchecked,
                       "failed_buckets": failed_buckets}, f, indent=1, default=str)

    if not args.write:
        print(f"\nDRY RUN — nothing written. Would insert {len(to_insert_bat)} batting "
              f"and {len(to_insert_pit)} pitching rows.")
        return 0

    with connection.get_session() as db:
        for i in range(0, len(to_insert_bat), 500):
            crud.bulk_insert_gamelogs(db, BattingGameLog, to_insert_bat[i:i + 500])
        for i in range(0, len(to_insert_pit), 500):
            crud.bulk_insert_gamelogs(db, PitchingGameLog, to_insert_pit[i:i + 500])
    print(f"\nWROTE (ON CONFLICT DO NOTHING): {len(to_insert_bat)} batting and "
          f"{len(to_insert_pit)} pitching rows submitted. Verify with a segmented "
          "read-back — the submit count is not proof of a write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
