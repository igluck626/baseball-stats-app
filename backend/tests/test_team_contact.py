#!/usr/bin/env python3
"""Team contact (AVG / xBA / balls hit 95+ mph) — team_contact.py.

Three 2026 postseason finals reproduce the table measured by hand on 2026-10-07,
whose AB and H also matched balldontlie's own box-score totals for all six sides;
then the exclusions, the untracked-ball rule (CLE: .194, not .188), the 90%
tracked and 9-at-bat display rules, and the finished-game cache.
Run: python3 backend/tests/test_team_contact.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "api"))
import team_contact as tc  # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


FIX = json.load(open(os.path.join(HERE, "fixtures", "team_contact_2026_postseason.json")))["games"]


def block(gid):
    return tc.team_contact(FIX[str(gid)]["plate_appearances"], final=True)


def r3(x):
    return None if x is None else round(x, 3)


print("three finals reproduce the measured table")
EXPECTED = {
    # game: ((away AB, H, AVG, xBA, 95+), (home ...))
    15333798: (("LAD", 36, 8, .222, .259, 12), ("ATL", 32, 5, .156, .146, 4)),
    15467376: (("NYY", 31, 4, .129, .195, 10), ("TB", 35, 8, .229, .313, 15)),
    15467377: (("CHW", 32, 6, .188, .204, 4), ("CLE", 32, 4, .125, .194, 10)),
}
for gid, sides in EXPECTED.items():
    b = block(gid)
    for (abbr, ab, h, avg, xba, hard), side in zip(sides, ("away", "home")):
        s = b[side]
        check(f"{abbr} {FIX[str(gid)]['date']}: {h}/{ab} AVG {avg:.3f}, xBA {xba:.3f}, {hard} hit 95+ "
              f"(got {s['h']}/{s['ab']} {r3(s['avg'])}, {r3(s['xba'])}, {s['hard_hit']})",
              (s["ab"], s["h"], r3(s["avg"]), r3(s["xba"]), s["hard_hit"]) == (ab, h, avg, xba, hard))
    check(f"game {gid} is shown (final, all sides tracked >= 90%)", b["show"] and b["reason"] is None)

print("the untracked ball (CLE, 2026-10-05: reached on an error, no tracking)")
cle = block(15467377)["home"]
check("values are sent unrounded (NYY xBA .19548 shows as .195; rounded twice it was .196)",
      abs(block(15467376)["away"]["xba"] - 6.06 / 31) < 1e-9)
check("CLE: 23 balls in play, 22 tracked", (cle["balls_in_play"], cle["tracked_balls_in_play"]) == (23, 22))
check("left out of the xBA at-bats (31) but not the real AB (32)", (cle["xba_ab"], cle["ab"]) == (31, 32))
check("so xBA is .194, not the .188 counting it as 0 would give", r3(cle["xba"]) == .194)
check("AVG is the real one, 4/32 = .125", r3(cle["avg"]) == .125)


def pa(half, result, xba=None, ev=None, call=None):
    p = []
    if xba is not None or ev is not None or call:
        p = [{"call_name": call or "In Play", "exit_velocity": ev, "expected_batting_average": xba}]
    return {"half_inning": half, "result": result, "pitches": p}


print("what is and isn't an at-bat")
side = tc._side([
    pa("top", "Single", .9, 100), pa("top", "Strikeout"), pa("top", "Walk"), pa("top", "Intent Walk"),
    pa("top", "Hit By Pitch"), pa("top", "Sac Fly", .3, 97), pa("top", "Sac Bunt", .1, 60),
    pa("top", "Catcher Interference"), pa("top", "Caught Stealing 2B"), pa("top", "Pickoff 1B"),
    pa("top", "Groundout", .2, 85), pa("top", "Strikeout Double Play"), pa("top", "Field Error", .4, 96),
])
check("AB counts single, strikeouts, groundout, error only: 5", side["ab"] == 5)
check("H = 1", side["h"] == 1)
check("strikeouts count 0: xBA = (.9 + .2 + .4) / 5 = .300", r3(side["xba"]) == .3)
check("hard-hit counts every tracked ball 95+, sac fly included: 3", side["hard_hit"] == 3)
check("balls in play (at-bats, not strikeouts): 3, all tracked", (side["balls_in_play"], side["tracked_share"]) == (3, 1.0))
check("an empty or unknown result is not an at-bat", not tc.is_at_bat(None) and not tc.is_at_bat(""))
check("a ball called in play with no tracking is a ball in play, untracked",
      tc._side([pa("top", "Groundout", call="In Play")])["tracked_balls_in_play"] == 0
      and tc._side([pa("top", "Groundout", call="In Play")])["balls_in_play"] == 1)

print("display rules")


def game(n_ab_away, n_ab_home, tracked_away=1.0, tracked_home=1.0):
    rows = []
    for half, n, share in (("top", n_ab_away, tracked_away), ("bottom", n_ab_home, tracked_home)):
        k = round(n * share)
        rows += [pa(half, "Groundout", .2, 90) for _ in range(k)]
        rows += [pa(half, "Groundout", call="In Play") for _ in range(n - k)]
    return rows


check("final, 10 AB a side, all tracked: shown", tc.team_contact(game(10, 10), final=True)["show"])
b = tc.team_contact(game(10, 10, tracked_away=0.8), final=True)
check("a side 80% tracked: hidden, reason 'untracked'", not b["show"] and b["reason"] == "untracked")
check("exactly 90% tracked is enough", tc.team_contact(game(10, 10, tracked_home=0.9), final=True)["show"])
b = tc.team_contact(game(8, 12), final=False)
check("live, a side with 8 AB: hidden, reason 'too_early'", not b["show"] and b["reason"] == "too_early")
check("live, 9 AB each: shown", tc.team_contact(game(9, 9), final=False)["show"])
check("final ignores the 9-AB rule", tc.team_contact(game(8, 8), final=True)["show"])
b = tc.team_contact([], final=True)
check("no plate appearances: hidden, reason 'no_data', no crash", not b["show"] and b["reason"] == "no_data"
      and b["away"]["xba"] is None and b["home"]["avg"] is None)
check("one side without a ball in play: 'no_data'",
      tc.team_contact(game(10, 0), final=True)["reason"] == "no_data")

print("finished-game fetch and cache")
calls = []


def fake_get(final, rows):
    def get(path, params):
        calls.append(path)
        if path.startswith("games/"):
            return {"data": {"status": "STATUS_FINAL" if final else "STATUS_IN_PROGRESS",
                             "away_team": {"id": 1}, "home_team": {"id": 2}}}
        if path in ("stats", "plays"):   # the box and the play stream (Team Stats)
            return {"data": []}
        return {"data": rows}
    return get


tc._FINAL_CACHE.clear(); calls.clear()
rows = FIX["15467377"]["plate_appearances"]
b1 = tc.for_game(15467377, fake_get(True, rows))
b2 = tc.for_game(15467377, fake_get(True, rows))
check("a final game is fetched once (game, plate appearances, box, plays) and then served from the cache",
      calls == ["games/15467377", "plate_appearances", "stats", "plays"] and b1 is b2)
check("the cached block is the same block (CLE xBA .194)", r3(b2["home"]["xba"]) == .194 and b2["game_id"] == 15467377)
tc._FINAL_CACHE.clear(); calls.clear()
tc.for_game(1, fake_get(False, rows)); tc.for_game(1, fake_get(False, rows))
check("a game still in progress is not cached", len(calls) == 8)
tc._FINAL_CACHE.clear(); calls.clear()
tc.for_game(2, fake_get(True, [])); tc.for_game(2, fake_get(True, []))
check("an empty fetch is not cached", len(calls) == 8)


def boom(path, params):
    raise RuntimeError("balldontlie down")


tc._FINAL_CACHE.clear()
try:
    tc.for_game(3, boom)
    raised = False
except RuntimeError:
    raised = True
check("a failed fetch raises (the endpoint returns 502) and caches nothing", raised and 3 not in tc._FINAL_CACHE)

print("wired in")
SRC = open(os.path.join(HERE, "..", "api", "live_service.py")).read()
check("the live snapshot carries team_contact from the same function, with the box and plays",
      '"team_contact":  _team_contact_live(' in SRC
      and "team_contact.team_contact(pas, final=status == \"final\", box=box, plays=plays," in SRC)
MAIN = open(os.path.join(HERE, "..", "api", "main.py")).read()
check("/games/{bdl_id}/team-contact is served from team_contact.for_game",
      '@app.get("/games/{bdl_id}/team-contact")' in MAIN and "team_contact.for_game(bdl_id" in MAIN)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
