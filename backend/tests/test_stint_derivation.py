#!/usr/bin/env python3
"""The nightly stint queries: every game keeps both of its teams, and batting stint G
counts every appearance.

⚠️ WHY THIS EXISTS. A player's stint team is "the other team in the game", read from a
game_team list built from the batting logs alone. 281 early-2026 games hold batting
lines for only ONE side, so that list had one team and the other side's batters lost
the game: 148 player-seasons whose stints summed short of season G for that reason
alone (the pitching query lost 113 games the same way). The fix builds game_team from
both logs, and batting stint G counts every game a player appeared in, with G_batted
for the batting games.

Runs the real SQL (AST-extracted from scripts/nightly_update.py) on an in-memory SQLite
fixture, plus the positional contract _derive_stints relies on.
Run: python3 backend/tests/test_stint_derivation.py
"""
import ast
import math
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NIGHTLY = os.path.join(HERE, "..", "scripts", "nightly_update.py")
TREE = ast.parse(open(NIGHTLY).read())

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


ns = {}
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in {"_STINT_BAT_SQL", "_STINT_PIT_SQL",
                                                 "_STINT_BAT_COLS", "_STINT_PIT_COLS"}
            for t in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), NIGHTLY, "exec"), ns)
BAT, PIT = ns["_STINT_BAT_SQL"], ns["_STINT_PIT_SQL"]


class _BoolOr:
    def __init__(self): self.v = False
    def step(self, x): self.v = self.v or bool(x)
    def finalize(self): return 1 if self.v else 0


db = sqlite3.connect(":memory:")
db.create_aggregate("bool_or", 1, _BoolOr)
db.create_function("floor", 1, math.floor)
db.executescript("""
CREATE TABLE batting_gamelogs (player_id, game_id, game_date, season, opponent,
  "AB", "R", "H", doubles, triples, "HR", "RBI", "BB", "SO", "SB", "CS", "IBB", "HBP",
  "SF", "SH", "GIDP", "PA");
CREATE TABLE pitching_gamelogs (player_id, game_id, game_date, season, opponent,
  result, "IP", "H", "R", "ER", "BB", "SO", "HR", "HBP", "WP");
""")


def bat(pid, gid, d, opp, ab=4, h=1, hr=0, r=0, sb=0, pa=4):
    db.execute("INSERT INTO batting_gamelogs VALUES (?,?,?,2026,?,?,?,?,0,0,?,0,0,1,?,0,0,0,0,0,0,?)",
               (pid, gid, d, opp, ab, r, h, hr, sb, pa))


def pit(pid, gid, d, opp, ip=1.0, so=1, result=None):
    db.execute("INSERT INTO pitching_gamelogs VALUES (?,?,?,2026,?,?,?,0,0,0,0,?,0,0,0)",
               (pid, gid, d, opp, result, ip, so))


# g1: two-sided in both logs (NYY at BOS).
bat(1, "g1", "2026-04-01", "BOS"); bat(2, "g1", "2026-04-01", "NYY")
pit(10, "g1", "2026-04-01", "BOS"); pit(20, "g1", "2026-04-01", "NYY")
# g2: batting lines for the Yankees ONLY; the pitching logs hold both sides.
bat(1, "g2", "2026-04-02", "BOS", hr=1)
pit(10, "g2", "2026-04-02", "BOS"); pit(20, "g2", "2026-04-02", "NYY")
# g3: pitching for one side only (BOS); both sides bat.
bat(1, "g3", "2026-04-03", "BOS"); bat(2, "g3", "2026-04-03", "NYY", sb=1)
pit(20, "g3", "2026-04-03", "NYY", result="W")
# g4/g5: player 3, a Yankee, bats in g4 and only pitches in g5.
bat(3, "g4", "2026-04-04", "BOS"); bat(2, "g4", "2026-04-04", "NYY")
pit(3, "g5", "2026-04-05", "BOS", ip=1.0); bat(2, "g5", "2026-04-05", "NYY")
# g6: player 1, traded to TOR, faces NYY; batting lines for TOR only.
bat(1, "g6", "2026-04-06", "NYY"); pit(30, "g6", "2026-04-06", "NYY"); pit(10, "g6", "2026-04-06", "TOR")

