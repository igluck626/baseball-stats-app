#!/usr/bin/env python3
"""The battery's drift rules for `leaders_val` and `answer100`
(diff_battery._leaders_drift_ok / _prose_drift_ok).

⚠️ BOTH DIRECTIONS ARE PINNED. The season must pass — the 2026-09-29 moves
against the 2026-08-13 golden are the real cases — and a genuine regression
must still fail: a total that drops, a figure that jumps, a board that shrinks,
a sentence about a different player.

Standalone, no pytest — matches test_streak_classify.py.
Run: python3 backend/tests/test_battery_drift.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diff_battery as db                                        # noqa: E402

results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def prose(g, n):
    return db._prose_drift_ok(g, n)[0]


def board(g, n):
    return db._leaders_drift_ok(json.dumps(g), json.dumps(n))[0]


print("answer100 — the season passes (real 08-13 -> 09-29 moves)")
check("Cole ERA 3.19 -> 3.21", prose("Gerrit Cole's career ERA is 3.19.", "Gerrit Cole's career ERA is 3.21."), True)
check("Cole K/9 10.34 -> 10.3", prose("Gerrit Cole's career K/9 is 10.34.", "Gerrit Cole's career K/9 is 10.3."), True)
check("Trout/Judge HR 424/385 -> 426/386",
      prose("Mike Trout leads with 424 to 385 home runs.", "Mike Trout leads with 426 to 386 home runs."), True)
check("Judge WAR 64.29 -> 64.21 (WAR moves both ways)",
      prose("Aaron Judge's career WAR is 64.29.", "Aaron Judge's career WAR is 64.21."), True)

print("answer100 — regressions still fail")
check("a home-run total that DROPS", prose("Mike Trout leads with 424 to 385 home runs.",
                                           "Mike Trout leads with 420 to 385 home runs."), False)
check("an ERA that jumps", prose("Gerrit Cole's career ERA is 3.19.", "Gerrit Cole's career ERA is 4.19."), False)
check("a different leader (the words change)",
      prose("Mike Trout leads with 424 to 385 home runs.", "Aaron Judge leads with 424 to 385 home runs."), False)
check("a different stat", prose("Gerrit Cole's career ERA is 3.19.", "Gerrit Cole's career WHIP is 3.19."), False)
check("an answer that vanished", prose("Gerrit Cole's career ERA is 3.19.", None), False)
check("a batting average .305 -> .330 (the floor below 1.0)",
      prose("Aaron Judge's career batting average is .305.", "Aaron Judge's career batting average is .330."), False)
check("  ...while .305 -> .310 is a season", prose("Aaron Judge's career batting average is .305.",
                                                "Aaron Judge's career batting average is .310."), True)

print("leaders_val — the season passes (real moves)")
hr15_old = [[None, None, None, v] for v in (385, 375, 359, 343, 338, 333, 326, 323)]
hr15_new = [[None, None, None, v] for v in (386, 385, 366, 355, 347, 337, 329, 328)]
check("HR since 2015: every position rose", board(hr15_old, hr15_new), True)
avg_old = [["arral001", None, 0.316, None], ["belta001", None, 0.307, None],
           ["freef001", None, 0.300, None], ["altuj001", None, 0.300, None]]
avg_new = [["arral001", None, 0.316, None], ["belta001", None, 0.307, None],
           ["altuj001", None, 0.300, None], ["freef001", None, 0.300, None]]
check("AVG since 2010: a tie at .300 re-sorts", board(avg_old, avg_new), True)

print("leaders_val — regressions still fail")
check("a position's count DROPS", board(hr15_old, hr15_old[:1] + [[None, None, None, 370]] + hr15_old[2:]), False)
check("a count jumps by more than a season", board(hr15_old, [[None, None, None, 2000]] + hr15_old[1:]), False)
check("the board shrinks", board(hr15_old, hr15_old[:-1]), False)
check("a rate collapses", board(avg_old, [["arral001", None, 0.200, None]] + avg_old[1:]), False)
check("a board AVG .316 -> .340 fails (was inside the old flat 0.075)",
      board(avg_old, [["arral001", None, 0.340, None]] + avg_old[1:]), False)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
