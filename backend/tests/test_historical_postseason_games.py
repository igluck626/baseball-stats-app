#!/usr/bin/env python3
"""Past postseason games on the Scores tab (Isaac, 2026-10-29).

⚠️ WHY THIS EXISTS. A past October date read "No games": `/games/by-date` reads
`batting_gamelogs`, which holds regular-season games only, while every
postseason box score since 1903 sat in `postseason_*_gamelogs`.

Checks, without importing the app (AST-extracted from api/main.py, like
test_this_postseason.py):
  • the series line for real rounds (fixtures/postseason_round_games.json,
    production rows): Game 7s, a lead, a tie, two series sharing one round, and
    the 1912 World Series — eight games because game 2 ended tied 6-6, which
    takes a number and moves nobody's total;
  • the slate and box-score SQL read the postseason tables, Retrosheet rows only,
    and the box score falls back to them only when the regular tables are empty;
  • a postseason box score's season line is that postseason's through the game
    (as balldontlie's is for 2026), and a postseason card says so.
Run: python3 backend/tests/test_historical_postseason_games.py
"""
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "..", "api", "main.py")
SRC = open(MAIN).read()
TREE = ast.parse(SRC)
sys.path.insert(0, os.path.join(HERE, "..", "api"))
import postseason_series                                   # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def extract(names, ns):
    for node in TREE.body:
        targets = []
        if isinstance(node, ast.FunctionDef):
            targets = [node.name]
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if any(t in names for t in targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), MAIN, "exec"), ns)


ns = {"postseason_series": postseason_series}
extract({"_RETRO_DISPLAY_ABBR", "_display_abbr", "_historical_series_line",
         "_POSTSEASON_GAMES_BY_DATE_SQL", "_POSTSEASON_ROUND_GAMES_SQL",
         "_BOX_POST_BAT_SQL", "_BOX_POST_PIT_SQL", "_BOX_BAT_SEASON_SQL", "_BOX_PIT_SEASON_SQL",
         "_BOX_POST_BAT_SEASON_SQL", "_BOX_POST_PIT_SEASON_SQL"}, ns)
line = ns["_historical_series_line"]
FX = json.load(open(os.path.join(HERE, "fixtures", "postseason_round_games.json")))


def gid(round_key, n):
    return FX[round_key][n - 1]["game_id"]


print("the series line")
check("2025 World Series Game 7: Dodgers win it 4-3",
      line("WS", None, FX["2025_WS"], gid("2025_WS", 7)) == "World Series · Game 7 · LAD wins 4-3",
      line("WS", None, FX["2025_WS"], gid("2025_WS", 7)))
check("1960 World Series Game 7 (Mazeroski): Pittsburgh wins 4-3",
      line("WS", None, FX["1960_WS"], gid("1960_WS", 7)) == "World Series · Game 7 · PIT wins 4-3",
      line("WS", None, FX["1960_WS"], gid("1960_WS", 7)))
g1 = line("WS", None, FX["2025_WS"], gid("2025_WS", 1))
check("Game 1 names the leader, not a winner", g1 == "World Series · Game 1 · TOR leads 1-0", g1)
g2 = line("WS", None, FX["2025_WS"], gid("2025_WS", 2))
check("Game 2 ties it", g2 == "World Series · Game 2 · Series tied 1-1", g2)

tie = line("WS", None, FX["1912_WS"], gid("1912_WS", 2))
check("1912 Game 2 ended 6-6: Game 2, and still 1-0", tie == "World Series · Game 2 · BOS leads 1-0", tie)
last = line("WS", None, FX["1912_WS"], gid("1912_WS", 8))
check("1912 Game 8 decides it 4-3 — a seven-game format can't be assumed",
      last == "World Series · Game 8 · BOS wins 4-3", last)

ds = FX["2025_DS"]
sea_det = [g for g in ds if {g["home"], g["away"]} == {"SEA", "DET"}]
check("the 2025 Division Series round holds four series; SEA-DET is five games", len(ds) == 18 and len(sea_det) == 5)
al = line("DS", "AL", ds, sea_det[-1]["game_id"])
check("a series is its two clubs within the round, with the league's name", al == "ALDS · Game 5 · SEA wins 3-2", al)
check("a game not in the list gets no line", line("WS", None, FX["2025_WS"], "retro-XXX202511019") is None)
check("an unknown league drops the name, keeps the rest",
      line("DS", None, ds, sea_det[0]["game_id"]).startswith("Game 1 · "))

print("the reads")
for name in ("_POSTSEASON_GAMES_BY_DATE_SQL", "_POSTSEASON_ROUND_GAMES_SQL", "_BOX_POST_BAT_SQL", "_BOX_POST_PIT_SQL"):
    sql = ns[name]
    check(f"{name}: postseason table, Retrosheet rows only",
          "postseason_" in sql and "source = 'retrosheet'" in sql)
check("the slate's postseason read still joins game info, so linescores appear once loaded",
      "LEFT JOIN retro_game_info" in ns["_POSTSEASON_GAMES_BY_DATE_SQL"])

fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "historical_boxscore")
body = ast.get_source_segment(SRC, fn)
check("the box score falls back to the postseason reads only when the regular ones are empty",
      "postseason = not bat and not pit" in body and "_BOX_POST_BAT_SQL" in body)
check("a postseason box score's season line is that postseason's, through the game",
      "_BOX_POST_BAT_SEASON_SQL if postseason" in body and "_BOX_POST_PIT_SEASON_SQL if postseason" in body)
for name in ("_BOX_POST_BAT_SEASON_SQL", "_BOX_POST_PIT_SEASON_SQL"):
    sql = ns[name]
    check(f"{name}: postseason table, Retrosheet rows, through this game",
          "postseason_" in sql and "source = 'retrosheet'" in sql and "game_id <= :gid" in sql)
slate = ast.get_source_segment(SRC, next(n for n in TREE.body
                                          if isinstance(n, ast.FunctionDef) and n.name == "games_by_date"))
check("a postseason card is marked postseason and carries its line",
      '"seasonType": "postseason"' in slate and '"seriesLine"' in slate)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
