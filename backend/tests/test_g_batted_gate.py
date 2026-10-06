#!/usr/bin/env python3
"""The streak/span completeness gate gives the same answer before and after
batting G becomes every appearance.

⚠️ WHY THIS EXISTS. A season counts as complete for streaks and spans when the
game-log rows cover every game in G (`_complete_seasons`, and the same gate in the
bulk `_backfill_leaderboard_core`). The game logs hold only the games a man batted
in. G used to count only those too; it now counts every appearance — the defensive
inning, the pinch-run, the relief outing without a turn at bat — so that Tommie
Aaron's 1962 reads 141 games, not 115. Gate on that G and a season with no
missing game looks short: measured on the Retrosheet store, 41,357 seasons would
flip to incomplete and lose their streaks and spans. The gate reads
COALESCE(G_batted, G), and G_batted keeps the batting-game count.

Three states, same players:
  BEFORE  G = batting games, G_batted NULL  (today's rows)
  AFTER   G = every appearance, G_batted = batting games
  BROKEN  AFTER's rows read through a gate on G alone — must differ, or this test
          could not see the bug it guards against.

It also checks the Retrosheet stint ingest is insert-only: a re-ingest must not put a
CSV's G back over a corrected stint, and must still add a stint the store lacks.

`_complete_seasons` runs on in-memory SQLite. The bulk path uses Postgres
`ANY(:ids)`, so it runs on an embedded local Postgres (`pip install pgserver`); without
it that half is reported SKIPPED and the script exits 2, never a silent pass.
Run: <backend python> backend/tests/test_g_batted_gate.py
"""
import contextlib
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "scripts")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
from sqlalchemy import create_engine, text                         # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
import main                                                        # noqa: E402
from database.models import Base, BattingGameLog, Player, PlayerSeason, PlayerSeasonStint   # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# (player, season): (batting games in the logs, batting games G counts, every appearance)
SEASONS = {
    (1, 2001): (10, 10, 14),   # complete: logs cover every batting game; 4 defensive-only games
    (1, 2002): (8, 9, 12),     # incomplete: one batting game missing from the logs
    (1, 2003): (5, 5, 5),      # no appearance-only games: the two counts agree
    (2, 2001): (12, 12, 30),   # a pitcher who batted 12 times in 30 games
}
LAHMAN_ONLY = {(3, 1880): 50}  # a Lahman row: no logs, no G_batted — never complete


def load(S, state):
    with S() as s:
        s.query(BattingGameLog).delete(); s.query(PlayerSeason).delete(); s.query(Player).delete()
        for pid in (1, 2, 3):
            s.add(Player(player_id=pid, name=f"Player {pid}"))
        for (pid, yr), (logs, batted, every) in SEASONS.items():
            g, gb = (batted, None) if state == "before" else (every, batted)
            s.add(PlayerSeason(player_id=pid, year=yr, G=g, G_batted=gb, AB=4 * logs, H=logs))
            day = datetime.date(yr, 4, 1)
            for n in range(logs):           # one hit a game: every complete season is one streak
                s.add(BattingGameLog(player_id=pid, game_id=f"retro-T{yr}{pid}{n:03d}", season=yr,
                                     game_date=day + datetime.timedelta(days=n), AB=4, H=1, HR=n % 2))
        for (pid, yr), g in LAHMAN_ONLY.items():
            s.add(PlayerSeason(player_id=pid, year=yr, G=g))
        s.commit()


def session_factory(S):
    @contextlib.contextmanager
    def get_session():                 # as connection.get_session: commit on a clean exit
        s = S()
        try:
            yield s
            s.commit()
        finally:
            s.close()
    return get_session


_orig_get_session, _orig_gate, _orig_rows = main.connection.get_session, main._GATE_BATTING_G, main._leaderboard_rows
try:
    # ---- _complete_seasons (SQLite) ----
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng, tables=[Player.__table__, PlayerSeason.__table__, BattingGameLog.__table__, PlayerSeasonStint.__table__])
    S = sessionmaker(bind=eng)
    main.connection.get_session = session_factory(S)
    print("_complete_seasons")
    out = {}
    for state in ("before", "after"):
        load(S, state)
        out[state] = {pid: main._complete_seasons(pid)[0] for pid in (1, 2, 3)}
    check("player 1: {2001, 2003} complete before", out["before"][1] == {2001, 2003})
    check("player 1: the same seasons complete after G becomes every appearance", out["after"][1] == out["before"][1])
    check("player 2 (pitcher who batted): complete before and after", out["before"][2] == out["after"][2] == {2001})
    check("a Lahman-only row (no logs, no G_batted) is never complete", out["before"][3] == out["after"][3] == set())
    main._GATE_BATTING_G = '"G"'
    broken = {pid: main._complete_seasons(pid)[0] for pid in (1, 2)}
    main._GATE_BATTING_G = _orig_gate
    check("BROKEN: gated on G alone, the AFTER rows lose complete seasons (the test can see the bug)",
          broken[1] == {2003} and broken[2] == set())

    # ---- _backfill_leaderboard_core (embedded Postgres) ----
    print("_backfill_leaderboard_core (bulk)")
    try:
        import pgserver
    except ImportError:
        pgserver = None
    if pgserver is None:
        print("  [SKIPPED] pgserver not installed — the bulk gate was NOT checked")
        results.append(None)
    else:
        srv = pgserver.get_server(tempfile.mkdtemp(prefix="g_batted_gate_"), cleanup_mode="stop")
        uri = srv.get_uri()
        assert "host=/" in uri or "@/" in uri, "embedded Postgres must be a local socket"
        peng = create_engine(uri)
        Base.metadata.create_all(peng)
        PS = sessionmaker(bind=peng)
        main.connection.get_session = session_factory(PS)
        seen = {}

        def spy(mlbam, games, complete):
            seen[mlbam] = set(complete)
            return _orig_rows(mlbam, games, complete)
        main._leaderboard_rows = spy
        bulk = {}
        for state in ("before", "after", "broken"):
            load(PS, "before" if state == "before" else "after")
            if state == "broken":
                main._GATE_BATTING_G = '"G"'
            seen.clear()
            summary = main._backfill_leaderboard_core(confirm=False, limit=None)
            main._GATE_BATTING_G = _orig_gate
            bulk[state] = (dict(seen), {k: summary[k] for k in ("players", "total_rows", "streak_rows", "span_rows", "top_hitting_streaks")})
        check("bulk: the same complete seasons before and after", bulk["before"][0] == bulk["after"][0] == {1: {2001, 2003}, 2: {2001}})
        check("bulk: identical computed rows and top streaks before and after", bulk["before"][1] == bulk["after"][1])
        check("bulk BROKEN: gated on G alone, complete seasons are lost", bulk["broken"][0] == {1: {2003}, 2: set()})
        with PS() as s:
            check("bulk dry run wrote nothing", s.execute(text("SELECT count(*) FROM game_unit_leaderboard")).scalar() == 0)
