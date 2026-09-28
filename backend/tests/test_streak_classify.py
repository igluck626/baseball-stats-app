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
fn = next(n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "_streak_classify")
ns: dict = {}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "<extract>", "exec"), ns)
classify = ns["_streak_classify"]


def case(label, kind, *, H=0, HR=0, AB=0, BB=0, HBP=0, SF=0, SH=0, expect):
    """PA is derived exactly as `_longest_streak` derives it."""
    PA = AB + BB + HBP + SF + SH
    reached = H + BB + HBP
    got = classify(kind, H, HR, AB, reached, PA, SF)
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

print()
if all(results):
    print(f"ALL PASS — {len(results)} cases, rule 9.23 including the sacrifice-fly exception")
    sys.exit(0)
print(f"FAILURES — {sum(1 for r in results if not r)} of {len(results)}")
sys.exit(1)
