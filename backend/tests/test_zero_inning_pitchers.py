#!/usr/bin/env python3
"""A pitching line with no evidence in it — 0 IP, no batters faced, every count 0 —
is kept only when the game's plays show the pitcher: a play's pitcher_id is the
line's balldontlie id or an alias of the same player, or the plays name the
pitcher and the last name belongs to exactly ONE player in the game's lines
(Isaac, 2026-10-19, option (c) plus the pitcher_id check).

Real data (fixtures/bdl_zero_inning.json, unmodified balldontlie responses):
  • 5059446 Kerkering — "Kerkering relieved Mayza", "Orion Kerkering pitches to
    Pete Alonso": kept;
  • 5058033 Hurter — no play names Hurter; Detroit's relievers were Holton,
    Finnegan and Jansen: dropped;
  • 5058000 Freeland — named only as the pitcher relieved ("Herget relieved
    Freeland"): kept;
  • 5058583 Detmers — 0 IP but 5 pitches and a walk on the line: kept without
    reading the plays;
  • 5058850 Halvorsen 06-15 — two balldontlie records (1015 and 5868766) on one
    game, the plays' pitcher_id 5868766. Both real lines carry evidence, so the
    alias case is built from them in the test (each change is labelled there).
Standalone, no pytest. Run: <backend python> backend/tests/test_zero_inning_pitchers.py
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


FX = json.load(open(os.path.join(HERE, "fixtures", "bdl_zero_inning.json")))["games"]
KERKERING, HURTER, FREELAND, DETMERS = 340, 102, 982, 527
HALVORSEN, HALVORSEN_ALIAS = 1015, 5868766


def run(gid, stats=None, plays_down=False, mapping=None):
    """fetch_bdl_game_stats on the real responses; every player mapped to their
    own balldontlie id unless `mapping` says otherwise. Returns (pitching rows
    by our id, plays pages requested)."""
    case = FX[gid]
    stats = stats or case["stats_page"]
    pages = case["plays_pages"]
    asked = []

    def fake(path, params=None):
        if path == "plays":
            asked.append((params or {}).get("cursor"))
            if plays_down or not pages:
                raise RuntimeError("plays unavailable")
            cur = (params or {}).get("cursor")
            if cur is None:
                return pages[0]
            return next(pages[i + 1] for i, p in enumerate(pages) if p["meta"].get("next_cursor") == cur)
        return stats
    orig = ds._bdl_get_json
    ds._bdl_get_json = fake
    try:
        mapping = mapping or {int(s["player"]["id"]): int(s["player"]["id"]) for s in stats["data"]}
        _, pit = ds.fetch_bdl_game_stats(int(gid), mapping, ds._bdl_game_ctx(case["game"]))
        return {pid: rows[0] for pid, rows in pit.items()}, asked
    finally:
        ds._bdl_get_json = orig


def line(gid, pid):
    return next(s for s in FX[gid]["stats_page"]["data"] if s["player"]["id"] == pid)


print("the lines themselves carry nothing")
for gid, pid in (("5059446", KERKERING), ("5058033", HURTER), ("5058000", FREELAND)):
    ln = line(gid, pid)
    check(f"{ln['player']['last_name']} {gid}: ip 0, batters_faced null, no pitch, hit, walk, run or K",
          ln["ip"] == 0 and ln["batters_faced"] is None and not any(
              ln.get(f) for f in ("pitch_count", "p_hits", "p_bb", "p_runs", "p_k", "pitching_hbp")),
          {f: ln.get(f) for f in ("ip", "batters_faced", "pitch_count", "p_hits", "p_bb")})
    check(f"  and the line alone does not admit it", ds._parse_bdl_pitching_gamelog(
        ln, ds._bdl_game_ctx(FX[gid]["game"])) is None)

print("named in the plays: kept")
rows, asked = run("5059446")
k = rows.get(KERKERING)
check("Kerkering kept", k is not None)
check("  as shipped: IP 0, H 0, BB 0, SO 0; HBP and WP unknown (null), never 0",
      k and k["IP"] == 0 and k["H"] == 0 and k["BB"] == 0 and k["SO"] == 0
      and k.get("HBP") is None and k.get("WP") is None, k)
check("  the plays read once, every page, in order", asked == [None] + [p["meta"]["next_cursor"]
      for p in FX["5059446"]["plays_pages"][:-1]], asked)
rows, _ = run("5058000")
check("Freeland kept — named as the pitcher relieved", FREELAND in rows)
check("  by the name: no play carries Freeland's id", not any(
    p.get("pitcher_id") == FREELAND for pg in FX["5058000"]["plays_pages"] for p in pg["data"]))

print("the pitcher on the mound: pitcher_id")
plays = [p for pg in FX["5059446"]["plays_pages"] for p in pg["data"]]
check("Kerkering's two plays carry pitcher_id 340 — 'Kerkering relieved Mayza', "
      "'Orion Kerkering pitches to Pete Alonso'",
      sorted(p["text"] for p in plays if p.get("pitcher_id") == KERKERING)
      == ["Kerkering relieved Mayza", "Orion Kerkering pitches to Pete Alonso"])
shared = copy.deepcopy(FX["5059446"]["stats_page"])
other = next(s for s in shared["data"] if s["player"]["id"] != KERKERING and s.get("ip") is None)
other["player"]["last_name"] = "Kerkering"              # IN-TEST: a second Kerkering in the lines
rows, _ = run("5059446", stats=shared)
check("Kerkering kept by id though a second Kerkering leaves the name rule unable to decide",
      KERKERING in rows)
check("Hurter: no play carries id 102", not any(
    p.get("pitcher_id") == HURTER for pg in FX["5058033"]["plays_pages"] for p in pg["data"]))

h = FX["5058850"]["stats_page"]["data"]
check("Halvorsen 06-15: two records on one game, both with evidence (BF 2; 10 pitches) — "
      "no real alias line lacks it",
      sorted((s["player"]["id"], s.get("batters_faced"), s.get("pitch_count")) for s in h
             if s["player"]["last_name"] == "Halvorsen") == [(HALVORSEN, 2, 10), (HALVORSEN_ALIAS, None, 10)])
plays = [p for pg in FX["5058850"]["plays_pages"] for p in pg["data"]]
check("  the plays carry the alias id 5868766, never 1015",
      any(p.get("pitcher_id") == HALVORSEN_ALIAS for p in plays)
      and not any(p.get("pitcher_id") == HALVORSEN for p in plays))
alias = copy.deepcopy(FX["5058850"]["stats_page"])
alias["data"] = [s for s in alias["data"] if s["player"]["id"] != HALVORSEN_ALIAS]   # IN-TEST: 1015's line only
line = next(s for s in alias["data"] if s["player"]["id"] == HALVORSEN)
for f in ("batters_faced", "pitch_count", "strikes", "p_hits", "p_bb", "p_runs", "er", "p_k", "pitching_hbp"):
    line[f] = 0 if f != "batters_faced" else None                                    # IN-TEST: no evidence
other = next(s for s in alias["data"] if s["player"]["id"] != HALVORSEN and s.get("ip") is None)
other["player"]["last_name"] = "Halvorsen"                                           # IN-TEST: name rule can't decide
own = {int(s["player"]["id"]): int(s["player"]["id"]) for s in alias["data"]}
rows, _ = run("5058850", stats=alias, mapping={**own, HALVORSEN_ALIAS: HALVORSEN})
check("alias: 1015's line kept because the plays' pitcher_id 5868766 is the same player", HALVORSEN in rows)
rows, _ = run("5058850", stats=alias, mapping=own)
check("  without the alias mapping, the same line is dropped", HALVORSEN not in rows)

print("named nowhere: dropped")
rows, asked = run("5058033")
check("Hurter dropped", HURTER not in rows)
check("  'hurter' is not among the plays' pitchers; 'Lynch IV relieved Lugo' reads as lynch",
      "hurter" not in ds._pitchers_named_in_plays([p for pg in FX["5058033"]["plays_pages"] for p in pg["data"]])
      and "lynch" in ds._pitchers_named_in_plays([p for pg in FX["5058033"]["plays_pages"] for p in pg["data"]]))
everyone_else = {s["player"]["id"] for s in FX["5058033"]["stats_page"]["data"]
                 if s.get("ip") is not None and s["player"]["id"] != HURTER}
check("  every other pitcher in the game kept", set(rows) == everyone_else, set(rows) ^ everyone_else)
check("  the plays read once though two consumers need them (Vierling and Tolbert ship no detail)",
      asked == [None] + [p["meta"]["next_cursor"] for p in FX["5058033"]["plays_pages"][:-1]], asked)

print("evidence on the line: kept, plays not needed")
rows, asked = run("5058583")
check("Detmers kept on 5 pitches and a walk (plays unavailable in this run)", DETMERS in rows)
check("  BB 1 as shipped", rows.get(DETMERS, {}).get("BB") == 1)

print("fail closed")
rows, _ = run("5059446", plays_down=True)
check("plays unreadable: Kerkering dropped", KERKERING not in rows)
shared = copy.deepcopy(FX["5058000"]["stats_page"])
other = next(s for s in shared["data"] if s["player"]["id"] != FREELAND and s.get("ip") is None)
other["player"]["last_name"] = "Freeland"                # IN-TEST: a second Freeland in the lines
rows, _ = run("5058000", stats=shared)
check("a second Freeland and no play carrying the id: the name can't say which — dropped",
      FREELAND not in rows)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
