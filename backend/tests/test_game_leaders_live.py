#!/usr/bin/env python3
"""Game Leaders, live: top 3 per category from completed at-bats, never withdrawn,
ties to the earlier event, shipped on the live snapshot with no new balldontlie
calls.

Fixtures are real payload (testdata/game-leaders.json, two finished games) plus
testdata/live-leaders.json, the server's top 3 that GameLeadersTests also reads
to check the client's final ranking agrees.
Run: python3 backend/tests/test_game_leaders_live.py
"""
import asyncio
import copy
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(HERE, "..", "api"))
import game_leaders_live as gl  # noqa: E402

results = []


def check(name, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


GAMES = json.load(open(os.path.join(REPO, "testdata", "game-leaders.json")))
SHARED = json.load(open(os.path.join(REPO, "testdata", "live-leaders.json")))
G = GAMES["stealAndSubs"]
PAS = sorted(G["pas"], key=lambda p: p["pa_number"])
NAMES = {int(k): v["name"] for k, v in G["identity"].items()}
AWAY, HOME = G["awayTeamId"], G["homeTeamId"]


def board(pas, previous=None, names=NAMES):
    return gl.build(pas, names, away_id=AWAY, home_id=HOME, previous=previous)


def ids(b, cat):
    return [(e["player_id"], e["inning"], e["half"], e["pa_number"], e["pitch_index"], e["value"])
            for e in (b or {}).get(cat, [])]


def pa(n, inning, half, batter, pitcher, result, pitches):
    return {"batter_id": batter, "pitcher_id": pitcher, "inning": inning, "half_inning": half,
            "pa_number": n, "result": result,
            "pitches": [{"release_speed": s, "exit_velocity": ev, "pitch_type": "Sinker"} for s, ev in pitches]}


SYN_NAMES = {1: "A", 2: "B", 3: "C", 9: "P"}
TOP3_CHANGES, FIRST_CHANGES = 20, 8      # stealAndSubs, replayed per completed at-bat (2026-10-09)

print("the server's top 3 is the shared fixture, and agrees with the client fixture's own reading")
spec = importlib.util.spec_from_file_location("mk", os.path.join(REPO, "testdata", "make-live-leaders-fixture.py"))
mk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mk)
check("testdata/live-leaders.json is what the server produces now (regenerate it if this fails)",
      mk.fixture() == SHARED)
for name, g in GAMES.items():
    b = gl.build(g["pas"], {int(k): v["name"] for k, v in g["identity"].items()},
                 away_id=g["awayTeamId"], home_id=g["homeTeamId"])
    exp = g["expected"]
    check(f"{name}: top 3 hits and pitches match the independent `expected` (players and values)",
          [(e["player_id"], e["value"]) for e in b["hardest_hit"]]
          == [(x["playerId"], x["value"]) for x in exp["topHits"][:3]]
          and [(e["player_id"], e["value"]) for e in b["fastest_pitches"]]
          == [(x["playerId"], x["value"]) for x in exp["topPitches"][:3]])

print("only completed at-bats")
live = [pa(1, 1, "top", 1, 9, None, [(101.0, None), (102.5, None)])]
check("an at-bat in progress (no result) puts nothing on the board: no card yet", board(live, names=SYN_NAMES) is None)
done = [pa(1, 1, "top", 1, 9, "Strikeout", [(101.0, None), (102.5, None)])]
b = board(done, names=SYN_NAMES)
check("when it ends, its pitches join, fastest first", [e["value"] for e in b["fastest_pitches"]] == [102.5, 101.0])
check("before the first completed tracked at-bat: None", board([], names=SYN_NAMES) is None)

print("never withdrawn")
b1 = board(PAS)
b2 = board([p for p in PAS if p["pa_number"] != b1["hardest_hit"][0]["pa_number"]], previous=b1)
check("the feed drops the hardest-hit at-bat: its row stays, in place", ids(b2, "hardest_hit") == ids(b1, "hardest_hit"))
check("and the board with no memory would have lost it (what the carry prevents)",
      ids(board([p for p in PAS if p["pa_number"] != b1["hardest_hit"][0]["pa_number"]]), "hardest_hit")
      != ids(b1, "hardest_hit"))
three = board([pa(1, 1, "top", 1, 9, "Out", [(99.0, None), (98.0, None), (97.0, None)])], names=SYN_NAMES)
tie = board([pa(2, 1, "top", 2, 9, "Out", [(97.0, None)])], previous=three, names=SYN_NAMES)
check("a later pitch EQUAL to third does not displace it (the earlier event keeps the place)",
      ids(tie, "fastest_pitches") == ids(three, "fastest_pitches"))
faster = board([pa(2, 1, "top", 2, 9, "Out", [(97.1, None)])], previous=three, names=SYN_NAMES)
check("a strictly faster one does, and only third leaves",
      [e["value"] for e in faster["fastest_pitches"]] == [99.0, 98.0, 97.1])
revised = board([pa(1, 1, "top", 1, 9, "Out", [(96.0, None), (98.0, None), (97.0, None)])],
                previous=three, names=SYN_NAMES)
check("a revised reading of the same pitch wins (99.0 -> 96.0 re-ranks it)",
      [e["value"] for e in revised["fastest_pitches"]] == [98.0, 97.0, 96.0])

