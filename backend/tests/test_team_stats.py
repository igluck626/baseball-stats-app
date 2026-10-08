#!/usr/bin/env python3
"""Team Stats — team_stats.py, shipped inside the team-contact block as `stats`.

Four 2026 postseason finals: RISP and team LOB reproduce Baseball-Reference's box
scores exactly (fetched by hand, one page per game, 2026-10-08), and every other row
the totals measured from balldontlie's box (XBH = 2B + 3B + HR from its batting
lines). The fourth (MIL @ SD 2026-10-06) has a half-inning that ended on a caught
stealing with a batter at the plate. Then the start-or-during RISP rule, the event
classifier, the fail-closed path (an unrecognised in-at-bat event hides RISP, with
the reason and a log line), the PA-count gate on the PA-feed rows, and live LOB.
Run: python3 backend/tests/test_team_stats.py
"""
import json
import logging
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


FIX = json.load(open(os.path.join(HERE, "fixtures", "team_contact_2026_postseason.json")))["games"]


def stats_for(gid, plays="fixture", **kw):
    g = FIX[str(gid)]
    p = g["plays"] if plays == "fixture" else plays
    return tc.team_contact(g["plate_appearances"], final=True, box=g["box"], plays=p, game_id=gid, **kw)["stats"]


def r3(x):
    return None if x is None else round(x, 3)


print("RISP and LOB match Baseball-Reference")
BREF = {   # game: ((away, RISP h, ab, LOB), (home, ...))
    15333798: (("LAD", 0, 4, 9), ("ATL", 0, 6, 6)),
    15467376: (("NYY", 0, 1, 4), ("TB", 1, 13, 9)),
    15467377: (("CHW", 3, 8, 5), ("CLE", 0, 5, 5)),
    15333797: (("MIL", 2, 7, 11), ("SD", 1, 3, 4)),
}
for gid, sides in BREF.items():
    st = stats_for(gid)
    for (abbr, h, ab, lob), side in zip(sides, ("away", "home")):
        s = st[side]
        check(f"{abbr}: {h} for {ab} with RISP, LOB {lob} (got {s['risp_h']} for {s['risp_ab']}, LOB {s['lob']})",
              (s["risp_h"], s["risp_ab"], s["lob"]) == (h, ab, lob))

print("the other rows match the box")
ROWS = {   # avg, xba, xbh (2B + 3B + HR), hr, bb, so, sb, dp turned
    15333798: ((.222, .259, 2, 2, 2, 12, 1, 0), (.156, .146, 1, 0, 2, 12, 0, 0)),
    15467376: ((.129, .195, 3, 2, 2, 7, 0, 0), (.229, .313, 2, 0, 2, 4, 3, 0)),
    15467377: ((.188, .204, 1, 0, 4, 13, 0, 0), (.125, .194, 1, 0, 3, 9, 0, 1)),
    15333797: ((.314, .234, 1, 1, 4, 9, 0, 0), (.207, .241, 1, 1, 2, 7, 2, 1)),
}
for gid, sides in ROWS.items():
    st = stats_for(gid)
    for want, side in zip(sides, ("away", "home")):
        s = st[side]
        got = (r3(s["avg"]), r3(s["xba"]), s["xbh"], s["hr"], s["bb"], s["so"], s["sb"], s["dp"])
        check(f"{FIX[str(gid)][side]} {gid}: {want} (got {got})", got == want)
    check(f"game {gid}: all ten rows shown, in order",
          st["rows"] == ["avg", "xba", "xbh", "hr", "risp", "lob", "bb", "so", "sb", "dp"]
          and st["hidden"] == {})

print("XBH is doubles + triples + home runs from the box, and fails closed")
g = FIX["15333797"]
for side in ("away", "home"):
    want = sum((r.get("doubles") or 0) + (r.get("triples") or 0) + (r.get("hr") or 0) for r in g["box"][side])
    check(f"MIL @ SD 10-06 {g[side]}: XBH {want} (2B + 3B + HR summed from the box's batting lines)",
          stats_for(15333797)[side]["xbh"] == want == 1)
