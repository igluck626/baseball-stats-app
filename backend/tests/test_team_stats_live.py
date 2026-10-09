#!/usr/bin/env python3
"""Team Stats, live: the feed cut back to the box, rows held through a short
mismatch, and the unrecognised-event warning logged once.

Synthetic live states from a real final (LAD @ ATL 2026-10-06, in the fixture): the
box is built from the first N plate appearances, and the feed runs ahead of it,
behind it, or with a hole — the shapes seen on 2026-10-08 (CLE @ CHW), when the
visible rows changed 41 times in a game.
Run: python3 backend/tests/test_team_stats_live.py
"""
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "api"))
import team_contact as tc   # noqa: E402
import team_stats as ts     # noqa: E402

FIX = json.load(open(os.path.join(HERE, "fixtures", "team_contact_2026_postseason.json")))["games"]
results = []


def check(name, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


G = FIX["15333798"]
PAS = sorted(G["plate_appearances"], key=lambda p: p["pa_number"])
PLAYS = G["plays"]


def box_upto(rows):
    """A live box as of these plate appearances: one batting line per side."""
    box = {}
    for side, half in (("away", "top"), ("home", "bottom")):
        mine = [p for p in rows if p["half_inning"].lower() == half and ts._is_plate_appearance(p.get("result"))]
        res = [p["result"] for p in mine]
        box[side] = [{"plate_appearances": len(mine), "at_bats": sum(tc.is_at_bat(r) for r in res),
                      "hits": sum(r in tc.HITS for r in res), "runs": 0, "hr": res.count("Home Run"),
                      "doubles": res.count("Double"), "triples": res.count("Triple"),
                      "bb": res.count("Walk"), "k": sum(r.startswith("Strikeout") for r in res),
                      "stolen_bases": 0}]
    return box


def live(feed, box, gid, now=None):
    b = tc.team_contact(feed, final=False, box=box, plays=PLAYS, game_id=gid)
    return ts.hold_transient(gid, b["stats"], final=False, now=now)


def vals(st, row):
    return tuple((st["away"].get(f), st["home"].get(f)) for f in ts.HOLDABLE.get(row, (row,)))


K = 40                      # plate appearances the box has counted
BASE = PAS[:K]
BOX = box_upto(BASE)
ASOF = tc.team_contact(BASE, final=False, box=BOX, plays=PLAYS)["stats"]

print("the feed is in order and numbered without holes (what the cut relies on)")
nums = [p["pa_number"] for p in G["plate_appearances"]]
check("API order is pa_number order", nums == sorted(nums))
check("pa_number runs 1..n with no holes", sorted(nums) == list(range(1, len(nums) + 1)))
halves = [(p["inning"], p["half_inning"].lower() != "top") for p in PAS]
check("pa_number order is inning order (never goes back)", halves == sorted(halves))

print("feed AHEAD of the box: cut back to the box, rows shown as of the box")
for ahead in (1, 3):
    st = live(PAS[:K + ahead], BOX, f"ahead{ahead}")
    extra = {s: n for s, n in st.get("feed_ahead", {}).items()}
    check(f"ahead by {ahead}: xBA, RISP and DP shown, nothing hidden",
          {"xba", "risp", "dp"} <= set(st["rows"]) and st["hidden"] == {})
    check(f"ahead by {ahead}: values are the box's (xBA, RISP, DP, AVG as with the first {K})",
          all(vals(st, r) == vals(ASOF, r) for r in ("xba", "risp", "dp", "avg")))
    check(f"ahead by {ahead}: the set-aside count is reported ({extra})", sum(extra.values()) >= 1)
    check(f"ahead by {ahead}: nothing held (a cut, not a hold)", "held" not in st)
old = tc.team_contact(PAS[:K + 1], final=True, box=BOX, plays=PLAYS)["stats"]
check("(a final is never cut: the same feed against the same box still mismatches)",
      old["hidden"].get("xba") == "pa_feed_mismatch")

print("a hole in pa_number: no cut")
holed = PAS[:10] + PAS[11:K + 2]          # one dropped mid-game, one beyond the box
st = live(holed, BOX, "hole")
check("not cut (feed_ahead absent), PA-feed rows hide as before",
      "feed_ahead" not in st and st["hidden"].get("xba") == "pa_feed_mismatch")

print("feed BEHIND the box: last good value held for up to 3 minutes")
gid = "behind"
good = live(BASE, BOX, gid, now=0)
check("t=0: in step, all rows shown", {"xba", "risp", "dp"} <= set(good["rows"]))
box_ahead = box_upto(PAS[:K + 1])          # the box has counted one the feed hasn't
st = live(BASE, box_ahead, gid, now=10)
check("t=10s: mismatch, but xBA/RISP/DP held at the t=0 values",
      {"xba", "risp", "dp"} <= set(st["rows"]) and all(vals(st, r) == vals(good, r) for r in ("xba", "risp", "dp"))
      and st["held"]["xba"]["reason"] == "pa_feed_mismatch")
st = live(BASE, box_ahead, gid, now=10 + 120)
check("2 minutes behind: still held (held 120s)", "xba" in st["rows"] and st["held"]["xba"]["seconds"] == 120)
st = live(BASE, box_ahead, gid, now=10 + 240)
check("4 minutes behind: hidden, as today", "xba" not in st["rows"] and st["hidden"].get("xba") == "pa_feed_mismatch"
      and "held" not in st)
st = live(PAS[:K + 1], box_ahead, gid, now=300)
check("back in step: shown, fresh values, the hold timer cleared",
      "xba" in st["rows"] and "held" not in st and "xba" not in ts._HELD[gid]["since"])

print("XBH: a batting line missing doubles/triples is held, then hidden")
gid = "xbh"
good = live(BASE, BOX, gid, now=0)
bad = {s: [dict(r) for r in rows] for s, rows in BOX.items()}
bad["away"][0]["doubles"] = None
st = live(BASE, bad, gid, now=60)
check("1 minute: XBH held at its last value (reason box_incomplete)",
      "xbh" in st["rows"] and vals(st, "xbh") == vals(good, "xbh") and st["held"]["xbh"]["reason"] == "box_incomplete")
st = live(BASE, bad, gid, now=60 + 181)
check("past 3 minutes: XBH hidden", "xbh" not in st["rows"] and st["hidden"].get("xbh") == "box_incomplete")

print("never held: a final, a first refresh with no good value, no plate appearances yet")
gid = "final"
live(BASE, BOX, gid, now=0)
fin = tc.team_contact(BASE, final=True, box=box_ahead, plays=PLAYS, game_id=gid)["stats"]
fin = ts.hold_transient(gid, fin, final=True, now=10)
check("a final: the mismatch hides at once, nothing held", "xba" not in fin["rows"] and "held" not in fin)
check("and the game's held state is cleared", gid not in ts._HELD)
st = live(BASE, box_ahead, "fresh", now=0)
check("a game's first refresh with no good value yet: hidden as today", "xba" not in st["rows"] and "held" not in st)
gid = "empty"
live(BASE, BOX, gid, now=0)
st = live([], {"away": [], "home": []}, gid, now=5)
check("before the first plate appearance: no rows, nothing held", st["rows"] == [] and "held" not in st)

print("the unrecognised-event warning: once per game per distinct text")


class _Capture(logging.Handler):
    def __init__(self): super().__init__(); self.records = []
    def emit(self, record): self.records.append(record.getMessage())


cap = _Capture(); logging.getLogger("team_stats").addHandler(cap)
odd1 = {"order": 10**12, "inning": 9, "inning_type": "Top", "batter_id": None, "type": "Error",
        "text": "Tucker safe at second on throwing error by catcher Baldwin."}
odd2 = dict(odd1, order=10**12 + 1, text="Ohtani safe at third on fielding error by left fielder Ozuna.")
full = sorted(G["plate_appearances"], key=lambda p: p["pa_number"])
for _ in range(3):
    tc.team_contact(full, final=False, box=G["box"], plays=PLAYS + [odd1], game_id="warn")
check("the same event over three refreshes: one warning", len(cap.records) == 1)
tc.team_contact(full, final=False, box=G["box"], plays=PLAYS + [odd1, odd2], game_id="warn")
check("a second, different event: one more warning, naming only the new text",
      len(cap.records) == 2 and "Ohtani" in cap.records[1] and "Tucker" not in cap.records[1])
tc.team_contact(full, final=False, box=G["box"], plays=PLAYS + [odd1], game_id="warn2")
check("the same text in another game: logged for that game", len(cap.records) == 3 and "warn2" in cap.records[2])

print("wired in")
SRC = open(os.path.join(HERE, "..", "api", "live_service.py")).read()
check("the live snapshot holds through team_stats.hold_transient, never at the final",
      'team_stats.hold_transient(game_id, block["stats"], final=status == "final")' in SRC)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
