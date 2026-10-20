#!/usr/bin/env python3
"""The nightly catch-up for players the Chadwick step mapped: their games from
before the mapping, which no re-read reaches (catch_up_player_gamelogs).

Real data (fixtures/bdl_catch_up_hector.json, unmodified balldontlie responses):
Hector Rodriguez, mapped 2026-10-17 — 54 season lines (41 regular-season finals,
13 spring training) and the full sheets of 5059470 (08-04) and 5059858 (09-02).
The database and balldontlie are stubbed; the parser, the plays reading and the
row selection are the real ones.
Standalone, no pytest. Run: <backend python> backend/tests/test_catch_up.py
"""
import contextlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
import data_service as ds                                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "bdl_catch_up_hector.json")))
BDL, PID = FX["bdl_id"], FX["player_id"]
REGULAR = sorted(int(g) for g, x in FX["games"].items() if x["season_type"] == "regular")
SHEETS = sorted(int(g) for g in FX["sheets"])


def by_cursor(pages, cur):
    return pages[0] if cur is None else next(
        pages[i + 1] for i, p in enumerate(pages) if p["meta"].get("next_cursor") == cur)


def run(held_bat, held_pit=(), pairs=((BDL, PID),)):
    """Returns (summary, saved rows by table, balldontlie requests)."""
    saved = {"bat": [], "pit": []}
    asked = []

    class _Q:
        def __init__(self, table):
            self.table = table

        def filter(self, *a):
            return self

        def all(self):
            ids = held_bat if self.table == "batting_gamelogs" else held_pit
            return [(str(g),) for g in ids]

    class _Session:
        def query(self, col):
            return _Q(col.class_.__tablename__)

    class _Conn:
        @staticmethod
        def db_available():
            return True

        @staticmethod
        @contextlib.contextmanager
        def get_session():
            yield _Session()

    class _Crud:
        @staticmethod
        def save_batting_gamelogs(db, pid, rows):
            saved["bat"].extend((pid, r) for r in rows)

        @staticmethod
        def save_pitching_gamelogs(db, pid, rows):
            saved["pit"].extend((pid, r) for r in rows)

    def fake(path, params=None):
        params = params or {}
        asked.append((path, params.get("game_ids[]") or params.get("player_ids[]")))
        if path == "stats" and "player_ids[]" in params:
            return by_cursor(FX["season_pages"], params.get("cursor"))
        if path == "stats":
            return FX["sheets"][str(params["game_ids[]"][0])]["stats_page"]
        if path.startswith("games/"):
            return {"data": FX["games"][path.split("/")[1]]}
        if path == "plays":
            return by_cursor(FX["sheets"][str(params["game_id"])]["plays_pages"], params.get("cursor"))
        raise AssertionError(path)
    saved_attrs = (ds.connection, ds.crud, ds._bdl_get_json, ds._gamelog_bdl_map, ds._BDL_RATE_LIMIT_SLEEP)
    ds.connection, ds.crud, ds._bdl_get_json = _Conn, _Crud, fake
    ds._gamelog_bdl_map = lambda db: {BDL: PID}
    ds._BDL_RATE_LIMIT_SLEEP = 0
    try:
        return ds.catch_up_player_gamelogs(list(pairs), 2026), saved, asked
    finally:
        ds.connection, ds.crud, ds._bdl_get_json, ds._gamelog_bdl_map, ds._BDL_RATE_LIMIT_SLEEP = saved_attrs


check("the fixture: 54 lines, all Hector's; 41 regular-season finals",
      sum(len(p["data"]) for p in FX["season_pages"]) == 54 and len(REGULAR) == 41
      and all(ln["player"]["id"] == BDL for p in FX["season_pages"] for ln in p["data"]))

print("two games missing")
held = [g for g in REGULAR if g not in SHEETS]
out, saved, asked = run(held)
check("exactly the two missing games saved, one batting row each",
      sorted(int(r["game_id"]) for _, r in saved["bat"]) == SHEETS and not saved["pit"], saved)
check("  only Hector's rows — no other player in those sheets", {p for p, _ in saved["bat"]} == {PID})
check("  summary: 2 games filled, 2 batting rows, 0 pitching", out["games_filled"] == 2
      and out["bat_rows"] == 2 and out["pit_rows"] == 0 and out["lines"] == 54, out)
r0 = next(r for _, r in saved["bat"] if r["game_id"] == "5059470")
check("  08-04 as the 2a dry run planned it: PA 3, AB 2, R 1, H 0, BB 1, SO 0",
      (r0["PA"], r0["AB"], r0["R"], r0["H"], r0["BB"], r0["SO"]) == (3, 2, 1, 0, 1, 0), r0)
sheets_read = sorted({ids[0] for p, ids in asked if p == "stats" and ids and ids[0] != BDL})
check("  sheets read only for the two missing regular-season games — never a spring game",
      sheets_read == SHEETS, sheets_read)

print("nothing missing: the log reads 0")
out, saved, asked = run(REGULAR)
check("every game held: nothing saved, 0 games filled", out["games_filled"] == 0
      and not saved["bat"] and not saved["pit"], out)
check("  no game sheet read", not any(p == "stats" and ids and ids[0] != BDL for p, ids in asked), asked)
out, saved, asked = run([], pairs=())
check("no one mapped tonight: no balldontlie call at all", not asked and out["players"] == 0)

print("legacy rows")
orig = ds._before_bdl_cutover
ds._before_bdl_cutover = lambda d: True              # as if both games were before 05-19
try:
    out, saved, _ = run(held + [822001])           # a six-digit MLB gamePk still held
finally:
    ds._before_bdl_cutover = orig
check("a legacy MLB-id row held: pre-cutover games skipped, never doubled",
      out["skipped_pre_cutover"] == 2 and not saved["bat"], out)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
