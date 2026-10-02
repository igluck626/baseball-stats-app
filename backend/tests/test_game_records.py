#!/usr/bin/env python3
"""`game_records` — each team's record entering and after every game, for the
Scores cards' "(W-L)".

⚠️ THE CHECKS THAT MATTER: a suspended game counts on the day it STARTED
(NYY@WSH 2018-05-15: NYY 28-13, WSH 25-18 after it); a forfeit counts as
forfeited whatever the field score (1971 WS2 finale, 7-5 on the field: NYY
82-80, WSH 63-96, flagged); "T" and ties count for neither; a doubleheader runs
in game-number order; the current season counts regular-season finals only —
balldontlie's walk also returns spring training, postseason and the All-Star
Game, and counting any of them is wrong; and every 1898-2025 team-season ends
at its team_seasons record except the known Retrosheet/Lahman differences.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_game_records.py
History checks read Retrosheet's game logs from RETRO_GL_DIR (gl1898.txt ...
gl2025.txt, from retrosheet.org/gamelogs); without it they are reported SKIPPED.
"""
import csv
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import game_records as gr                                         # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def row(date, num, away, home, ascore, hscore, outs=51, forfeit="", completion="", aline="1", hline="1"):
    """A game-log row with only the fields game_records reads filled in."""
    r = [""] * 30
    r[0], r[1], r[3], r[6] = date, str(num), away, home
    r[9], r[10], r[11], r[13], r[14] = str(ascore), str(hscore), str(outs), completion, forfeit
    r[19], r[20] = aline, hline
    return r


def rec(g, side, when):
    w, l = g[f"{side}_w_{when}"], g[f"{side}_l_{when}"]
    return None if w is None else f"{w}-{l}"


print("doubleheaders, ties, forfeits, no-decisions")
season = gr.season_from_game_logs([
    row("20190718", 2, "TBA", "NYA", 1, 5),            # listed first, played second
    row("20190718", 1, "TBA", "NYA", 2, 6),
    row("20190719", 0, "TBA", "NYA", 3, 3),            # a tie
    row("20190720", 0, "TBA", "NYA", 7, 2, forfeit="T"),    # protest upheld: no decision
    row("20190721", 0, "TBA", "NYA", 5, 7, forfeit="V"),    # forfeited to the visitors despite the score
    row("20190722", 0, "TBA", "NYA", 0, 0, outs=0, forfeit="H", aline="", hline=""),  # never played
])
g = {x["game_id"]: x for x in season}
check("doubleheader game 1 comes first: NYY 1-0 after it", rec(g["retro-NYA201907181"], "home", "after") == "1-0")
check("doubleheader game 2 enters at 1-0 and leaves at 2-0", rec(g["retro-NYA201907182"], "home", "before") == "1-0"
      and rec(g["retro-NYA201907182"], "home", "after") == "2-0")
check("a tie counts for neither (2-0 before and after)", rec(g["retro-NYA201907190"], "home", "after") == "2-0"
      and g["retro-NYA201907190"]["winner"] is None)
check("'T' (protest upheld) counts for neither, flagged no_decision, not a forfeit",
      rec(g["retro-NYA201907200"], "away", "after") == "0-2" and g["retro-NYA201907200"]["no_decision"]
      and g["retro-NYA201907200"]["forfeit"] is None)
f = g["retro-NYA201907210"]
check("a forfeit counts as forfeited (V: TBA wins though NYY led 7-5), on-field score kept, flagged",
      rec(f, "away", "after") == "1-2" and rec(f, "home", "after") == "2-1"
      and (f["away_score"], f["home_score"]) == (5, 7) and f["forfeit"] == "V")
n = g["retro-NYA201907220"]
check("a never-played forfeit counts on its date and is marked played=False",
      not n["played"] and rec(n, "home", "after") == "3-1")

