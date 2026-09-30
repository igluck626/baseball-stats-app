#!/usr/bin/env python3
"""`retrosheet_postseason.parse_year` on a hand-built retrosplits slice.

⚠️ THE CHECKS THAT MATTER:
  • Only phases F/D/L/W are read, mapped to WC/DS/CS/WS. R (regular season)
    and A (All-Star) never reach the postseason tables.
  • De-duplication is the regular ingest's rule: one row per
    (person, game, slot, seq), highest source wins (evt > box > ded).
  • ER is P_ER as recorded — never rebuilt.
  • An unmapped player's rows are KEPT (player_id NULL, retro id set) and
    the id is reported — never dropped.

Offline; the bridge is passed in. Standalone, no pytest. Needs the backend's
Python (3.10+): it imports retrosheet_gamelogs, which imports `database`.
Run: <backend python> backend/tests/test_retrosheet_postseason.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import retrosheet_postseason as rp                                # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


PCOLS = ["game.key", "game.source", "game.date", "season.phase", "team.alignment", "team.key",
         "opponent.key", "person.key", "slot", "seq", "B_G", "B_PA", "B_AB", "B_R", "B_H", "B_2B",
         "B_3B", "B_HR", "B_RBI", "B_BB", "B_IBB", "B_SO", "B_GDP", "B_HP", "B_SH", "B_SF", "B_SB",
         "B_CS", "P_G", "P_GS", "P_W", "P_L", "P_SV", "P_OUT", "P_R", "P_ER", "P_H", "P_HR", "P_BB",
         "P_SO", "P_HP"]


def prow(**kw):
    base = {c: "" for c in PCOLS}
    base.update({"game.source": "evt", "game.date": "2025-10-01", "team.alignment": "1",
                 "team.key": "NYA", "opponent.key": "BOS", "slot": "1", "seq": "1"})
    base.update({k.replace("__", "."): str(v) for k, v in kw.items()})
    return ",".join(base[c] for c in PCOLS)


playing = "\n".join([",".join(PCOLS),
    # Wild Card game, batter — evt and a lower-priority box duplicate
    prow(game__key="NYA202510010", season__phase="F", person__key="judga001",
         B_G=1, B_PA=4, B_AB=3, B_H=2, B_HR=1, B_RBI=2, B_BB=1),
    prow(game__key="NYA202510010", game__source="box", season__phase="F", person__key="judga001",
         B_G=1, B_PA=4, B_AB=4, B_H=0),
    # same game, pitcher: 20 outs, 3 R, 2 ER as recorded
    prow(game__key="NYA202510010", season__phase="F", person__key="friem001", slot="10",
         P_G=1, P_GS=1, P_W=1, P_OUT=20, P_R=3, P_ER=2, P_H=5, P_SO=8),
    # one each of D, L, W and the two phases that must be ignored
    prow(game__key="NYA202510050", season__phase="D", person__key="judga001", B_G=1, B_AB=4),
    prow(game__key="NYA202510130", season__phase="L", person__key="judga001", B_G=1, B_AB=4),
    prow(game__key="NYA202510250", season__phase="W", person__key="judga001", B_G=1, B_AB=4),
    prow(game__key="NYA202509280", season__phase="R", person__key="judga001", B_G=1, B_AB=4),
    prow(game__key="ALS202507150", season__phase="A", person__key="judga001", B_G=1, B_AB=4),
    # unmapped player
    prow(game__key="NYA202510050", season__phase="D", person__key="nobod001", slot="2", B_G=1, B_AB=1),
]) + "\n"

TCOLS = ["game.key", "season.phase", "team.key", "B_R", "R_W", "R_L", "R_T"]
teams = "\n".join([",".join(TCOLS),
                   "NYA202510010,F,NYA,5,1,0,0",
                   "NYA202510010,F,BOS,3,0,1,0"]) + "\n"

bridge = {"judga001": 592450, "friem001": 608331}
bat, pit, unmapped = rp.parse_year(2025, playing, teams, bridge)

print("phases and rounds")
mapped_bat = [r for r in bat if r["player_id"] is not None]
check("four mapped batting rows: one per postseason round, R and A dropped",
      sorted(r["round"] for r in mapped_bat) == ["CS", "DS", "WC", "WS"], [r["round"] for r in mapped_bat])
check("nothing from the regular season or the All-Star Game",
      not any(r["game_id"] in ("retro-NYA202509280", "retro-ALS202507150") for r in bat + pit))

print("de-duplication")
wc = [r for r in bat if r["round"] == "WC"]
check("the evt row beats the box duplicate", len(wc) == 1 and wc[0]["H"] == 2 and wc[0]["AB"] == 3, wc)

print("pitching")
check("one pitching row", len(pit) == 1, pit)
p = pit[0]
check("ER is P_ER as recorded (2, not R's 3)", p["ER"] == 2 and p["R"] == 3)
check("IP from outs: 20 outs -> 6.667", abs(p["IP"] - 6.667) < 1e-9, p["IP"])
check("the pitcher's decision, not the team's result", p["result"] == "W" and p["W"] == 1)

print("scores and identity")
w = wc[0]
check("team result and score from the teams file", (w["result"], w["team_score"], w["opp_score"]) == ("W", 5, 3), w)
check("MLBAM id from the bridge, source retrosheet",
      w["player_id"] == 592450 and w["bdl_player_id"] is None and w["source"] == "retrosheet")
check("the unmapped id is reported", unmapped == {"nobod001"}, unmapped)
nob = [r for r in bat if r["retro_player_id"] == "nobod001"]
check("  ...and its row is KEPT, player_id NULL, retro id set",
      len(nob) == 1 and nob[0]["player_id"] is None and nob[0]["round"] == "DS", nob)
check("every row carries its retro id", all(r["retro_player_id"] for r in bat + pit))

print("two-way starters and the appearance gate")
# Ohtani, 2025 World Series: a real batting row in slot 1, and an EMPTY one in
# slot 0 on the row that carries his pitching.
two_way = "\n".join([",".join(PCOLS),
    prow(game__key="TOR202511010", season__phase="W", person__key="ohtas001", slot="1", seq="1",
         B_G=1, B_PA=6, B_AB=5, B_H=2),
    prow(game__key="TOR202511010", season__phase="W", person__key="ohtas001", slot="0", seq="1",
         B_G=1, P_G=1, P_GS=1, P_OUT=10, P_ER=3),
]) + "\n"
b25, p25, _ = rp.parse_year(2025, two_way, None, {"ohtas001": 660271})
check("2025: one batting row (slot 1's) and one pitching row, not two batting rows",
      len(b25) == 1 and b25[0]["H"] == 2 and len(p25) == 1 and p25[0]["ER"] == 3, (b25, p25))
old_era = "\n".join([",".join(PCOLS),
    prow(game__key="NYA199610200", season__phase="W", person__key="petta001", slot="9",
         B_G=1, P_G=1, P_GS=1, P_OUT=15),
]) + "\n"
b96, _, _ = rp.parse_year(1996, old_era, None, {"petta001": 120003})
check("before 2022 a pitcher's empty batting row is kept, as the regular ingest keeps it", len(b96) == 1, b96)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
