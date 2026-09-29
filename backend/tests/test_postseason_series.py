#!/usr/bin/env python3
"""Postseason series derivation (`api/postseason_series.py`).

⚠️ THE CHECK THAT MATTERS: every 2022-2025 series derived from balldontlie's
games and seeds must match Lahman `series_post` on round AND W-L. 44 series.
The fixtures are real balldontlie payloads (trimmed) plus the Lahman rows; see
fixtures/postseason_2022_2025.json.

Standalone, no pytest — matches test_streak_classify.py.
Run: python3 backend/tests/test_postseason_series.py
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "api"))
import postseason_series as ps                                   # noqa: E402

FIX = json.load(open(os.path.join(HERE, "fixtures", "postseason_2022_2025.json")))["seasons"]
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def series_for(year):
    f = FIX[str(year)]
    return ps.build_series(f["games"], f["standings"], year)


def find(series, a, b):
    return next(s for s in series if set(s["teams"]) == {a, b})


# ── 1. every series vs Lahman ────────────────────────────────────────────────
print("2022-2025 vs Lahman series_post (round and W-L)")
matched = 0
for year in (2022, 2023, 2024, 2025):
    got = series_for(year)
    for row in FIX[str(year)]["lahman"]:
        s = next((x for x in got if set(x["teams"]) == {row["winner"], row["loser"]}), None)
        ok = (s is not None and s["round"] is not None and s["round"] in row["round"]
              and s["wins"][row["winner"]] == row["wins"] and s["wins"][row["loser"]] == row["losses"]
              and s["is_over"] and s["winner"] == row["winner"])
        matched += ok
        if not ok:
            print(f"    MISMATCH {year} {row} -> {s and (s['round'], s['wins'], s['winner'])}")
    check(f"{year}: {len(got)} series derived, {len(FIX[str(year)]['lahman'])} in Lahman",
          len(got) == len(FIX[str(year)]["lahman"]))
check(f"all 44 series match Lahman on round and W-L ({matched}/44)", matched == 44)

# ── 2. game numbers, text, sweeps, clinch ────────────────────────────────────
print("game numbers and display lines")
s25 = series_for(2025)
ws = find(s25, "LAD", "TOR")
check("2025 World Series is 7 games, numbered 1-7 in start order",
      [g["game_number"] for g in ws["games"]] == [1, 2, 3, 4, 5, 6, 7]
      and [g["date"] for g in ws["games"]] == sorted(g["date"] for g in ws["games"]))
check("  ...round 'World Series', best of 7", ws["round_name"] == "World Series" and ws["best_of"] == 7)
check("  ...Game 7 line is 'LAD wins 4-3'", ws["games"][-1]["line"] == "LAD wins 4-3", ws["games"][-1]["line"])
check("  ...Game 6 line is 'Series tied 3-3'", ws["games"][5]["line"] == "Series tied 3-3", ws["games"][5]["line"])

wc = find(s25, "CIN", "LAD")
check("2025 NL Wild Card LAD-CIN is a 2-0 sweep, over after Game 2",
      len(wc["games"]) == 2 and wc["wins"] == {"CIN": 0, "LAD": 2} and wc["is_over"] and wc["winner"] == "LAD")
check("  ...Game 1 'LAD leads 1-0', Game 2 'LAD wins 2-0'",
      [g["line"] for g in wc["games"]] == ["LAD leads 1-0", "LAD wins 2-0"], [g["line"] for g in wc["games"]])
check("  ...round name 'NL Wild Card', best of 3", wc["round_name"] == "NL Wild Card" and wc["best_of"] == 3)

nlcs = find(s25, "LAD", "MIL")
check("2025 NLCS 4-0 sweep: 'LAD wins 4-0'", nlcs["games"][-1]["line"] == "LAD wins 4-0" and nlcs["round_name"] == "NLCS")

# ── 3. pre-game and live lines, clinch and elimination flags ─────────────────
print("pre-game / live lines and flags")


def with_game2_pending(year, a, b):
    """The series as it stood before Game 2 was played: Game 2 scheduled."""
    f = copy.deepcopy(FIX[str(year)])
    pair = sorted((g for g in f["games"] if {g["home_team"]["abbreviation"], g["away_team"]["abbreviation"]} == {a, b}),
                  key=lambda g: g["date"])
    pending = pair[1]
    pending["status"] = "STATUS_SCHEDULED"
    pending["home_team_data"]["runs"] = pending["away_team_data"]["runs"] = 0
    keep = {g["id"] for g in pair[:2]}
    f["games"] = [g for g in f["games"] if g["id"] in keep or
                  {g["home_team"]["abbreviation"], g["away_team"]["abbreviation"]} != {a, b}]
    return find(ps.build_series(f["games"], f["standings"], year), a, b)


pre = with_game2_pending(2025, "CIN", "LAD")
g1, g2 = pre["games"]
check("Game 1 (final) line 'LAD leads 1-0'", g1["line"] == "LAD leads 1-0", g1["line"])
check("Game 2 pre-game line 'NL Wild Card · Game 2 · LAD leads 1-0'",
      g2["line"] == "NL Wild Card · Game 2 · LAD leads 1-0", g2["line"])
check("  ...LAD can clinch, CIN faces elimination",
      g2["can_clinch"] and g2["clinch_teams"] == ["LAD"] and g2["elimination_teams"] == ["CIN"])
check("  ...series not over, no winner", not pre["is_over"] and pre["winner"] is None)
check("  ...an if-necessary Game 3 is not invented", len(pre["games"]) == 2)

g1pre = copy.deepcopy(FIX["2025"])
cle_det = sorted((g for g in g1pre["games"] if {g["home_team"]["abbreviation"], g["away_team"]["abbreviation"]} == {"CLE", "DET"}),
                 key=lambda g: g["date"])
for g in cle_det:
    g["status"] = "STATUS_SCHEDULED"
s = find(ps.build_series(g1pre["games"], g1pre["standings"], 2025), "CLE", "DET")
check("Game 1 pre-game line is just 'AL Wild Card · Game 1'", s["games"][0]["line"] == "AL Wild Card · Game 1", s["games"][0]["line"])
check("  ...no clinch or elimination flags", not s["games"][0]["can_clinch"] and not s["games"][0]["elimination_game"])

g3 = find(series_for(2025), "CLE", "DET")["games"][2]
check("CLE-DET Game 3 (1-1 entering) was a winner-advances game in the final data",
      find(series_for(2025), "CLE", "DET")["games"][1]["line"] == "Series tied 1-1")
live = copy.deepcopy(FIX["2025"])
g3src = next(g for g in live["games"] if g["id"] == g3["game_id"])
g3src["status"] = "STATUS_IN_PROGRESS"
s = find(ps.build_series(live["games"], live["standings"], 2025), "CLE", "DET")
check("Game 3 live at 1-1: both can clinch, both face elimination",
      sorted(s["games"][2]["clinch_teams"]) == ["CLE", "DET"] and sorted(s["games"][2]["elimination_teams"]) == ["CLE", "DET"])
check("  ...live line 'AL Wild Card · Game 3 · Series tied 1-1'",
      s["games"][2]["line"] == "AL Wild Card · Game 3 · Series tied 1-1", s["games"][2]["line"])

# ── 4. postponed / cancelled games are not numbered ──────────────────────────
print("postponed and cancelled games")
pp = copy.deepcopy(FIX["2025"])
src = sorted((g for g in pp["games"] if {g["home_team"]["abbreviation"], g["away_team"]["abbreviation"]} == {"LAD", "TOR"}),
             key=lambda g: g["date"])
ghost = copy.deepcopy(src[0]); ghost["id"] = 1; ghost["status"] = "STATUS_POSTPONED"
ghost["date"] = "2025-10-24T00:00:00.000Z"
pp["games"].append(ghost)
s = find(ps.build_series(pp["games"], pp["standings"], 2025), "LAD", "TOR")
check("a postponed game before Game 1 takes no number",
      [g["game_number"] for g in s["games"]] == list(range(1, 8)) and all(g["game_id"] != 1 for g in s["games"]))

# ── 5. seed safety ───────────────────────────────────────────────────────────
print("seed safety")
bad = copy.deepcopy(FIX["2025"])
for row in bad["standings"]:
    if row["team"]["abbreviation"] == "DET":
        row["playoff_seed"] = 5          # DET is really 6; CLE(3)-DET would read {3,5}
s = ps.build_series(bad["games"], bad["standings"], 2025)
al = [x for x in s if x["league"] == "AL"]
check("AL first-round pairs not {3,6}/{4,5}: every AL round label hidden",
      all(x["round"] is None and x["round_name"] is None and x["best_of"] is None for x in al))
check("  ...but the AL series scores still show",
      find(s, "CLE", "DET")["wins"] == {"CLE": 1, "DET": 2}
      and find(s, "CLE", "DET")["games"][-1]["line"] == "DET leads 2-1")
check("  ...a series with no best-of never claims 'wins'",
      all("wins" not in (g["line"] or "") for x in al for g in x["games"]))
check("  ...the World Series is hidden too (it needs both leagues)", find(s, "LAD", "TOR")["round"] is None)
check("  ...the NL is unaffected", all(x["round"] is not None for x in s if x["league"] == "NL"))

unseeded = copy.deepcopy(FIX["2025"])
unseeded["standings"] = [r for r in unseeded["standings"] if r["team"]["abbreviation"] != "SD"]
s = ps.build_series(unseeded["games"], unseeded["standings"], 2025)
check("a missing seed hides that league's rounds",
      all(x["round"] is None for x in s if x["league"] == "NL"))

override = copy.deepcopy(bad)
ps.SEED_OVERRIDES[2025] = {"DET": 6}
s = ps.build_series(override["games"], override["standings"], 2025)
ps.SEED_OVERRIDES.clear()
check("the per-season override repairs a wrong seed", find(s, "CLE", "DET")["round_name"] == "AL Wild Card")
check("the override table ships empty", ps.SEED_OVERRIDES == {})

# ── 6. the real 2026 pre-Wild-Card payload: placeholders and if-necessary ────
print("2026 before the Wild Card round (real payload)")
P26 = json.load(open(os.path.join(HERE, "fixtures", "postseason_2026_pre_wildcard.json")))
s26 = ps.build_series(P26["games"], P26["standings"], 2026)
unk = [sum(1 for g in P26["games"] if [g["home_team"]["abbreviation"], g["away_team"]["abbreviation"]].count("UNK") == k)
       for k in (2, 1)]
check("53 listed games: 21 all-'UNK' and 20 half-known are skipped, leaving 12 in 4 series",
      len(P26["games"]) == 53 and unk == [21, 20] and len(s26) == 4
      and sum(len(x["games"]) for x in s26) == 12, (len(P26["games"]), unk, len(s26)))
check("the four pairs are the 3v6 / 4v5 Wild Cards",
      sorted(sorted(x["teams"]) for x in s26) == [["ATL", "PHI"], ["BOS", "NYY"], ["CHC", "SD"], ["CHW", "HOU"]]
      and all(x["best_of"] == 3 for x in s26))
atl = find(s26, "ATL", "PHI")
check("Games 1-2 guaranteed, Game 3 listed as 'if necessary'",
      [g["if_necessary"] for g in atl["games"]] == [False, False, True]
      and atl["games"][2]["line"] == "NL Wild Card · Game 3 (if necessary)", [g["line"] for g in atl["games"]])


def play(payload, pair, results_):
    """Finish the pair's first games with the given winners (home abbreviation wins if 'H')."""
    p2 = copy.deepcopy(payload)
    gs = sorted((g for g in p2["games"] if {g["home_team"]["abbreviation"], g["away_team"]["abbreviation"]} == pair),
                key=lambda g: g["date"])
    for g, who in zip(gs, results_):
        g["status"] = "STATUS_FINAL"
        home_wins = g["home_team"]["abbreviation"] == who
        g["home_team_data"]["runs"], g["away_team_data"]["runs"] = (3, 1) if home_wins else (1, 3)
    return find(ps.build_series(p2["games"], p2["standings"], 2026), *sorted(pair))


split = play(P26, {"ATL", "PHI"}, ["ATL", "PHI"])
check("at 1-1 Game 3 is no longer 'if necessary', and is a winner-advances game",
      split["games"][2]["if_necessary"] is False
      and split["games"][2]["line"] == "NL Wild Card · Game 3 · Series tied 1-1"
      and sorted(split["games"][2]["clinch_teams"]) == ["ATL", "PHI"], split["games"][2])
sweep = play(P26, {"ATL", "PHI"}, ["ATL", "ATL"])
check("after a 2-0 sweep the listed Game 3 is dropped, and the series reads 'ATL wins 2-0'",
      len(sweep["games"]) == 2 and sweep["is_over"] and sweep["winner"] == "ATL"
      and sweep["games"][-1]["line"] == "ATL wins 2-0", [g["line"] for g in sweep["games"]])
one = play(P26, {"ATL", "PHI"}, ["PHI"])
check("after Game 1 the Game 2 pre-game line carries the series state",
      one["games"][1]["line"] == "NL Wild Card · Game 2 · PHI leads 1-0"
      and one["games"][1]["clinch_teams"] == ["PHI"] and one["games"][1]["elimination_teams"] == ["ATL"])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