print("the one documented override")
o = gr.season_from_game_logs([row("19010723", 0, "WS1", "CLE", 4, 4, forfeit="H")])[0]
check("1901-07-23 WS1 @ CLE (game log: forfeit H) is a no-decision: counts for neither, no forfeit flag",
      o["game_id"] == "retro-CLE190107230" and o["no_decision"] and o["forfeit"] is None
      and o["winner"] is None and rec(o, "home", "after") == "0-0" and rec(o, "away", "after") == "0-0")
check("the override cites its source", "Baseball-Reference" in gr.NO_DECISION_OVERRIDES["retro-CLE190107230"])
same_day_other = gr.season_from_game_logs([row("19010723", 0, "WS1", "CLE", 4, 4, forfeit="H"),
                                          row("19010724", 0, "WS1", "CLE", 4, 4, forfeit="H")])[1]
check("no other game is touched (a 1901-07-24 forfeit H still counts for CLE)",
      same_day_other["forfeit"] == "H" and same_day_other["winner"] == "home")

print("suspended games count on their start date")
s = gr.season_from_game_logs([
    row("20180515", 0, "NYA", "WAS", 3, 5, completion="20180618,,3,3,33"),
    row("20180516", 0, "NYA", "WAS", 1, 2),
])
check("the start-date row is the game, completed_on 2018-06-18",
      s[0]["game_date"] == datetime.date(2018, 5, 15) and s[0]["completed_on"] == datetime.date(2018, 6, 18))
check("it counts before the next day's game (WAS 1-0 entering 5/16)", rec(s[1], "home", "before") == "1-0")

print("the payload")
pay = gr.history_payload(season, lambda gid: -len(gid))
check("history games are keyed by the card's synthetic gamePk", all(p["game_pk"] < 0 for p in pay))
check("history games are final, with before and after", all(p["final"] and p["home"]["after"] for p in pay))
check("the forfeit flag rides on the payload (the V forfeit, and the never-played H one marked played=False)",
      [(p["forfeit"], p["played"]) for p in pay if p["forfeit"]] == [("V", True), ("H", False)])

print("current season (balldontlie)")
MLB = {1, 2, 3}


def bdl(i, date, home, away, hr, ar, status="STATUS_FINAL", stype="regular", post=False):
    return {"id": i, "date": date, "status": status, "season_type": stype, "postseason": post,
            "home_team": {"id": home, "abbreviation": f"T{home}"}, "away_team": {"id": away, "abbreviation": f"T{away}"},
            "home_team_data": {"runs": hr}, "away_team_data": {"runs": ar}}


cur = {x["bdl_game_id"]: x for x in gr.current_season_records([
    bdl(1, "2026-03-01T18:05:00.000Z", 1, 2, 9, 0, stype="spring_training"),
    bdl(2, "2026-04-01T23:05:00.000Z", 1, 2, 5, 3),
    bdl(3, "2026-04-02T17:05:00.000Z", 1, 2, 2, 4),                         # DH game 1 (ET 4/2)
    bdl(4, "2026-04-02T23:05:00.000Z", 1, 2, None, None, status="STATUS_POSTPONED"),
    bdl(5, "2026-04-03T02:10:00.000Z", 1, 2, 6, 1),                         # 10:10 pm ET 4/2: DH game 2
    bdl(6, "2026-07-14T00:00:00.000Z", 900, 901, 5, 4),                     # All-Star: non-MLB teams
    bdl(7, "2026-04-04T23:05:00.000Z", 1, 2, None, None, status="STATUS_SCHEDULED"),
    bdl(8, "2026-10-04T23:05:00.000Z", 1, 2, 3, 0, stype="postseason", post=True),
], MLB)}
check("spring training does not count (game 2 enters at 0-0)", rec(cur[2], "home", "before") == "0-0")
check("doubleheader in start-time order: game 1 enters 1-0, game 2 enters 1-1",
      rec(cur[3], "home", "before") == "1-0" and rec(cur[5], "home", "before") == "1-1")
check("10:10 pm ET is still the ET date (game 5 dated 2026-04-02)", cur[5]["game_date"] == datetime.date(2026, 4, 2))
check("a postponed row: record entering, none after, counts for no one",
      rec(cur[4], "home", "before") == "1-1" and cur[4]["home_w_after"] is None)
