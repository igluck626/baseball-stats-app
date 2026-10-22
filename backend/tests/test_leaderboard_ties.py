#!/usr/bin/env python3
"""Leaderboard ties fall to player_id, as on every other leaderboard.

⚠️ THE CHECK THAT MATTERS: the /leaderboards boards (season, all-time, career)
sorted on the stat alone, so tied rows came back in whatever order the database
picked. Once the game-log counts are summed, 2026's complete-game board is 21
pitchers at 1 and its shutout board 11 at 1. Ties now fall to player_id (then
year), the order the streak, span and count boards already use.

Rows: the 21 pitchers the 9cu dry run credits with a 2026 complete game (team,
league, IP and GS from production read-only, 2026-10-22; CG and SHO from that
plan), loaded in two different orders. Needs an embedded Postgres
(`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_leaderboard_ties.py
"""
import contextlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
from sqlalchemy import create_engine                               # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
import data_service as ds                                          # noqa: E402
from database import connection                                    # noqa: E402
from database.models import Base, Pitcher, PitcherSeason           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


# (player_id, name, team, league, IP, GS, CG, SHO)
ROWS = [
    (518876, "Merrill Kelly", "ARI", "NL", 162.0, 29, 1, 0),
    (519242, "Chris Sale", "ATL", "NL", 162.667, 27, 1, 1),
    (607259, "Nick Martinez", "TBA", "AL", 180.0, 31, 1, 0),
    (607536, "Kyle Freeland", "COL", "NL", 135.333, 26, 1, 0),
    (641927, "Bailey Ober", "MIN", "AL", 131.0, 24, 1, 1),
    (645261, "Sandy Alcantara", "MIA", "NL", 214.0, 33, 1, 1),
    (650911, "Cristopher Sanchez", "PHI", "NL", 207.0, 33, 1, 1),
    (656302, "Dylan Cease", "TOR", "AL", 169.0, 29, 1, 1),
    (657277, "Logan Webb", "SFN", "NL", 160.667, 27, 1, 0),
    (663568, "Stephen Kolek", "Kansas City", "AL", 54.0, 10, 1, 1),
    (666200, "Jesus Luzardo", "PHI", "NL", 179.667, 29, 1, 1),
    (669160, "Dustin May", "MIL", "NL", 145.667, 31, 1, 1),
    (669358, "Shane Baz", "BAL", "AL", 179.667, 31, 1, 0),
    (669372, "J. T. Ginn", "ATH", "AL", 138.333, 24, 1, 0),
    (669923, "George Kirby", "SEA", "AL", 174.667, 30, 1, 0),
    (677952, "Braxton Ashcraft", "PIT", "NL", 165.333, 28, 1, 0),
    (691587, "Eury Pérez", "MIA", "NL", 159.0, 29, 1, 1),
    (694819, "Jacob Misiorowski", "MIL", "NL", 174.667, 30, 1, 1),
    (700712, "Walbert Ureña", "LAA", "AL", 148.333, 27, 1, 0),
    (702070, "Noah Cameron", "KCA", "AL", 177.333, 30, 1, 1),
    (808967, "Yoshinobu Yamamoto", "LAN", "NL", 185.0, 28, 1, 0),
]
BY_ID = sorted(r[0] for r in ROWS)
SHO_BY_ID = sorted(r[0] for r in ROWS if r[7]) + sorted(r[0] for r in ROWS if not r[7])

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the order is SQL)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="leaderboard_ties_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)

    @contextlib.contextmanager
    def get_session():
        s = S()
        try:
            yield s
        finally:
            s.close()

    patched = (connection.get_session, connection.db_available)
    connection.get_session, connection.db_available = get_session, (lambda: True)

    def load(rows):
        with eng.begin() as c:
            c.execute(PitcherSeason.__table__.delete())
            c.execute(Pitcher.__table__.delete())
            for pid, name, team, lg, ip, gs, cg, sho in rows:
                c.execute(Pitcher.__table__.insert().values(player_id=pid, name=name))
                c.execute(PitcherSeason.__table__.insert().values(
                    player_id=pid, year=2026, team=team, league=lg, IP=ip, G=gs, GS=gs, CG=cg, SHO=sho))

    def ids(board):
        return [r["player"]["player_id"] for r in (board or {}).get("leaders", [])]

    try:
        for label, order in (("loaded by player_id", ROWS), ("loaded in reverse", ROWS[::-1]),
                             ("loaded by IP", sorted(ROWS, key=lambda r: r[4]))):
            print(label)
            load(order)
            season = ds.get_leaderboard("CG", 2026, "pitcher", mode="season", limit=25)
            check("2026 CG: all 21, in player_id order", ids(season) == BY_ID, ids(season))
            sho = ds.get_leaderboard("SHO", 2026, "pitcher", mode="season", limit=25)
            check("2026 SHO: the 11 at 1, then the 10 at 0, each in player_id order", ids(sho) == SHO_BY_ID, ids(sho))
            top = ds.get_leaderboard("CG", 2026, "pitcher", mode="season", limit=5)
            check("a cutoff inside the tie keeps the lowest five ids", ids(top) == BY_ID[:5], ids(top))
            at = ds._leaderboard_all_time("CG", False, 25, None, None, None, None)
            check("all-time CG: player_id order", ids(at) == BY_ID, ids(at))
            car = ds._leaderboard_career("CG", False, 25, None, None, None, None)
            check("career CG: player_id order", ids(car) == BY_ID, ids(car))
        load(ROWS)
        with eng.begin() as c:
            c.execute(PitcherSeason.__table__.update().where(PitcherSeason.player_id == 808967).values(CG=2))
        season = ds.get_leaderboard("CG", 2026, "pitcher", mode="season", limit=25)
        check("the stat still sorts first: a 2-CG pitcher leads the 1s",
              ids(season)[:2] == [808967, BY_ID[0]], ids(season)[:2])
    finally:
        connection.get_session, connection.db_available = patched
        eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if results and all(results) else 1)
