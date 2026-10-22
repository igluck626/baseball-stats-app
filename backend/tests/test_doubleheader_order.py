#!/usr/bin/env python3
"""Doubleheaders in start order: `game_start` on the game-log tables.

⚠️ THE CHECK THAT MATTERS: a balldontlie game id has no game number in it, and a
rescheduled game gets a new id — NYY-BOS 2026-08-29 started with 8611970, which
sorts after '5059812' — and even same-length ids aren't in start order (ARI-SF
2026-08-29 was 5059828 first). Ordering by (game_date, game_id) put game 2 first in 13 of the
25 doubleheaders of 2026, and Michael Busch's on-base streak read 26, not 27.
Rows now order by (game_date, game_start NULLS LAST, game_id); a Retrosheet row
has no game_start and keeps its id's trailing digit.

Real rows (fixtures/dh_order_2026.json, production read-only 2026-10-20): one
batter who played both games of each of the 25 doubleheaders of 2026 (start
order confirmed against each game's first play, 25/25), Busch's whole 2026, a
2025 Retrosheet doubleheader of his, and balldontlie's NYY-BOS 2026-08-29 game
records whole. Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_doubleheader_order.py
"""
import contextlib
import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
from sqlalchemy import create_engine                               # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
import main                                                        # noqa: E402
import data_service as ds                                          # noqa: E402
from database import connection, crud                              # noqa: E402
from database.models import Base, BattingGameLog                   # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "dh_order_2026.json")))
START = FX["game_start"]
BUSCH = 683737
iso = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))   # noqa: E731

print("the nightly fills it")
g1, g2 = FX["bdl_games"]["8611970"], FX["bdl_games"]["5059812"]
c1, c2 = ds._bdl_game_ctx(g1), ds._bdl_game_ctx(g2)
check("the game context carries balldontlie's start time, in UTC",
      c1["game_start"] == iso(g1["date"]) and c1["game_start"].tzinfo is not None, c1.get("game_start"))
check("NYY-BOS 2026-08-29: 8611970 started first, though its id sorts after 5059812",
      c1["game_start"] < c2["game_start"] and c1["game_date"] == c2["game_date"])
row = ds._parse_bdl_batting_gamelog({"player": {"id": 1}, "team_name": g1["home_team_name"],
                                     "at_bats": 4, "hits": 1, "plate_appearances": 4}, c1)
check("a parsed game line carries game_start", row is not None and row.get("game_start") == c1["game_start"], row)
check("the column is added at deploy on both tables (and their staging twins)",
      ("game_start", "TIMESTAMPTZ") in connection._BATTING_GAMELOGS_NEW_COLUMNS
      and ("game_start", "TIMESTAMPTZ") in connection._PITCHING_GAMELOGS_NEW_COLUMNS)

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the order is SQL)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="dh_order_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[BattingGameLog.__table__])
    cols = {c.name for c in BattingGameLog.__table__.columns}

    def load(with_start):
        with eng.begin() as c:
            c.execute(BattingGameLog.__table__.delete())
            for r in FX["batting_gamelogs"] + FX["retro_doubleheader"]:
                v = {k: x for k, x in r.items() if k in cols}
                v["game_start"] = iso(START[r["game_id"]]) if with_start and r["game_id"] in START else None
                c.execute(BattingGameLog.__table__.insert().values(v))
    S = sessionmaker(bind=eng)

    @contextlib.contextmanager
    def get_session():
        s = S()
        try:
            yield s
        finally:
            s.close()

    orig = (main.connection.get_session, main.connection.db_available)
    main.connection.get_session = get_session
    main.connection.db_available = lambda: True
    first_of = {}
    for _home, _d, games, _ in FX["doubleheaders"]:
        first_of[str(games[0][0])] = str(games[1][0])
    pid_of = {}
    for r in FX["batting_gamelogs"]:
        if r["game_id"] in first_of or r["game_id"] in first_of.values():
            pid_of.setdefault(r["game_id"], set()).add(r["player_id"])

    def in_order():
        right = 0
        for a, b in first_of.items():
            pid = sorted(pid_of[a] & pid_of[b])[0]
            ids = [str(g["game_id"]) for g in main._daily_games(pid, 2026)]
            right += ids.index(a) < ids.index(b)
        return right
    try:
        print("before the backfill (game_start NULL on every 2026 row)")
        load(False)
        check("ordered by game_id, 12 of the 25 doubleheaders come out in start order", in_order() == 12, in_order())
        check("Busch's on-base streak reads 26 that way", main._longest_streak(main._daily_games(BUSCH, 2026), "on_base")[0] == 26)

        print("with game_start")
        load(True)
        check("every one of the 25 doubleheaders comes out in start order", in_order() == 25, in_order())
        lo = main._longest_streak(main._daily_games(BUSCH, 2026), "on_base")
        check("Busch's on-base streak: 27, 2026-05-24 to 2026-06-24",
              (lo[0], str(lo[1]), str(lo[2])) == (27, "2026-05-24", "2026-06-24"), lo[:3])
        retro = [g["game_id"] for g in main._daily_games(BUSCH, 2025)]
        check("a Retrosheet doubleheader (no game_start) still orders by its id's game number",
              retro == ["retro-CHN202508191", "retro-CHN202508192"], retro)
        with get_session() as db:
            logs = [l.game_id for l in crud.get_batting_gamelogs(db, BUSCH, season=2026)]
        dh = [(a, b) for a, b in first_of.items() if a in logs and b in logs]
        check("the game log (newest first) shows game 2 above game 1",
              dh and all(logs.index(b) < logs.index(a) for a, b in dh), dh)
    finally:
        main.connection.get_session, main.connection.db_available = orig

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
