#!/usr/bin/env python3
"""Does `_parse_bdl_dob` read every format BDL actually ships?

⚠️ WHY THIS EXISTS. The function was written for MM/DD/YY, which is 6% of
BDL's player table. The other 94% it either mangled — "18/2/2000" became the
year 3900, which is how a real player's birth year got corrupted — or dropped
entirely, returning None for the ISO form that BDL uses for essentially every
ACTIVE player. The consequence was quiet: `_score_bdl_candidate` calls this
for its "+50, strongest single signal", so that signal could almost never
fire, and the rubric guarding against name-twin collisions was running on four
signals out of five.

AST-extracted from data_service.py, like test_guard_tool.py beside it, because
importing that module pulls in the database and the whole FastAPI app.

Run: python3 backend/tests/test_bdl_dob.py
"""

import ast
import os
import sys
import typing

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "api", "data_service.py")

_ns = {"Optional": typing.Optional}
for _node in ast.parse(open(SRC).read()).body:
    if isinstance(_node, ast.FunctionDef) and _node.name == "_parse_bdl_dob":
        exec(compile(ast.Module(body=[_node], type_ignores=[]), "<ds>", "exec"), _ns)
parse = _ns.get("_parse_bdl_dob")
assert parse, "could not extract _parse_bdl_dob from data_service.py"

# Real values, taken from BDL's player table on 2026-09-07.
CASES = [
    # ISO — what BDL serves for active players (936 of 940 on 2026 rosters).
    ("1999-09-23", (1999, 9, 23)),
    ("2004-03-11", (2004, 3, 11)),
    # D/M/YYYY — day first, 10,742 records. THE ONE THAT CORRUPTED A ROW:
    # this used to return (3900, 18, 2) and was written to a real player.
    ("18/2/2000",  (2000, 2, 18)),
    ("20/9/1995",  (1995, 9, 20)),
    ("27/12/1981", (1981, 12, 27)),
    ("1/5/1981",   (1981, 5, 1)),
    ("7/2/1979",   (1979, 2, 7)),
    # M/D/YY — month first, 788 records, the only form the old code handled.
    ("08/07/91",   (1991, 8, 7)),
    ("08/03/98",   (1998, 8, 3)),
    ("01/31/03",   (2003, 1, 31)),
    ("12/30/97",   (1997, 12, 30)),
    # The Y2K window is unchanged: YY > 25 → 19xx, else 20xx. So 26 is 1926,
    # not 2026 — correct for a BIRTH year, where 2026 would be a newborn.
    # This is the boundary, and it is the shipped rule, not a new one.
    ("06/15/26",   (1926, 6, 15)),
    ("06/15/25",   (2025, 6, 15)),
    ("06/15/99",   (1999, 6, 15)),
    # Nothing usable.
    (None, (None, None, None)),
    ("", (None, None, None)),
    ("   ", (None, None, None)),
    ("not a date", (None, None, None)),
    ("1999-09", (None, None, None)),
    ("1/2", (None, None, None)),
    ("a/b/c", (None, None, None)),
]


def main():
    bad = []
    for raw, want in CASES:
        got = parse(raw)
        if got != want:
            bad.append(f"  {raw!r:14s} -> {got}   expected {want}")

    # ⚠️ The property that matters more than any single case: a date BDL
    # actually ships must never produce an impossible month or a year outside
    # living memory. Both were symptoms of the bug — an impossible month is
    # how it was first spotted.
    for raw, _ in CASES:
        y, m, d = parse(raw)
        if y is None:
            continue
        if not (1820 <= y <= 2030):
            bad.append(f"  {raw!r}: year {y} outside any plausible range")
        if m is not None and not (1 <= m <= 12):
            bad.append(f"  {raw!r}: month {m} is impossible")
        if d is not None and not (1 <= d <= 31):
            bad.append(f"  {raw!r}: day {d} is impossible")

    if bad:
        print("FAILED:")
        print("\n".join(bad))
        return 1
    print(f"checked {len(CASES)} date forms across all three BDL formats")
    print("ALL PASS — _parse_bdl_dob reads every format BDL ships")
    return 0


if __name__ == "__main__":
    sys.exit(main())
