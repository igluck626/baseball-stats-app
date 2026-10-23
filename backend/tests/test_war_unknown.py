#!/usr/bin/env python3
"""Career WAR after Baseball-Reference (Isaac, 2026-10-22).

⚠️ THE CHECKS THAT MATTER:
  • WAR comes only from stored Baseball-Reference figures, and that fetch is
    gone: data_service.WAR_LAST_STORED_SEASON = 2026 is the last season stored.
    A LATER season with games but no WAR is unknown, so career WAR, WAR_off and
    WAR_def are None ("—") — never the short sum that used to read as a total
    (or 0.0 for a player whose only seasons are new).
  • A 2026 season with WAR sums as always. A NULL in 2026 or earlier is "not
    rated" and reads as 0, as it always has.
  • A placeholder season (no games, no WAR) after 2026 changes nothing.
  • The same rule on the career leaderboard (the row drops out) and in /ask
    (career and single-season WAR answer "I don't have WAR on record").

Synthetic season rows: a 2027 season can't come from production yet.
Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_war_unknown.py
"""
import contextlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
from sqlalchemy import create_engine, select                       # noqa: E402
from sqlalchemy.orm import Session, sessionmaker                   # noqa: E402
import main                                                        # noqa: E402
import data_service as ds                                          # noqa: E402
from database.models import Base, PlayerSeason, PitcherSeason      # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def bat(year, G, WAR, WAR_off=None, WAR_def=None):
    return {"year": year, "G": G, "WAR": WAR, "WAR_off": WAR_off, "WAR_def": WAR_def}


PAST = bat(2025, 150, 3.1, 2.5, 0.6)
S2026 = bat(2026, 140, 2.4, 2.0, 0.4)          # a 2026 season with WAR
S2027 = bat(2027, 120, None)                   # a 2027-style season: games, no WAR
PLACEHOLDER = bat(2027, 0, None)               # a placeholder season: no games, no WAR

print("the rule")
check("WAR_LAST_STORED_SEASON is 2026", ds.WAR_LAST_STORED_SEASON == 2026, ds.WAR_LAST_STORED_SEASON)

print("profile career totals (batting)")
t = ds._batting_career_totals([PAST, S2026])
check("2025 + 2026 with WAR: career WAR 5.5, WAR_off 4.5, WAR_def 1.0 (the plain sums)",
      (t["WAR"], t["WAR_off"], t["WAR_def"]) == (5.5, 4.5, 1.0), t)
t = ds._batting_career_totals([PAST, S2026, S2027])
check("plus a 2027 season with games and no WAR: WAR, WAR_off and WAR_def all None, not 5.5 / 4.5 / 1.0",
      (t["WAR"], t["WAR_off"], t["WAR_def"]) == (None, None, None), t)
t = ds._batting_career_totals([S2027])
check("a player whose only season is 2027-style: None, not the 0.0 it used to show",
      t["WAR"] is None, t)
t = ds._batting_career_totals([PAST, S2026, PLACEHOLDER])
check("a 2027 placeholder with no games changes nothing: 5.5", t["WAR"] == 5.5, t)
t = ds._batting_career_totals([PAST, bat(2026, 140, None)])
check("a 2026 season with games and no WAR is 'not rated' (0), as before: 3.1", t["WAR"] == 3.1, t)
t = ds._batting_career_totals([PAST, S2026, bat(2027, 120, 1.2, 1.0, 0.2)])
check("a 2027 season that HAS WAR sums: 6.7", t["WAR"] == 6.7, t)

