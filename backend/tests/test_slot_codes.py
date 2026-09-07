#!/usr/bin/env python3
"""Does the Python half of the substitute rule still agree with the fixture?

⚠️ THE POINT OF THIS FILE IS DRIFT, NOT CORRECTNESS. The same rule exists in
Swift (`substituteBattingOrders` in BaseballStats/Models/Scores.swift): that
copy serves a finished game the app fetches itself, this one serves a game in
progress whose box score the backend assembles. They fill ONE field that ONE
view reads (`stats_battingOrder`, read by `BoxScoreView.substitutionDepth`), so
they must produce identical codes, and nothing in either language stops them
drifting apart.

So the expected codes live in `testdata/box-score-order.json`, beside the real
captured payloads they came from, and BOTH suites read them from there. Neither
suite owns them and neither imports the other. Whichever side drifts fails
against the same file and names itself in the failure.

Run: python3 backend/tests/test_slot_codes.py
(Standalone and dependency-free, like test_guard_tool.py beside it. Imports
`slot_codes` directly — that module is pure stdlib precisely so this can.)
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "api"))
from slot_codes import _slot_codes, _carried_codes, carry_forward  # noqa: E402

FIXTURES = os.path.join(HERE, "..", "..", "testdata", "box-score-order.json")

# The away side bats the top half, the home side the bottom.
HALVES = {"away": "top", "home": "bottom"}


def load():
    with open(FIXTURES) as fh:
        return json.load(fh)


def render(codes, names):
    return sorted(f"{names.get(pid, pid)}={code}" for pid, code in codes.items())


def main():
    fixtures = load()
    checked = 0
    substitutes = 0
    failures = []

    for label, fixture in sorted(fixtures.items()):
        names = {}
        for row in fixture["lineups"]:
            names[row["player"]["id"]] = row["player"]["full_name"]
        for row in fixture["stats"]:
            names.setdefault(row["player"]["id"], row["player"]["full_name"])

        for side in ("away", "home"):
            team = fixture[side]
            stats_pa = sum(
                (row.get("plate_appearances") or 0)
                for row in fixture["stats"]
                if (row.get("team") or {}).get("id") == team["id"]
            )
            got = _slot_codes(
                fixture["pas"], fixture["lineups"], team["id"],
                HALVES[side], stats_pa, fixture["isFinal"],
            )
            expected = {e["id"]: e["code"] for e in fixture["expected"][side]}
            checked += len(expected)
            substitutes += sum(1 for e in fixture["expected"][side] if e["code"] % 100)

            if got != expected:
                failures.append(
                    f"  {label} / {side} ({team['abbreviation']})\n"
                    f"      expected: {render(expected, names)}\n"
                    f"      got:      {render(got, names)}"
                )

    # A fixture of nothing but starters would pass while proving nothing.
    assert substitutes >= 8, f"only {substitutes} substitutes across the corpus"

    # A damaged sequence must place FEWER men, never the same men elsewhere.
    fixture = fixtures["pinchHitters"]
    team = fixture["home"]
    stats_pa = sum(
        (row.get("plate_appearances") or 0)
        for row in fixture["stats"]
        if (row.get("team") or {}).get("id") == team["id"]
    )
    whole = _slot_codes(fixture["pas"], fixture["lineups"], team["id"],
                        "bottom", stats_pa, True)
    bottom = [p for p in fixture["pas"] if p["half_inning"] == "bottom"]
    dropped = bottom[len(bottom) // 2]
    damaged = [p for p in fixture["pas"] if p is not dropped]
    after = _slot_codes(damaged, fixture["lineups"], team["id"],
                        "bottom", stats_pa, True)
    assert len(after) <= len(whole), "a damaged sequence placed MORE men"
    for pid, code in after.items():
        assert whole.get(pid) == code, (
            f"a damaged sequence MOVED someone: {pid} was {whole.get(pid)}, now {code}"
        )

    # ⚠️ A PLACEMENT ONCE MADE MUST NOT BE WITHDRAWN.
    #
    # This is the test that was missing. The original stability check replayed
    # every side and asserted no man's slot CHANGED VALUE, which was true and
    # did not mean what it appeared to: a placement can also be taken away
    # entirely, and comparing codes only where both exist cannot see that. In
    # production a substitute held slot 401 for twenty-five snapshots and then
    # lost it when a dropped plate appearance made his side unprovable.
    #
    # First: prove the fault is real, by replaying each side plate appearance
    # by plate appearance with NO memory, exactly as it shipped. If this stops
    # producing withdrawals the corpus has stopped covering the case and the
    # test below is guarding nothing.
    bare_withdrawals = []
    for label, fixture in sorted(fixtures.items()):
        names = {r["player"]["id"]: r["player"]["full_name"] for r in fixture["lineups"]}
        for row in fixture["stats"]:
            names.setdefault(row["player"]["id"], row["player"]["full_name"])
        for side in ("away", "home"):
            team = fixture[side]
            rows = [p for p in fixture["pas"]
                    if (p.get("half_inning") or "").lower() == HALVES[side]]
            rows.sort(key=lambda p: (p.get("inning") or 0, p.get("pa_number") or 0))
            previous: dict = {}
            for n in range(1, len(rows) + 1):
                # Both lag states: stats level with the feed, and one behind
                # because the man at the plate has not been counted yet.
                for lag in (0, 1):
                    fresh = _slot_codes(rows[:n], fixture["lineups"], team["id"],
                                        HALVES[side], n - lag, False)
                    for pid, code in previous.items():
                        if pid not in fresh:
                            bare_withdrawals.append(
                                f"{label}/{side}: {names.get(pid, pid)} "
                                f"lost slot {code} at PA {n}")
                    previous = fresh
    assert bare_withdrawals, (
        "the memoryless replay produced no withdrawals, so this test is not "
        "exercising the fault it exists for — the corpus has stopped covering it"
    )

    # Second: the SHIPPED merge must repair every one of them. `carry_forward`
    # is the function live_service actually calls, not a replica of it.
    for label, fixture in sorted(fixtures.items()):
        for side in ("away", "home"):
            team = fixture[side]
            rows = [p for p in fixture["pas"]
                    if (p.get("half_inning") or "").lower() == HALVES[side]]
            rows.sort(key=lambda p: (p.get("inning") or 0, p.get("pa_number") or 0))
            carried: dict = {}
            for n in range(1, len(rows) + 1):
                for lag in (0, 1):
                    fresh = _slot_codes(rows[:n], fixture["lineups"], team["id"],
                                        HALVES[side], n - lag, False)
                    merged = carry_forward(fresh, carried)
                    missing = set(carried) - set(merged)
                    assert not missing, (
                        f"{label}/{side}: carry_forward dropped {missing} at PA {n}")
                    # A fresh answer always outranks a remembered one.
                    for pid, code in fresh.items():
                        assert merged[pid] == code, (
                            f"{label}/{side}: memory overrode a fresh code for {pid}")
                    carried = merged

    # And the reader that feeds it: codes lifted off a previous snapshot.
    snapshot = {"batting": {
        "away": [{"id": 1, "batting_order": 401}, {"id": 2, "batting_order": None}],
        "home": [{"id": 3, "batting_order": 900}],
    }}
    assert _carried_codes(snapshot) == {1: 401, 3: 900}
    assert _carried_codes(None) == {}, "no previous snapshot must mean no memory"
    assert _carried_codes({}) == {}

    if failures:
        print("PYTHON SIDE DIVERGED from testdata/box-score-order.json:\n")
        print("\n".join(failures))
        print("\nIf slot_codes.py changed on purpose, the Swift copy")
        print("(substituteBattingOrders in Scores.swift) needs the same change,")
        print("and the fixture's expectations need regenerating for BOTH.")
        return 1

    print(f"checked {checked} batting-order codes across "
          f"{len(fixtures)} games x 2 sides ({substitutes} substitutes)")
    print("ALL PASS — python slot_codes agrees with the shared fixture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
