#!/usr/bin/env python3
"""Rule 9.23 as `_streak_classify` implements it.

⚠️ THE CASE THIS EXISTS FOR is the sacrifice fly. Rule 9.23 names exactly what
cannot end a consecutive-game hitting streak — base on balls, hit by pitch,
defensive interference or obstruction, and a sacrifice BUNT. A sacrifice fly is
not on that list, so a hitless game whose only plate appearance was a sac fly
ENDS the streak. Every other at-bat-less game carries it, and a sac fly is not
an official at-bat either, so `AB == 0` cannot tell them apart.

Standalone, no pytest — matches test_slot_codes.py / test_guard_tool.py.
Run: python3 backend/tests/test_streak_classify.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "..", "api", "main.py")

# Extract the one function by AST rather than importing main.py, which pulls in
# the whole app (DB, network, model clients). Same trick as test_slot_codes.py.
tree = ast.parse(open(MAIN).read())
fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
       and n.name in ("_streak_classify", "_longest_streak")]
ns: dict = {}
exec(compile(ast.Module(body=fns, type_ignores=[]), "<extract>", "exec"), ns)
classify = ns["_streak_classify"]
longest = ns["_longest_streak"]


def case(label, kind, *, H=0, HR=0, AB=0, BB=0, HBP=0, SF=0, SH=0, season=None, expect):
    """PA is derived exactly as `_longest_streak` derives it."""
    PA = AB + BB + HBP + SF + SH
    reached = H + BB + HBP
    got = classify(kind, H, HR, AB, reached, PA, SF, season)
    ok = got == expect
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:52s} -> {got} (expected {expect})")
    return ok


results = []
print("hitting streaks — rule 9.23")
results.append(case("sac fly only, no hit  -> BREAKS (9.23 omits SF)",
                    "hitting", SF=1, expect="break"))
results.append(case("walk + sac fly, no hit -> BREAKS",
                    "hitting", BB=1, SF=1, expect="break"))
results.append(case("sac fly WITH a hit     -> extends",
                    "hitting", H=1, AB=2, SF=1, expect="extend"))
results.append(case("walk only, no hit      -> skip (excused)",
                    "hitting", BB=1, expect="skip"))
results.append(case("HBP only, no hit       -> skip (excused)",
                    "hitting", HBP=1, expect="skip"))
results.append(case("sac BUNT only, no hit  -> skip (excused)",
                    "hitting", SH=1, expect="skip"))
results.append(case("zero PA (pinch run/def)-> skip (neutral)",
                    "hitting", expect="skip"))
results.append(case("hitless with at-bats   -> break",
                    "hitting", AB=4, expect="break"))
results.append(case("a hit                  -> extend",
                    "hitting", H=1, AB=4, expect="extend"))

print("\nRetrosheet-era rows carry no SF: default must not change behaviour")
PA = 0 + 1 + 0 + 0 + 0
results.append((lambda: (print("  [PASS] SF omitted, walk only -> skip") or True)
                if classify("hitting", 0, 0, 0, 1, PA) == "skip"
                else (print("  [FAIL] SF omitted, walk only") or False))())
results.append((lambda: (print("  [PASS] SF=None, walk only    -> skip") or True)
                if classify("hitting", 0, 0, 0, 1, PA, None) == "skip"
                else (print("  [FAIL] SF=None, walk only") or False))())

print("\non-base streaks — unchanged by this rule (reaching is all that counts)")
results.append(case("walk        -> extend", "on_base", BB=1, expect="extend"))
results.append(case("sac fly, no reach -> break", "on_base", SF=1, expect="break"))
results.append(case("hitless at-bats   -> break", "on_base", AB=3, expect="break"))
results.append(case("zero PA           -> skip", "on_base", expect="skip"))

print("\nhome-run / multi-hit keep the old skip (9.23 is about hitting streaks)")
results.append(case("HR streak, sac fly only -> skip", "home_run", SF=1, expect="skip"))
results.append(case("multi-hit, sac fly only -> skip", "multi_hit", SF=1, expect="skip"))

print("\nthe era gate — a sac fly was scored as a SACRIFICE in 1908-1930 and 1939")
results.append(case("1925 sac fly only -> skip (a sacrifice then)",
                    "hitting", SF=1, season=1925, expect="skip"))
results.append(case("1939 sac fly only -> skip (a sacrifice then)",
                    "hitting", SF=1, season=1939, expect="skip"))
results.append(case("1945 sac fly only -> break (not a sacrifice)",
                    "hitting", SF=1, season=1945, expect="break"))
results.append(case("2001 sac fly only -> break (rule 9.23)",
                    "hitting", SF=1, season=2001, expect="break"))
results.append(case("1908 and 1930 are inside the gate",
                    "hitting", SF=1, season=1908, expect="skip")
               and case("  ...1930", "hitting", SF=1, season=1930, expect="skip"))
results.append(case("1907 and 1931 are outside it",
                    "hitting", SF=1, season=1907, expect="break")
               and case("  ...1931", "hitting", SF=1, season=1931, expect="break"))
results.append(case("1925 hitless WITH at-bats still breaks",
                    "hitting", AB=3, SF=1, season=1925, expect="break"))
results.append(case("1925 multi-hit, sac fly only -> skip (unchanged)",
                    "multi_hit", SF=1, season=1925, expect="skip"))

print("\n_longest_streak hands each game's season to the gate")


def g(d, season, **kw):
    row = {"game_date": d, "season": season, "H": 0, "HR": 0, "AB": 0,
           "BB": 0, "HBP": 0, "SF": 0, "SH": 0}
    row.update(kw)
    return row


def run_len(season):
    games = [g("a", season, H=1, AB=4), g("b", season, SF=1), g("c", season, H=1, AB=4)]
    return longest(games, "hitting")[0]


for season, want in ((1925, 2), (2001, 1)):
    got = run_len(season)
    ok = got == want
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] hit / SF-only / hit in {season}: longest {got} (expected {want})")

print()
if all(results):
    print(f"ALL PASS — {len(results)} cases, rule 9.23 including the sacrifice-fly exception and its eras")
    sys.exit(0)
print(f"FAILURES — {sum(1 for r in results if not r)} of {len(results)}")
sys.exit(1)
