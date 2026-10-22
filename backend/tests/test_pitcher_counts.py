#!/usr/bin/env python3
"""Pitcher BK / CG / SHO / GF on 2026 game lines, set at ingest, and summed
into the season row under the activity rule.

⚠️ THE CHECKS THAT MATTER:
  • CG is judged on balldontlie's WHOLE box (every line of the team, mapped or
    not), never on our rows: Davis Martin (CHW, 5057968) looks complete in our
    rows because the reliever's row is missing, but the box has two lines.
  • GF is the pitcher whose FIRST appearance for his side comes last in the
    plays, ordered by (inning, order) — not the last play's pitcher. balldontlie
    posts stray plays: an inning-7 pitch numbered after the final out
    (5057860, Erceg finished), a replaced pitcher's pitches listed after his
    reliever's (5059605, Montgomery finished). A position player who finishes
    (5059595, Sanoja) gets the GF. A complete game is not a game finished.
  • Plays that can't be read leave GF unknown (None), never 0. A game whose
    stored rows already have GF isn't re-read for it.
  • A season field is NULL ("—") when any of the pitcher's lines has it NULL.

Real games (fixtures/bdl_pitcher_counts.json, balldontlie wire shape; plays
from the 6th inning on). Standalone, no pytest. Needs pgserver for the last part.
Run: <backend python> backend/tests/test_pitcher_counts.py
"""
import contextlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
import data_service as ds                                          # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "bdl_pitcher_counts.json")))["games"]
plays_reads = []


def run(gid, mapping=None, *, plays_fail=False, gf_stored=False):
    g = FX[str(gid)]
    ids = {s["player"]["id"] for s in g["stats"]}
    mapping = mapping if mapping is not None else {b: 100000 + b for b in ids}

    def fake_get(path, params):
        if path == "stats":
            return {"data": g["stats"], "meta": {}}
        if path == "plays":
            plays_reads.append(gid)
            if plays_fail:
                raise RuntimeError("balldontlie down")
            return {"data": g["plays"], "meta": {}}
        raise AssertionError(path)
    saved = ds._bdl_get_json
    ds._bdl_get_json = fake_get
    try:
        _bat, pit = ds.fetch_bdl_game_stats(int(gid), mapping, ds._bdl_game_ctx(g["game"]), gf_stored=gf_stored)
    finally:
        ds._bdl_get_json = saved
    name = {100000 + s["player"]["id"]: s["player"]["full_name"] for s in g["stats"]}
    return {name.get(pid, pid): r for pid, rows in pit.items() for r in rows}


print("balks")
rows = run(5057860)
check("BK is read from balldontlie's `balks` on every pitching line",
      all(r["BK"] is not None for r in rows.values()), {n: r["BK"] for n, r in rows.items()})

print("games finished")
check("5057860: Erceg finished (a stray inning-7 pitch is numbered after the final out)",
      rows["Lucas Erceg"]["GF"] == 1 and sum(r["GF"] for r in rows.values() if r["home_away"] == "H") == 1,
      {n: r["GF"] for n, r in rows.items()})
rows = run(5059605)
check("5059605: Montgomery finished (Yates's pitches are listed after Montgomery relieved him)",
      rows["Mason Montgomery"]["GF"] == 1 and rows["Kirby Yates"]["GF"] == 0)
check("  ...the save goes to the finisher", rows["Mason Montgomery"]["SV"] == 1)
rows = run(5059595)
check("5059595: a position player who finishes gets the GF (Javier Sanoja)", rows["Javier Sanoja"]["GF"] == 1)

print("complete games")
check("5059595: Braxton Ashcraft, the only PIT pitching line, a start — CG 1, SHO 0 (1 run)",
      (rows["Braxton Ashcraft"]["CG"], rows["Braxton Ashcraft"]["SHO"], rows["Braxton Ashcraft"]["R"]) == (1, 0, 1))