st = stats_for(15467377)
check("a triple counts (CLE 10-05: 0 2B, 1 3B, 0 HR -> 1)", st["home"]["xbh"] == 1)
quiet = {"away": [dict(r, doubles=0, triples=0, hr=0) for r in FIX["15467377"]["box"]["away"]],
         "home": FIX["15467377"]["box"]["home"]}
st = tc.team_contact(FIX["15467377"]["plate_appearances"], final=True, box=quiet, plays=FIX["15467377"]["plays"])["stats"]
check("a side with no extra-base hits reads 0, and the row shows", st["away"]["xbh"] == 0 and "xbh" in st["rows"])
first = FIX["15467377"]["plate_appearances"][:1]
live_box = {"away": [{"plate_appearances": 1, "at_bats": 1, "hits": 1, "runs": 0, "doubles": 1, "triples": 0, "hr": 0}],
            "home": []}
st = tc.team_contact(first, final=False, box=live_box, plays=FIX["15467377"]["plays"])["stats"]
check("live after the first plate appearance (a double): XBH 1 against 0, row shown",
      "xbh" in st["rows"] and (st["away"]["xbh"], st["home"]["xbh"]) == (1, 0))
gap = {"away": [dict(r) for r in FIX["15467377"]["box"]["away"]], "home": FIX["15467377"]["box"]["home"]}
batter = next(r for r in gap["away"] if r.get("at_bats"))
batter.pop("doubles")
st = tc.team_contact(FIX["15467377"]["plate_appearances"], final=True, box=gap, plays=FIX["15467377"]["plays"])["stats"]
check("a batting line with an at-bat but no doubles: XBH hidden, reason 'box_incomplete', others stay",
      "xbh" not in st["rows"] and st["hidden"].get("xbh") == "box_incomplete"
      and st["away"]["xbh"] is None and st["home"]["xbh"] is None and "hr" in st["rows"])

print("an inning that ends on the bases isn't a plate appearance")
g = FIX["15333797"]
sd = [p for p in g["plate_appearances"] if p["half_inning"].lower() == "bottom"]
taylor = [p for p in sd if p["batter_id"] == 610]
check("the feed has a row for Taylor's 6th, result 'Caught Stealing 2B' (the inning's third out)",
      any(p["result"] == "Caught Stealing 2B" for p in taylor))
box_sd = sum(r.get("plate_appearances") or 0 for r in g["box"]["home"])
check(f"SD: 33 feed rows with a result, 32 true plate appearances, the box's {box_sd}",
      sum(1 for p in sd if p["result"]) == 33 and sum(1 for p in sd if ts._is_plate_appearance(p["result"])) == 32 == box_sd)
st = stats_for(15333797)
check("so the feed rows aren't hidden for MIL @ SD (they were, in production, on 2026-10-08)",
      st["hidden"] == {} and st["rows"] == ["avg", "xba", "xbh", "hr", "risp", "lob", "bb", "so", "sb", "dp"])
check("a dropped row is still a mismatch", tc.team_contact(g["plate_appearances"][1:], final=True, box=g["box"],
      plays=g["plays"])["stats"]["hidden"].get("risp") == "pa_feed_mismatch")

print("RISP counts a runner who reaches scoring position during the at-bat")
# Without the play stream's in-at-bat moves the three sides come out short, as measured.
moved_off = {}
for gid, half, side in ((15333798, "top", "away"), (15467376, "bottom", "home"), (15467377, "top", "away")):
    g = FIX[str(gid)]
    pas = [p for p in g["plate_appearances"] if p["half_inning"].lower() == half and p["result"]]
    no_events = [p for p in g["plays"] if p["batter_id"] is not None or p["type"] in ts.MARKERS
                 or "Inning" in (p["type"] or "")]
    res, _ = ts._risp(pas, no_events, half)
    moved_off[side + str(gid)] = res
