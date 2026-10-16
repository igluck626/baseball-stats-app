#!/usr/bin/env python3
"""The cross-format gamelog dedup (database/connection.py
dedupe_gamelog_duplicates) must never delete a doubleheader game.

⚠️ THE CHECKS THAT MATTER:
  • Chase Shugart, 2026-04-30, SF @ PHI doubleheader: one row under the legacy
    gamePk 823472, one under balldontlie's 5058234 — two DIFFERENT games. The
    old query deleted one of them (shown below, against the same rows); the
    new one keeps both, with or without schedule evidence.
  • Dylan Lee and Michael Petersen, 2026-05-19, ATL vs MIA (one game): legacy
    823865 and balldontlie 5058486 are the SAME game. With the schedule as
    evidence, one row goes (the one with fewer recorded stats); without
    evidence, nothing is deleted.
  • Two balldontlie ids, two gamePks, or three rows on one day are never
    candidates.
  • The schedule evidence uses the Eastern date, and answers to both the
    abbreviation and the full name (our opponent column holds full names
    before 2026-05-19).

Real data (fixtures/dedup_cases.json): balldontlie /games and /stats rows
kept whole, and our pitching_gamelogs rows as stored, read 2026-10-16.

Needs an embedded Postgres (`pip install pgserver`): the dedup is
Postgres-only, so without it this test FAILS rather than skipping.

Standalone, no pytest. Run: <backend python> backend/tests/test_gamelog_dedup.py
"""
import copy
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
from sqlalchemy import create_engine, text                         # noqa: E402
from database import connection                                     # noqa: E402
from database.models import Base, PitchingGameLog                   # noqa: E402
import data_service as ds                                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "dedup_cases.json")))
BDL_TO_OURS = {p["bdl_id"]: p["player_id"] for p in FX["players"]}
SHUGART, LEE, PETERSEN = 663767, 669276, 656848
COLS = [c.name for c in PitchingGameLog.__table__.columns]
Q = connection._PITCHING_GAMELOGS_QUALITY_COLUMNS

# The query that ran until build 18, verbatim but for the table name.
OLD_SQL = """
    WITH ranked AS (
        SELECT ctid,
               ROW_NUMBER() OVER (PARTITION BY player_id, game_date
                                  ORDER BY {q} DESC, ctid ASC) AS rn,
               MIN(CAST(game_id AS BIGINT)) OVER (PARTITION BY player_id, game_date) AS min_game_id
        FROM pitching_gamelogs WHERE game_id ~ '^[0-9]+$'
    )
    DELETE FROM pitching_gamelogs t USING ranked r
    WHERE t.ctid = r.ctid AND r.rn > 1 AND r.min_game_id < 1000000
"""


def bdl_rows():
    """Our rows for the fixture's balldontlie lines, through the real parser."""
    out = []
    for gid, stats in FX["stats"].items():
        ctx = ds._bdl_game_ctx(FX["games"][gid])
        for st in stats:
            row = ds._parse_bdl_pitching_gamelog(st, ctx)
            assert row is not None, (gid, st["player"]["id"])
            row["player_id"] = BDL_TO_OURS[st["player"]["id"]]
            out.append(row)
    return out


def load(eng, rows):
    with eng.begin() as c:
        c.execute(text("TRUNCATE pitching_gamelogs"))
        for r in rows:
            r = {k: r.get(k) for k in COLS}
            c.execute(text(f"INSERT INTO pitching_gamelogs ({', '.join(chr(34) + k + chr(34) for k in COLS)}) "
                           f"VALUES ({', '.join(':' + k for k in COLS)})"), r)


def held(eng):
    with eng.connect() as c:
        return sorted((r[0], str(r[1]), r[2]) for r in c.execute(text(
            "SELECT player_id, game_date, game_id FROM pitching_gamelogs")))


def evidence(*days):
    return connection.single_game_team_dates([g for d in days for g in FX["slates"][d]])


print("schedule evidence")
ev = evidence("2026-04-30")
check("SF and PHI played twice on 04-30: neither is a single-game team",
      not {("2026-04-30", "SF"), ("2026-04-30", "PHI"), ("2026-04-30", "San Francisco Giants"),
           ("2026-04-30", "Philadelphia Phillies")} & ev)
ev = evidence("2026-05-19")
check("ATL and MIA played once on 05-19, under the abbreviation and the full name",
      {("2026-05-19", "ATL"), ("2026-05-19", "MIA"), ("2026-05-19", "Atlanta Braves"),
       ("2026-05-19", "Miami Marlins")} <= ev)
late = copy.deepcopy(FX["games"]["5058486"])
late["date"] = "2026-05-20T01:40:00.000Z"
check("a 9:40 pm Eastern start (01:40Z next day) counts on the Eastern date",
      ("2026-05-19", "ATL") in connection.single_game_team_dates([late]))
