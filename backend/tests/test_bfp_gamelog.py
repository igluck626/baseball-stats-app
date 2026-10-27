#!/usr/bin/env python3
"""Batters faced is stored on each pitching game log and summed into the season
row and each club's line (Isaac, 2026-10-27).

⚠️ THE CHECKS THAT MATTER:
  • A 2026 season row's BF was a spring snapshot nothing refreshed: balldontlie's
    season stats have no batters faced, and the game logs had no BF column.
    Skubal's stored 166 is his total through a game in the spring; his 26 game
    lines add up to 620.
  • The parser keeps `batters_faced`; a line without it stays unknown, never 0.
  • The column reaches production through the startup column list
    (`_PITCHING_GAMELOGS_NEW_COLUMNS`), not only the model: `create_all` never
    alters a table that already exists.
  • The nightly rollup overwrites the current season's BF with the game-log sum,
    and one unknown game makes it unknown: Milner has one line without BF, so
    his season BF is "—", not a short sum.
  • A traded pitcher's club rows carry BF, and /season-teams sends it once it
    adds up to the season row (Skubal DET + LAN = 620).

Real rows: fixtures/stint_pitching_counts.json (production, read-only) plus
fixtures/bfp_two_pitchers.json (those rows' batters_faced from balldontlie, the
2026-10-27 dry run). Needs an embedded Postgres (`pip install pgserver`); FAILS
without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_bfp_gamelog.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "scripts")]
os.environ.pop("DATABASE_URL", None)

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "stint_pitching_counts.json")))
BF = {(r["player_id"], r["game_id"]): r["BFP"]
      for r in json.load(open(os.path.join(HERE, "fixtures", "bfp_two_pitchers.json")))["BFP"]}
SKUBAL, MILNER = 669373, 571948

import data_service                                                 # noqa: E402
from database import connection                                     # noqa: E402
from database.models import PitchingGameLog, StagingPitchingGameLog  # noqa: E402

print("the column and the parser")
check("pitching_gamelogs has a BFP column", "BFP" in PitchingGameLog.__table__.columns)
check("the staging twin has it too", "BFP" in StagingPitchingGameLog.__table__.columns)
check("startup adds it to production's existing table",
      ("BFP", "INTEGER") in connection._PITCHING_GAMELOGS_NEW_COLUMNS)
WIRE = json.load(open(os.path.join(HERE, "fixtures", "bfp_two_pitchers.json")))["wire_line"]["line"]
ctx = {"game_id": str(WIRE["game_id"]), "game_date": "2026-09-01", "season": 2026,
       "home_team_display_name": WIRE["team_name"], "away_team_display_name": "Other Team",
       "home_team_abbr": "COL", "away_team_abbr": "OTH"}
line = WIRE
row = data_service._parse_bdl_pitching_gamelog(line, ctx)
check(f"the parser keeps batters_faced (a real line: {WIRE['batters_faced']})",
      row and row.get("BFP") == WIRE["batters_faced"] == 23, row)
row = data_service._parse_bdl_pitching_gamelog({**line, "batters_faced": None}, ctx)
check("a line without batters_faced stays unknown, never 0", row and "BFP" in row and row["BFP"] is None, row)
check("BFP is a counting field the nightly sums", "BFP" in data_service._PITCHING_COUNTING_FIELDS)

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the rollup is Postgres SQL)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="bfp_gamelog_"), cleanup_mode="stop")
    os.environ["DATABASE_URL"] = srv.get_uri()
    from sqlalchemy import create_engine                            # noqa: E402
    from sqlalchemy.orm import Session                               # noqa: E402
    from database.models import (Base, BattingGameLog, PitcherSeason,  # noqa: E402
                                 PitcherSeasonStint, PitcherSeasonTeam)
    from database import crud                                        # noqa: E402
    import nightly_update as nu                                      # noqa: E402
    import season_teams                                              # noqa: E402
    data_service._current_year = lambda: 2026   # the overwrite path, whatever today's year
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[m.__table__ for m in (BattingGameLog, PitchingGameLog, PitcherSeason,
                                                                PitcherSeasonStint, PitcherSeasonTeam)])
    own = [{**r, "BFP": BF[(r["player_id"], r["game_id"])]} for r in FX["own_pitching"]]
    with eng.begin() as c:
        for m, rows in ((PitchingGameLog, own), (BattingGameLog, FX["opponent_rows"]),
                        (PitcherSeason, FX["pitcher_seasons"])):
            cols = {col.name for col in m.__table__.columns}
            for r in rows:
                c.execute(m.__table__.insert().values({k: v for k, v in r.items() if k in cols}))
    stored = {r["player_id"]: r["BFP"] for r in FX["pitcher_seasons"]}
    logs = {pid: [r["BFP"] for r in own if r["player_id"] == pid] for pid in (SKUBAL, MILNER)}

    print("the season rollup")
    check(f"Skubal's stored BF {stored[SKUBAL]} is short of his lines' {sum(logs[SKUBAL])} (the stale snapshot)",
          stored[SKUBAL] == 166 and sum(logs[SKUBAL]) == 620, (stored[SKUBAL], sum(logs[SKUBAL])))
    with Session(eng) as db:
        data_service.recalculate_pitching_counting(db, 2026)
        db.commit()
        now = {pid: db.get(PitcherSeason, (pid, 2026)).BFP for pid in (SKUBAL, MILNER)}
    check("Skubal's season BF is now the sum of his lines, 620", now[SKUBAL] == 620, now[SKUBAL])
    check("Milner: one line without BF, so his season BF is unknown — not a short sum",
          logs[MILNER].count(None) == 1 and now[MILNER] is None, (logs[MILNER].count(None), now[MILNER]))

    print("the club lines")
    with Session(eng) as db:
        pit, _ = nu._derive_stints(db, 2026, True)
        crud.save_pitcher_season_stints(db, pit)
        db.commit()
        sent = season_teams.seasons(db, SKUBAL, "pit")
    sk = sorted((r for r in pit if r["player_id"] == SKUBAL), key=lambda r: r["stint_order"])
    check("Skubal: each club row's BF is the sum of its games, adding to 620",
          [r["team"] for r in sk] == ["DET", "LAN"] and sum(r["BFP"] for r in sk) == 620,
          [(r["team"], r["BFP"]) for r in sk])
    mil = [r for r in pit if r["player_id"] == MILNER]
    check("Milner: the club with the unknown line has BF unknown",
          any(r["BFP"] is None for r in mil), [(r["team"], r["BFP"]) for r in mil])
    s26 = next((s for s in sent if s["year"] == 2026), None)
    clubs = s26["teams"] if s26 else []
    check("/season-teams sends BF on both of Skubal's club rows",
          len(clubs) == 2 and all(t["stats"].get("BFP") is not None for t in clubs),
          [t["stats"].get("BFP") for t in clubs])
    eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
