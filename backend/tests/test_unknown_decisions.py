#!/usr/bin/env python3
"""Readers of a pitcher's game-log decisions when balldontlie didn't ship one
(result and W/L/SV all NULL: unknown, not "no decision").

⚠️ THE CHECKS THAT MATTER (Isaac, 2026-10-18):
  • the box-score record (/pitcher-record-at-date) never sends null in a field
    build 17 decodes as non-optional: wins/losses/saves stay numbers (the
    known games) and `*_unknown` flags say the count is incomplete;
  • a win streak STOPS at an unknown game, never runs through it;
  • quality starts read GS, falling back to the old inference only on a row
    not yet re-parsed (GS NULL);
  • spans and the stored span boards decline / hold NULL for W, L, SV; the
    stat line's W/L/SV read "—" (None); strikeouts are unaffected;
  • the milestone gate (game-log sum vs pitcher_seasons) declines a milestone
    an unknown game could hide.
Real rows (fixtures/unknown_decisions_warren.json): Will Warren's 2026, as the
widened write would store it. His 05-06 start (5058322) is unknown; production
holds an L there, so a streak run through it would join 4 wins to 3 more.
Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_unknown_decisions.py
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
from database.models import Base, PitchingGameLog, PitcherSeason   # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "unknown_decisions_warren.json")))
WARREN = 701542
UNKNOWN_DATE = "2026-05-06"

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the loaders are Postgres SQL)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="unknown_decisions_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[PitchingGameLog.__table__, PitcherSeason.__table__])
    with eng.begin() as c:
        for m, rows in ((PitchingGameLog, FX["pitching_gamelogs"]), (PitcherSeason, FX["pitcher_seasons"])):
            cols = {col.name for col in m.__table__.columns}
            for r in rows:
                c.execute(m.__table__.insert().values({k: v for k, v in r.items() if k in cols}))
    S = sessionmaker(bind=eng)

    @contextlib.contextmanager
    def get_session():
        s = S()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    patched = {"get_session": main.connection.get_session, "db_available": main.connection.db_available,
               "_resolve_retro_id": main._resolve_retro_id}
    main.connection.get_session = get_session
    main.connection.db_available = lambda: True
    main._resolve_retro_id = lambda player, role=None: [{"mlbam_id": WARREN, "name": "Will Warren"}]
    try:
        print("box-score record (/pitcher-record-at-date)")
        rec = main.player_pitcher_record_at_date(WARREN, datetime.date(2026, 9, 30))
        check("through 09-30: wins 11, losses 5 (the known games), flags true",
              (rec["wins"], rec["losses"], rec["saves"]) == (11, 5, 0)
              and rec["wins_unknown"] and rec["losses_unknown"] and rec["saves_unknown"], rec)
        # build 17's PitcherRecordResponse (APIClient.swift at 4c12f2c): these
        # fields are non-optional; a null in any of them fails the whole decode.
        b17 = {"player_id": int, "game_date": str, "wins": int, "losses": int, "saves": int, "includes_today": bool}
        check("every field build 17 decodes as non-optional is present and of its type (never null)",
              all(isinstance(rec.get(k), t) and not (t is int and isinstance(rec.get(k), bool))
                  for k, t in b17.items()), {k: rec.get(k) for k in b17})
        early = main.player_pitcher_record_at_date(WARREN, datetime.date(2026, 5, 5))
        check("through 05-05 (before the unknown game): flags false",
              not early["wins_unknown"] and not early["losses_unknown"], early)
        hist = main._decision({"W": None, "result": "W"}, "W"), main._decision({"W": None, "result": "ND"}, "L")
        check("a row from before the decision columns reads its result string", hist == (1, 0), hist)

        games = main._daily_pitching_games(WARREN, 2026)
        unk = [g for g in games if g["unknown"]]
        check("the loader marks exactly the 05-06 game unknown",
              [str(g["game_date"]) for g in unk] == [UNKNOWN_DATE], [str(g["game_date"]) for g in unk])

        print("win streak")
        length, start, end, _ = main._longest_pitcher_streak(games, "win")
        through = [dict(g, unknown=frozenset()) for g in games]          # the old reading: unknown = no decision
        old_len = main._longest_pitcher_streak(through, "win")[0]
        check(f"the streak stops at the unknown game: {length} (read through it: {old_len})",
              length == 4 and old_len == 7, (length, old_len, start, end))

        print("quality starts")
        relief = next(g for g in games if str(g["game_date"]) == "2026-09-04")
        check("09-04 is a relief outing (GS 0, 2.1 IP, no decision)",
              relief["GS"] == 0 and relief["result"] == "ND" and relief["IP"] < 6, relief)
        check("GS 0: skipped, not a failed start", main._pitcher_streak_classify("quality_start", relief) == "skip")
        check("the same row with no GS yet falls back to the old inference (ND reads as a start: break)",
              main._pitcher_streak_classify("quality_start", dict(relief, GS=None)) == "break")

        print("spans, boards, lines")
        span = main._run_pitcher_span("Will Warren", "W", 5, season=2026)
        check("most wins in 5 games: declined, naming the unknown game", span.get("declined") and "decision" in span["reason"],
              span.get("reason"))
        k = main._run_pitcher_span("Will Warren", "K", 5, season=2026)
        check("most strikeouts in 5 games: answered (an unknown decision doesn't touch K)",
              not k.get("declined") and k["span"]["total"] > 0, k.get("reason"))
        rows = main._pitcher_leaderboard_rows(WARREN, games, {2026})
        spans = {(r["window"], r["event"]): r["value"] for r in rows if r["metric"] == "span"}
        check("stored span boards: W and SV rows hold NULL, K rows a number",
              all(v is None for (w, e), v in spans.items() if e in ("W", "SV"))
              and all(v is not None for (w, e), v in spans.items() if e == "K"), spans)
        line = main._pitcher_stat_line(games)
        check("stat line: W/L/SV '—' (None); IP, SO, ERA stand",
              line["W"] is None and line["L"] is None and line["SV"] is None and line["SO"] and line["ERA"] is not None, line)

        print("milestones (the season gate)")
        m11 = main._run_pitcher_milestone("Will Warren", "W", 11, season=2026)
        check("11th win: answered — the game logs' 11 wins equal the season's 11",
              m11.get("milestone", {}) and m11["milestone"].get("reached") and not m11.get("declined"), m11)
        m6 = main._run_pitcher_milestone("Will Warren", "L", 6, season=2026)
        check("6th loss: declined — the logs know 5 of the season's 6", m6.get("declined"), m6)
    finally:
        main.connection.get_session = patched["get_session"]
        main.connection.db_available = patched["db_available"]
        main._resolve_retro_id = patched["_resolve_retro_id"]
    eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
