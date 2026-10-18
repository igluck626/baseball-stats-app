#!/usr/bin/env python3
"""The current-season counting aggregation (data_service._apply_counting_aggregation,
nightly Phase 4b) and the rates built on it (recalculate_batting_rates, Phase 4c)
under Isaac's "activity" rule (2026-10-16).

⚠️ THE CHECKS THAT MATTER:
  • A game-log cell is NULL when balldontlie didn't ship it. On a row with
    activity — AB or BB above 0, or any pitching row — the player's season field
    becomes NULL ("—"), never a short sum. Isaac Collins (9 such games) used to
    get season PA 484 this way.
  • On a batting row with no AB and no BB (defensive sub, pinch runner) a NULL
    counts as 0: Billy Cook's 28 such games leave his season PA a number.
  • K%, BB% and wOBA are NULL when the PA (or HBP / SF / IBB) they need is.
  • A pitcher's unknown HBP makes his season HBP NULL (Codi Heuer read 0).
  • Careers: an unknown CURRENT-season value makes the career total (and the
    rates on it) unknown on the career leaderboard and the profile; a NULL in a
    PAST season still reads as 0, as it always has.
balldontlie's season stats ship none of PA/HBP/SF/SH/CS/IBB/GIDP (checked
2026-10-16), so the game logs are the only source for these fields.

Real rows (fixtures/season_counting_activity.json), read from production.
Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_season_counting_activity.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
from sqlalchemy import create_engine, text                         # noqa: E402
from sqlalchemy.orm import Session                                  # noqa: E402
from database.models import (Base, BattingGameLog, PitchingGameLog,  # noqa: E402
                             PlayerSeason, PitcherSeason)
import data_service as ds                                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "season_counting_activity.json")))
COLLINS, COOK, HEUER = 686555, 695257, 676051
MODELS = {"batting_gamelogs": BattingGameLog, "pitching_gamelogs": PitchingGameLog,
          "player_seasons": PlayerSeason, "pitcher_seasons": PitcherSeason}

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the aggregation is Postgres SQL)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="season_activity_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[m.__table__ for m in MODELS.values()])
    with eng.begin() as c:
        for t, m in MODELS.items():
            cols = {col.name for col in m.__table__.columns}
            for r in FX[t]:
                c.execute(m.__table__.insert().values({k: v for k, v in r.items() if k in cols}))

    def season(pid, table="player_seasons"):
        with eng.connect() as c:
            return dict(c.execute(text(f"SELECT * FROM {table} WHERE player_id = :p AND year = 2026"),
                                  {"p": pid}).mappings().one())

    print("the old sum (what production holds)")
    with eng.connect() as c:
        old = c.execute(text('SELECT SUM(COALESCE("PA", 0)) FROM batting_gamelogs WHERE player_id = :p'),
                        {"p": COLLINS}).scalar()
    check("SUM(COALESCE(PA, 0)) gives Collins 484 — his 9 unknown games read as 0",
          old == 484 and season(COLLINS)["PA"] == 484, old)

    print("the activity rule")
    with Session(eng) as db:
        ds.recalculate_batting_counting(db, 2026)
        ds.recalculate_pitching_counting(db, 2026)
        ds.recalculate_batting_rates(db, 2026)
        db.commit()
    col = season(COLLINS)
    check("Collins: PA, HBP, SF, CS, IBB, GIDP, SH all NULL (unknown), not short",
          all(col[f] is None for f in ("PA", "HBP", "SF", "CS", "IBB", "GIDP", "SH")),
          {f: col[f] for f in ("PA", "HBP", "SF", "CS", "IBB", "GIDP", "SH")})
    check("Collins: K%, BB% and wOBA NULL with them", col["K_pct"] is None and col["BB_pct"] is None
          and col["wOBA"] is None, (col["K_pct"], col["BB_pct"], col["wOBA"]))
    check("Collins: balldontlie's own fields untouched (AB, H, HR, SB)",
          (col["AB"], col["H"], col["HR"], col["SB"]) == tuple(
              next(r for r in FX["player_seasons"] if r["player_id"] == COLLINS)[k] for k in ("AB", "H", "HR", "SB")))
    cook = season(COOK)
    with eng.connect() as c:
        want = c.execute(text('SELECT SUM(COALESCE("PA", 0)) FROM batting_gamelogs WHERE player_id = :p'),
                         {"p": COOK}).scalar()
    check("Billy Cook: 28 unknown cells, all on no-AB/no-BB games — PA stays a number", cook["PA"] == want and want > 0,
          (cook["PA"], want))
    check("Billy Cook: his rates are computed (K% = SO / PA)",
          cook["K_pct"] is not None and abs(cook["K_pct"] - round(cook["SO"] / cook["PA"], 3)) < 1e-9)
    heu = season(HEUER, "pitcher_seasons")
    check("Codi Heuer: an unknown HBP in a game he pitched makes his season HBP NULL (was 0)",
          heu["HBP"] is None, heu["HBP"])
    with eng.connect() as c:
        r_sum = c.execute(text('SELECT SUM("R") FROM pitching_gamelogs WHERE player_id = :p'), {"p": HEUER}).scalar()
    check("Codi Heuer: R (never unknown) is still the sum", heu["R"] == r_sum, (heu["R"], r_sum))

    print("careers")
    from sqlalchemy import select
    with Session(eng) as db:
        q = select(PlayerSeason.player_id, ds._career_sum_known(PlayerSeason, PlayerSeason.PA),
                   ds._career_sum_known(PlayerSeason, PlayerSeason.HBP)).group_by(PlayerSeason.player_id)
        car = {r[0]: (r[1], r[2]) for r in db.execute(q)}
        rows = {pid: [ds._db_row_to_season(r) for r in db.query(PlayerSeason).filter(PlayerSeason.player_id == pid)]
                for pid in (COLLINS, COOK)}
    past_pa = sum(r["PA"] for r in FX["player_seasons"] if r["player_id"] == COLLINS and r["year"] < 2026)
    check(f"career leaderboard: Collins's career PA is unknown, not his {past_pa} pre-2026 PA",
          car[COLLINS] == (None, None) and past_pa > 0, car[COLLINS])
    check("career leaderboard: Cook's career PA is the plain sum", car[COOK][0] == sum(
        season(COOK)["PA"] if r["year"] == 2026 else r["PA"] for r in FX["player_seasons"] if r["player_id"] == COOK), car[COOK])
    ct = ds._batting_career_totals(rows[COLLINS])
    check("profile career: Collins's career OBP and OPS unknown; AVG (AB, H from balldontlie) stands",
          ct.get("OBP") is None and ct.get("OPS") is None and ct.get("AVG") is not None, ct)
    ck = ds._batting_career_totals(rows[COOK])
    check("profile career: Cook's career OBP computed", ck.get("OBP") is not None, ck)
    with Session(eng) as db:
        db.execute(text('UPDATE player_seasons SET "SF" = NULL WHERE player_id = :p AND year = 2024'), {"p": COOK})
        past_null = db.execute(select(ds._career_sum_known(PlayerSeason, PlayerSeason.SF))
                               .where(PlayerSeason.player_id == COOK)).scalar()
        known = db.execute(text('SELECT SUM("SF") FROM player_seasons WHERE player_id = :p'), {"p": COOK}).scalar()
        db.rollback()
    check("a NULL in a PAST season (2024) is 'not recorded', not unknown: the career sum skips it as before",
          past_null is not None and past_null == known, (past_null, known))
    eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
