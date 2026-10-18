#!/usr/bin/env python3
"""Answers built from the daily game logs under Isaac's "activity" rule
(2026-10-16): /ask streaks, spans, milestones, stat lines, the stored
game-unit boards, and the Game Logs splits.

⚠️ THE CHECKS THAT MATTER:
  • `_daily_games` marks, per game, the fields that are unknown on a game with
    AB or BB (g["unknown"]); a no-AB/no-BB game's unknowns read as 0.
  • A stat line over a game with an unknown double/HBP shows "—" (None) for the
    totals and rates built on it, never a short number.
  • A streak whose length an unknown game could change is reported as a range
    (low, high, exact=False) — computed both ways, never guessed.
  • The stored span boards hold value NULL for a player whose games leave the
    event unknown; the readers leave that player out and name him.
  • The Game Logs splits null SB / OBP / SLG over a window with an unknown.

Real rows (fixtures/season_counting_activity.json): Isaac Collins (9 games with
AB/BB and unknown 2B/SB/HBP/SF/PA) and Billy Cook (unknowns only on no-AB/no-BB
games). Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_activity_answers.py
"""
import contextlib
import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "scripts")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
from sqlalchemy import create_engine                               # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
import main                                                        # noqa: E402
import data_service as ds                                          # noqa: E402
from database.models import Base, BattingGameLog                   # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "season_counting_activity.json")))
COLLINS, COOK = 686555, 695257
ROWS = [r for r in FX["batting_gamelogs"] if r["player_id"] in (COLLINS, COOK)]

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the loader SQL is Postgres)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="activity_answers_"), cleanup_mode="stop")
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
            s.commit()
        finally:
            s.close()

    orig = main.connection.get_session
    main.connection.get_session = get_session
    try:
        print("the loader")
        col = main._daily_games(COLLINS, 2026)
        cook = main._daily_games(COOK, 2026)
        fields = ("PA", "doubles", "triples", "HBP", "SF", "SH", "IBB", "SB", "CS")
        act_null = [r for r in ROWS if r["player_id"] == COLLINS and ((r["AB"] or 0) > 0 or (r["BB"] or 0) > 0)
                    and any(r[f] is None for f in fields)]
        flagged = [g for g in col if g["unknown"]]
        pa_unknown = [g for g in flagged if "PA" in g["unknown"]]
        check(f"Collins: every game with AB/BB and any unknown field is flagged ({len(act_null)}), "
              f"{len(pa_unknown)} of them with PA unknown",
              len(flagged) == len(act_null) and len(pa_unknown) == 9, (len(flagged), len(act_null), len(pa_unknown)))
        check("Cook: his unknowns are all on no-AB/no-BB games — none flagged",
              not any(g["unknown"] for g in cook) and any(r["PA"] is None for r in ROWS if r["player_id"] == COOK))

        print("stat lines")
        line = main._stat_line(col)
        check("Collins's season line: 2B, 3B, OBP, SLG, OPS are '—' (None); AVG stands",
              line["2B"] is None and line["OBP"] is None and line["SLG"] is None and line["OPS"] is None
              and line["AVG"] is not None, line)
        clean = [g for g in col if not g["unknown"]]
        line2 = main._stat_line(clean)
        check("over only his recorded games, the line is complete again",
              line2["OBP"] is not None and line2["2B"] is not None, line2)
        check("Cook's line is complete", main._stat_line(cook)["OBP"] is not None)

        print("streaks")
        lo, hi, exact = main._longest_streak_bounds(col, "on_base")
        hinges = [g for g in col if main._streak_hinges(g, "on_base")]
        check(f"on-base: {len(hinges)} of Collins's games turn on an unknown HBP; low <= high",
              hinges and lo[0] <= hi[0], (len(hinges), lo[0], hi[0]))
        check("the bound is exact only when both ways agree", exact == (lo[:3] == hi[:3]))
        lo_h, hi_h, ex_h = main._longest_streak_bounds(col, "hitting")
        check("hitting: no game turns on an unknown SF (each has an AB or no walk) — exact",
              ex_h and lo_h == hi_h)
        lo_c, hi_c, ex_c = main._longest_streak_bounds(cook, "on_base")
        check("Cook: nothing unknown that matters — exact, equal to the plain streak",
              ex_c and lo_c == main._longest_streak(cook, "on_base"))

        print("stored game-unit boards")
        rows = main._leaderboard_rows(COLLINS, col, {2026})
        spans = {(r["window"], r["event"]): r["value"] for r in rows if r["metric"] == "span"}
        check("Collins's 2B / TB / SB / HBP span rows hold value NULL (left out, named)",
              all(v is None for (w, ev), v in spans.items() if ev in ("2B", "TB", "SB", "HBP", "1B", "3B")),
              {k: v for k, v in spans.items() if k[1] in ("2B", "TB", "SB")})
        check("his HR / H span rows are numbers (HR and H are never unknown)",
              all(v is not None for (w, ev), v in spans.items() if ev in ("HR", "H")),
              {k: v for k, v in spans.items() if k[1] in ("HR", "H")})
    finally:
        main.connection.get_session = orig

    print("Game Logs splits")
    raw = [dict(r) for r in ROWS if r["player_id"] == COLLINS]
    w = ds._aggregate_batting_window(raw)
    check("a window with an unknown double/HBP/SB: SB, OBP, SLG, OPS are None",
          w["SB"] is None and w["OBP"] is None and w["SLG"] is None and w["OPS"] is None, w)
    check("...while AB, H, HR, AVG stand", w["AB"] and w["H"] is not None and w["BA"] is not None)
    rc = ds._aggregate_batting_window([dict(r) for r in ROWS if r["player_id"] == COOK])
    check("Cook's window is complete", rc["SB"] is not None and rc["OBP"] is not None, rc)
    eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
