#!/usr/bin/env python3
"""Line scores for past postseason games (Isaac, 2026-10-29).

⚠️ WHY THIS EXISTS. A past postseason box score drew no line score — and so no
plays, which live inside the line-score card — because `retro_game_info` was
filled from the regular-season game logs only. Retrosheet publishes the
postseason in four more files of the same 161-field rows.

Checks:
  • `parse_rows` reads real postseason rows (fixtures/gamelog_ws_rows.txt,
    copied verbatim from glws.zip): 2025 World Series Game 7, eleven innings,
    and 1912 Game 2, an eleven-inning 6-6 tie;
  • the four file names, and the id shape the postseason lines use;
  • the coverage boundary counts regular-season games only, so postseason rows
    cannot move it;
  • the admin endpoint passes `postseason` through.
Run: python3 backend/tests/test_postseason_gameinfo.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "scripts"), os.path.join(HERE, "..", "api"),
                os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "sqlite://")
import retrosheet_gameinfo as gi                                         # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


print("parsing postseason game-log rows")
rows, mismatch = gi.parse_rows(open(os.path.join(HERE, "fixtures", "gamelog_ws_rows.txt"),
                                    encoding="latin-1").read(), {"TOR02": "Rogers Centre"})
by_id = {r["game_id"]: r for r in rows}
g7 = by_id.get("retro-TOR202511010")
check("both rows parse, none dropped", len(rows) == 2 and mismatch == 0, (len(rows), mismatch))
check("Game 7 keys on the postseason lines' id", g7 is not None, list(by_id))
check("Game 7: LAD 5, TOR 4 over eleven innings",
      g7 and (g7["away_runs"], g7["home_runs"]) == (5, 4)
      and len(gi.parse_linescore(g7["away_line"])) == 11, g7)
check("Game 7: hits and errors, park named", g7 and (g7["away_hits"], g7["home_hits"]) == (11, 14)
      and (g7["away_errors"], g7["home_errors"]) == (0, 0) and g7["park_name"] == "Rogers Centre", g7)
tie = by_id.get("retro-BOS191210090")
check("1912 Game 2: a 6-6 tie keeps both lines", tie and tie["away_runs"] == tie["home_runs"] == 6
      and len(gi.parse_linescore(tie["home_line"])) == 11, tie)
check("an unknown park is left null, not shown as a code", tie and tie["park_name"] is None, tie)
check("the four postseason files", gi.POSTSEASON_FILES == ("glws", "gllc", "gldv", "glwc"))

print("the server")
MAIN = os.path.join(HERE, "..", "api", "main.py")
SRC = open(MAIN).read()
TREE = ast.parse(SRC)
ns = {}
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_COVERAGE_SEASON_COUNTS_SQL"
                                            for t in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), MAIN, "exec"), ns)
sql = ns.get("_COVERAGE_SEASON_COUNTS_SQL", "")
check("the coverage count leaves postseason games out",
      "NOT EXISTS" in sql and "postseason_batting_gamelogs" in sql)
cov = ast.get_source_segment(SRC, next(n for n in TREE.body
                                       if isinstance(n, ast.FunctionDef) and n.name == "meta_coverage"))
check("/meta/coverage uses that count", "_COVERAGE_SEASON_COUNTS_SQL" in cov)
ep = ast.get_source_segment(SRC, next(n for n in TREE.body
                                      if isinstance(n, ast.FunctionDef) and n.name == "start_ingest_gameinfo"))
run = ast.get_source_segment(SRC, next(n for n in TREE.body
                                       if isinstance(n, ast.FunctionDef) and n.name == "_run_gameinfo"))
check("the admin endpoint passes postseason through to the postseason reader",
      "postseason" in ep and "ingest_postseason" in run)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
