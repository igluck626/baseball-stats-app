#!/usr/bin/env python3
"""BDL `/season_stats` sends a regular AND a postseason row per player once he
has played in the postseason, in no guaranteed order — and ignores
`postseason=false`. `_fetch_bdl_batch_stats` must keep the regular row.

⚠️ THE CASE THIS EXISTS FOR is Cade Smith 2024, which BDL still ships
postseason-first (re-fetched 2026-09-28): 9 G / 10 IP, then 74 G / 75.1 IP.
Keeping the first row wrote the playoff line as his season.

Standalone, no pytest — matches test_streak_classify.py.
Run: python3 backend/tests/test_bdl_season_rows.py
"""
import ast
import logging
import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_SERVICE = os.path.join(HERE, "..", "api", "data_service.py")

tree = ast.parse(open(DATA_SERVICE).read())
wanted = {"_regular_season_rows", "_pick_regular_season_row", "_fetch_bdl_batch_stats"}
fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
assert {f.name for f in fns} == wanted, {f.name for f in fns}
pages: list = []
ns: dict = {"Optional": Optional, "log": logging.getLogger("t"), "_BDL_RATE_LIMIT_SLEEP": 0,
            "time": type("T", (), {"sleep": staticmethod(lambda s: None)}),
            "_bdl_get_json": lambda path, params: pages.pop(0)}
future = ast.parse("from __future__ import annotations").body
exec(compile(ast.Module(body=future + fns, type_ignores=[]), DATA_SERVICE, "exec"), ns)
fetch = ns["_fetch_bdl_batch_stats"]
pick = ns["_pick_regular_season_row"]

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def row(pid, post, gp, ip=None, ab=None):
    r = {"player": {"id": pid}, "pitching_gp": gp, "pitching_ip": ip, "batting_ab": ab}
    if post is not None:
        r["postseason"] = post
        r["season_type"] = "postseason" if post else "regular"
    return r


CADE_POST = row(841, True, 9, 10)          # BDL's first row
CADE_REG = row(841, False, 74, 75.1)

print("_fetch_bdl_batch_stats")
pages[:] = [{"data": [CADE_POST, CADE_REG], "meta": {}}]
out = fetch([841], 2024)
check("Cade Smith 2024, postseason row FIRST -> keeps the regular row",
      out.get(841) is CADE_REG, out.get(841))

pages[:] = [{"data": [row(208, False, 158, 14), row(2544, False, 102), row(208, True, 17, 4)],
             "meta": {}}]
out = fetch([208, 2544], 2025)
check("normal order (regular first) -> unchanged",
      out[208]["pitching_gp"] == 158 and out[2544]["pitching_gp"] == 102, out)

# The two rows can land on different pages of the same chunk.
pages[:] = [{"data": [CADE_POST], "meta": {"next_cursor": 7}},
            {"data": [CADE_REG], "meta": {}}]
out = fetch([841], 2024)
check("postseason row on page 1, regular on page 2 -> regular", out.get(841) is CADE_REG, out)

pages[:] = [{"data": [CADE_POST], "meta": {}}]
out = fetch([841], 2024)
check("postseason row only -> player left out, never the playoff line", 841 not in out, out)

pages[:] = [{"data": [row(5, None, 30, 40)], "meta": {}}]
out = fetch([5], 2020)
check("legacy row with no postseason flag -> kept", out.get(5, {}).get("pitching_gp") == 30, out)

print("_pick_regular_season_row (now built on the same rule)")
check("Cade Smith 2024 -> regular", pick([CADE_POST, CADE_REG], "pitching_ip") is CADE_REG)
check("postseason only -> None", pick([CADE_POST], "pitching_ip") is None)
check("stat filter still applies before the rule",
      pick([row(1, False, 3, None, None), row(1, True, 2, 1)], "pitching_ip") is None)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
