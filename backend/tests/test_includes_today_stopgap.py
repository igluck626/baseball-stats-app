#!/usr/bin/env python3
"""The post-regular-season `includes_today` stopgap on the record-at-date
endpoints.

⚠️ TEMPORARY, like the code it tests: builds without the client's postseason
filter add a Wild Card final to the regular-season line whenever
`includes_today` is false. After the season's last regular-season day the
endpoints answer true so those builds refuse. Before and ON that day nothing
may change — a regular-season date answers from the gamelog exactly as it did.
Delete this file with the stopgap.

Standalone, no pytest — matches test_streak_classify.py.
Run: python3 backend/tests/test_includes_today_stopgap.py
"""
import ast
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "..", "api", "main.py")

tree = ast.parse(open(MAIN).read())
body = [n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name == "_includes_today")
        or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "_LAST_REGULAR_SEASON_DAY")]
ns: dict = {"datetime": datetime}
exec(compile(ast.Module(body=body, type_ignores=[]), MAIN, "exec"), ns)
includes_today = ns["_includes_today"]

results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:62s} -> {got} (expected {want})")


class Row:
    def __init__(self, d):
        self.game_date = datetime.date.fromisoformat(d)


D = datetime.date.fromisoformat
through_0919 = [Row("2026-09-18"), Row("2026-09-19")]
through_0927 = through_0919 + [Row("2026-09-26"), Row("2026-09-27")]

print("regular-season dates answer from the gamelog, unchanged")
check("2026-09-20, gamelog has not reached it", includes_today(through_0919, D("2026-09-20")), False)
check("2026-09-20, gamelog has a row that day",
      includes_today(through_0919 + [Row("2026-09-20")], D("2026-09-20")), True)
check("2026-09-27 (last regular day), no row yet", includes_today(through_0919, D("2026-09-27")), False)
check("2026-09-27 (last regular day), row present", includes_today(through_0927, D("2026-09-27")), True)

print("after the regular season -> true, so old builds refuse")
check("2026-09-29 (Wild Card day 1), no row can exist", includes_today(through_0927, D("2026-09-29")), True)
check("2026-09-28 (off day), no row", includes_today(through_0927, D("2026-09-28")), True)
check("2026-10-23 (World Series), a player with no rows at all", includes_today([], D("2026-10-23")), True)

print("a season missing from the table is left alone")
check("2025-10-01 (not in the table), no row", includes_today([], D("2025-10-01")), False)
check("2027-04-01 (not in the table), no row", includes_today([], D("2027-04-01")), False)

print("both endpoints use it")
calls = {}
for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_includes_today":
            calls[fn.name] = True
check("pitcher_record_at_date + batter_stats_at_date call _includes_today",
      sorted(calls), sorted(k for k in calls if "at_date" in k) or ["<none>"])
check("  ...and there are exactly two callers", len(calls), 2)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
