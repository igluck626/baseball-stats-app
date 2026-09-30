#!/usr/bin/env python3
"""`postseason_stats.player_postseason_gamelogs` — one postseason's game lines
for the profile's Game Logs.

⚠️ THE CHECKS THAT MATTER:
  • One source per season (`postseason_source`): a season Retrosheet has
    published reads only Retrosheet rows, even when balldontlie rows exist too.
  • The game number counts the TEAM's games in the series: a man who sat out
    Game 1 shows G2 for his first game.
  • A pitcher's line carries HIS decision; the game's result and score are his
    team's, from its batting rows.
  • Innings come back in thirds from outs ("6.2"), never as a float.

The database is a small in-memory stand-in answering the function's four
queries from fixture rows. Standalone, no pytest. Needs the backend's Python.
Run: <backend python> backend/tests/test_postseason_gamelogs.py
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import postseason_stats as ps                                     # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


D = datetime.date
BAT0 = {c: 0 for c in ps._BAT_SUM}
PIT0 = {c: 0 for c in ps._PIT_SUM}


def bat(pid, gid, day, team, opp, res, ts, os_, src="retrosheet", season=2009, rnd="WS", **st):
    return {"player_id": pid, "game_id": gid, "game_date": day, "season": season, "round": rnd, "team": team,
            "opponent": opp, "home_away": "H", "result": res, "team_score": ts, "opp_score": os_, "source": src,
            **BAT0, **st}


def pit(pid, gid, day, team, opp, dec, ip, src="retrosheet", season=2009, rnd="WS", **st):
    return {"player_id": pid, "game_id": gid, "game_date": day, "season": season, "round": rnd, "team": team,
            "opponent": opp, "home_away": "A", "result": dec, "IP": ip, "source": src, **PIT0, **st}


# 2009 World Series, Yankees v Phillies. Player 1 (a batter) sits out Game 1;
# player 2 pitches Games 1 and 4. A stale balldontlie copy of Game 3 exists too.
BAT = [
    bat(9, "g1", D(2009, 10, 28), "NYA", "PHI", "L", 1, 6),
    bat(9, "g2", D(2009, 10, 29), "NYA", "PHI", "W", 3, 1),
    bat(1, "g3", D(2009, 10, 31), "NYA", "PHI", "W", 8, 5, AB=4, H=2, HR=1, RBI=3, BB=1),
    bat(1, "g4", D(2009, 11, 1), "NYA", "PHI", "W", 7, 4, AB=5, H=1, doubles=1),
    bat(1, "bdl-g3", D(2009, 10, 31), "NYA", "PHI", "W", 8, 5, src="bdl", AB=9, H=9),
    bat(8, "g1", D(2009, 10, 28), "PHI", "NYA", "W", 6, 1),
]
PIT = [
    pit(2, "g1", D(2009, 10, 28), "NYA", "PHI", "L", 7.0, H=4, ER=2, BB=3, SO=6, L=1, GS=1),
    pit(2, "g4", D(2009, 11, 1), "NYA", "PHI", "ND", 6.666666666666667, H=7, ER=3, BB=3, SO=6, GS=1),
]


class Result:
    def __init__(self, rows=None, scalar=None):
        self.rows, self._scalar = rows or [], scalar

    def scalar(self):
        return self._scalar

    def __iter__(self):
        return iter(self.rows)


class Row(tuple):
    """A result row with `_mapping`, like SQLAlchemy's."""
    def __new__(cls, d):
        r = super().__new__(cls, tuple(d.values()))
        r._mapping = d
        return r


class FakeDB:
    def execute(self, stmt, params=None):
        sql, p = str(stmt), params or {}
        if "max(season)" in sql:
            return Result(scalar=max(r["season"] for r in BAT if r["source"] == "retrosheet"))
        if "FROM team_seasons" in sql:
            return Result([("NYY", 2009, "AL", "New York Yankees"), ("PHI", 2009, "NL", "Philadelphia Phillies")])
        table = BAT if "batting" in sql else PIT
        if "SELECT DISTINCT" in sql:
            seen, out = set(), []
            for r in sorted(table, key=lambda r: (r["game_date"], r["game_id"])):
                if r["season"] == p["season"] and r["source"] == p["src"] and r["team"] in p["tt"]:
                    k = (r["game_id"], r["game_date"], r["round"], r["team"], r["opponent"], r["result"],
                         r["team_score"], r["opp_score"])
                    if k not in seen:
                        seen.add(k)
                        out.append(k)
            return Result(out)
        rows = [r for r in table if r["player_id"] == p["pid"] and r["season"] == p["season"] and r["source"] == p["src"]]
        out = []
        for r in rows:
            d = {k: r[k] for k in ("game_id", "game_date", "round", "team", "opponent", "home_away")}
            d.update({c: r[c] for c in (ps._BAT_SUM if table is BAT else ps._PIT_SUM)})
            if table is PIT:
                d["IP"], d["decision"] = r["IP"], r["result"]
            out.append(Row(d))
        return Result(out)


db = FakeDB()

print("a batter who sat out Game 1")
ps._retro_last_cache.clear()
g = ps.player_postseason_gamelogs(db, 1, 2009)
b = g["batting"]
check("2009 is a published season: Retrosheet only", g["source"] == "retrosheet", g["source"])
check("  ...the stale balldontlie copy of Game 3 is not read", len(b) == 2 and all(r["game_id"] != "bdl-g3" for r in b), b)
check("his first game is G3 (his team's third), not G1", [r["game_number"] for r in b] == [3, 4], [r["game_number"] for r in b])
check("oldest first, with round names", [r["date"] for r in b] == ["2009-10-31", "2009-11-01"]
      and b[0]["round_name"] == "World Series", b)
check("the team's result and score", (b[0]["result"], b[0]["team_score"], b[0]["opp_score"]) == ("W", 8, 5), b[0])
check("the batting line and rates", b[0]["H"] == 2 and b[0]["HR"] == 1 and b[0]["AVG"] == 0.5, b[0])
check("no pitching side", g["pitching"] is None)

print("a pitcher")
p = ps.player_postseason_gamelogs(db, 2, 2009)["pitching"]
check("his decision, and his team's result from its batting rows",
      [(r["decision"], r["result"], r["team_score"], r["opp_score"]) for r in p] == [("L", "L", 1, 6), ("ND", "W", 7, 4)], p)
check("innings in thirds from outs: 7.0 and 6.2", [r["IP"] for r in p] == ["7.0", "6.2"] and p[1]["outs"] == 20, p)
check("game numbers G1 and G4", [r["game_number"] for r in p] == [1, 4], p)

print("a player with no games that postseason")
n = ps.player_postseason_gamelogs(db, 77, 2009)
check("both sides null", n["batting"] is None and n["pitching"] is None, n)

print("the current season reads balldontlie")
ps._retro_last_cache.clear()
check("2026 -> bdl", ps.player_postseason_gamelogs(db, 1, 2026)["source"] == "bdl")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