check("starting runners alone: LAD 0-3, TB 1-11, CHW 3-7 (Baseball-Reference: 0-4, 1-13, 3-8)",
      list(moved_off.values()) == [(0, 3), (1, 11), (3, 7)])

print("the event classifier")
for text, want in [
    ("Tucker stole second.", True), ("Ramírez stole third.", True), ("A stole second, B stole third.", True),
    ("Grichuk to second on wild pitch by Sabrowski, Vargas to third on wild pitch by Sabrowski.", True),
    ("Meidroth to second on pickoff error by pitcher Gaddis.", True),
    ("Dubón to second on fielder's indifference.", True),
    ("X scored on a balk, Y to second on a balk.", True), ("X to second on passed ball by Raleigh.", True),
    ("X scored on Glasnow wild pitch.", False), ("X picked off second.", False),
    ("X caught stealing third, pitcher to third.", False), ("X caught stealing second, catcher to shortstop.", False),
    ("X scored on pickoff error by catcher Salas, Y picked off second.", False),
    ("X safe at second on throwing error by catcher.", None), ("X advanced on obstruction.", None), ("", None),
    # a replay review after the event (seen on TB, 2026-10-05): upheld keeps the play
    ("Simpson stole second. New York Yankees challenged: call on the field was upheld.", True),
    ("X stole second. Y challenged (tag play): call on the field was confirmed.", True),
    ("X caught stealing second. Y challenged: call on the field was overturned.", None),
    # an abbreviation's period mid-name (TB, 2026-10-05)
    ("Mesa Jr. stole second.", True), ("J.D. Martinez to third on wild pitch by Hall.", True),
    # a dropped third strike: the batter to first puts nobody in scoring position
    ("Edman struck out, Edman to first on wild pitch by Miller.", False),
    ("Edman struck out, Edman to first on wild pitch by Miller, Arenado to second on wild pitch by Miller.", True),
]:
    check(f"{text!r} -> {want}", ts.classify(text) is want)

print("fail closed: an unrecognised in-at-bat event hides RISP")
g = FIX["15333798"]
odd = list(g["plays"])
# a made-up wording, in the middle of LAD's second inning (between two rows of it)
i = next(k for k, p in enumerate(odd) if p["inning"] == 2 and p["inning_type"] == "Top" and p["type"] == "Stolen Base")
odd.insert(i, {"order": odd[i]["order"] - 0.5, "inning": 2, "inning_type": "Top", "batter_id": None,
               "type": "Error", "text": "Tucker safe at second on throwing error by catcher Baldwin."})


class _Capture(logging.Handler):
    def __init__(self): super().__init__(); self.records = []
    def emit(self, record): self.records.append(record.getMessage())


cap = _Capture(); logging.getLogger("team_stats").addHandler(cap)
st = stats_for(15333798, plays=odd)
check("RISP is hidden, reason 'unrecognised_event'", "risp" not in st["rows"] and st["hidden"].get("risp") == "unrecognised_event")
check("its counts are blanked for both sides", st["away"]["risp_ab"] is None and st["home"]["risp_ab"] is None)
check("the event text is recorded in the block", st["unrecognised_events"] == ["Tucker safe at second on throwing error by catcher Baldwin."])
check("and logged server-side", any("unrecognised in-at-bat event" in m and "15333798" in m for m in cap.records))
check("every other row still shows", st["rows"] == ["avg", "xba", "xbh", "hr", "lob", "bb", "so", "sb", "dp"])
note = list(g["plays"]) + [{"order": 10**12, "inning": 9, "inning_type": "Top", "batter_id": None,
                            "type": "Mound Visit", "text": "Mound visit."}]