finally:
    main.connection.get_session, main._GATE_BATTING_G, main._leaderboard_rows = _orig_get_session, _orig_gate, _orig_rows

# ---- the Retrosheet stint ingest never rewrites a stored stint ----
import csv as _csv                                                 # noqa: E402
import retrosheet_ingest as ri                                     # noqa: E402
from database import crud                                          # noqa: E402


def ingest_check(label, S):
    with S() as s:
        s.query(PlayerSeasonStint).delete()
        s.add(PlayerSeasonStint(player_id=1, year=2001, team="NYA", stint_order=1, G=14, G_batted=10, AB=40, H=10, source="retrosheet"))
        s.commit()
    path = os.path.join(tempfile.mkdtemp(prefix="stints_"), "stints.csv")
    cols = ["player_id", "year", "team"] + ri._BAT_STINT_COLS + ["G_batted", "stint_order"]
    with open(path, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=cols); w.writeheader()
        base = {c: 0 for c in cols}
        w.writerow({**base, "player_id": 1, "year": 2001, "team": "NYA", "G": 99, "G_batted": 99, "AB": 1, "stint_order": 1})
        w.writerow({**base, "player_id": 1, "year": 2001, "team": "BOS", "G": 6, "G_batted": 0, "stint_order": 2})
    ri.connection.get_session = session_factory(S)
    run = lambda: ri._ingest_stints(path, ri._BAT_STINT_COLS, False, crud.insert_absent_player_season_stints, None, None, None, "batting_stints_upserted", pa_fallback=True)
    first, second = run(), run()
    with S() as s:
        nya = s.get(PlayerSeasonStint, (1, 2001, "NYA")); bos = s.get(PlayerSeasonStint, (1, 2001, "BOS"))
        check(f"{label}: re-ingest leaves the stored stint's G and G_batted (14 / 10) and AB untouched",
              (nya.G, nya.G_batted, nya.AB) == (14, 10, 40))
        check(f"{label}: a stint the store lacks is inserted (BOS, 6 G, G_batted 0)", bos is not None and (bos.G, bos.G_batted) == (6, 0))
        check(f"{label}: the summary counts it — first run 1 inserted / 1 present, second run 0 / 2",
              (first["inserted"], first["already_present"], second["inserted"], second["already_present"]) == (1, 1, 0, 2))
    with S() as s:
        crud.save_player_season_stints(s, [{"player_id": 1, "year": 2001, "team": "NYA", "G": 15}]); s.commit()
        check(f"{label}: the nightly's saver still rewrites (current-season stints are meant to be)",
              s.get(PlayerSeasonStint, (1, 2001, "NYA")).G == 15)


_orig_ri_session = ri.connection.get_session
try:
    print("Retrosheet stint ingest")
    ingest_check("sqlite", S)
    if pgserver is not None:
        ingest_check("postgres", PS)
    else:
        print("  [SKIPPED] pgserver not installed — the Postgres ON CONFLICT path was NOT checked")
        results.append(None)
finally:
    ri.connection.get_session = _orig_ri_session

src_ingest = open(os.path.join(HERE, "..", "scripts", "retrosheet_ingest.py")).read()
check("the Retrosheet ingest calls only the insert-only stint savers",
      "crud.insert_absent_player_season_stints" in src_ingest and "crud.insert_absent_pitcher_season_stints" in src_ingest
      and "crud.save_player_season_stints" not in src_ingest and "crud.save_pitcher_season_stints" not in src_ingest)

print("both gates read the same expression")
src = open(os.path.join(HERE, "..", "api", "main.py")).read()
check("_complete_seasons and the bulk backfill both gate on _GATE_BATTING_G",
      src.count("SUM({_GATE_BATTING_G}) FROM player_seasons") == 2 and 'SUM("G") FROM player_seasons' not in src)

ran = [r for r in results if r is not None]
print(f"\n{sum(ran)}/{len(ran)} passed" + (f", {results.count(None)} SKIPPED" if None in results else ""))
sys.exit(0 if all(ran) and None not in results else (2 if all(ran) else 1))
