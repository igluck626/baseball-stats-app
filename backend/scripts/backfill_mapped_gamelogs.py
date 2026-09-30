#!/usr/bin/env python3
"""Backfill a season's regular-season game logs for players mapped late.

The nightly ingest skips any balldontlie player with no MLBAM mapping, so a
player mapped mid-season — by the Chadwick matcher (`chadwick_catchup.py`) —
has no game-log rows for the games he played while unmapped. This reads those
games from balldontlie and writes his rows, and nothing else.

HOW IT FINDS THE GAMES, and why this way:
  • `/stats?player_ids[]=…&seasons[]=…` lists each player's games — but as
    bare `game_id`s, with no date or season type.
  • `/games` IGNORES `ids[]` / `game_ids[]` silently (it returns everything,
    from 2000 on), so each game is read singly from `/games/{id}`.
  • Only REGULAR-SEASON FINALS are kept. The postseason is under way: a
    Wild Card game must never land in a regular-season table.
  • The rows come from the same per-game parser the nightly uses
    (`fetch_bdl_game_stats`), with the mapping restricted to these players.

⚠️ THE 2026 CUTOVER (`data_service._BDL_INGEST_CUTOVER`, 05-19). Before it the
legacy MLB path owns the season under bare six-digit gamePks, and a BDL row
there is a second copy the (player_id, game_id) key can't see. So the rule is
PER PLAYER AND ET DATE, for every player:
  • a game is written only if the player has NO legacy (bare-gamePk) row on
    that Eastern date — before or after the cutover;
  • a DOUBLEHEADER date on which he has a legacy row for one game is HELD and
    listed: which half the legacy row is can't be told from the date alone;
  • a row already present by (player_id, game_id) is skipped.

Dry run by default. The mapping may be supplied (`--approved`, {bdl_id: mlbam})
so the dry run can run before the mapping write; `--write` instead REQUIRES
each player's bdl_id to be mapped in our tables to that same MLBAM id, and
refuses any that isn't. Writes ONE ET DATE PER TRANSACTION with ON CONFLICT DO
NOTHING, counts the rows each date actually stored (RETURNING), and exits
non-zero if stored differs from submitted or a date fails. Season row counts
are printed before and after.

Usage (the backend's Python 3.11 environment, DATABASE_URL + BDL_KEY set):
    python backend/scripts/backfill_mapped_gamelogs.py --approved approved.json          # dry run
    python backend/scripts/backfill_mapped_gamelogs.py --approved approved.json --write
"""
import argparse
import collections
import datetime
import json
import sys
import os
import time

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

import data_service                                              # noqa: E402
from database import connection                                  # noqa: E402
from database.models import BattingGameLog, PitchingGameLog      # noqa: E402
from sqlalchemy import text                                      # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert   # noqa: E402

_BARE_GAMEPK = r"^[0-9]{6}$"      # the legacy MLB path's game-id vocabulary


def classify_row(row: dict, pid: int, *, have: set, bare: set, games_on: dict) -> str:
    """What to do with one parsed row for player `pid`. Pure.

    `have`: (player_id, game_id) already stored. `bare`: (player_id, ET date)
    holding a legacy bare-gamePk row. `games_on`: {(player_id, ET date): number
    of balldontlie regular-season games he played that date}."""
    if (pid, row["game_id"]) in have:
        return "present"
    key = (pid, row["game_date"])
    if key in bare:
        return "doubleheader_held" if games_on.get(key, 1) > 1 else "legacy_on_date"
    return "new"


def _insert(db, model, rows: list[dict]) -> int:
    """Rows actually stored; an existing (player_id, game_id) is never rewritten."""
    if not rows:
        return 0
    stmt = (pg_insert(model).values(rows)
            .on_conflict_do_nothing(index_elements=["player_id", "game_id"])
            .returning(model.player_id))
    return len(db.execute(stmt).fetchall())


