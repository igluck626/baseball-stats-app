#!/usr/bin/env python3
"""`postseason_ingest.build_rows` on a real balldontlie postseason game
(CHW @ HOU, AL Wild Card Game 1, 2026-09-29 — fixtures/bdl_postseason_game_15457178.json).

⚠️ THE CHECKS THAT MATTER:
  • An UNMAPPED player is kept (player_id None, bdl_player_id set) — never
    dropped, unlike the regular-season ingest.
  • Every row carries its round and the balldontlie game id; team and
    opponent are Lahman codes from balldontlie's team ids.
  • A pitcher's W / L / SV / GS come from the stat row; IP is decimal.
  • The round map follows build_series: a withheld round is None.
  • Revisions: one logged change per value, IP compared to three decimals;
    a dropped line is reported, never deleted. `run_safely` is idle outside
    September-November, never raises, skips while another session holds the
    advisory lock, and revises only when asked (the nightly / catch-up).

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_postseason_ingest.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import postseason_ingest as pi                                    # noqa: E402

FIX = json.load(open(os.path.join(HERE, "fixtures", "bdl_postseason_game_15457178.json")))
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


HENDRICKSON = 6113194
ids = {s["player"]["id"] for s in FIX["stats"]}
mapped = {b: 900000 + i for i, b in enumerate(sorted(ids)) if b != HENDRICKSON}   # everyone but him
bat, pit, unmapped = pi.build_rows(FIX["game"], "WC", FIX["stats"], mapped)

print("identity")
check("the unmapped pitcher is reported", unmapped == {HENDRICKSON}, unmapped)
h = [r for r in pit if r["bdl_player_id"] == HENDRICKSON]
check("  ...and KEPT: player_id None, bdl_player_id set", len(h) == 1 and h[0]["player_id"] is None, h)
check("every row carries its bdl id, source and round",
      all(r["bdl_player_id"] and r["source"] == "bdl" and r["round"] == "WC" for r in bat + pit))
check("the game id is balldontlie's, as a string", {r["game_id"] for r in bat + pit} == {"15457178"})

print("teams")
check("team and opponent are Lahman codes (CHA, HOU)",
      {(r["team"], r["opponent"]) for r in bat + pit} == {("CHA", "HOU"), ("HOU", "CHA")})
check("home side is HOU", all((r["home_away"] == "H") == (r["team"] == "HOU") for r in bat + pit))

print("pitching")
by_name = {s["player"]["id"]: s["player"]["last_name"] for s in FIX["stats"]}
dec = {by_name[r["bdl_player_id"]]: (r["W"], r["L"], r["SV"], r["GS"]) for r in pit}
check("Hicks the win, Blubaugh the loss and the start", dec.get("Hicks") == (1, 0, 0, 0)
      and dec.get("Blubaugh") == (0, 1, 0, 1), dec)
check("one winner and one loser in the game", sum(r["W"] for r in pit) == 1 and sum(r["L"] for r in pit) == 1)
check("IP is decimal: Hendrickson's 1.2 is 1.667", abs(h[0]["IP"] - 5 / 3) < 0.01, h[0]["IP"])

print("batting")
check("no batting row for a pitcher who didn't bat (the appearance rule)",
      not any(r["bdl_player_id"] == HENDRICKSON for r in bat))
check("the batting rows' runs add up to each side's score",
      all(sum(r["R"] or 0 for r in bat if r["team"] == t) == next(r["team_score"] for r in bat if r["team"] == t)
          for t in ("CHA", "HOU")))

print("rounds")
series = [{"round": "WC", "games": [{"game_id": 1}, {"game_id": 2}]},
          {"round": None, "games": [{"game_id": 3}]}]
check("the round map follows build_series, withheld as None", pi.rounds_by_game(series) == {1: "WC", 2: "WC", 3: None})

print("revisions")
old = [{"game_id": "1", "bdl_player_id": 10, "H": 1, "IP": 1.667},
       {"game_id": "1", "bdl_player_id": 11, "H": 0, "IP": 2.0},
       {"game_id": "1", "bdl_player_id": 12, "H": 0, "IP": 0.0}]
new = [{"game_id": "1", "bdl_player_id": 10, "H": 2, "IP": 1.6667},
       {"game_id": "1", "bdl_player_id": 11, "H": 0, "IP": 2.333},
       {"game_id": "1", "bdl_player_id": 13, "H": 1, "IP": 0.0}]
ins, ch, gone = pi.diff_rows(old, new, ("H", "IP"))
check("a scorer's change is one (key, column, old, new) per value",
      sorted(ch) == [(("1", 10), "H", 1, 2), (("1", 11), "IP", 2.0, 2.333)], ch)
check("  ...IP compared to three decimals (1.667 == 1.6667)", not any(c[0] == ("1", 10) and c[1] == "IP" for c in ch))
check("a line balldontlie adds later is inserted", [r["bdl_player_id"] for r in ins] == [13])
check("a stored line balldontlie drops is reported, not deleted", [r["bdl_player_id"] for r in gone] == [12])

print("run_safely")
import datetime
from unittest import mock


class FakeLock:
    """Stands in for the Postgres advisory lock; `held_elsewhere` is another
    session holding it."""
    held_elsewhere = False

    def __enter__(self):
        self.acquired = not FakeLock.held_elsewhere
        return self

    def __exit__(self, *exc):
        return False


OCT = datetime.datetime(2026, 10, 4, 12)
check("idle outside September-November",
      pi.run_safely("test", now=datetime.datetime(2026, 7, 4, 12), lock=FakeLock) is None)
with mock.patch.object(pi, "refresh", side_effect=RuntimeError("balldontlie down")):
    try:
        out = pi.run_safely("test", now=OCT, lock=FakeLock)
        check("never raises: a failure is logged and returns None", out is None)
    except Exception as exc:                                       # noqa: BLE001
        check("never raises: a failure is logged and returns None", False, repr(exc))
with mock.patch.object(pi, "refresh", return_value={"season": 2026}) as rf:
    FakeLock.held_elsewhere = True
    out = pi.run_safely("test", now=OCT, lock=FakeLock)
    check("another session holding the advisory lock makes this run skip", out is None and not rf.called)
    FakeLock.held_elsewhere = False
    check("  ...and it runs once the lock is free", pi.run_safely("test", now=OCT, lock=FakeLock) == {"season": 2026})
    check("the loop's run is insert-only (revise=False)", rf.call_args.kwargs.get("revise") is False)
    pi.run_safely("nightly", now=OCT, revise=True, lock=FakeLock)
    check("the nightly / catch-up run revises (revise=True)", rf.call_args.kwargs.get("revise") is True)
with mock.patch.object(pi, "refresh", return_value={}) as rf:
    class Boom(FakeLock):
        def __enter__(self):
            raise RuntimeError("database unreachable")
    try:
        check("a failure taking the lock is logged, never raised", pi.run_safely("test", now=OCT, lock=Boom) is None)
    except Exception as exc:                                       # noqa: BLE001
        check("a failure taking the lock is logged, never raised", False, repr(exc))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
