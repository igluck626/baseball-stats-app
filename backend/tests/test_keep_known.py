#!/usr/bin/env python3
"""Re-saving a game-log line never replaces a known value with NULL.

⚠️ THE CHECK THAT MATTERS: balldontlie re-shipped Connor Joe's 06-19 line
without its detail (PA 2 -> null). The nightly re-reads recent dates, the
catch-up re-reads doubleheader dates and the backfills re-read any range, so
the save keeps a stored value when the incoming one is NULL — while a real
incoming value (a scoring correction) still replaces it.

Real rows: Connor Joe's stored 06-19 line, and balldontlie's line for it as
shipped 2026-10-17 (fixtures/keep_known_connor_joe.json).
Standalone, no pytest. Run: <backend python> backend/tests/test_keep_known.py
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
from sqlalchemy import create_engine                               # noqa: E402
from sqlalchemy.orm import Session                                  # noqa: E402
from database.models import Base, BattingGameLog                   # noqa: E402
from database import crud                                           # noqa: E402
import data_service as ds                                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "keep_known_connor_joe.json")))
stored = dict(FX["stored"]); stored["game_date"] = datetime.date.fromisoformat(stored["game_date"])
eng = create_engine("sqlite://")
Base.metadata.create_all(eng, tables=[BattingGameLog.__table__])
cols = {c.name for c in BattingGameLog.__table__.columns}
with Session(eng) as db:
    db.add(BattingGameLog(**{k: v for k, v in stored.items() if k in cols})); db.commit()
incoming = ds._parse_bdl_batting_gamelog(FX["bdl_line"], ds._bdl_game_ctx(FX["game"]))
check("balldontlie's line today has no PA (the regression)", incoming["PA"] is None and incoming["AB"] == 0)
with Session(eng) as db:
    crud.save_batting_gamelogs(db, stored["player_id"], [incoming]); db.commit()
    row = db.get(BattingGameLog, (stored["player_id"], stored["game_id"]))
    check("re-saving keeps the stored PA 2 and the other stored detail",
          row.PA == 2 and row.HBP == stored["HBP"] and row.SF == stored["SF"], (row.PA, row.HBP, row.SF))
    corrected = dict(incoming, H=1)
    crud.save_batting_gamelogs(db, stored["player_id"], [corrected]); db.commit()
    row = db.get(BattingGameLog, (stored["player_id"], stored["game_id"]))
    check("a real incoming value still replaces the stored one (H 0 -> 1)", row.H == 1, row.H)
    fresh = dict(incoming, game_id="999999999")
    crud.save_batting_gamelogs(db, stored["player_id"], [fresh]); db.commit()
    row = db.get(BattingGameLog, (stored["player_id"], "999999999"))
    check("a new game is stored as shipped (PA NULL)", row is not None and row.PA is None)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
