#!/usr/bin/env python3
"""A final game whose /stats lines credit no win and no loss: its decisions were
not shipped, and are stored NULL, never the 0 balldontlie sends.

⚠️ THE CHECK THAT MATTERS: 12 games of 2026-05-01..07 ship wins = losses =
saves = 0 on every pitching line, while balldontlie's team totals and standings
count their decisions (no other endpoint records them per game: checked the game
record, scoring summary, plays and the docs on 2026-10-18). Reading those zeros
lost 31 real decisions. Now W/L/SV/HLD/BS and result are NULL for such a game;
games started is not a decision and stays; a normal game is untouched.

Real pages (fixtures/bdl_unshipped_decisions.json): game 5058241 and, as the
control, game 5057789. Standalone, no pytest.
Run: <backend python> backend/tests/test_unshipped_decisions.py
"""
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


FX = json.load(open(os.path.join(HERE, "fixtures", "bdl_unshipped_decisions.json")))["games"]


def run(gid):
    case = FX[gid]
    page = case["page"]
    orig = ds._bdl_get_json
    ds._bdl_get_json = lambda path, params=None: page if path == "stats" else {"data": [], "meta": {}}
    try:
        mapping = {int(s["player"]["id"]): 100000 + i for i, s in enumerate(page["data"])}
        return ds.fetch_bdl_game_stats(int(gid), mapping, ds._bdl_game_ctx(case["game"]))
    finally:
        ds._bdl_get_json = orig


_, pit = run("5058241")
rows = [r for rs in pit.values() for r in rs]
check("game 5058241: no line has a win or a loss — decisions not shipped",
      not ds._decisions_shipped(FX["5058241"]["page"]["data"]))
check("every pitching row's W, L, SV, HLD, BS is NULL (balldontlie sent 0)",
      rows and all(r[c] is None for r in rows for c in ("W", "L", "SV", "HLD", "BS")),
      [(r["W"], r["L"]) for r in rows][:3])
check("...and the result is NULL (unknown), not 'ND'", all(r["result"] is None for r in rows))
check("games started is kept: the two starters still read GS = 1",
      sum(r["GS"] or 0 for r in rows) == 2, [r["GS"] for r in rows])
_, pit = run("5057789")
rows = [r for rs in pit.values() for r in rs]
check("a normal game is untouched: one win and one loss", sum(r["W"] for r in rows) == 1 and sum(r["L"] for r in rows) == 1)
check("...and Estrada's loss + blown save is still read", any(r["L"] == 1 and r["BS"] == 1 and r["result"] == "L" for r in rows))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
