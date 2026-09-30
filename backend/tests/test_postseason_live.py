#!/usr/bin/env python3
"""The live postseason line in `/players/{id}/postseason`:
`postseason_stats.live_rows` and its merge into `player_postseason`.

⚠️ THE CHECKS THAT MATTER:
  • A live postseason game's line is added ONCE, into the season, round and
    career totals, with the rates recomputed; the response names the game.
  • Keyed by game id: once that game's final is stored for him, the live
    line is not added again — no double count.
  • A live REGULAR-season game (or another season's, or one whose round is
    withheld) is never added.
  • After the final: the line keeps counting (untagged) until the final is
    stored — then the stored row replaces it with no dip — and a line older
    than the expiry is dropped. The ingest is triggered when a game goes final.

The live game is a real balldontlie payload (CHW @ HOU, AL Wild Card Game 1,
2026-09-29 — fixtures/bdl_postseason_game_15457178.json) standing in for an
in-progress one; the database is a small in-memory stand-in.
Standalone, no pytest. Needs the backend's Python.
Run: <backend python> backend/tests/test_postseason_live.py
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
import postseason_stats as ps                                     # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FIX = json.load(open(os.path.join(HERE, "fixtures", "bdl_postseason_game_15457178.json")))
RAW = {"game": FIX["game"], "stats": FIX["stats"]}
ALTUVE, ALTUVE_BDL = 514888, 528
ROUNDS = {15457178: "WC"}

print("live_rows")
live = ps.live_rows([RAW], ALTUVE, {ALTUVE_BDL}, 2026, ROUNDS)
bat = live["bat"]
check("his line from the postseason game, built by the ingest's own parser",
      len(bat) == 1 and bat[0]["player_id"] == ALTUVE and bat[0]["game_id"] == "15457178"
      and bat[0]["round"] == "WC" and bat[0]["AB"] == 4 and bat[0]["H"] == 0, bat)
check("  ...and nobody else's", all(r["player_id"] == ALTUVE for r in bat + live["pit"]))
regular = copy.deepcopy(RAW)
regular["game"]["season_type"], regular["game"]["postseason"] = "regular", False
check("a live REGULAR-season game is never used",
      ps.live_rows([regular], ALTUVE, {ALTUVE_BDL}, 2026, ROUNDS) == {"bat": [], "pit": []})
check("another season's game is never used",
      ps.live_rows([RAW], ALTUVE, {ALTUVE_BDL}, 2027, ROUNDS) == {"bat": [], "pit": []})
check("a game whose round is withheld is never used",
      ps.live_rows([RAW], ALTUVE, {ALTUVE_BDL}, 2026, {15457178: None}) == {"bat": [], "pit": []})
check("a player with no balldontlie id gets nothing", ps.live_rows([RAW], ALTUVE, set(), 2026, ROUNDS)["bat"] == [])


# ---- player_postseason with an in-memory database -------------------------

def stored_row(gid, season, rnd, h, ab, source="bdl"):
    r = {c: 0 for c in ps._BAT_SUM}
    r.update(season=season, round=rnd, team="HOU", opponent="CHA", game_id=gid, source=source, H=h, AB=ab, PA=ab)
    return r


class Row(tuple):
    def __new__(cls, d):
        t = super().__new__(cls, tuple(d.values()))
        t._mapping = d
        return t


class Result(list):
    def scalar(self):
        return self[0] if self else None


class FakeDB:
    def __init__(self, bat_rows):
        self.bat_rows = bat_rows

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "max(season)" in sql:
            return Result([2025])
        if "FROM team_seasons" in sql:
            return Result([])
        if "count(DISTINCT game_id)" in sql:
            return Result([])
        if "postseason_batting_gamelogs" in sql:
            return Result([Row(dict(r)) for r in self.bat_rows])
        return Result([])


HISTORY = [stored_row("retro-HOU201710180", 2017, "CS", 2, 4, source="retrosheet")]
ps._retro_last_cache.clear()

print("player_postseason: the live line is added once")
before = ps.player_postseason(FakeDB(HISTORY), ALTUVE, 2026, [])
out = ps.player_postseason(FakeDB(HISTORY), ALTUVE, 2026, [], live)
season26 = next(s for s in out["batting"]["seasons"] if s["season"] == 2026)
check("the current season appears, with the live game in it", season26["totals"]["G"] == 1 and season26["totals"]["AB"] == 4)
check("the career total includes it once, rates recomputed",
      out["batting"]["career"]["AB"] == before["batting"]["career"]["AB"] + 4
      and out["batting"]["career"]["G"] == before["batting"]["career"]["G"] + 1
      and out["batting"]["career"]["AVG"] == round(2 / 8, 3), out["batting"]["career"])
check("the round is listed", [r["round"] for r in season26["rounds"]] == ["WC"])
check("the response names the live game", out["live"] == [{"game_id": "15457178", "sides": ["bat"]}], out["live"])
check("he has appeared this postseason", out["current"]["player_appeared"] is True)
check("without a live line: no live flag", before["live"] is None)

print("player_postseason: a stored final is not added again")
stored = HISTORY + [stored_row("15457178", 2026, "WC", 0, 4)]
out2 = ps.player_postseason(FakeDB(stored), ALTUVE, 2026, [], live)
check("the stored game counts once", out2["batting"]["career"]["G"] == 2 and out2["batting"]["career"]["AB"] == 8,
      out2["batting"]["career"])
check("  ...and no live flag", out2["live"] is None, out2["live"])

print("player_postseason: after the final")
final_live = ps.live_rows([dict(RAW, in_progress=False)], ALTUVE, {ALTUVE_BDL}, 2026, ROUNDS)
out3 = ps.player_postseason(FakeDB(HISTORY), ALTUVE, 2026, [], final_live)
check("final but not stored: the line still counts, once", out3["batting"]["career"]["G"] == 2
      and out3["batting"]["career"]["AB"] == 8, out3["batting"]["career"])
check("  ...with no Live tag", out3["live"] is None, out3["live"])
out4 = ps.player_postseason(FakeDB(stored), ALTUVE, 2026, [], final_live)
check("the stored final replaces it with no dip: identical totals",
      out4["batting"]["career"] == out3["batting"]["career"], (out4["batting"]["career"], out3["batting"]["career"]))

print("live_service: the line store")
import live_service as ls                                         # noqa: E402
ls._line_inputs.clear()
SLATE_LIVE = {15457178: dict(FIX["game"], status="STATUS_IN_PROGRESS")}
SLATE_FINAL = {15457178: dict(FIX["game"], status="STATUS_FINAL")}
T0 = 1_000_000.0
check("an in-progress postseason game is recorded",
      ls.note_cycle({15457178: RAW}, {15457178}, SLATE_LIVE, now=T0) == []
      and [e["in_progress"] for e in ls.get_postseason_line_inputs(now=T0)] == [True])
check("a failed fetch keeps the previous inputs",
      ls.note_cycle({}, {15457178}, SLATE_LIVE, now=T0 + 10) == [] and len(ls.get_postseason_line_inputs(now=T0 + 10)) == 1)
check("going final is reported (to trigger the ingest) and the line is kept, flagged final",
      ls.note_cycle({}, set(), SLATE_FINAL, now=T0 + 20) == [15457178]
      and [e["in_progress"] for e in ls.get_postseason_line_inputs(now=T0 + 20)] == [False])
check("still there 5 hours later", len(ls.get_postseason_line_inputs(now=T0 + 20 + 5 * 3600)) == 1)
check("expiry: gone after 6 hours", ls.get_postseason_line_inputs(now=T0 + 21 + ls.FINAL_LINE_TTL_S) == [])
ls.note_cycle({}, set(), SLATE_FINAL, now=T0 + 21 + ls.FINAL_LINE_TTL_S)
check("  ...and pruned from the store", ls._line_inputs == {}, list(ls._line_inputs))
expired = ps.live_rows(ls.get_postseason_line_inputs(now=T0 + 21 + ls.FINAL_LINE_TTL_S), ALTUVE, {ALTUVE_BDL}, 2026, ROUNDS)
check("an expired line adds nothing to the totals",
      ps.player_postseason(FakeDB(HISTORY), ALTUVE, 2026, [], expired)["batting"]["career"] == before["batting"]["career"])
ls._line_inputs.clear()
ls.note_cycle({15457178: RAW}, {15457178}, SLATE_LIVE, now=T0)
check("a game that leaves the live set WITHOUT a final (postponed, suspended) is dropped",
      ls.note_cycle({}, set(), {15457178: dict(FIX["game"], status="STATUS_POSTPONED")}, now=T0 + 5) == []
      and ls._line_inputs == {})
reg = dict(RAW, game=dict(FIX["game"], season_type="regular", postseason=False))
ls.note_cycle({1: reg}, {1}, {1: reg["game"]}, now=T0)
check("a regular-season game is never recorded", 1 not in ls._line_inputs)
check("an in-progress line with no recent cycle is not served",
      ls.note_cycle({15457178: RAW}, {15457178}, SLATE_LIVE, now=T0) == []
      and ls.get_postseason_line_inputs(now=T0 + ls.LIVE_CACHE_TTL_S + 1) == [])

print("live_service: going final triggers the postseason ingest")
import postseason_ingest                                          # noqa: E402
fired = []
postseason_ingest.run_safely = lambda trigger, *a, **k: fired.append(trigger)
import threading as _t                                            # noqa: E402
before_threads = set(_t.enumerate())
ls._ingest_finals([15457178])
for th in set(_t.enumerate()) - before_threads:
    th.join(timeout=5)
check("run_safely('final') runs, off the caller's thread", fired == ["final"], fired)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