b_rows = db.execute(BAT, {"yr": 2026}).fetchall()
bstints = {(r[0], r[1]): r for r in b_rows}
_NONE = (None,) * 64
ci = {c: 3 + i for i, c in enumerate(ns["_STINT_BAT_COLS"])}

print("the batting stint query")
check("player 1's game in a one-sided batting game (g2) is kept: NYY G = 3",
      bstints.get((1, "NYY"), _NONE)[ci["G"]] == 3)
check("and its line is counted: NYY HR = 1", bstints.get((1, "NYY"), _NONE)[ci["HR"]] == 1)
check("a trade still splits by team: player 1 has a TOR stint with G = 1",
      bstints.get((1, "TOR"), _NONE)[ci["G"]] == 1)
check("a pitching-only game counts toward G: player 3 G = 2, G_batted = 1",
      bstints.get((3, "NYY"), _NONE)[ci["G"]] == 2 and bstints.get((3, "NYY"), _NONE)[ci["G_batted"]] == 1)
check("a pitching-only game adds no batting stats: player 3 AB = 4, PA = 4",
      bstints.get((3, "NYY"), _NONE)[ci["AB"]] == 4 and bstints.get((3, "NYY"), _NONE)[ci["PA"]] == 4)
check("a pitcher who never batted gets a stint with G_batted = 0 and zero stats",
      bstints.get((10, "NYY"), _NONE)[ci["G"]] == 3 and bstints.get((10, "NYY"), _NONE)[ci["G_batted"]] == 0
      and (bstints.get((10, "NYY"), _NONE)[ci["AB"]] or 0) == 0)
check("a game in both logs counts once: player 2 BOS G = 4 (g1, g3, g4, g5), G_batted = 4",
      bstints.get((2, "BOS"), _NONE)[ci["G"]] == 4 and bstints.get((2, "BOS"), _NONE)[ci["G_batted"]] == 4)
check("a batter's line in a one-sided pitching game (g3) keeps its stats: player 2 SB = 1",
      bstints.get((2, "BOS"), _NONE)[ci["SB"]] == 1)
check("player 1's stints are exactly NYY and TOR",
      sorted(t for (pid, t) in bstints if pid == 1) == ["NYY", "TOR"])

print("the pitching stint query")
p_rows = db.execute(PIT.replace("::int", ""), {"yr": 2026}).fetchall()
pstints = {(r[0], r[1]): r for r in p_rows}
check("a pitcher in a one-sided PITCHING game (g3) is kept: player 20 BOS G = 3, W = 1",
      pstints.get((20, "BOS"), _NONE)[3] == 3 and pstints.get((20, "BOS"), _NONE)[4] == 1)
check("pitching G counts pitching games only: player 3 G = 1", pstints.get((3, "NYY"), _NONE)[3] == 1)
check("both sides of g6 are kept: player 10 NYY G = 3, player 30 TOR G = 1",
      pstints.get((10, "NYY"), _NONE)[3] == 3 and pstints.get((30, "TOR"), _NONE)[3] == 1)

print("the contract _derive_stints relies on")


def select_aliases(sql):
    body = sql[sql.rindex("\nSELECT"):sql.rindex("\nFROM")]
    out = []
    for part in re.split(r",(?![^()]*\))", body.replace("\nSELECT", "", 1)):
        part = part.strip()
        m = re.search(r'(?:AS\s+)?"?([A-Za-z_]+)"?\s*$', part)
        out.append(m.group(1) if m else part)
    return out


al = select_aliases(BAT)
check("batting SELECT is player_id, raw_team, first_date, then _STINT_BAT_COLS in order",
      al[:3] == ["player_id", "raw_team", "first_date"] and al[3:] == ns["_STINT_BAT_COLS"])
check("G_batted is a batting stint column", "G_batted" in ns["_STINT_BAT_COLS"])
for name, sql in (("batting", BAT), ("pitching", PIT)):
    gt = sql[sql.index("game_team AS"):sql.index("),", sql.index("game_team AS"))]
    check(f"{name} game_team is built from BOTH logs",
          "batting_gamelogs" in gt and "pitching_gamelogs" in gt and "UNION" in gt)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
