#!/usr/bin/env python3
"""A second balldontlie record of a player we map (data/bdl_id_aliases.json):
read for game lines only, one appearance per game, the line with more known
fields, never summed.

⚠️ THE CHECKS THAT MATTER: Seth Halvorsen has two records. 5868766 (mapped)
carries all 32 of his 2026 games, but its 21 Colorado lines lack HBP, WP and
decisions; 1015 carries those 21 with full detail. With 1015 as an alias:
  • a game with a line under both ids gives ONE pitching row, from 1015's line;
  • a Dodgers game (5868766 only) is unchanged;
  • the alias reaches game lines only — _bdl_to_mlbam_map (season stats and
    everything else) does not see it.

Real lines (fixtures/bdl_alias_halvorsen.json), kept whole.
Standalone, no pytest. Run: <backend python> backend/tests/test_bdl_alias.py
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


FX = json.load(open(os.path.join(HERE, "fixtures", "bdl_alias_halvorsen.json")))["games"]
HALVORSEN = 678020
check("the alias file names Halvorsen only: 1015 -> 5868766", ds._bdl_aliases() == {1015: 5868766}, ds._bdl_aliases())


def run(gid, with_alias=True):
    case = FX[gid]
    mapping = {5868766: HALVORSEN}
    if with_alias:
        mapping[1015] = HALVORSEN
    orig = ds._bdl_get_json
    ds._bdl_get_json = lambda path, params=None: case["page"] if path == "stats" else {"data": [], "meta": {}}
    try:
        return ds.fetch_bdl_game_stats(int(gid), mapping, ds._bdl_game_ctx(case["game"]))
    finally:
        ds._bdl_get_json = orig


col, lad = sorted(FX, key=lambda g: g != "5058496")
_, pit = run(col)
rows = pit.get(HALVORSEN, [])
check("Colorado game, lines under both ids: ONE pitching row, never two or a sum", len(rows) == 1, len(rows))
check("...taken from 1015's detailed line: HBP and WP known", rows and rows[0]["HBP"] is not None and rows[0]["WP"] is not None,
      rows and (rows[0]["HBP"], rows[0]["WP"]))
line_1015 = next(x for x in FX[col]["page"]["data"] if x["player"]["id"] == 1015)
check("...and its innings are the game's, not doubled", rows and rows[0]["IP"] == ds._ip_str_to_decimal(line_1015["ip"]))
_, pit = run(col, with_alias=False)
check("without the alias the same game reads 5868766's sparse line (HBP unknown)",
      pit[HALVORSEN][0]["HBP"] is None, pit[HALVORSEN][0]["HBP"])
_, pit = run(lad)
check("a Dodgers game (5868766 only): one row, as before", len(pit.get(HALVORSEN, [])) == 1)


class _Q:
    def filter(self, *a): return self
    def all(self): return []


class _DB:
    def query(self, *a): return _Q()


orig = ds._shared_bdl_ids
ds._shared_bdl_ids = lambda db: set()
try:
    check("season stats and other mappings don't see the alias", 1015 not in ds._bdl_to_mlbam_map(_DB()))
finally:
    ds._shared_bdl_ids = orig

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