dh = [copy.deepcopy(FX["games"]["5058486"]), copy.deepcopy(FX["games"]["5058486"])]
dh[1]["id"] = 1
check("the same club twice in one day is never single-game", not connection.single_game_team_dates(dh))
dh[1]["status"] = "STATUS_POSTPONED"
check("...unless the second listing was postponed",
      ("2026-05-19", "ATL") in connection.single_game_team_dates(dh))

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the dedup is Postgres-only)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="gamelog_dedup_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[PitchingGameLog.__table__])
    orig = connection._engine
    connection._engine = eng
    try:
        stored = {(r["player_id"], r["game_id"]): r for r in FX["our_rows"]}
        parsed = {(r["player_id"], str(r["game_id"])): r for r in bdl_rows()}
        shugart_dh = [stored[(SHUGART, "823472")], parsed[(SHUGART, "5058234")]]
        same_game = [stored[(LEE, "823865")], parsed[(LEE, "5058486")],
                     stored[(PETERSEN, "823865")], parsed[(PETERSEN, "5058486")]]

        print("the old query (before build 18)")
        load(eng, shugart_dh)
        with eng.begin() as c:
            gone = c.execute(text(OLD_SQL.format(q="num_nonnulls(" + ", ".join(f'"{x}"' for x in Q) + ")"))).rowcount
        check("deleted one of Shugart's two doubleheader games — the hazard", gone == 1 and len(held(eng)) == 1, gone)

        print("the doubleheader is kept")
        load(eng, shugart_dh)
        r = connection.dedupe_gamelog_duplicates("pitching_gamelogs", Q)
        check("no evidence: Shugart's pair is a candidate, nothing deleted",
              r["candidates"] == 1 and r["deleted"] == 0 and len(held(eng)) == 2, r)
        r = connection.dedupe_gamelog_duplicates("pitching_gamelogs", Q, single_game_dates=evidence("2026-04-30"))
        check("with 04-30's schedule as evidence: still both games kept",
              r["deleted"] == 0 and len(held(eng)) == 2, r)
        everything = evidence("2026-04-30") | evidence("2026-05-19")
        r = connection.dedupe_gamelog_duplicates("pitching_gamelogs", Q, single_game_dates=everything)
        check("...and with any other day's evidence mixed in", r["deleted"] == 0 and len(held(eng)) == 2, r)

        print("the same game twice is collapsed, only with evidence")
        load(eng, same_game)
        r = connection.dedupe_gamelog_duplicates("pitching_gamelogs", Q)
        check("no evidence: two candidates, nothing deleted", r == {"candidates": 2, "deleted": 0,
              "candidate_dates": ["2026-05-19"]} and len(held(eng)) == 4, r)
        r = connection.dedupe_gamelog_duplicates("pitching_gamelogs", Q, single_game_dates=evidence("2026-05-19"))
        h = held(eng)
        check("with 05-19's schedule: one row each for Lee and Petersen", r["deleted"] == 2 and
              sorted(p for p, _, _ in h) == sorted([LEE, PETERSEN]), (r, h))
        q = sum(v is not None for k, v in stored[(LEE, "823865")].items() if k in Q)
        p = sum(v is not None for k, v in parsed[(LEE, "5058486")].items() if k in Q)
        keep = "5058486" if p >= q else "823865"
        check(f"the kept row is the one with more recorded stats ({keep})",
              all(g == keep for _, _, g in h), (q, p, h))
        r = connection.dedupe_gamelog_duplicates("pitching_gamelogs", Q, single_game_dates=evidence("2026-05-19"))
        check("re-running deletes nothing", r["deleted"] == 0 and r["candidates"] == 0, r)

        print("never candidates")
        two_bdl = copy.deepcopy(parsed[(SHUGART, "5058234")]); two_bdl["game_id"] = "7364165"
        two_legacy = copy.deepcopy(stored[(SHUGART, "823472")]); two_legacy["game_id"] = "823471"
        for label, rows in (("two balldontlie ids", [parsed[(SHUGART, "5058234")], two_bdl]),
                            ("two gamePks", [stored[(SHUGART, "823472")], two_legacy]),
                            ("three rows (gamePk + two balldontlie)", shugart_dh + [two_bdl])):
            load(eng, rows)
            r = connection.dedupe_gamelog_duplicates("pitching_gamelogs", Q,
                                                     single_game_dates={("2026-04-30", "SF"), ("2026-04-30", "PHI"),
                                                                        ("2026-04-30", "San Francisco Giants"),
                                                                        ("2026-04-30", "Philadelphia Phillies")})
            check(f"{label} on one day, even with (false) single-game evidence: untouched",
                  r["candidates"] == 0 and len(held(eng)) == len(rows), r)
        retro = copy.deepcopy(stored[(SHUGART, "823472")]); retro["game_id"] = "PHI202604300"
        load(eng, [retro, parsed[(SHUGART, "5058234")]])
        r = connection.dedupe_gamelog_duplicates("pitching_gamelogs", Q, single_game_dates=evidence("2026-05-19"))
        check("a Retrosheet-style id is never considered", r["candidates"] == 0 and len(held(eng)) == 2, r)
    finally:
        connection._engine = orig
        eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
