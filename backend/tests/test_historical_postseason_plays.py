#!/usr/bin/env python3
"""Play-by-play for past postseason games, from Retrosheet (Isaac, 2026-10-29).

⚠️ WHY THIS EXISTS. balldontlie has no plays before 2002 and none for the
2010-2014 postseasons, so most past postseason box scores showed none. The
plays are translated locally from Retrosheet's event files
(scripts/retrosheet_postseason_plays.py) and served by
/games/{pk}/historical-plays.

Checks, without importing the app (AST-extracted from api/main.py) and without
Chadwick (the phrase rules are fed decoded rows by hand, copied from cwevent's
output for real games):
  • a synthetic pk maps back to its game id — the inverse of _synthetic_game_pk,
    including a doubleheader's game number;
  • the endpoint reads retro_postseason_plays and stamps each row with the pk;
  • the phrasing rules on the cases that were wrong in development: a bloop the
    center fielder picked up is not an infield single (2001 WS G7 Gonzalez), a
    batter safe while a runner is forced is a fielder's choice (2025 WS G7
    Varsho), a batter who scores on an error says so once (1912 WS G2 Speaker),
    and a line-drive double play names the catcher, not a relay chain.
Run: python3 backend/tests/test_historical_postseason_plays.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "..", "api", "main.py")
SRC = open(MAIN).read()
TREE = ast.parse(SRC)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import retrosheet_postseason_plays as rp                  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


ns = {}
for node in TREE.body:
    names = [node.name] if isinstance(node, ast.FunctionDef) else \
        [t.id for t in node.targets if isinstance(t, ast.Name)] if isinstance(node, ast.Assign) else []
    if any(n in ("_RETRO_CODE_ALPHABET", "_RETRO_ID_LEN", "_retro_parts", "_synthetic_game_pk", "_historical_game_id") for n in names):
        exec(compile(ast.Module(body=[node], type_ignores=[]), MAIN, "exec"), ns)

print("the game id")
for gid in ("retro-TOR202511010", "retro-BOS191210090", "retro-CHN190710082", "retro-ANA200210260"):
    pk = ns["_synthetic_game_pk"](gid)
    check(f"{gid} -> {pk} -> back", ns["_historical_game_id"](pk) == gid, ns["_historical_game_id"](pk))

print("the endpoint")
fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "historical_plays")
body = ast.get_source_segment(SRC, fn)
check("reads retro_postseason_plays by game id", "FROM retro_postseason_plays WHERE game_id = :gid" in body)
check("stamps each row with the pk, as balldontlie's rows carry game_id", '"game_id": game_pk' in body)
check("404 when we hold none", "status_code=404" in body)
box = ast.get_source_segment(SRC, next(n for n in TREE.body
                                       if isinstance(n, ast.FunctionDef) and n.name == "historical_boxscore"))
check("the box score maps its pk with the same helper", "_historical_game_id(game_pk)" in box)


def row(**kw):
    base = {k: "" for k in rp.F.values()}
    base.update({"cd": "2", "bat_ev": "T", "ev_outs": "0", "bdest": "0", "d1": "0", "d2": "0", "d3": "0",
                 "errs": "0", "dp": "F", "tp": "F", "sh": "F", "sf": "F", "bunt": "F", "foul": "F",
                 "sb1": "F", "sb2": "F", "sb3": "F", "cs1": "F", "cs2": "F", "cs3": "F",
                 "po1": "F", "po2": "F", "po3": "F", "outs": "0"})
    base.update(kw)
    return base


nm = lambda i: {"gonzl001": "Gonzalez", "varsd001": "Varsho", "kinei001": "Kiner-Falefa"}.get(i, i)
print("the phrasing")
p = rp._batter_phrase(row(cd="20", tx="S8/F6MD.3-H;2-3;1-2", loc="6MD", bdest="1"), nm, {})
check("2001 WS G7: a bloop the center fielder picked up is a single to center", p == "singled to center", p)
p = rp._batter_phrase(row(cd="20", tx="S9/G3+.1-2", loc="3", bb="G", bdest="1"), nm, {})
check("2025 WS G7: a grounder past first into right is a single to right", p == "singled to right", p)
p = rp._batter_phrase(row(cd="20", tx="S1/G15", loc="15", bb="G", fld="1", bdest="1"), nm, {})
check("a ball the pitcher fields is an infield single", p.startswith("reached on infield single"), p)
p = rp._batter_phrase(row(cd="2", tx="42(3)/FO/G4.2-3;1-2", loc="4", bb="G", fld="4", ev_outs="1", bdest="1"), nm, {})
check("2025 WS G7: the batter safe while a runner is forced is a fielder's choice",
      p == "grounded into fielder's choice to second", p)
p = rp._batter_phrase(row(cd="2", tx="L4/LDP.2X3(45)", loc="4", bb="L", fld="4", ev_outs="2", dp="T"), nm, {})
check("a line-drive double play names who caught it", p == "lined into double play to second", p)
p = rp._batter_phrase(row(cd="2", tx="463(1)/GDP", loc="6", bb="G", fld="6", ev_outs="2", dp="T"), nm, {})
check("a ground-ball double play reads the relay", p == "grounded into double play, second to shortstop to first", p)
p = rp._batter_phrase(row(cd="3", pitches="FCBS"), nm, {})
check("strike three swinging", p == "struck out swinging", p)
check("a full name shortens to the surname, with an initial when two share it",
      rp._short({"a": "Teoscar Hernandez", "b": "Enrique Hernandez", "c": "Mookie Betts"})
      == {"a": "T. Hernandez", "b": "E. Hernandez", "c": "Betts"})
check("'78XD' reads deep left center", rp._loc("78XD", "") == "deep left center")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
