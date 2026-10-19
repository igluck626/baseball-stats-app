#!/usr/bin/env python3
"""/ask head-to-head (`_run_head_to_head`) on balldontlie game ids.

⚠️ THE CHECK THAT MATTERS: Yankees–Red Sox 2026 is 7–6 in 13. The query used to
take the HOME club from the game_id prefix ('retro-NYA2025…' → NYA). A
balldontlie id (2026 on) is a bare number, so no 2026 game ever matched and
production answered "I don't have any regular-season games between the New York
Yankees and the Boston Red Sox in 2026" (2026-10-19). The home club of a
balldontlie game now comes from that game's AWAY rows, whose `opponent` is the
home club; Retrosheet ids keep the prefix.

Real rows (fixtures/h2h_nyy_bos.json, production batting_gamelogs read-only on
2026-10-19): every row of the 13 NYY–BOS games of 2026 and the 13 of 2025, an
NYY–TOR and a BOS–TOR game of 2026, and the BAL–TOR doubleheader of 2026-09-23
(two real games, both won 4–2 by Baltimore, so a dedup on date and score would
be wrong). Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_head_to_head.py
"""
import contextlib
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
from database.models import Base, BattingGameLog                   # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


ROWS = json.load(open(os.path.join(HERE, "fixtures", "h2h_nyy_bos.json")))["batting_gamelogs"]

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the head-to-head SQL is Postgres)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="head_to_head_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[BattingGameLog.__table__])
    cols = {c.name for c in BattingGameLog.__table__.columns}
    with eng.begin() as c:
        for r in ROWS:
            c.execute(BattingGameLog.__table__.insert().values({k: v for k, v in r.items() if k in cols}))
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
    try:
        print("2026 (balldontlie ids)")
        r = main._run_head_to_head("Yankees", "Red Sox", season=2026)
        check("Yankees–Red Sox 2026: 7–6",
              (r.get("a_wins"), r.get("a_losses")) == (7, 6), r)
        check("the answer says it", r.get("answer") ==
              "The New York Yankees went 7-6 against the Boston Red Sox in the 2026 regular season.",
              r.get("answer"))
        r = main._run_head_to_head("Red Sox", "Yankees", season=2026)
        check("asked the other way round: Red Sox 6–7",
              (r.get("a_wins"), r.get("a_losses")) == (6, 7), r)
        r = main._run_head_to_head("Yankees", "Blue Jays", season=2026)
        check("Yankees–Blue Jays 2026: the one NYY–TOR game, not the BOS–TOR or BAL–TOR ones",
              (r.get("a_wins") or 0) + (r.get("a_losses") or 0) == 1, r)
        r = main._run_head_to_head("Orioles", "Blue Jays", season=2026)
        check("the 09-23 doubleheader, both 4–2: two games, Orioles 2–0",
              (r.get("a_wins"), r.get("a_losses")) == (2, 0), r)

        print("2025 (Retrosheet ids) unchanged")
        r = main._run_head_to_head("Yankees", "Red Sox", season=2025)
        check("Yankees–Red Sox 2025: 4–9", (r.get("a_wins"), r.get("a_losses")) == (4, 9), r)

        print("both kinds of id in one range")
        r = main._run_head_to_head("Yankees", "Red Sox", season_start=2025, season_end=2026)
        check("2025–2026: 11–15 (4–9 plus 7–6)",
              (r.get("a_wins"), r.get("a_losses")) == (11, 15), r)
    finally:
        main.connection.get_session, main.connection.db_available = orig

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
