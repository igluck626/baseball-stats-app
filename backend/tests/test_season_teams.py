#!/usr/bin/env python3
"""The per-team split rule behind player_season_teams / pitcher_season_teams
(scripts/load_season_teams.py).

⚠️ THE CHECKS THAT MATTER:
  • Retrosheet's per-team rows win when they add up exactly to our season row.
  • Otherwise Lahman's, only if THEY add up exactly; otherwise no breakdown.
  • Only columns recorded on both sides are compared; a missing value is never
    read as 0 — it is left out, and the club row shows "—" for it.
  • Innings compare as outs (our decimal thirds vs Lahman's IPouts).

Inputs are real rows (fixtures/season_teams_cases.json): our season and stint
rows as stored, and Lahman's CSV lines verbatim — J. D. Martinez 2017 (Retrosheet
adds up), Jimmy Sheckard 1902 (Retrosheet doesn't; Lahman does, without PA or
CS), Bert Humphries 1911 (pitching, Lahman), Ed Daily 1887 (no Retrosheet at all).

Standalone, no pytest. Run: <backend python> backend/tests/test_season_teams.py
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import load_season_teams as L                                     # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


CASES = {(c["bbref"], c["year"], c["role"]): c
         for c in json.load(open(os.path.join(HERE, "fixtures", "season_teams_cases.json")))["cases"]}


def inputs(bbref, year, role):
    """The case as load_sources() shapes it: season row, Retrosheet rows, Lahman rows."""
    c = CASES[(bbref, year, role)]
    store = L.STORE[role]
    season = L._row(store, [c["season"].get(col) for col in store], role)
    retro = []
    for s in c["retrosheet_stints"]:
        row = L._row(store, [s.get(col) for col in store], role)
        row.update(_team=s["team"], _order=s["stint_order"], _source=s["source"])
        retro.append(row)
    lahman = []
    for r in c["lahman_rows"]:
        row = {col: L.num(r[lc]) for col, lc in L.TABLES[role]["lmap"].items()}
        if role == "bat":
            row["PA"] = None
        row.update(_team=r["teamID"], _order=int(r["stint"]), _source="lahman")
        lahman.append(row)
    return season, retro, lahman


print("which source")
s, r, lh = inputs("martijd02", 2017, "bat")
src, clubs = L.choose("bat", s, r, lh)
check("Martinez 2017: Retrosheet's rows add up, so Retrosheet", src == "retrosheet", src)
check("  DET 16 HR, ARI 29 HR", sorted((c["_team"], c["HR"]) for c in clubs) == [("ARI", 29), ("DET", 16)])
s, r, lh = inputs("sheckji01", 1902, "bat")
check("Sheckard 1902: Retrosheet's rows don't add up", not L.adds_up(r, s, L.CHECK["bat"]))
src, clubs = L.choose("bat", s, r, lh)
check("  so Lahman's, which do", src == "lahman" and sorted(c["_team"] for c in clubs) == ["BLA", "BRO"], src)
ok = L.verified(clubs, s, L.STORE["bat"])
check("  PA and CS aren't recorded by Lahman: left out, never counted as 0",
      "PA" not in L.compared(clubs, s, L.CHECK["bat"]) and "CS" not in ok and "H" in ok and "AB" in ok, ok)
s, r, lh = inputs("dailyed01", 1887, "bat")
src, _ = L.choose("bat", s, r, lh)
check("Daily 1887 batting: no Retrosheet rows, Lahman's add up", src == "lahman" and not r, src)
s, r, lh = inputs("dailyed01", 1887, "pit")
src, clubs = L.choose("pit", s, r, lh)
check("Daily 1887 pitching: PHI 6 G / 124 outs, WS8 1 G / 21 outs, from Lahman",
      src == "lahman" and sorted((c["_team"], c["G"], c["IP"]) for c in clubs) == [("PHI", 6, 124), ("WS8", 1, 21)],
      [(c["_team"], c["G"], c["IP"]) for c in clubs])
s, r, lh = inputs("humphbe01", 1911, "pit")
src, clubs = L.choose("pit", s, r, lh)
check("Humphries 1911: innings compare as outs (our thirds vs Lahman's IPouts)",
      src in ("retrosheet", "lahman") and sum(c["IP"] for c in clubs) == s["IP"], (src, s["IP"]))

print("missing is never 0")
s, r, lh = inputs("martijd02", 2017, "bat")
r0 = copy.deepcopy(r)
r0[0]["SB"] = None
check("a club row missing SB: SB left out of the check, the split still stands",
      L.adds_up(r0, s, L.CHECK["bat"]) and "SB" not in L.verified(r0, s, L.STORE["bat"]))
s0 = dict(s, SB=None)
check("the season row missing SB: the same", L.adds_up(r, s0, L.CHECK["bat"]) and "SB" not in L.verified(r, s0, L.CHECK["bat"]))
r1 = copy.deepcopy(r)
r1[0]["HR"] += 1
check("one HR too many on a club row: no longer adds up", not L.adds_up(r1, s, L.CHECK["bat"]))
check("...and with Lahman's also short, no breakdown at all",
      L.choose("bat", s, r1, [dict(x, HR=0) for x in lh])[0] is None)
blank = [{c: None for c in L.STORE["bat"]} | {"_team": t, "_order": i} for i, t in enumerate(["A", "B"], 1)]
check("club rows that record nothing compare nothing, so they don't qualify",
      not L.adds_up(blank, s, L.CHECK["bat"]))
check("a single club is not a split", L.choose("bat", s, r[:1], lh[:1])[0] is None)

print("the stored rows")
rows = [{"player_id": 2, "year": 1902, "stint_order": 1, "team": "BLA", "source": "lahman", "verified": "G|AB",
         **{c: None for c in L.STORE["bat"]}},
        {"player_id": 1, "year": 2017, "stint_order": 2, "team": "ARI", "source": "retrosheet", "verified": "G",
         **{c: 1 for c in L.STORE["bat"]}}]
text = L.canonical(rows, "bat")
check("the checksum text is sorted and order-independent", text == L.canonical(list(reversed(rows)), "bat"))
line = next(l for l in text.splitlines() if ",BLA," in l)
check("a missing value is written empty, not 0, and verified stays one field",
      line.startswith("2,1902,1,BLA,lahman,G|AB,") and line.endswith(",,") and "0" not in line.split(",", 6)[6])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