def _season_counts(db, season: int) -> tuple[int, int]:
    return tuple(db.execute(text(f"SELECT count(*) FROM {t} WHERE season = :s"), {"s": season}).scalar()
                 for t in ("batting_gamelogs", "pitching_gamelogs"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--approved", required=True, help="JSON {bdl_id: mlbam}")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--write", action="store_true", help="Insert. Without it: dry run.")
    ap.add_argument("--json", default=None, help="Also write the per-player report here.")
    args = ap.parse_args()
    approved = {int(k): int(v) for k, v in json.load(open(args.approved)).items()}
    season = args.season

    with connection.get_session() as db:
        if not args.write:
            db.execute(text("SET TRANSACTION READ ONLY"))
        db_map = data_service._bdl_to_mlbam_map(db)
        pids = list(approved.values())
        have_bat = {tuple(r) for r in db.execute(text(
            "SELECT player_id, game_id FROM batting_gamelogs WHERE season = :s AND player_id = ANY(:p)"),
            {"s": season, "p": pids})}
        have_pit = {tuple(r) for r in db.execute(text(
            "SELECT player_id, game_id FROM pitching_gamelogs WHERE season = :s AND player_id = ANY(:p)"),
            {"s": season, "p": pids})}
        bare_bat = {tuple(r) for r in db.execute(text(
            "SELECT player_id, game_date FROM batting_gamelogs WHERE season = :s AND player_id = ANY(:p) "
            "AND game_id ~ :re"), {"s": season, "p": pids, "re": _BARE_GAMEPK})}
        bare_pit = {tuple(r) for r in db.execute(text(
            "SELECT player_id, game_date FROM pitching_gamelogs WHERE season = :s AND player_id = ANY(:p) "
            "AND game_id ~ :re"), {"s": season, "p": pids, "re": _BARE_GAMEPK})}

    # Which players this run may touch. A write needs the mapping in our tables.
    target: dict[int, int] = {}
    refused: dict[int, str] = {}
    for bdl_id, mlbam in approved.items():
        on = db_map.get(bdl_id)
        if on == mlbam:
            target[bdl_id] = mlbam
        elif on is not None:
            refused[bdl_id] = f"bdl_id is mapped to {on}, not {mlbam}"
        elif args.write:
            refused[bdl_id] = "not mapped yet — run the mapping write first"
        else:
            target[bdl_id] = mlbam        # dry run: preview with the approved mapping
    mode = "WRITE" if args.write else "DRY RUN"
    print(f"{mode}: {len(target)} players, season {season}", file=sys.stderr)

    # 1. Each player's game ids.
    games_of: dict[int, set] = collections.defaultdict(set)
    ids = sorted(target)
    for i in range(0, len(ids), 25):
        cursor = None
        while True:
            params = {"player_ids[]": ids[i:i + 25], "seasons[]": [season], "per_page": 100}
            if cursor:
                params["cursor"] = cursor
            d = data_service._bdl_get_json("stats", params)
            for r in d.get("data") or []:
                games_of[r["game_id"]].add(r["player"]["id"])
            cursor = (d.get("meta") or {}).get("next_cursor")
            time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
            if not cursor:
                break

    # 2. Each game singly; keep regular-season finals; parse our players' rows.
    per_player: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    to_bat: list[dict] = []
    to_pit: list[dict] = []
    failed_games: list[int] = []
    parsed: list[tuple] = []                 # (kind, pid, row) — classified once all games are read
    games_on: dict = collections.Counter()   # (pid, ET date) -> regular-season games that day
    for gid in sorted(games_of):
        try:
            g = (data_service._bdl_get_json(f"games/{gid}", {}) or {}).get("data") or {}
        except Exception:
            failed_games.append(gid)
            continue
        time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
        who = [target[b] for b in games_of[gid] if b in target]
        if g.get("season_type") != "regular":
            for pid in who:
                per_player[pid][f"skipped_{g.get('season_type') or 'unknown'}"] += 1
            continue
        if g.get("status") != "STATUS_FINAL":
            for pid in who:
                per_player[pid]["skipped_not_final"] += 1
            continue
        ctx = data_service._bdl_game_ctx(g)
        bat_by_pid, pit_by_pid = data_service.fetch_bdl_game_stats(gid, {b: target[b] for b in games_of[gid] if b in target}, ctx)
        time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
        if not bat_by_pid and not pit_by_pid:
            failed_games.append(gid)          # the parser swallows fetch errors: unchecked
            continue
        for pid in {target[b] for b in games_of[gid] if b in target}:
            games_on[(pid, ctx["game_date"])] += 1
        for kind, by_pid in (("bat", bat_by_pid), ("pit", pit_by_pid)):
            for pid, rows in by_pid.items():
                for row in rows:
                    parsed.append((kind, pid, row))

    # A doubleheader is only visible once every game is read, so classify last.
    held: list[dict] = []
    for kind, pid, row in parsed:
        have, bare, sink = (have_bat, bare_bat, to_bat) if kind == "bat" else (have_pit, bare_pit, to_pit)
        verdict = classify_row(row, pid, have=have, bare=bare, games_on=games_on)
        per_player[pid][f"{kind}_{verdict}"] += 1
        if verdict == "new":
            sink.append({**row, "player_id": pid})
        elif verdict in ("legacy_on_date", "doubleheader_held"):
            held.append({"kind": kind, "player_id": pid, "game_id": row["game_id"],
                         "game_date": row["game_date"].isoformat(), "verdict": verdict})

    cols = ["bat_new", "pit_new", "bat_present", "pit_present", "bat_legacy_on_date", "pit_legacy_on_date",
            "bat_doubleheader_held", "pit_doubleheader_held", "skipped_postseason", "skipped_not_final"]
    name_of = {}
    with connection.get_session() as db:
        for pid, name in db.execute(text("SELECT player_id, name FROM players WHERE player_id = ANY(:p) "
                                         "UNION SELECT player_id, name FROM pitchers WHERE player_id = ANY(:p)"),
                                    {"p": list(target.values())}):
            name_of[pid] = name
    print("mlbam\tname\t" + "\t".join(cols))
    totals = collections.Counter()
    for bdl_id, pid in sorted(target.items(), key=lambda kv: kv[1]):
        c = per_player[pid]
        totals.update(c)
        print(f"{pid}\t{name_of.get(pid, '(no bio yet)')}\t" + "\t".join(str(c[k]) for k in cols))
    print("TOTAL\t\t" + "\t".join(str(totals[k]) for k in cols))
    other = {k: v for k, v in totals.items() if k.startswith("skipped_") and k not in cols}
    if other:
        print(f"other skips: {other}")
    dh = [h for h in held if h["verdict"] == "doubleheader_held"]
    if dh:
        print(f"\nHELD — doubleheader dates with one legacy row ({len(dh)}):")
        for h in dh:
            print(f"  {h}")
    if refused:
        print(f"\nrefused ({len(refused)}): {refused}")
    if failed_games:
        print(f"\n⚠️ {len(failed_games)} game(s) could not be read and were NOT checked: {failed_games}")
    if args.json:
        json.dump({"mode": mode, "held": held, "new_rows": [
                       {"kind": k, "player_id": r["player_id"], "game_id": r["game_id"],
                        "game_date": r["game_date"].isoformat()} for k, rs in (("bat", to_bat), ("pit", to_pit))
                       for r in rs],
                   "per_player": {str(p): dict(c) for p, c in per_player.items()},
                   "totals": dict(totals), "refused": refused, "failed_games": failed_games},
                  open(args.json, "w"), indent=1, default=str)

    if not args.write:
        print(f"\nDRY RUN — nothing written. Would insert {len(to_bat)} batting and {len(to_pit)} pitching rows.")
        return 0

    by_date: dict[datetime.date, tuple[list, list]] = collections.defaultdict(lambda: ([], []))
    for r in to_bat:
        by_date[r["game_date"]][0].append(r)
    for r in to_pit:
        by_date[r["game_date"]][1].append(r)
    with connection.get_session() as db:
        before = _season_counts(db, season)
    print(f"\n{season} rows BEFORE: batting {before[0]}, pitching {before[1]}")
    stored_bat = stored_pit = 0
    for d in sorted(by_date):
        bats, pits = by_date[d]
        try:
            with connection.get_session() as db:      # commits on exit, rolls back on raise
                nb = _insert(db, BattingGameLog, bats)
                np_ = _insert(db, PitchingGameLog, pits)
        except Exception as exc:
            print(f"{d}\tFAILED — rolled back, stopping: {exc}")
            return 1
        stored_bat += nb
        stored_pit += np_
        print(f"{d}\tbat {nb}/{len(bats)}\tpit {np_}/{len(pits)}", flush=True)
    with connection.get_session() as db:
        after = _season_counts(db, season)
    print(f"stored: batting {stored_bat} of {len(to_bat)}, pitching {stored_pit} of {len(to_pit)}")
    print(f"{season} rows AFTER:  batting {after[0]} ({after[0] - before[0]:+d}), "
          f"pitching {after[1]} ({after[1] - before[1]:+d})")
    if (stored_bat, stored_pit) != (len(to_bat), len(to_pit)) or failed_games:
        print("⚠️ STORED ≠ SUBMITTED, or games went unchecked — investigate.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