print("profile career totals (pitching)")
t = ds._pitching_career_totals([{"year": 2026, "G": 30, "WAR": 4.0}, {"year": 2027, "G": 31, "WAR": None}])
check("2026 with WAR + 2027 with games and no WAR: career WAR None", t["WAR"] is None, t)
t = ds._pitching_career_totals([{"year": 2026, "G": 30, "WAR": 4.0}, {"year": 2027, "G": 0, "WAR": None}])
check("2026 with WAR + a 2027 placeholder: 4.0", t["WAR"] == 4.0, t)

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the leaderboard and /ask WAR sums are Postgres SQL)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="war_unknown_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[PlayerSeason.__table__, PitcherSeason.__table__])
    KNOWN, NEW, ROOKIE, HOLDER = 1, 2, 3, 4   # synthetic player ids
    rows = {KNOWN: [PAST, S2026], NEW: [PAST, S2026, S2027], ROOKIE: [S2027], HOLDER: [PAST, S2026, PLACEHOLDER]}
    with eng.begin() as c:
        for pid, seasons in rows.items():
            for s in seasons:
                c.execute(PlayerSeason.__table__.insert().values(player_id=pid, **s))
        c.execute(PitcherSeason.__table__.insert().values(player_id=NEW, year=2026, G=30, WAR=4.0))
        c.execute(PitcherSeason.__table__.insert().values(player_id=NEW, year=2027, G=31, WAR=None))

    print("career leaderboard sum")
    with Session(eng) as db:
        q = select(PlayerSeason.player_id, ds._career_war_known(PlayerSeason, PlayerSeason.WAR)) \
            .group_by(PlayerSeason.player_id)
        car = {r[0]: r[1] for r in db.execute(q)}
        pq = select(PitcherSeason.player_id, ds._career_war_known(PitcherSeason, PitcherSeason.WAR)) \
            .group_by(PitcherSeason.player_id)
        pcar = {r[0]: r[1] for r in db.execute(pq)}
    check("2025 + 2026 with WAR: 5.5", car[KNOWN] is not None and round(car[KNOWN], 1) == 5.5, car)
    check("with a 2027 season with games and no WAR: NULL (the row leaves the board)", car[NEW] is None, car)
    check("2027-only, no WAR: NULL, not 0", car[ROOKIE] is None, car)
    check("with a 2027 placeholder: 5.5", car[HOLDER] is not None and round(car[HOLDER], 1) == 5.5, car)
    check("pitcher: 2026 with WAR + 2027 with games and no WAR: NULL", pcar[NEW] is None, pcar)

    print("/ask (_run_season_rate)")
    S = sessionmaker(bind=eng)

    @contextlib.contextmanager
    def get_session():
        s = S()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    saved = (main.connection.get_session, main.connection.db_available, main._resolve_retro_id)
    main.connection.get_session = get_session
    main.connection.db_available = lambda: True
    try:
        def ask(pid, role="bat", season=None):
            main._resolve_retro_id = lambda player, r: [{"mlbam_id": pid, "name": f"Player {pid}"}]
            return main._run_season_rate(str(pid), role, "WAR", season, None, None, "R")
        a = ask(KNOWN)
        check("career WAR, 2025 + 2026 with WAR: 5.5", (a.get("stat_value") or {}).get("value") == 5.5, a.get("answer"))
        a = ask(NEW)
        check("career WAR with a 2027 season with games and no WAR: unknown, not 5.5",
              a.get("stat_value") is None and a.get("empty"), a.get("answer"))
        a = ask(NEW, season=2027)
        check("2027 WAR for that season: unknown", a.get("stat_value") is None and a.get("empty"), a.get("answer"))
        a = ask(NEW, season=2026)
        check("2026 WAR for the same player: 2.4", (a.get("stat_value") or {}).get("value") == 2.4, a.get("answer"))
        a = ask(ROOKIE)
        check("2027-only player, no WAR: unknown, not 0", a.get("stat_value") is None and a.get("empty"), a.get("answer"))
        a = ask(HOLDER)
        check("with a 2027 placeholder: 5.5", (a.get("stat_value") or {}).get("value") == 5.5, a.get("answer"))
        a = ask(NEW, role="pit")
        check("pitcher career WAR with a 2027 season with games and no WAR: unknown",
              a.get("stat_value") is None and a.get("empty"), a.get("answer"))
    finally:
        main.connection.get_session, main.connection.db_available, main._resolve_retro_id = saved
    eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
