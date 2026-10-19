#!/usr/bin/env python3
"""Team batting and pitching totals with MLB and league ranks — the team page's
Season Stats card (api/team_season_stats.py).

⚠️ THE CHECKS THAT MATTER:
  • A team's totals are the sum of its players' per-team rows; rates come from
    those totals (OBP from H, BB, HBP, AB, SF; ERA and WHIP from outs).
  • A missing value makes its total — and every rate built on it — missing,
    never a smaller number (it isn't read as 0).
  • Rank = 1 + the teams strictly better, ties marked; lower is better for ERA,
    WHIP and home runs allowed; league ranks only among the league's clubs.
  • balldontlie's team totals (the card's source): regular season only; innings
    in baseball notation become outs; rates from the counts; OBP as shipped.
  • The drift check: beyond the threshold is drift; a stat we can't compute is
    INCOMPLETE, never compared as 0.
  • Run differential: runs scored minus runs allowed from OUR standings
    (fixtures/standings_2026.json, a real response), ranked by the same rule;
    left out of the drift check (balldontlie's team totals have no runs allowed).

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


def bat(team, ab, h, bb=0, hbp=0, sf=0, d=0, t=0, hr=0, r=0, sb=0):
    return {"team": team, "PA": ab + bb + hbp + sf, "AB": ab, "R": r, "H": h, "doubles": d, "triples": t,
            "HR": hr, "RBI": 0, "BB": bb, "SO": 0, "SB": sb, "HBP": hbp, "SF": sf}


def pit(team, ip, h, er, bb=0, so=0, hr=0, sv=0):
    return {"team": team, "IP": ip, "H": h, "ER": er, "BB": bb, "SO": so, "HR": hr, "SV": sv, "W": 0, "L": 0}


print("totals and rates")
tot = T.team_totals([bat("LAN", 300, 90, bb=30, hbp=2, sf=3, d=20, t=1, hr=10), bat("LAN", 200, 50, d=5, hr=5)],
                    [pit("LAN", 100.333, 80, 30, bb=25), pit("LAN", 50.667, 40, 20, bb=10)])
b, p = tot["LAN"]["bat"], tot["LAN"]["pit"]
check("hits and at-bats sum over the club's rows", b["H"] == 140 and b["AB"] == 500)
check("AVG from the totals: 140 / 500", abs(b["AVG"] - 0.28) < 1e-12)
check("OBP: (H + BB + HBP) / (AB + BB + HBP + SF)", abs(b["OBP"] - (140 + 30 + 2) / (500 + 30 + 2 + 3)) < 1e-12)
check("SLG from total bases", abs(b["SLG"] - (140 + 25 + 2 * 1 + 3 * 15) / 500) < 1e-12)
check("innings sum as outs: 100.333 + 50.667 = 151 innings", p["IP"] == 151.0)
check("ERA = 27 x ER / outs", abs(p["ERA"] - 27 * 50 / 453) < 1e-12)
check("WHIP = 3 x (H + BB) / outs", abs(p["WHIP"] - 3 * 155 / 453) < 1e-12)

print("missing is never 0")
tot = T.team_totals([bat("COL", 300, 90), dict(bat("COL", 200, 50), SF=None)], [pit("COL", 10.0, 9, 4)])
check("one row missing SF: the team's SF is missing, not short", tot["COL"]["bat"]["SF"] is None)
check("...so OBP and OPS are missing too", tot["COL"]["bat"]["OBP"] is None and tot["COL"]["bat"]["OPS"] is None)
check("...while AVG, which doesn't need SF, stands", abs(tot["COL"]["bat"]["AVG"] - 140 / 500) < 1e-12)

print("ranks")
r = T.rank({"A": 3.10, "B": 2.90, "C": 3.10, "D": None}, lower_is_better=True)
check("lowest ERA ranks 1st", r["B"] == (1, False))
check("two teams on 3.10 are both 2nd, tied", r["A"] == (2, True) and r["C"] == (2, True))
check("a team without a value has no rank", "D" not in r)
r = T.rank({"A": 200, "B": 250, "C": 180}, lower_is_better=False)
check("most home runs ranks 1st", r["B"] == (1, False) and r["C"] == (3, False))
tot = T.team_totals([bat("NYA", 100, 30, hr=10), bat("BOS", 100, 25, hr=12), bat("LAN", 100, 35, hr=20)],
                    [pit("NYA", 9, 5, 1), pit("BOS", 9, 8, 4), pit("LAN", 9, 7, 3)])
pay = T.payload(tot, {"NYA": "AL", "BOS": "AL", "LAN": "NL"})
check("MLB rank across both leagues: the Dodgers' 20 HR are 1st", pay["LAN"]["stats"]["HR"]["mlb_rank"] == 1)
check("league rank only among the league: the Red Sox' 12 HR are 1st in the AL",
      pay["BOS"]["stats"]["HR"]["league_rank"] == 1 and pay["BOS"]["stats"]["HR"]["mlb_rank"] == 2)
check("ERA: lower is better (Yankees 1.00 first)", pay["NYA"]["stats"]["ERA"]["mlb_rank"] == 1)

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

print("drift check")
import copy
ours = copy.deepcopy(tot)
ours["LAD"]["bat"]["SB"] = 72 - 2           # within max(2, 1%): not drift
ours["COL"]["bat"]["SB"] = 109 - 5          # beyond: drift
ours["NYY"]["bat"]["OBP"] = None            # unknown cell: incomplete, not 0
del ours["COL"]["pit"]["ERA"]
d = T.drift(ours, tot)
check("2 short of 72 SB is within the threshold", not [x for x in d["drift"] if x[:2] == ("LAD", "SB")])
check("5 short of 109 SB is drift", ("COL", "SB", 104, 109) in d["drift"], d["drift"])
check("an unknown OBP is incomplete, never drift", ("NYY", "OBP") in d["incomplete"]
      and not [x for x in d["drift"] if x[:2] == ("NYY", "OBP")])
check("a missing stat is incomplete too", ("COL", "ERA") in d["incomplete"])
d = T.drift({"LAD": ours["LAD"]}, tot)
check("a club we have no rows for is listed as missing", d["missing_team"] == ["COL", "NYY"])

print("run differential (our standings)")
STAND = json.load(open(os.path.join(HERE, "fixtures", "standings_2026.json")))["response"]["standings"]
# balldontlie team id -> Lahman code, as data_service._BDL_TO_LAHMAN_TEAM_MAP has it (all 30).
LAHMAN = {1: "ARI", 2: "ATL", 3: "BAL", 4: "BOS", 5: "CHN", 6: "CHA", 7: "CIN", 8: "CLE", 9: "COL",
          10: "DET", 11: "HOU", 12: "KCA", 13: "LAA", 14: "LAN", 15: "MIA", 16: "MIL", 17: "MIN",
          18: "NYN", 19: "NYA", 20: "ATH", 21: "PHI", 22: "PIT", 23: "SDN", 24: "SFN", 25: "SEA",
          26: "SLN", 27: "TBA", 28: "TEX", 29: "TOR", 30: "WAS"}
check("four standings rows carry no bdl_id (KCR, TBD, SDP, SFG): the join can't use it",
      sorted(r["franch_id"] for r in STAND if r.get("bdl_id") is None) == ["KCR", "SDP", "SFG", "TBD"])
tot, teams = T.bdl_totals(ROWS)
T.with_run_differential(tot, teams, STAND, LAHMAN)
check("Yankees +138 (739 - 601), Dodgers +201, Rockies -192",
      (tot["NYY"]["team"]["RD"], tot["LAD"]["team"]["RD"], tot["COL"]["team"]["RD"]) == (138, 201, -192),
      {a: tot[a]["team"] for a in tot})
d = T.drift(tot, tot)
check("left out of the drift check", not any(k == "RD" for _, k, *_ in d["drift"])
      and not any(k == "RD" for _, k in d["incomplete"]))
by_code = {code: bid for bid, code in LAHMAN.items()}
everyone = {r["franch_id"]: {"bat": {}, "pit": {}} for r in STAND}
ids = {r["franch_id"]: {"id": by_code[r["team_id"]]} for r in STAND}
T.with_run_differential(everyone, ids, STAND, LAHMAN)
check("every one of the 30 gets a run differential", all(everyone[a]["team"]["RD"] is not None for a in everyone))
pay = T.payload(everyone, {r["franch_id"]: r["league"] for r in STAND})
rd = {a: pay[a]["stats"]["RD"] for a in pay}
check("all 30: the Yankees' +138 is 4th, not tied (MIL +214, LAD +201, CHC +147 ahead)",
      (rd["NYY"]["mlb_rank"], rd["NYY"]["mlb_tied"]) == (4, False), rd["NYY"])
check("Twins and Mariners both -58: the same rank, both marked tied",
      rd["MIN"]["mlb_rank"] == rd["SEA"]["mlb_rank"] and rd["MIN"]["mlb_tied"] and rd["SEA"]["mlb_tied"],
      (rd["MIN"], rd["SEA"]))
check("  ...ranked 1 + the clubs strictly better",
      rd["MIN"]["mlb_rank"] == 1 + sum(1 for r in STAND if r["runs_scored"] - r["runs_allowed"] > -58))
check("label 'Run differential', value an integer", rd["NYY"]["label"] == "Run differential" and rd["NYY"]["value"] == 138)
tot, teams = T.bdl_totals(ROWS)
T.with_run_differential(tot, teams, [r for r in STAND if r["franch_id"] != "COL"], LAHMAN)
pay = T.payload(tot, {a: teams[a].get("league") for a in teams})
check("a club missing from the standings: no value and no rank, never 0",
      pay["COL"]["stats"]["RD"]["value"] is None and pay["COL"]["stats"]["RD"]["mlb_rank"] is None)
T.with_run_differential(tot, teams, [dict(r, runs_allowed=None) if r["franch_id"] == "NYY" else r for r in STAND], LAHMAN)
check("runs allowed missing: no run differential", tot["NYY"]["team"]["RD"] is None)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
