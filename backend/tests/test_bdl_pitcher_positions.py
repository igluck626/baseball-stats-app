#!/usr/bin/env python3
"""Which balldontlie position strings mean "pitcher".

⚠️ balldontlie spells some pitchers out — "Pitcher", "Starting Pitcher",
"Relief Pitcher" — instead of "SP"/"RP". The set used to create a new bio held
only the codes, so those players were filed as batters (Tyler Uberstine, J.P.
France, Christian Roa on 2026-09-30). The scoring rubric had its own fuller
copy; there is now one set, and this pins both.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_bdl_pitcher_positions.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import data_service as ds                                         # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


for pos in ("P", "SP", "RP", "CL", "Pitcher", "Starting Pitcher", "Relief Pitcher"):
    check(f"{pos!r} is a pitcher", ds._bdl_is_pitcher({"position": pos}))
for pos in ("C", "SS", "LF", "DH", "OF", "Outfielder", "Catcher", "Unspecified Position", ""):
    check(f"{pos!r} is not", not ds._bdl_is_pitcher({"position": pos}))
check("the scoring rubric uses the same set", ds._BDL_PITCHER_POSITION_LABELS is ds._BDL_PITCHER_POSITIONS)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
