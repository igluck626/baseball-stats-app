#!/usr/bin/env python3
"""Pitching decisions from balldontlie's /stats: one column each, and a display
`result` that never hides a win or a loss.

⚠️ THE CHECKS THAT MATTER: balldontlie ships wins / losses / saves / holds /
blown_saves / games_started separately, and a pitcher can take two at once
(2026: 235 loss + blown save, 68 win + blown save, 25 loss + hold). Each lands
in its own column (W, L, SV, HLD, BS, GS), and `result` is W > L > S > H > BS
> ND — a loss used to read 'H' behind a hold (this parser) or 'BS' behind a
blown save (the old MLB-API one), and was then never counted.

Real lines (fixtures/bdl_decision_lines.json), kept whole: Jeremiah Estrada
(loss + blown save), Jeff Hoffman (win + blown save), Garrett Cleavinger
(loss + hold).
Standalone, no pytest. Run: <backend python> backend/tests/test_bdl_decisions.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
import data_service as ds                                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "bdl_decision_lines.json")))["cases"]


def parse(case):
    c = FX[case]
    return ds._parse_bdl_pitching_gamelog(c["line"], ds._bdl_game_ctx(c["game"]))


dec = lambda r: {k: r[k] for k in ("W", "L", "SV", "HLD", "BS", "GS")}
r = parse("losses+blown_saves")
check("Estrada, loss + blown save: L = 1 and BS = 1 in their own columns",
      dec(r) == {"W": 0, "L": 1, "SV": 0, "HLD": 0, "BS": 1, "GS": 0}, dec(r))
check("...and the display result is 'L' — the loss is not hidden behind the blown save", r["result"] == "L", r["result"])
r = parse("wins+blown_saves")
check("Hoffman, win + blown save: W = 1 and BS = 1", dec(r) == {"W": 1, "L": 0, "SV": 0, "HLD": 0, "BS": 1, "GS": 0}, dec(r))
check("...result 'W'", r["result"] == "W", r["result"])
r = parse("losses+holds")
check("Cleavinger, loss + hold: L = 1 and HLD = 1", dec(r) == {"W": 0, "L": 1, "SV": 0, "HLD": 1, "BS": 0, "GS": 0}, dec(r))
check("...result 'L' (it read 'H', and the loss was never counted)", r["result"] == "L", r["result"])
line = dict(FX["losses+blown_saves"]["line"], wins=0, losses=0, holds=0, blown_saves=1)
r = ds._parse_bdl_pitching_gamelog(line, ds._bdl_game_ctx(FX["losses+blown_saves"]["game"]))
check("a blown save alone shows 'BS'", r["result"] == "BS" and r["BS"] == 1, r["result"])
line = {k: v for k, v in FX["wins+blown_saves"]["line"].items()
        if k not in ("wins", "losses", "saves", "holds", "blown_saves", "games_started")}
r = ds._parse_bdl_pitching_gamelog(line, ds._bdl_game_ctx(FX["wins+blown_saves"]["game"]))
check("a line without the decision fields stores NULL in the columns and 'ND'",
      all(v is None for v in dec(r).values()) and r["result"] == "ND", (dec(r), r["result"]))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