check("a scheduled game enters at the latest record (2-1)", rec(cur[7], "home", "before") == "2-1"
      and cur[7]["home_w_after"] is None)
check("the All-Star Game (non-MLB sides) has no record", cur[6]["home_w_before"] is None and not cur[6]["counts"])
check("a postseason game has no record", cur[8]["home_w_before"] is None and cur[8]["home_w_after"] is None)
cp = {p["game_pk"]: p for p in gr.current_payload(list(cur.values()))}
check("current payload is keyed by the balldontlie id, postseason flagged, no record",
      cp[8]["postseason"] and cp[8]["home"]["before"] is None and cp[2]["home"]["after"] == {"w": 1, "l": 0})

print("2026 from the real walk (testdata/game-records/bdl-2026-walk.json)")
walk = json.load(open(os.path.join(REPO, "testdata", "game-records", "bdl-2026-walk.json")))["games"]
TEAMS = set(range(1, 31))
recs = gr.current_season_records(walk, TEAMS)
pit_nyy = [x for x in recs if x["game_date"] == datetime.date(2026, 7, 20) and x["home_abbr"] == "NYY"]
check("PIT@NYY 2026-07-20 (5-8): PIT 52-48 -> 52-49, NYY 55-44 -> 56-44",
      len(pit_nyy) == 1 and (rec(pit_nyy[0], "away", "before"), rec(pit_nyy[0], "away", "after"),
                             rec(pit_nyy[0], "home", "before"), rec(pit_nyy[0], "home", "after"))
      == ("52-48", "52-49", "55-44", "56-44"))
check("2,429 regular-season finals counted (spring training and postseason excluded)", sum(x["counts"] for x in recs) == 2429)
last = {}
for x in sorted([x for x in recs if x["counts"]], key=lambda x: (x["game_date"], x["start"])):
    last[x["away_abbr"]] = rec(x, "away", "after"); last[x["home_abbr"]] = rec(x, "home", "after")
check("season-final NYY 93-68, LAD 100-62, COL 58-104 (team_seasons 2026)",
      (last.get("NYY"), last.get("LAD"), last.get("COL")) == ("93-68", "100-62", "58-104"))

print("GET /games/records reads only what the background stored")
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
import main                                                       # noqa: E402
calls = []
main.data_service._bdl_get_json = lambda *a, **k: calls.append(a) or {"data": []}
gr._store.clear()
try:
    main.games_records(date=datetime.date(2026, 7, 20)); code = 200
except main.HTTPException as e:
    code = e.status_code
check("current season not walked yet: 503, not an empty or wrong answer", code == 503)
gr._rebuild(2026, {g["id"]: g for g in walk}, TEAMS)
body = main.games_records(date=datetime.date(2026, 7, 20))
nyy = [x for x in body["games"] if x["home"]["team"] == "NYY"]
check("2026-07-20 served from the stored walk: NYY 55-44 -> 56-44, keyed by the balldontlie id",
      body["source"] == "balldontlie" and len(nyy) == 1 and nyy[0]["home"]["before"] == {"w": 55, "l": 44}
      and nyy[0]["home"]["after"] == {"w": 56, "l": 44} and nyy[0]["game_pk"] == nyy[0]["bdl_game_id"])
check("the request path never called balldontlie", calls == [])
try:
    main.games_records(date=datetime.date(1986, 8, 2)); code = 200
except main.HTTPException as e:
    code = e.status_code
check("a history date with the table unreachable: 503, not a guess", code == 503)

print("history lookups: an empty or failed result is never cached")
import types                                                      # noqa: E402
main.meta_coverage = lambda: {"retrosheet_last_season": 2025}
main.connection.db_available = lambda: True
answers = []          # what each successive DB session returns: a list of rows, or an exception


class FakeSession:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k):
        nxt = answers.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return [types.SimpleNamespace(_mapping=r) for r in nxt]