check("a note that moves nobody (a mound visit) doesn't hide RISP", "risp" in stats_for(15333798, plays=note)["rows"])
st = stats_for(15333798, plays=None)
check("no play stream: RISP hidden, reason 'no_plays'", "risp" not in st["rows"] and st["hidden"].get("risp") == "no_plays")

print("PA-feed rows hide when the feed's count disagrees with the box")
dropped = [p for p in g["plate_appearances"] if not (p["inning"] == 3 and p["half_inning"] == "Top")][:]
dropped = g["plate_appearances"][:5] + g["plate_appearances"][6:]          # one plate appearance missing
st = tc.team_contact(dropped, final=True, box=g["box"], plays=g["plays"])["stats"]
check("xBA, RISP and DP hide, reason 'pa_feed_mismatch'",
      all(st["hidden"].get(k) == "pa_feed_mismatch" for k in ("xba", "risp", "dp"))
      and not set(st["rows"]) & {"xba", "risp", "dp"})
check("the box rows stay (AVG, XBH, HR, LOB, BB, SO, SB)",
      st["rows"] == ["avg", "xbh", "hr", "lob", "bb", "so", "sb"])
inprogress = g["plate_appearances"] + [{"inning": 9, "half_inning": "bottom", "pa_number": 999,
                                        "batter_id": 1, "result": None, "pitches": []}]
check("a plate appearance still in progress (no result) isn't a mismatch",
      "risp" in tc.team_contact(inprogress, final=False, box=g["box"], plays=g["plays"])["stats"]["rows"])

print("xBA keeps its own rule")
g = FIX["15333798"]
st = tc.team_contact(g["plate_appearances"], final=False, box=g["box"], plays=g["plays"])["stats"]
check("final=False with 30+ at-bats a side: xBA still shown", "xba" in st["rows"])
top1 = [p for p in g["plate_appearances"] if p["inning"] == 1 and p["half_inning"].lower() == "top"]
box_top1 = {"away": [{"plate_appearances": len(top1), "at_bats": len(top1), "hits": 0, "runs": 0}], "home": []}
st = tc.team_contact(top1, final=False, box=box_top1, plays=g["plays"])["stats"]
check("live, top of the 1st (the home side yet to bat): xBA shown, the home value None",
      "xba" in st["rows"] and st["away"]["xba"] is not None and st["home"]["xba"] is None)
st = tc.team_contact([], final=False, box={"away": [], "home": []}, plays=g["plays"])["stats"]
check("before the first plate appearance: no rows, xBA 'no_data'",
      st["rows"] == [] and st["hidden"].get("xba") == "no_data")
first = [p for p in g["plate_appearances"] if p["inning"] == 1]
box_first = {side: [{"plate_appearances": sum(1 for p in first if p["half_inning"].lower() == half),
                     "at_bats": 3, "hits": 0, "runs": 0}] for side, half in (("away", "top"), ("home", "bottom"))}
st = tc.team_contact(first, final=False, box=box_first, plays=g["plays"])["stats"]
check("live, after one inning (no wait for nine at-bats): xBA shown", "xba" in st["rows"])

print("live LOB leaves out runners still on base")
base = tc.team_contact(g["plate_appearances"], final=True, box=g["box"], plays=g["plays"])["stats"]
live = tc.team_contact(g["plate_appearances"], final=False, box=g["box"], plays=g["plays"],
                       on_base={"home": 2})["stats"]
check("two runners on for the side batting: its LOB is 2 lower, the other side's unchanged",
      live["home"]["lob"] == base["home"]["lob"] - 2 and live["away"]["lob"] == base["away"]["lob"])

print("before the first plate appearance")
empty_box = {"away": [], "home": []}
st = tc.team_contact([], final=False, box=empty_box, plays=[])["stats"]
check("no rows at all, so no card", st["rows"] == [])
check("without a box there is no stats object (an older caller)", "stats" not in tc.team_contact([], final=True))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
