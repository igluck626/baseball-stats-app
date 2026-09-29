#!/usr/bin/env python3
"""Recompute stored hitting-streak leaderboard rows under the current rule.

`game_unit_leaderboard` holds each player's longest hitting streak per season,
computed once by the backfill and refreshed nightly only for players who are
still playing. A change to the streak rule (daa5df4's sac fly, c567127's era
gate) or to the game logs underneath therefore leaves every retired player's
row on the old answer. This recomputes those rows with the app's own
`_daily_games` + `_longest_streak` — imported, never re-implemented, so a fix to
either (the doubleheader ordering, for one) is picked up by re-running this.

SCOPE, and why:
  • metric = 'hitting_streak', role 'bat', window 0 — nothing else. Multi-hit,
    on-base and HR streaks do not change under the sac-fly rule.
  • season < 2026. The current season belongs to the nightly's leaderboard
    refresh, which recomputes it under the same rule.
  • EXISTING rows only. A season that has no stored row is not created here,
    and a stored row whose recompute comes out empty is reported, not deleted.

A WRITE updates rows in place, one transaction per season, keyed on the
primary key and GUARDED on the stored length, start and end dates read at the
start of the run: a row that changed since then matches nothing and is
reported as a guard skip rather than overwritten. A database error stops the
run; seasons already committed stay committed.

EXCLUSIONS: `--exclude PLAYER_ID:SEASON`, repeatable. Use it for a row whose
game log is known to be wrong — 110250:1962 (Craig Anderson: our 1962-07-24 row
says AB 1, MLB says he did not bat), until that row is corrected.

Usage (the backend's Python 3.11 environment, DATABASE_URL set):
    python backend/scripts/recompute_hitting_streaks.py                     # dry run
    python backend/scripts/recompute_hitting_streaks.py --exclude 110250:1962 --write
"""
import argparse
import collections
import concurrent.futures as cf
import json
import logging
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

import main                                                      # noqa: E402
from database import connection                                  # noqa: E402
from sqlalchemy import text                                      # noqa: E402

_FIRST_UNTOUCHED_SEASON = 2026


def _stored_rows() -> dict[tuple[int, int], tuple]:
    """{(player_id, season): (value, start_date, end_date, event)}"""
    with connection.get_session() as db:
        rows = db.execute(text(
            "SELECT player_id, season, value, start_date, end_date, event "
            "FROM game_unit_leaderboard WHERE role = 'bat' AND metric = 'hitting_streak' "
            "AND \"window\" = 0 AND season < :s"), {"s": _FIRST_UNTOUCHED_SEASON}).fetchall()
    return {(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in rows}


def _recompute(pid: int, seasons: set[int]) -> dict[int, tuple]:
    games = main._daily_games(pid)
    out = {}
    for s in seasons:
        length, start, end, _run = main._longest_streak([g for g in games if g["season"] == s], "hitting")
        out[s] = (length, start, end)
    return out


def main_() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="Update rows. Without it: dry run.")
    ap.add_argument("--exclude", action="append", default=[], metavar="PLAYER_ID:SEASON")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", default=None, help="Also write the change list here.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    excluded = {tuple(int(x) for x in e.split(":")) for e in args.exclude}

    stored = _stored_rows()
    by_player: dict[int, set[int]] = collections.defaultdict(set)
    for pid, s in stored:
        by_player[pid].add(s)
    print(f"{'WRITE' if args.write else 'DRY RUN'}: {len(stored)} stored hitting_streak rows, "
          f"{len(by_player)} players, seasons < {_FIRST_UNTOUCHED_SEASON}", file=sys.stderr)

    changes, empty, skipped_excluded = [], [], []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_recompute, pid, seasons): pid for pid, seasons in by_player.items()}
        for f in cf.as_completed(futs):
            pid = futs[f]
            for s, new in f.result().items():
                old = stored[(pid, s)]
                if new[0] == 0:
                    empty.append((pid, s, old))
                    continue
                if tuple(new) == tuple(old[:3]):
                    continue
                (skipped_excluded if (pid, s) in excluded else changes).append((pid, s, old, new))

    changes.sort(key=lambda c: (c[1], c[0]))
    print("season\tplayer_id\told\told_start\told_end\tnew\tnew_start\tnew_end")
    for pid, s, old, new in changes:
        print(f"{s}\t{pid}\t{old[0]}\t{old[1]}\t{old[2]}\t{new[0]}\t{new[1]}\t{new[2]}")
    print(f"\nwould change: {len(changes)} "
          f"({sum(1 for c in changes if c[2][0] != c[3][0])} length, "
          f"{sum(1 for c in changes if c[2][0] == c[3][0])} dates only)")
    for pid, s, old, new in skipped_excluded:
        print(f"EXCLUDED {pid}:{s}  {old[:3]} -> {new}")
    for pid, s, old in empty:
        print(f"⚠️ recompute EMPTY, row left alone: {pid}:{s} stored {old[:3]}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"changes": changes, "excluded": skipped_excluded, "empty": empty},
                      fh, indent=1, default=str)
    if not args.write:
        print("DRY RUN — nothing written.")
        return 0

    by_season: dict[int, list] = collections.defaultdict(list)
    for c in changes:
        by_season[c[1]].append(c)
    updated = guard_skips = 0
    print("\nseason\tsubmitted\tupdated\tguard_skipped")
    for s in sorted(by_season):
        rows, n_upd, skips = by_season[s], 0, []
        try:
            with connection.get_session() as db:       # commits on exit, rolls back on raise
                for pid, _s, old, new in rows:
                    rc = db.execute(text(
                        "UPDATE game_unit_leaderboard SET value = :nv, start_date = :ns, end_date = :ne "
                        "WHERE player_id = :p AND metric = 'hitting_streak' AND \"window\" = 0 "
                        "AND season = :s AND event = :ev AND role = 'bat' "
                        "AND value = :ov AND start_date IS NOT DISTINCT FROM :os "
                        "AND end_date IS NOT DISTINCT FROM :oe"),
                        {"nv": new[0], "ns": new[1], "ne": new[2], "p": pid, "s": s, "ev": old[3],
                         "ov": old[0], "os": old[1], "oe": old[2]}).rowcount
                    if rc == 1:
                        n_upd += 1
                    else:
                        skips.append(pid)
        except Exception as exc:
            print(f"{s}\tFAILED — rolled back, stopping: {exc}")
            print(f"UPDATED {updated}, guard skips {guard_skips}; seasons before {s} are committed.")
            return 1
        updated += n_upd
        guard_skips += len(skips)
        print(f"{s}\t{len(rows)}\t{n_upd}\t{len(skips)}" + (f"\tskipped: {skips}" if skips else ""),
              flush=True)
    print(f"UPDATED {updated}, guard skips {guard_skips}.")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
