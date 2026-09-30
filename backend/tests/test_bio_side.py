#!/usr/bin/env python3
"""`_batter_row_is_phantom` — whether a player with both a batter and a
pitcher bio row is shown as a pitcher.

⚠️ THE CHECKS THAT MATTER: a BLANK position no longer makes a hitter a
pitcher on its own (Paul O'Neill: 8,329 PA, 2.0 IP opened as a pitcher with no
batting side). A blank row is a hitter's only when PA >= 3 x IP and PA >= 50,
so one-game pitchers stay pitchers; a "P" row stays a pitcher's; the
IP-dominates rule for 19th-century aces and pre-DH pitchers is unchanged.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_bio_side.py
"""
import os
import sys
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import data_service as ds                                         # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def phantom(position, pa, ip, has_pitcher_row=True):
    """Run the real rule with the career volumes stubbed."""
    ds.crud.get_player_seasons = lambda db, pid: [NS(PA=pa)]
    ds.crud.get_pitcher_seasons = lambda db, pid: [NS(IP=ip)]
    batter = NS(position=position, player_id=1)
    pitcher = NS(player_id=1) if has_pitcher_row else None
    return ds._batter_row_is_phantom(batter, pitcher, db=None)


_orig = (ds.crud.get_player_seasons, ds.crud.get_pitcher_seasons)
try:
    print("blank position: decided by volume")
    check("Paul O'Neill (blank, 8,329 PA, 2.0 IP) is a hitter", not phantom("", 8329, 2.0))
    check("Lefty O'Doul (blank, 3,529 PA, 77.7 IP) is a hitter", not phantom("", 3529, 77.7))
    check("James Lynch (blank, 64 PA, 17.0 IP; a right fielder) is a hitter", not phantom("", 64, 17.0))
    check("a one-game pitcher (blank, 1 PA, 1.0 IP) stays a pitcher", phantom("", 1, 1.0))
    check("under 50 PA stays a pitcher even at 3x (blank, 40 PA, 2.0 IP)", phantom("", 40, 2.0))
    check("under 3x stays a pitcher (blank, 336 PA, 303 IP; Leo Birdine)", phantom("", 336, 303.3))
    check("blank with no pitcher row is still a phantom", phantom("", 0, 0.0, has_pitcher_row=False))
    print("a pitcher position: unchanged")
    check("Misiorowski ('P', 0 PA) is a pitcher", phantom("P", 0, 40.0))
    check("a 'P' row is not volume-checked (Josh D. Smith, conflated 108 PA)", phantom("P", 108, 14.3))
    print("a fielding position: the IP-dominates rule, unchanged")
    check("Cy Young ('1B', 3,085 PA, 7,356 IP) is a pitcher", phantom("1B", 3085, 7356.0))
    check("Ohtani ('OF', 4,600 PA, 600 IP) is a hitter", not phantom("OF", 4600, 600.0))
    check("a position player with a mop-up inning ('SS', 2,000 PA, 1 IP) is a hitter", not phantom("SS", 2000, 1.0))
finally:
    ds.crud.get_player_seasons, ds.crud.get_pitcher_seasons = _orig

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
