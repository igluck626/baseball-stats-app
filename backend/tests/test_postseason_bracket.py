#!/usr/bin/env python3
"""The postseason bracket (`postseason_series.build_bracket`).

⚠️ THE CHECKS THAT MATTER:
  • Every 2022-2025 bracket built from balldontlie's games and seeds must
    match Lahman `series_post` slot for slot: round, both teams, winner, W-L.
    Lahman appears here as a TEST reference only; the app never reads it for
    the bracket's current seasons.
  • The Division Series pairing: seed 1 meets the 4/5 Wild Card winner and
    seed 2 the 3/6 winner, every season.
  • Slots come from the bracket structure, never balldontlie's placeholder
    games: before the Wild Card round a side is TBD with its candidates.

Standalone, no pytest — matches test_streak_classify.py.
Run: python3 backend/tests/test_postseason_bracket.py
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "api"))
import postseason_series as ps                                   # noqa: E402

FIX = json.load(open(os.path.join(HERE, "fixtures", "postseason_2022_2025.json")))["seasons"]
P26 = json.load(open(os.path.join(HERE, "fixtures", "postseason_2026_pre_wildcard.json")))
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def slots(b):
    out = [s for lg in ("AL", "NL") for s in b["leagues"][lg]["slots"]]
    return out + ([b["world_series"]] if b["world_series"] else [])


def by_id(b, slot_id):
    return next(s for s in slots(b) if s["id"] == slot_id)


LAHMAN_ROUND = {"WC": "WC", "DS": "DS", "CS": "CS", "WS": "WS"}

# ── 1. 2022-2025 against Lahman, slot for slot ───────────────────────────────
print("2022-2025 brackets vs Lahman series_post")
for year in (2022, 2023, 2024, 2025):
    f = FIX[str(year)]
    b = ps.build_bracket(f["games"], f["standings"], year)
    got = slots(b)
    ok = len(got) == 11 and all(s["state"] == "complete" for s in got)
    matched = 0
    for s in got:
        loser = next(x["team"] for x in s["sides"] if x["team"] != s["winner"])
        row = next((r for r in f["lahman"] if {r["winner"], r["loser"]} == {s["winner"], loser}), None)
        if (row and row["winner"] == s["winner"] and s["round"] in row["round"]
                and (s["league"] is None or row["round"].startswith(s["league"]))
                and s["series"]["wins"][s["winner"]] == row["wins"]
                and s["series"]["wins"][loser] == row["losses"]):
            matched += 1
    check(f"{year}: 11 slots, all complete, {matched}/11 match Lahman", ok and matched == 11)

# ── 2. the Division Series pairing ───────────────────────────────────────────
print("Division Series pairing: 1 meets the 4/5 winner, 2 the 3/6 winner")
for year in (2022, 2023, 2024, 2025):
    f = FIX[str(year)]
    b = ps.build_bracket(f["games"], f["standings"], year)
    ok = True
    for lg in ("AL", "NL"):
        seed = {x["seed"]: x["team"] for x in b["leagues"][lg]["seeds"]}
        ds1, ds2 = by_id(b, f"{lg}-DS-1"), by_id(b, f"{lg}-DS-2")
        w45, w36 = by_id(b, f"{lg}-WC-4v5")["winner"], by_id(b, f"{lg}-WC-3v6")["winner"]
        ok &= [x["team"] for x in ds1["sides"]] == [seed[1], w45]
        ok &= [x["team"] for x in ds2["sides"]] == [seed[2], w36]
    check(f"{year}: both leagues", ok)

# ── 3. TBD states from the real 2026 payload ─────────────────────────────────
print("2026 before the Wild Card round (real payload)")
b = ps.build_bracket(P26["games"], P26["standings"], 2026)
al_ds1 = by_id(b, "AL-DS-1")
check("AL-DS-1 is TB (1) vs the NYY/BOS winner",
      al_ds1["sides"][0]["team"] == "TB" and al_ds1["sides"][0]["seed"] == 1
      and al_ds1["sides"][1]["team"] is None and sorted(al_ds1["sides"][1]["candidates"]) == ["BOS", "NYY"]
      and al_ds1["state"] == "tbd" and al_ds1["series"] is None, al_ds1)
check("the Wild Card slots are scheduled, with their series attached",
      all(by_id(b, i)["state"] == "scheduled" and by_id(b, i)["series"] for i in ("AL-WC-3v6", "NL-WC-4v5")))
ws = b["world_series"]
check("the World Series is TBD with six candidates a side",
      ws["state"] == "tbd" and [len(x["candidates"]) for x in ws["sides"]] == [6, 6])
check("no placeholder team ever appears", "UNK" not in json.dumps(b))


def finish(payload, pair, winners):
    p = copy.deepcopy(payload)
    gs = sorted((g for g in p["games"] if {g["home_team"]["abbreviation"], g["away_team"]["abbreviation"]} == pair),
                key=lambda g: g["date"])
    for g, w in zip(gs, winners):
        g["status"] = "STATUS_FINAL"
        home = g["home_team"]["abbreviation"] == w
        g["home_team_data"]["runs"], g["away_team_data"]["runs"] = (3, 1) if home else (1, 3)
    return p


mid = finish(P26, {"ATL", "PHI"}, ["ATL"])
b = ps.build_bracket(mid["games"], mid["standings"], 2026)
check("at 1-0 the Wild Card is in progress and the Division Series still TBD",
      by_id(b, "NL-WC-3v6")["state"] == "in_progress" and by_id(b, "NL-DS-2")["state"] == "tbd")
done = finish(P26, {"ATL", "PHI"}, ["ATL", "ATL"])
b = ps.build_bracket(done["games"], done["standings"], 2026)
ds2 = by_id(b, "NL-DS-2")
check("once ATL sweeps, NL-DS-2 is LAD (2) vs ATL (3)",
      [(x["team"], x["seed"]) for x in ds2["sides"]] == [("LAD", 2), ("ATL", 3)], ds2["sides"])
check("  ...and the NLCS candidate list narrows to include ATL, not PHI",
      "ATL" in by_id(b, "NL-CS")["sides"][1]["candidates"]
      and "PHI" not in by_id(b, "NL-CS")["sides"][1]["candidates"])

# ── 4. seed safety ───────────────────────────────────────────────────────────
print("seed safety")
bad = copy.deepcopy(FIX["2025"])
for row in bad["standings"]:
    if row["team"]["abbreviation"] == "DET":
        row["playoff_seed"] = 5
b = ps.build_bracket(bad["games"], bad["standings"], 2025)
check("a league with untrustworthy seeds gets no slots, only its series as a list",
      not b["leagues"]["AL"]["trusted"] and b["leagues"]["AL"]["slots"] == []
      and len(b["leagues"]["AL"]["series"]) == 5)
check("  ...the World Series slot needs both leagues", b["world_series"] is None)
check("  ...the other league is untouched", b["leagues"]["NL"]["trusted"] and len(b["leagues"]["NL"]["slots"]) == 5)
check("the bracket starts in 2022", ps.BRACKET_FIRST_SEASON == 2022)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
