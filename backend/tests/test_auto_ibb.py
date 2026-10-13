#!/usr/bin/env python3
"""Stand-ins for automatic intentional walks — team_stats.fill_automatic_ibbs, applied
inside team_contact.team_contact so the live snapshot and the finished-game endpoint
both get them.

balldontlie's /plate_appearances writes no row for an automatic intentional walk (no
pitch thrown): pa_number skips one, the side's feed counts one plate appearance fewer
than its box, and the PA-feed rows (xBA, RISP, DP) hid as "pa_feed_mismatch" from that
walk to the end of the game. Real games (fixtures/auto_ibb_2026.json, made by
testdata/make-auto-ibb-fixture.py, balldontlie's field names and nesting kept): 10-07 CLE @ CHW, 10-10 CHW @ CLE, 10-11 LAD @ MIL
(two in one inning), and 5059902 (the old missingPARow game); 10-05 CHW @ CLE has a
pitched intentional walk, which the feed does carry. LAD @ MIL also runs end to end
through team_contact.for_game on its /stats rows exactly as served
(fixtures/bdl_stats_15467418.json).
Run: python3 backend/tests/test_auto_ibb.py
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "api"))
import team_contact as tc  # noqa: E402
import team_stats as ts  # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


G = json.load(open(os.path.join(HERE, "fixtures", "auto_ibb_2026.json")))["games"]
HOLES = {   # game: {hole: (half, inning, walked batter)}
    "15467381": {71: ("top", 8, 3781026)},
    "15467417": {59: ("bottom", 8, 965)},
    "15467418": {62: ("bottom", 8, 168), 64: ("bottom", 8, 775)},
    "5059902":  {71: ("top", 10, 92)},
}
SHORT = {"15467381": "away", "15467417": "home", "15467418": "home", "5059902": "away"}


def box_of(g):
    """The /stats rows by side, split on team.id as team_contact.for_game splits them."""
    ids = {"away": g["game"]["away_team"]["id"], "home": g["game"]["home_team"]["id"]}
    return {side: [r for r in g["stats"] if r["team"]["id"] == tid] for side, tid in ids.items()}


def block(g, pas=None, plays=None, box=None, final=True):
    return tc.team_contact(g["plate_appearances"] if pas is None else pas, final=final,
                           box=box_of(g) if box is None else box,
                           plays=g["plays"] if plays is None else plays, game_id=None)


def r3(x):
    return None if x is None else round(x, 3)


def side_pas(pas, half):
    return [p for p in pas if (p.get("half_inning") or "").lower() == half and p.get("result")]


print("the four games: every row shows, from the walks' stand-ins")
for gid, holes in HOLES.items():
    g = G[gid]
    st = block(g)["stats"]
    label = f'{g["game"]["away_team"]["abbreviation"]} @ {g["game"]["home_team"]["abbreviation"]} ({gid})'
    check(f"{label}: nothing hidden for a feed mismatch (hidden {st['hidden']})",
          "pa_feed_mismatch" not in st["hidden"].values())
    check(f"{label}: xBA, RISP and DP are rows", {"xba", "risp", "dp"} <= set(st["rows"]))
    s = st[SHORT[gid]]
    check(f"{label}: the {SHORT[gid]} feed now numbers the box ({s['feed_pa']}/{s['pa']})", s["feed_pa"] == s["pa"])
    check(f"{label}: stand-ins {st.get('stand_ins')} at exactly the holes",
          st.get("stand_ins") == {SHORT[gid]: len(holes)})


print("the stand-ins are the walks, and change nothing but the count")
g = G["15467418"]
filled, n = ts.fill_automatic_ibbs(g["plate_appearances"], g["plays"], box_of(g), final=True) \
    if hasattr(ts, "fill_automatic_ibbs") else (g["plate_appearances"], {})
new = [p for p in filled if p.get("stand_in")]
got = {p["pa_number"]: ((p["half_inning"] or "").lower(), p["inning"], p["batter_id"]) for p in new}
check(f"LAD @ MIL: 62 Contreras and 64 Vaughn, bottom 8th (got {got})", got == HOLES["15467418"])
check("each is an intentional walk with no pitch, from the play stream's outs (1 and 2)",
      all(p["result"] == "Intent Walk" and p["pitches"] == [] for p in new)
      and [p["outs"] for p in sorted(new, key=lambda p: p["pa_number"])] == [1, 2])
check("the input feed isn't touched (Game Leaders and contact_pas read it)",
      len(g["plate_appearances"]) == 68 and not any(p.get("stand_in") for p in g["plate_appearances"]))
raw = tc._side(side_pas(g["plate_appearances"], "bottom"))
fil = tc._side(side_pas(filled, "bottom"))
check("MIL's contact block (AB, xBA, tracked share) is the same with or without them", raw == fil)

for gid in HOLES:
    g = G[gid]
    before = tc.team_contact(g["plate_appearances"], final=True, box=box_of(g), plays=None)
    after = block(g)
    for side in ("away", "home"):
        b, a = before["stats"][side], after["stats"][side]
        same = all(b[k] == a[k] for k in ("avg", "xba", "xbh", "hr", "lob", "bb", "so", "sb", "dp"))
        check(f"{gid} {side}: AVG, xBA, XBH, HR, LOB, BB, SO, SB, DP unchanged by the stand-ins", same)
    for side, half in (("away", "top"), ("home", "bottom")):
        res, _ = ts._risp(side_pas(g["plate_appearances"], half), g["plays"], half)
        a = after["stats"][side]
        check(f"{gid} {side}: RISP {a['risp_h']} for {a['risp_ab']} = the feed's own count {res}",
              res is not None and (a["risp_h"], a["risp_ab"]) == res)

print("LAD @ MIL, every row (to check against Baseball-Reference by hand)")
st = block(G["15467418"])["stats"]
for side in ("away", "home"):
    s = st[side]
    print(f"    {side}: AVG {r3(s['avg'])} xBA {r3(s['xba'])} XBH {s['xbh']} HR {s['hr']} RISP {s['risp_h']}-for-{s['risp_ab']} "
          f"LOB {s['lob']} BB {s['bb']} SO {s['so']} SB {s['sb']} DP {s['dp']}")

print("a pitched intentional walk is the feed's own row: nothing to fill")
g = G["15467377"]
st = block(g)["stats"]
check(f"10-05 CHW @ CLE: no stand-ins (got {st.get('stand_ins')})", not st.get("stand_ins"))
check("all ten rows, feed and box agree on both sides",
      len(st["rows"]) == 10 and all(st[s]["feed_pa"] == st[s]["pa"] for s in ("away", "home")))
check("identical to the block without the play stream's help, apart from RISP",
      {k: v for k, v in st["away"].items() if not k.startswith("risp")}
      == {k: v for k, v in tc.team_contact(g["plate_appearances"], final=True, box=box_of(g), plays=None)["stats"]["away"].items()
          if not k.startswith("risp")})


def without_at_bat(plays, inning, half, batter):
    """The play stream minus one at-bat: its Start marker through its End marker."""
    out, skipping = [], False
    for p in sorted(plays, key=lambda r: r["order"]):
        if (p.get("inning_type") or "").lower() == half and p["inning"] == inning:
            if p["type"] == "Start Batter/Pitcher" and p["batter_id"] == batter:
                skipping = True
            if skipping:
                if p["type"] == "End Batter/Pitcher" and p["batter_id"] == batter:
                    skipping = False
                continue
        out.append(p)
    return out


print("no proof, no stand-in: the rows hide as before")
g = G["15467418"]
plays = without_at_bat(g["plays"], 8, "bottom", 168)
st = block(g, plays=plays)["stats"]
check("Contreras's walk gone from the play stream: hole 62 unproven, the side short, rows hidden",
      st["hidden"].get("xba") == "pa_feed_mismatch" and not st.get("stand_ins"))

plays = copy.deepcopy(g["plays"])
for p in plays:
    if p["batter_id"] == 168 and p["inning"] == 8 and p["type"] == "Automatic Ball - IBB":
        p["type"], p["text"] = "Ball", "Ball"
st = block(g, plays=plays)["stats"]
check("Contreras's walk made a pitched one: not an automatic walk, rows hidden",
      st["hidden"].get("risp") == "pa_feed_mismatch" and not st.get("stand_ins"))

plays = copy.deepcopy(g["plays"])
for p in plays:
    if p["batter_id"] == 168 and p["inning"] == 8 and p["type"] == "Play Result":
        p["text"] = "Contreras hit by pitch."
st = block(g, plays=plays)["stats"]
check("the at-bat's result something else (hit by pitch): rows hidden",
      st["hidden"].get("dp") == "pa_feed_mismatch" and not st.get("stand_ins"))

box = copy.deepcopy(box_of(g))
next(r for r in box["home"] if r["player"]["id"] == 168)["plate_appearances"] += 1
next(r for r in box["home"] if r["player"]["id"] == 274)["plate_appearances"] += 1
st = block(g, box=box)["stats"]
check(f"filling still leaves MIL short of its box (33 + 2 = 35): rows hidden ({st['home']['feed_pa']}/{st['home']['pa']})",
      st["hidden"].get("xba") == "pa_feed_mismatch")

pas = [p for p in g["plate_appearances"] if p["pa_number"] != 60]
st = block(g, pas=pas)["stats"]
check("a second hole (60, Chourio's single, not a walk) beside the walks: rows hidden",
      st["hidden"].get("xba") == "pa_feed_mismatch")


print("RISP: a batter walked and batting again in the same inning gets the right at-bat")
# Bottom 1st: 1 singles; 2 is walked automatically (no feed row); 3 strikes out with men
# on first and second; 2 bats again with a man on first, a wild pitch puts him on second,
# and 2 singles. That last at-bat is with RISP (the runner reached second DURING it).
def pa(n, half, batter, result, on1=False, on2=False):
    return {"pa_number": n, "half_inning": half, "inning": 1, "batter_id": batter, "result": result,
            "runner_on_first": on1, "runner_on_second": on2, "runner_on_third": False, "pitches": []}


def row(pid, pa_, ab, h=0, bb=0, k=0, outs=0):
    return {"player": {"id": pid}, "plate_appearances": pa_, "at_bats": ab, "hits": h, "bb": bb, "k": k, "runs": 0,
            "doubles": 0, "triples": 0, "hr": 0, "stolen_bases": 0, "pitching_outs": outs}


feed = [pa(1, "top", 900, "Groundout"),
        pa(2, "bottom", 1, "Single"),
        pa(4, "bottom", 3, "Strikeout", on1=True, on2=True),
        pa(5, "bottom", 2, "Single", on1=True)]
box_ = {"away": [row(900, 1, 1), row(800, 0, 0, k=1, outs=1)],
        "home": [row(1, 1, 1, h=1), row(2, 2, 1, h=1, bb=1), row(3, 1, 1, k=1), row(700, 0, 0, outs=1)]}
o = iter(range(1, 100))
plays_ = [
    {"order": next(o), "inning": 1, "inning_type": "Top", "type": "Start Batter/Pitcher", "batter_id": 900, "text": "", "outs": 0},
    {"order": next(o), "inning": 1, "inning_type": "Top", "type": "Play Result", "batter_id": 900, "text": "X grounded out.", "outs": 1},
    {"order": next(o), "inning": 1, "inning_type": "Top", "type": "End Inning", "batter_id": None, "text": "", "outs": 3},
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Start Batter/Pitcher", "batter_id": 1, "text": "", "outs": 0},
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Play Result", "batter_id": 1, "text": "A singled.", "outs": 0},
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Start Batter/Pitcher", "batter_id": 2, "text": "", "outs": 0},
    *[{"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Automatic Ball - IBB", "batter_id": 2,
       "text": f"Pitch {i} : Automatic Ball - IBB {i}", "outs": 0} for i in (1, 2, 3, 4)],
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Play Result", "batter_id": 2,
     "text": "B intentionally walked, A to second.", "outs": 0},
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "End Batter/Pitcher", "batter_id": 2, "text": "", "outs": 0},
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Start Batter/Pitcher", "batter_id": 3, "text": "", "outs": 0},
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Play Result", "batter_id": 3, "text": "C struck out swinging.", "outs": 1},
    # A compressed lineup: in a real game 2 comes up again only after the order bats
    # around. What matters is that 2 has two at-bats in the inning, the walk first.
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Start Batter/Pitcher", "batter_id": 2, "text": "", "outs": 1},
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Wild Pitch", "batter_id": None,
     "text": "D to second on wild pitch by Z.", "outs": 1},
    {"order": next(o), "inning": 1, "inning_type": "Bottom", "type": "Play Result", "batter_id": 2, "text": "B singled.", "outs": 1},
]
st = tc.team_contact(feed, final=True, box=box_, plays=plays_)["stats"]
check(f"home RISP: 2's second at-bat counts (1 for 2: 3's strikeout and 2's single; got {st['home']['risp_h']} for {st['home']['risp_ab']})",
      (st["home"]["risp_h"], st["home"]["risp_ab"]) == (1, 2))
check("and the walk itself is no at-bat", st["home"]["risp_ab"] == 2)
res, _ = ts._risp([p for p in feed if p["half_inning"] == "bottom"], plays_, "bottom")
check(f"without the stand-in, 2's second at-bat reads as his first and misses its wild pitch: 0 for 1 (got {res})",
      res == (0, 1))


print("Game Leaders never sees a stand-in")
import game_leaders_live as gl  # noqa: E402
g = G["15467418"]
filled, _ = ts.fill_automatic_ibbs(g["plate_appearances"], g["plays"], box_of(g), final=True)
check("the live board from the feed with the stand-ins is the board from the feed alone (no pitch, no event)",
      gl.build(filled, {}, away_id=None, home_id=None) == gl.build(g["plate_appearances"], {}, away_id=None, home_id=None))


print("live: the stand-ins fill the hole, so the feed-ahead trim works again")
g = G["15467418"]
extra = {**g["plate_appearances"][-1], "pa_number": 71, "half_inning": "bottom", "inning": 9, "batter_id": 274,
         "result": "Single", "pitches": []}
st = block(g, pas=g["plate_appearances"] + [extra], final=False)["stats"]
check(f"a PA the box hasn't counted yet is set aside (feed_ahead {st.get('feed_ahead')}), rows shown",
      st.get("feed_ahead") == {"home": 1} and "pa_feed_mismatch" not in st["hidden"].values())

print("LAD @ MIL end to end, on its /stats rows exactly as balldontlie serves them")
RAW = json.load(open(os.path.join(HERE, "fixtures", "bdl_stats_15467418.json")))["data"]
check(f"the fixture is untrimmed: {len(RAW)} rows of 59 fields, player.id and team.id nested, no player_id",
      len(RAW) == 29 and all(r["player"]["id"] and r["team"]["id"] and "player_id" not in r and len(r) == 59 for r in RAW))
g = G["15467418"]


def bdl(path, params):
    if path.startswith("games/"):
        return {"data": {**g["game"], "status": "STATUS_FINAL"}}
    return {"data": {"plate_appearances": g["plate_appearances"], "stats": RAW, "plays": g["plays"]}[path]}


tc._FINAL_CACHE.clear()
st = tc.for_game(15467418, bdl)["stats"]
tc._FINAL_CACHE.clear()
check(f"for_game: stand-ins for MIL's two walks (got {st.get('stand_ins')})", st.get("stand_ins") == {"home": 2})
check(f"for_game: nothing hidden for a feed mismatch, xBA, RISP and DP shown (hidden {st['hidden']})",
      "pa_feed_mismatch" not in st["hidden"].values() and {"xba", "risp", "dp"} <= set(st["rows"]))
check(f"for_game: MIL's feed numbers its box ({st['home']['feed_pa']}/{st['home']['pa']})",
      st["home"]["feed_pa"] == st["home"]["pa"] == 33)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
