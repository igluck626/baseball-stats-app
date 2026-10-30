#!/usr/bin/env python3
"""`postseason=true` on the record-at-date endpoints (Isaac, 2026-10-29): a
playoff box score shows "W: Yamamoto (5-1)" and "2B: Rice (3)" — the
postseason through that game, not the regular season.

  • it counts the postseason tables, one source per season (Retrosheet where it
    has the season, else balldontlie), never both
  • the W/L/SV columns decide, not the `result` label (11 balldontlie rows of
    2026 read "ND" on a pitcher who won)
  • `includes_today` is the plain "a row on that date" — the after-the-season
    stopgap, which answers true for every October date, must not suppress the
    app's bump on a game the ingest hasn't reached
  • the regular-season answer is unchanged

Standalone, no pytest. Run: <backend python> backend/tests/test_postseason_at_date.py
"""
import contextlib
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
from sqlalchemy import create_engine                               # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
import main                                                        # noqa: E402
from database.models import (Base, BattingGameLog, PitchingGameLog,  # noqa: E402
                             PostseasonBattingGameLog, PostseasonPitchingGameLog)

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


D = datetime.date
P, B = 1001, 2002


def pit(source, gid, date, w=0, l=0, sv=0, result="ND", season=None):
    return dict(player_id=P, source=source, game_id=gid, game_date=date, season=season or date.year, round="DS",
                result=result, W=w, L=l, SV=sv, **({"retro_player_id": "x"} if source == "retrosheet" else {"bdl_player_id": 9}))


def bat(source, gid, date, hr=0, d2=0, ab=4, season=None):
    return dict(player_id=B, source=source, game_id=gid, game_date=date, season=season or date.year, round="DS",
                AB=ab, BB=0, HR=hr, doubles=d2, triples=0,
                **({"retro_player_id": "y"} if source == "retrosheet" else {"bdl_player_id": 8}))


ROWS = {
    PostseasonPitchingGameLog: [
        pit("bdl", "1", D(2026, 10, 4), w=1, result="ND"),       # a win under a stale "ND" label
        pit("bdl", "2", D(2026, 10, 10), sv=1, result="S"),
        pit("bdl", "3", D(2026, 10, 20), l=1, result="L"),
        # 2025: Retrosheet has the season, and a balldontlie copy of the same game must not add to it
        pit("retrosheet", "retro-1", D(2025, 10, 5), w=1, result="W"),
        pit("bdl", "4", D(2025, 10, 5), w=1, result="W"),
    ],
    PostseasonBattingGameLog: [
        bat("bdl", "1", D(2026, 10, 4), hr=1, d2=1),
        bat("bdl", "2", D(2026, 10, 10), d2=2),
        bat("bdl", "3", D(2026, 10, 12), d2=None),               # unknown doubles on a game he batted
    ],
    PitchingGameLog: [dict(player_id=P, game_id="r1", game_date=D(2026, 6, 1), season=2026,
                           result="W", W=1, L=0, SV=0)],
    BattingGameLog: [dict(player_id=B, game_id="r1", game_date=D(2026, 6, 1), season=2026,
                          AB=4, BB=0, HR=2, doubles=1, triples=0)],
}

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="postseason_at_date_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[m.__table__ for m in ROWS])
    with eng.begin() as c:
        for m, rows in ROWS.items():
            for r in rows:
                c.execute(m.__table__.insert().values(r))
    S = sessionmaker(bind=eng)

    @contextlib.contextmanager
    def get_session():
        s = S()
        try:
            yield s
        finally:
            s.close()

    saved = (main.connection.get_session, main.connection.db_available)
    main.connection.get_session, main.connection.db_available = get_session, lambda: True
    try:
        print("pitcher record, postseason=true")
        r = main.player_pitcher_record_at_date(P, D(2026, 10, 28), postseason=True)
        check("2026: 1-1 with 1 save, the stale 'ND' row counted as the win it was",
              (r["wins"], r["losses"], r["saves"], r["wins_unknown"]) == (1, 1, 1, False), r)
        r = main.player_pitcher_record_at_date(P, D(2026, 10, 10), postseason=True)
        check("through 10-10: 1-0, 1 save, and that date's row is in", (r["wins"], r["losses"], r["saves"], r["includes_today"]) == (1, 0, 1, True), r)
        r = main.player_pitcher_record_at_date(P, D(2026, 10, 27), postseason=True)
        check("a date with no row yet says so (the stopgap would say true)", r["includes_today"] is False, r)
        r = main.player_pitcher_record_at_date(P, D(2025, 10, 31), postseason=True)
        check("2025: Retrosheet's season only, the balldontlie copy not added", (r["wins"], r["gamelog_rows"]) == (1, 1), r)
        r = main.player_pitcher_record_at_date(P, D(2026, 10, 28))
        check("regular season unchanged: 1-0 from June, stopgap still true", (r["wins"], r["losses"], r["includes_today"]) == (1, 0, True), r)

        print("batter totals, postseason=true")
        r = main.player_batter_stats_at_date(B, D(2026, 10, 10), postseason=True)
        check("through 10-10: 1 HR, 3 doubles, known", (r["home_runs"], r["doubles"], r["doubles_unknown"]) == (1, 3, False), r)
        r = main.player_batter_stats_at_date(B, D(2026, 10, 12), postseason=True)
        check("a NULL double on a game he batted makes the total unknown", r["doubles_unknown"] is True and r["home_runs"] == 1, r)
        r = main.player_batter_stats_at_date(B, D(2026, 10, 12))
        check("regular season unchanged: 2 HR, 1 double", (r["home_runs"], r["doubles"]) == (2, 1), r)
    finally:
        main.connection.get_session, main.connection.db_available = saved

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if results and all(results) else 1)
