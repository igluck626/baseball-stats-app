#!/usr/bin/env python3
"""'this postseason' means the CURRENT season's postseason.

⚠️ WHY THIS EXISTS. "Who has the most home runs this postseason?", asked during the
2026 postseason, answered with Manny Ramirez's 29 — the CAREER postseason board. The
only relative-time detector knew "this season" / "this year", so the model's reading
(postseason, no season) went through untouched, and a season-less postseason board is
a career board by design. The data was fine: "the 2026 postseason" asked outright
returned the 2026 board.

Checks, without importing the app (AST-extracted from api/main.py, like
test_guard_tool.py): the detector's positives and negatives, the board titles, the
"has the postseason started" check, and that the ask route applies the override —
postseason tools only, decline when there's nothing yet, BEFORE the 'this season'
override.
Run: python3 backend/tests/test_this_postseason.py
"""
import ast
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "..", "api", "main.py")
SRC = open(MAIN).read()
TREE = ast.parse(SRC)

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def extract(names, ns):
    for node in TREE.body:
        targets = []
        if isinstance(node, (ast.FunctionDef, ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.FunctionDef):
                targets = [node.name]
            elif isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            else:
                targets = [node.target.id] if isinstance(node.target, ast.Name) else []
        if any(t in names for t in targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), MAIN, "exec"), ns)


ns = {"re": re, "time": time}
extract({"_THIS_POSTSEASON_RE", "_THIS_SEASON_RE", "_POSTSEASON_TOOLS", "_lb_title",
         "_MONTH_NAMES", "_POSTSEASON_STATS_CACHE", "_postseason_has_stats"}, ns)
R = ns["_THIS_POSTSEASON_RE"]

print("the detector")
for q in ["Who has the most home runs this postseason?", "home run leaders these playoffs",
          "Who has the most strikeouts this post-season?", "most RBI this October",
          "Who has the most hits in this year's playoffs?", "most saves in this year's postseason",
          "Who leads the current playoffs in home runs?", "most home runs so far this postseason",
          "Who has hit the most home runs in the playoffs this year?"]:
    check(f"matches: {q!r}", R.search(q))
for q in ["Who has the most postseason home runs?", "most postseason home runs last year",
          "Who has the most home runs in the 2025 postseason?", "Who has the most home runs this season?",
          "most home runs in October 1977", "Who hit the most home runs in last year's playoffs?",
          "How many postseason home runs does Aaron Judge have?"]:
    check(f"leaves alone: {q!r}", not R.search(q))

print("titles say postseason")
T = ns["_lb_title"]
check("2026 postseason board: 'Most postseason home runs in 2026'",
      T("home runs", season=2026, game_type="P") == "Most postseason home runs in 2026")
check("career postseason board: 'Most career postseason home runs'",
      T("home runs", game_type="P") == "Most career postseason home runs")
check("regular-season board unchanged: 'Most home runs in 2026'", T("home runs", season=2026) == "Most home runs in 2026")
check("career regular board still untitled", T("home runs") is None)
check("lowercase 'p' is postseason too", T("hits", season=2026, game_type="p") == "Most postseason hits in 2026")

print("has the postseason started")


class _Result:
    def __init__(self, n): self.n = n
    def scalar(self): return self.n


class _DB:
    def __init__(self, n): self.n = n; self.calls = 0
    def execute(self, *a, **k): self.calls += 1; return _Result(self.n)


class _Conn:
    def __init__(self, db): self.db = db
    def get_session(self):
        db = self.db
        class _Ctx:
            def __enter__(self): return db
            def __exit__(self, *a): return False
        return _Ctx()


class _PS:
    TABLE = {"bat": "postseason_batting_gamelogs"}
    SOURCE_SQL = "1=1"
    @staticmethod
    def retro_last(db): return 2025


ns.update({"postseason_stats": _PS, "_sa_text": lambda s: s})
ns["connection"] = _Conn(_DB(0))
ns["_POSTSEASON_STATS_CACHE"].clear()
check("no 2027 postseason rows -> False", ns["_postseason_has_stats"](2027) is False)
ns["connection"] = _Conn(_DB(412))
ns["_POSTSEASON_STATS_CACHE"].clear()
check("2026 has rows -> True", ns["_postseason_has_stats"](2026) is True)
db = _DB(412); ns["connection"] = _Conn(db)
ns["_postseason_has_stats"](2026)
check("a True answer is cached (no second query)", db.calls == 0)


class _Boom:
    def get_session(self): raise RuntimeError("db down")


ns["connection"] = _Boom(); ns["_POSTSEASON_STATS_CACHE"].clear()
check("DB trouble does not invent a decline (-> True)", ns["_postseason_has_stats"](2026) is True)

print("the ask route applies it")
i_post = SRC.index("if tool_input is not None and _THIS_POSTSEASON_RE.search(q):")
i_season = SRC.index("if tool_input is not None and _THIS_SEASON_RE.search(q):")
block = SRC[i_post:i_season]
check("the postseason override runs BEFORE the 'this season' override", i_post < i_season)
check("only postseason-capable tools are rewritten; others decline as out of scope",
      "tool_name not in _POSTSEASON_TOOLS" in block and 'base["out_of_scope"] = True' in block)
check("declines when the current postseason has no stats yet",
      "_postseason_has_stats(_cs)" in block and 'base["declined"] = True' in block)
check("forces postseason + the current season and clears any range",
      'tool_input["game_type"] = "P"' in block and 'tool_input["season"] = _cs' in block
      and 'tool_input.pop("season_start", None)' in block and 'tool_input.pop("season_end", None)' in block)
check("drops a month the model read off 'this October'", 'tool_input.pop("month", None)' in block)
check("both leaderboard paths pass the game type to the title",
      "_lb_title(_lbl, team_display, season, season_start, season_end, month, gt)" in SRC
      and "game_type=gt)," in SRC)
check("the tools that can say postseason are the three with a game_type parameter",
      set(ns["_POSTSEASON_TOOLS"]) == {"query_leaderboard", "query_situational", "query_comparison"})

print("a player with no games this postseason")
i_tot = SRC.index("def _run_season_total(")
tot = SRC[i_tot:SRC.index("\ndef ", i_tot + 10)]
i_none = tot.find('if gt == "P" and not nrows and season is not None and int(season) == _current_season():')
check("the postseason count has a 'no games this postseason' branch", i_none > 0)
branch = tot[i_none:i_none + 900]
check("it says he hasn't played, by name and season, instead of answering 0",
      "hasn't played in the {int(season)} postseason." in branch and "{resolved['name']}" in branch
      and '"declined": True' in branch and '"stat_value": None' in branch)
check("it fires only for the CURRENT season (a past postseason with no games is untouched)",
      "int(season) == _current_season()" in branch)
check("it comes after the known-gap branch and before the count is returned",
      tot.index("if gap:") < i_none < tot.index('"count": int(total) if nrows else 0'))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
