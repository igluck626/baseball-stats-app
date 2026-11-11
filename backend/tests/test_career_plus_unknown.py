#!/usr/bin/env python3
"""Career OPS+ / ERA+ when a season has none (Isaac, 2026-11-11).

⚠️ THE CHECKS THAT MATTER:
  • Our own OPS+ / ERA+ start in 1898 (no game scores earlier for a park
    factor). A season with at-bats (or innings AND earned runs) but no figure
    makes the career figure None ("—") — never a weighted average over part
    of the career (Cy Young, Kid Nichols, Bill Dahlen…).
  • Negro Leagues seasons (postseason_stats.NEGRO_CIRCUITS) carry no figure
    and are hidden; they don't turn a career unknown (Jackie Robinson 1945).
  • Seasons where a blank is normal don't either: no at-bats; no earned runs
    (ERA+ is infinite); no innings.
  • Every season present → the same PA- / ER-weighted average as before.

Pure functions, synthetic rows; no database needed.
Standalone, no pytest. Run: <backend python> backend/tests/test_career_plus_unknown.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
import data_service as ds                                          # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def bat(year, ops_plus, pa=500, ab=450, league="NL"):
    return {"year": year, "league": league, "G": 140, "PA": pa, "AB": ab, "H": 130, "doubles": 20,
            "triples": 5, "HR": 10, "RBI": 60, "BB": 40, "HBP": 5, "SF": 5, "SO": 50, "SB": 10,
            "OPS_plus": ops_plus}


def pit(year, era_plus, ip=200.0, er=70, league="NL"):
    return {"year": year, "league": league, "G": 30, "IP": ip, "ER": er, "H": 180, "BB": 50,
            "SO": 120, "W": 12, "L": 10, "HR": 15, "HBP": 5, "ERA_plus": era_plus}


print("OPS+")
t = ds._batting_career_totals([bat(1898, 120), bat(1899, 140, pa=250)])
check("all seasons present → PA-weighted average", t.get("OPS_plus") == round((120 * 500 + 140 * 250) / 750, 1), t)
t = ds._batting_career_totals([bat(1897, None), bat(1898, 120)])
check("a pre-1898 season with at-bats and no OPS+ → career unknown", t.get("OPS_plus") is None, t)
t = ds._batting_career_totals([bat(1945, None, league="NAL"), bat(1947, 130, league="NL")])
check("a Negro Leagues season doesn't turn the career unknown", t.get("OPS_plus") == 130.0, t)
t = ds._batting_career_totals([bat(1960, None, pa=2, ab=0), bat(1961, 110)])
check("a season with no at-bats doesn't turn it unknown", t.get("OPS_plus") == 110.0, t)
t = ds._batting_career_totals([bat(1897, None)])
check("only seasons without OPS+ → unknown", t.get("OPS_plus") is None, t)

print("ERA+")
t = ds._pitching_career_totals([pit(1990, 150, er=60), pit(1991, 100, er=90)])
check("all seasons present → ER-weighted average", t.get("ERA_plus") == round((150 * 60 + 100 * 90) / 150, 1), t)
t = ds._pitching_career_totals([pit(1895, None), pit(1898, 130)])
check("a pre-1898 season with innings and earned runs and no ERA+ → career unknown", t.get("ERA_plus") is None, t)
t = ds._pitching_career_totals([pit(1940, None, league="NN2"), pit(1948, 125)])
check("a Negro Leagues season doesn't turn the career unknown", t.get("ERA_plus") == 125.0, t)
t = ds._pitching_career_totals([pit(2000, None, ip=3.0, er=0), pit(2001, 115)])
check("a season with no earned runs (infinite ERA+) doesn't turn it unknown", t.get("ERA_plus") == 115.0, t)
t = ds._pitching_career_totals([pit(1979, None, ip=0.0, er=2), pit(1980, 90)])
check("a season with earned runs but no innings doesn't turn it unknown", t.get("ERA_plus") == 90.0, t)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