print("ties go to the earlier event: (inning, half, pa_number, pitch index)")
t = SHARED["ties"]
tb = gl.build(t["pas"], {int(k): v["name"] for k, v in t["identity"].items()},
              away_id=t["awayTeamId"], home_id=t["homeTeamId"])
check("four 99.5s fed out of game order: the three earliest, earliest first",
      [list(x[:5]) for x in ids(tb, "fastest_pitches")] == [[10, 1, "top", 1, 1], [11, 1, "bottom", 4, 0], [10, 2, "top", 7, 0]])
check("three 101.0s behind a 103.0: top of the 1st before the bottom",
      [list(x[:5]) for x in ids(tb, "hardest_hit")] == [[20, 2, "top", 8, 0], [20, 1, "top", 1, 1], [21, 1, "bottom", 4, 0]])

print("the last live board is the final top 3 when nothing is missing")
carried, top3_changes, first_changes = None, 0, 0
for n in range(1, len(PAS) + 1):            # one cycle per completed at-bat, carried
    nxt = board(PAS[:n], previous=carried)
    for cat in gl.CATEGORIES:               # a category's first row counts as a change
        top3_changes += ids(nxt, cat) != ids(carried, cat)
        first_changes += ids(nxt, cat)[:1] != ids(carried, cat)[:1]
    carried = nxt or carried
check("replayed cycle by cycle with the carry, it ends exactly on the shared fixture's final top 3",
      {c: [list(x) for x in ids(carried, c)] for c in gl.CATEGORIES} == SHARED["games"]["stealAndSubs"]["top3"])
check(f"visible changes on this game, both categories, filling included: top 3 {top3_changes}, "
      f"collapsed #1 {first_changes} (pinned 2026-10-09; today's ten-row board: 31 after filling)",
      (top3_changes, first_changes) == (TOP3_CHANGES, FIRST_CHANGES))

print("names, sides and the payload")
check("an id with no name is dropped, not shown blank",
      board([pa(1, 1, "top", 77, 9, "Single", [(95.0, 104.0)])], names={9: "P"})["hardest_hit"] == [])
e = b1["fastest_pitches"][0]
check("a pitch row carries the pitcher's team (home pitches the top)", e["team_id"] == (HOME if e["half"] == "top" else AWAY))
size = len(json.dumps(b1, separators=(",", ":")))
check(f"a full game's board is under 1.5KB ({size} bytes)", size < 1500)

print("wired into the live snapshot, no new balldontlie calls")
import live_service as ls  # noqa: E402
plays = sorted(G["plays"], key=lambda p: p.get("order") or 0)
stats = [{"player": {"id": int(k), "full_name": v["name"], "team": {"id": v["teamId"]}}}
         for k, v in G["identity"].items()]
game = {"id": G["gameId"], "status": "STATUS_IN_PROGRESS",
        "home_team": {"id": HOME, "display_name": "Home"}, "away_team": {"id": AWAY, "display_name": "Away"},
        "home_team_data": {}, "away_team_data": {}}
snap = ls.assemble_unified(game, stats, plays, PAS, None)
gb = snap["game_leaders"]
check("the snapshot carries game_leaders, equal to the board built directly",
      ids(gb, "hardest_hit") == ids(b1, "hardest_hit") and ids(gb, "fastest_pitches") == ids(b1, "fastest_pitches"))
by_order = {p["order"]: p for p in plays}
marked = [x for x in gb["fastest_pitches"] if x["play_order"] is not None]
check(f"each marked pitch's play_order is a stream pitch row by that pitcher ({len(marked)}/3 marked)",
      marked and all((by_order[x["play_order"]].get("text") or "").startswith("Pitch ")
                     and by_order[x["play_order"]].get("pitcher_id") == x["player_id"] for x in marked))
final = ls.assemble_unified(dict(game, status="STATUS_FINAL"), stats, plays, PAS, None)
check("at the final: no board (the client builds its own ten)", final["game_leaders"] is None)
dropped = [p for p in PAS if p["pa_number"] != b1["hardest_hit"][0]["pa_number"]]
again = ls.assemble_unified(game, stats, plays, dropped, None, previous_leaders=snap["game_leaders"])
check("assemble_unified carries the previous board: a dropped at-bat's row stays",
      ids(again["game_leaders"], "hardest_hit") == ids(b1, "hardest_hit"))

calls = []


async def fake_bdl(path, params):
    calls.append(path)
    return {"games": {"data": [game]}, "stats": {"data": stats}, "plays": {"data": plays, "meta": {}},
            "plate_appearances": {"data": dropped if len(calls) > 6 else PAS},
            "lineups": {"data": []}}[path]

ls._bdl_call = fake_bdl
ls.BDL_PACING_S = 0
ls._cache.clear() if hasattr(ls._cache, "clear") else None
asyncio.run(ls._refresh_cycle())
first = list(calls)
calls.clear()
asyncio.run(ls._refresh_cycle())
# The fake's lineup is empty, which `_get_lineup_cached` retries every cycle by
# design; the point is that the board adds no call of its own.
check(f"a cycle makes the same five calls as before, each once: {first}, then {calls}",
      sorted(first) == sorted(calls) == ["games", "lineups", "plate_appearances", "plays", "stats"])
cached = ls.get_live_game(G["gameId"])
check("the second cycle, with the hardest-hit at-bat dropped from the feed, still shows it (carried via the snapshot)",
      cached and ids(cached["game_leaders"], "hardest_hit") == ids(b1, "hardest_hit"))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
