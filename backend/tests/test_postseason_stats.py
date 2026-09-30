#!/usr/bin/env python3
"""`postseason_stats` — the per-season source rule, which /ask expressions
the postseason logs can answer, team codes through the crosswalk, and rates.

⚠️ THE CHECKS THAT MATTER:
  • One source per season: Retrosheet up to its newest season, balldontlie
    after — never both, so nothing is counted twice when Retrosheet catches up.
  • A stat the postseason logs don't carry (CG, WAR...) returns None, so /ask
    falls through instead of answering from a missing column.
  • Retrosheet 'ANA' and balldontlie 'LAA' are one franchise, through the
    crosswalk; so are 'ATH' and balldontlie's 'OAK'.
  • The two known gaps (pre-1903, the Negro Leagues postseason) are never
    silent: a question inside one is declined, and totals and boards say
    what they cover.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_postseason_stats.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import postseason_stats as ps                                     # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


print("source rule")
check("a published season is Retrosheet's", ps.postseason_source(2025, 2025) == "retrosheet")
check("the current season is balldontlie's", ps.postseason_source(2026, 2025) == "bdl")
check("the day Retrosheet publishes 2026, it takes 2026 over", ps.postseason_source(2026, 2026) == "retrosheet")
check("the SQL rule names both sources and excludes unmapped rows",
      "source = 'retrosheet'" in ps.SOURCE_SQL and "source = 'bdl'" in ps.SOURCE_SQL
      and "player_id IS NOT NULL" in ps.SOURCE_SQL)

print("which /ask expressions the logs can answer")
check("HR", ps.column_expr("bat", '"HR"') == '"HR"')
check("singles, a derived expression", ps.column_expr("bat", '("H" - doubles - triples - "HR")') is not None)
check("a pitcher's G becomes a count of game rows", ps.column_expr("pit", '"G"') == "1")
for expr in ('"CG"', '"SHO"', '"WAR"', '"BFP"', '"GF"'):
    check(f"{expr} is not in the postseason logs -> None", ps.column_expr("pit", expr) is None)

print("team codes through the crosswalk")
for code, season, fr in (("ANA", 2014, "ANA"), ("LAA", 2026, "ANA"), ("LAA", 1962, "ANA"), ("CAL", 1986, "ANA"),
                         ("ATH", 2025, "OAK"), ("OAK", 2026, "OAK"), ("OAK", 2020, "OAK"),
                         ("MLN", 1957, "ATL"), ("NYA", 2026, "NYY"), ("NYY", 2026, "NYY"), ("SDN", 2026, "SDP")):
    check(f"{code} {season} -> {fr}", ps.franchise_of(code, season) == fr, ps.franchise_of(code, season))

print("labels and rates")
check("round names", [ps.round_name(r, "AL") for r in ("WC", "DS", "CS", "WS")]
      == ["AL Wild Card", "ALDS", "ALCS", "World Series"])
t = {"H": 42, "doubles": 5, "triples": 2, "HR": 15, "AB": 129, "BB": 33, "HBP": 0, "SF": 0}
r = ps.batting_rates(t)
check("Ruth's World Series AVG .326", r["AVG"] == 0.326, r)
p = ps.pitching_rates({"outs": 93, "ER": 3, "BB": 12, "H": 17})
check("IP shown in thirds, ERA over outs: 31.0 IP, 0.87", p["IP"] == "31.0" and p["ERA"] == 0.87, p)
check("no innings: rates are None, not a division error",
      ps.pitching_rates({"outs": 0, "ER": 1, "BB": 1, "H": 1})["ERA"] is None)

print("coverage: the two known gaps are never silent")
d, n = ps.coverage()
check("a career total states its span", d is None and n == "Postseason, 1903 to present.", n)
d, n = ps.coverage(leaderboard=True)
check("a leaderboard adds the Negro Leagues note", "Negro Leagues postseason isn't included" in n, n)
check("a season before 1903 is declined, not answered", ps.coverage(season=1888)[0] == ps.PRE_1903)
check("  ...and so is a range entirely before 1903", ps.coverage(season_start=1884, season_end=1890)[0] == ps.PRE_1903)
d, n = ps.coverage(season_start=1900, season_end=1935)
check("a range straddling 1903 answers, naming the missing years", d is None and "1900-1902 isn't available" in n, n)
check("a Negro Leagues season is declined", ps.coverage(season=1942, negro_seasons={1942})[0] == ps.NEGRO_DECLINE)
check("one covered season needs no footnote", ps.coverage(season=2004) == (None, None))
check("a career with early seasons says so",
      "his seasons before 1903 aren't covered" in ps.coverage(early_seasons=True)[1])
print("a career entirely in a gap declines instead of answering 0")
check("King Kelly (all 1880s): pre-1903", ps.uncovered_reason([(1880, "NL"), (1887, "NL")], None, None) == ps.PRE_1903)
check("Buck Leonard (IND 1934, NN2 1935-48): Negro Leagues",
      ps.uncovered_reason([(1934, "IND"), (1940, "NN2")], None, None) == ps.NEGRO_DECLINE)
check("Satchel Paige (NN2, then Cleveland 1948): a real 0",
      ps.uncovered_reason([(1940, "NN2"), (1948, "AL")], None, None) is None)
check("Arlie Latham (1880s, then 1909 Giants): a real 0",
      ps.uncovered_reason([(1886, "AA"), (1909, "NL")], None, None) is None)
check("a Federal League-only career: no postseason existed, a real 0",
      ps.uncovered_reason([(1914, "FL"), (1915, "FL")], None, None) is None)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
