#!/usr/bin/env python3
"""Team batting and pitching totals with MLB and league ranks — the team page's
Season Stats card (api/team_season_stats.py).

⚠️ THE CHECKS THAT MATTER:
  • Rank = 1 + the teams strictly better, ties marked; lower is better for ERA,
    WHIP and home runs allowed; league ranks only among the league's clubs.
  • balldontlie's team totals (the card's source): regular season only; innings
    in baseball notation become outs; rates from the counts; OBP as shipped.

Standalone, no pytest. Run: <backend python> backend/tests/test_team_season_stats.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import team_season_stats as T                                     # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


print("ranks")
r = T.rank({"A": 3.10, "B": 2.90, "C": 3.10, "D": None}, lower_is_better=True)
check("lowest ERA ranks 1st", r["B"] == (1, False))
check("two teams on 3.10 are both 2nd, tied", r["A"] == (2, True) and r["C"] == (2, True))
check("a team without a value has no rank", "D" not in r)
r = T.rank({"A": 200, "B": 250, "C": 180}, lower_is_better=False)
check("most home runs ranks 1st", r["B"] == (1, False) and r["C"] == (3, False))
print("balldontlie team totals (real rows)")
import json
ROWS = json.load(open(os.path.join(HERE, "fixtures", "bdl_team_season_stats_2026.json")))["rows"]
tot, teams = T.bdl_totals(ROWS)
check("postseason rows dropped: three clubs, one row each", sorted(tot) == ["COL", "LAD", "NYY"], sorted(tot))
lad = tot["LAD"]
check("Dodgers regular season: 5451 AB, 1399 H, 801 R (not the postseason's 257 AB)",
      (lad["bat"]["AB"], lad["bat"]["H"], lad["bat"]["R"]) == (5451, 1399, 801))
check("'1435.2' innings = 4307 outs", lad["pit"]["outs"] == 4307 and T.outs_from_bdl_ip("1435.2") == 4307)
check("ERA from the counts: 27 x 566 / 4307", abs(lad["pit"]["ERA"] - 27 * 566 / 4307) < 1e-12)
lad_row = next(r for r in ROWS if r["team"]["abbreviation"] == "LAD" and r["season_type"] == "regular")
check("AVG from the counts; OBP as shipped; OPS = OBP + SLG",
      abs(lad["bat"]["AVG"] - 1399 / 5451) < 1e-12 and lad["bat"]["OBP"] == lad_row["batting_obp"]
      and abs(lad["bat"]["OPS"] - (lad["bat"]["OBP"] + lad["bat"]["SLG"])) < 1e-12)
pay = T.payload(tot, {a: teams[a].get("league") for a in teams})
check("among these three, the Yankees' 3.24 ERA ranks 1st and the Rockies' 5.59 3rd",
      pay["NYY"]["stats"]["ERA"]["mlb_rank"] == 1 and pay["COL"]["stats"]["ERA"]["mlb_rank"] == 3)
check("league ranks stay within the league: the Yankees are alone in the AL here",
      pay["NYY"]["stats"]["HR"]["league_rank"] == 1 and pay["COL"]["stats"]["HR"]["league_rank"] == 2)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
