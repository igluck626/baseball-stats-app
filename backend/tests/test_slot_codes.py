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
from slot_codes import _slot_codes  # noqa: E402

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