check("  ...and a complete game is not a game finished", rows["Braxton Ashcraft"]["GF"] == 0)
rows = run(5059785)
check("5059785: Chris Sale — CG 1, SHO 1", (rows["Chris Sale"]["CG"], rows["Chris Sale"]["SHO"]) == (1, 1))
check("  ...no reliever on either side gets a CG", sum(r["CG"] for r in rows.values()) == 1)
g = FX["5057968"]
chw = [s for s in g["stats"] if s["team"]["abbreviation"] == "CHW" and s.get("ip") is not None]
martin = next(s["player"]["id"] for s in chw if s.get("games_started") == 1)
only_martin = {s["player"]["id"]: 100000 + s["player"]["id"] for s in g["stats"]
               if not (s["team"]["abbreviation"] == "CHW" and s.get("ip") is not None) or s["player"]["id"] == martin}
rows = run(5057968, only_martin)
check("5057968: with the reliever unmapped, Davis Martin is our only CHW row — and still not a CG (the box has 2 lines)",
      len(chw) == 2 and rows["Davis Martin"]["CG"] == 0 and rows["Davis Martin"]["GS"] == 1, len(chw))

print("unknown, never 0")
rows = run(5057860, plays_fail=True)
check("plays that can't be read leave GF None on every row (CG / SHO still set from the box)",
      all(r["GF"] is None and r["CG"] in (0, 1) for r in rows.values()))
g = FX["5059785"]
detailed = {s["player"]["id"]: 100000 + s["player"]["id"] for s in g["stats"] if not ds._no_detail(s)}
plays_reads.clear()
rows = run(5059785, detailed, gf_stored=True)
check("a game whose stored rows have GF isn't re-read for it when nothing else needs the plays: "
      "no plays request, GF None (keep-known holds the stored value)",
      plays_reads == [] and all(r["GF"] is None for r in rows.values()), plays_reads)
plays_reads.clear()
rows = run(5059785, gf_stored=True)
check("  ...but when the SB/CS step reads the plays anyway, GF is set from them",
      plays_reads == [5059785] and rows["Chris Sale"]["GF"] == 0 and all(r["GF"] in (0, 1) for r in rows.values()), plays_reads)

print("season sums")
check("WP, BK, CG, SHO and GF are summed into the season row",
      {"WP", "BK", "CG", "SHO", "GF"} <= set(ds._PITCHING_COUNTING_FIELDS))
try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the season sum is SQL)", False, "pip install pgserver")
else:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from database.models import Base, PitchingGameLog, PitcherSeason
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="pitcher_counts_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[PitchingGameLog.__table__, PitcherSeason.__table__])
    rows = run(5059595)
    with eng.begin() as c:
        for pid, (n, r) in enumerate(sorted(rows.items())):
            for k, gid in enumerate(("g1", "g2")):
                v = {**{x: r.get(x) for x in ("IP", "H", "R", "ER", "BB", "SO", "HR", "HBP", "WP", "BK", "CG", "SHO", "GF", "GS")},
                     "player_id": 1 + pid, "game_id": gid, "season": 2026, "game_date": r["game_date"]}
                if n == "Javier Sanoja" and k == 1:
                    v["BK"] = None                   # one unknown line
                c.execute(PitchingGameLog.__table__.insert().values(v))
            c.execute(PitcherSeason.__table__.insert().values(player_id=1 + pid, year=2026))
    pids = {n: 1 + i for i, n in enumerate(sorted(rows))}
    s = sessionmaker(bind=eng)()
    saved = ds._current_year
    ds._current_year = lambda: 2026
    try:
        ds.recalculate_pitching_counting(s, 2026)
        s.commit()
        get = lambda n, f: s.execute(text(f'SELECT "{f}" FROM pitcher_seasons WHERE player_id = :p'), {"p": pids[n]}).scalar()   # noqa: E731
        check("Ashcraft over two such games: CG 2, SHO 0, GF 0", (get("Braxton Ashcraft", "CG"), get("Braxton Ashcraft", "SHO"), get("Braxton Ashcraft", "GF")) == (2, 0, 0))
        check("Sanoja: GF 2; BK '—' (one line unknown), never a short sum", get("Javier Sanoja", "GF") == 2 and get("Javier Sanoja", "BK") is None)
    finally:
        ds._current_year = saved
        s.close()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