main.connection.get_session = lambda: FakeSession()
main._cache.clear()
day = datetime.date(1986, 8, 2)
one = gr.season_from_game_logs([row("19860802", 0, "MON", "NYN", 1, 4)])
answers[:] = [[], RuntimeError("connection reset"), one, AssertionError("must not query again")]
first = main.games_records(date=day)
check("an empty result is returned empty", first["games"] == [])
try:
    main.games_records(date=day); code = 200
except main.HTTPException as e:
    code = e.status_code
check("...and was not cached: the next request queried again (and its failure is a 503)", code == 503)
third = main.games_records(date=day)
check("...and the failure was not cached either: the next request finds the game",
      [g["retro_game_id"] for g in third["games"]] == ["retro-NYN198608020"])
fourth = main.games_records(date=day)
check("a result WITH games is cached (no further query)", fourth == third and answers and isinstance(answers[0], AssertionError))

print("history from Retrosheet game logs (RETRO_GL_DIR)")
GL = os.environ.get("RETRO_GL_DIR")
if not GL or not os.path.exists(os.path.join(GL, "gl1898.txt")):
    print("  [SKIPPED] RETRO_GL_DIR not set — spot checks and season-final validation not run")
else:
    rows = []
    for y in range(1898, 2026):
        rows += gr.season_from_game_logs(list(csv.reader(open(os.path.join(GL, f"gl{y}.txt"), encoding="latin-1"))))
    by = {x["game_id"]: x for x in rows}
    a = by["retro-WAS201805150"]
    check("suspended NYY@WSH 2018-05-15: NYY 28-13, WSH 25-18 after it",
          (rec(a, "away", "after"), rec(a, "home", "after")) == ("28-13", "25-18"))
    b = by["retro-WS2197109300"]
    check("1971 WS2 finale, forfeited: NYY 82-80, WSH 63-96, forfeit flag set",
          (rec(b, "away", "after"), rec(b, "home", "after"), b["forfeit"]) == ("82-80", "63-96", "V"))
    for gid, want in (("retro-NYA201907181", ("56-41", "56-42", "60-33", "61-33")),
                      ("retro-NYA201907182", ("56-42", "56-43", "61-33", "62-33")),
                      ("retro-NYN198608020", ("50-48", "50-49", "67-32", "68-32"))):
        x = by[gid]
        check(f"{gid}: entering/after {want}",
              (rec(x, "away", "before"), rec(x, "away", "after"), rec(x, "home", "before"), rec(x, "home", "after")) == want)
    final = {}
    for x in sorted(rows, key=gr.history_order):
        for s in ("away", "home"):
            final[(x["season"], x[f"{s}_team"])] = (x[f"{s}_w_after"], x[f"{s}_l_after"])
    teams = {}
    for t in csv.DictReader(open(os.path.join(REPO, "backend", "data", "lahman", "Teams.csv"), encoding="utf-8-sig")):
        teams[(int(t["yearID"]), t["teamIDretro"] or t["teamID"])] = (int(t["W"]), int(t["L"]))
    off = sorted((s, tm) for (s, tm), wl in final.items() if teams.get((s, tm)) and teams[(s, tm)] != wl)
    KNOWN = {(1898, "BLN"), (1898, "CHN"), (1898, "NY1"), (1898, "PHI"), (1899, "BLN"), (1899, "BRO"),
             (1899, "CIN"), (1899, "LS3"), (1899, "NY1"), (1899, "PIT"), (1899, "SLN"), (1900, "BRO"), (1900, "SLN")}
    check(f"season-final W-L == team_seasons for every team-season but the 13 known 1898-1900 differences; "
          f"off: {len(off)}", set(off) == KNOWN)
    check("1901 CLE 54-82 and WS1 61-72 (the override makes them match)",
          final[(1901, "CLE")] == (54, 82) and final[(1901, "WS1")] == (61, 72))
    check("every MLB team-season 1898-2025 compared (2,754)", sum(1 for k in final if k in teams) == 2754)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
