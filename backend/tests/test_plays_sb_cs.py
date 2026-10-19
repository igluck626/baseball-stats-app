#!/usr/bin/env python3
"""SB and CS from balldontlie's play-by-play, for lines shipped without detail.

⚠️ THE CHECKS THAT MATTER (Isaac's guardrails, 2026-10-18):
  • only lines with no detail (no PA, doubles or SB) are filled;
  • a value, 0 included, only when the feed is provably complete: its scores
    reach the final score and every half-inning is there;
  • repeats collapse by PLAY IDENTITY, not text: an echo (even one carrying a
    review note) is one steal, and two separate steals by one runner are two;
  • filled only when the runner's last name belongs to exactly ONE player in the
    game's lines; otherwise SB/CS stay NULL; likewise if the plays can't be
    read, or a call naming the runner was overturned.
Real data (fixtures/bdl_plays_sb_cs.json): James Outman and Tyler Tolbert stole
second in game 5057862; Jonny DeLuca was caught stealing in 5057791; Taylor
Walls stole second, then third on a double steal, in one half-inning of 5059245; Jorge Mateo's
steal in 9470135 was overturned; 5058236's plays overshoot its final score;
Myles Straw's steal in 9470134 was upheld on review.
Standalone, no pytest. Run: <backend python> backend/tests/test_plays_sb_cs.py
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
import data_service as ds                                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "bdl_plays_sb_cs.json")))["games"]


def run(gid, stats=None, plays=None, fail_plays=False):
    case = FX[gid]
    stats = stats or case["stats_page"]
    plays = plays or case["plays_page"]

    def fake(path, params=None):
        if path == "plays":
            if fail_plays:
                raise RuntimeError("plays down")
            return plays
        return stats
    orig = ds._bdl_get_json
    ds._bdl_get_json = fake
    try:
        mapping = {int(s["player"]["id"]): 900000 + i for i, s in enumerate(stats["data"])}
        bat, _ = ds.fetch_bdl_game_stats(int(gid), mapping, ds._bdl_game_ctx(case["game"]))
        inv = {v: k for k, v in mapping.items()}
        names = {int(s["player"]["id"]): s["player"]["last_name"] for s in stats["data"]}
        return {names[inv[pid]]: rows[0] for pid, rows in bat.items()}
    finally:
        ds._bdl_get_json = orig


plays = FX["5057862"]["plays_page"]["data"]
texts = [p["text"] for p in plays if "stole" in (p.get("text") or "")]
check("the feed repeats plays: 'Outman stole second.' appears more than once", texts.count("Outman stole second.") > 1)
rows = run("5057862")
check("Outman (no detail): SB 1 — counted once despite the repeats", rows["Outman"]["SB"] == 1, rows["Outman"]["SB"])
check("Tolbert (no detail): SB 1, CS 0", rows["Tolbert"]["SB"] == 1 and rows["Tolbert"]["CS"] == 0)
detailed = [n for n, r in rows.items() if r["PA"] is not None]
check("lines WITH detail keep balldontlie's own SB/CS (never touched)",
      detailed and all(rows[n]["SB"] == next(s for s in FX["5057862"]["stats_page"]["data"]
                                              if s["player"]["last_name"] == n)["stolen_bases"] for n in detailed))
rows = run("5057791")
check("DeLuca (no detail): CS 1", rows["DeLuca"]["CS"] == 1, rows["DeLuca"]["CS"])
twin = copy.deepcopy(FX["5057862"]["stats_page"])
extra = copy.deepcopy(next(s for s in twin["data"] if s["player"]["last_name"] == "Outman"))
extra["player"]["id"] = 1; extra["player"]["first_name"] = "Other"   # a second "Outman" in the game
twin["data"].append(extra)
rows = run("5057862", stats=twin)
check("a last name two players share: SB/CS stay NULL", rows["Outman"]["SB"] is None and rows["Outman"]["CS"] is None,
      (rows["Outman"]["SB"], rows["Outman"]["CS"]))
rows = run("5057862", fail_plays=True)
check("plays unavailable: SB/CS stay NULL", rows["Outman"]["SB"] is None)

print("play identity")
walls = [p["text"] for p in FX["5059245"]["plays_page"]["data"] if "Walls stole" in (p.get("text") or "")]
check("Walls's two steals are two different plays — second, then third on a double steal — each echoed",
      walls.count("Walls stole second.") == 2 and walls.count("Mullins stole second, Walls stole third.") == 2, walls)
rows = run("5059245")
check("Walls (no detail): SB 2 — two separate steals in one half-inning both count, the echoes don't",
      rows["Walls"]["SB"] == 2 and rows["Walls"]["CS"] == 0, (rows["Walls"]["SB"], rows["Walls"]["CS"]))
straw = {p["text"] for p in FX["9470134"]["plays_page"]["data"] if (p.get("text") or "").startswith("Straw stole")}
check("Straw's steal echoes with a different text (the review note)", len(straw) == 2, straw)
rows = run("9470134")
check("Straw (no detail): SB 1 — an echo with different text is still one play (by text it read 2)",
      rows["Straw"]["SB"] == 1, rows["Straw"]["SB"])
ev = ds._steal_events([{"inning": 1, "inning_type": "Top", "outs": 0, "text": "Abrams picked off and caught stealing second."},
                       {"inning": 1, "inning_type": "Top", "outs": 3,
                        "text": "Young struck out swinging, Abrams caught stealing second, catcher to second."}])
check("an event and its echo with different outs (Abrams 2026-04-14, texts as shipped): one CS",
      ev.get("abrams") == {(1, "Top", "CS", 2)}, ev.get("abrams"))

print("unreadable plays")
rows = run("9470135")
check("Mateo: a call naming him was overturned — SB/CS stay NULL",
      rows["Mateo"]["SB"] is None and rows["Mateo"]["CS"] is None, (rows["Mateo"]["SB"], rows["Mateo"]["CS"]))
g = FX["5058236"]
check("5058236's plays reach 14 home runs against a final of 13",
      max(p.get("home_score") or 0 for p in g["plays_page"]["data"]) == 14 and g["game"]["home_team_data"]["runs"] == 13)
rows = run("5058236")
nd = [n for n, r in rows.items() if r["PA"] is None]
check("feed not provably complete: every no-detail line keeps SB/CS NULL (no 0 filled)",
      nd and all(rows[n]["SB"] is None and rows[n]["CS"] is None for n in nd), {n: rows[n]["SB"] for n in nd})
cut = copy.deepcopy(FX["5059245"]["plays_page"])
cut["data"] = [p for p in cut["data"] if not (p.get("inning") == 9 and p.get("inning_type") == "Top")]
rows = run("5059245", plays=cut)
check("a half-inning missing from the feed: SB/CS stay NULL", rows["Walls"]["SB"] is None, rows["Walls"]["SB"])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
