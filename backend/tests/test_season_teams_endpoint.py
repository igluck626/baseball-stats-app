#!/usr/bin/env python3
"""GET /players/{id}/season-teams — the player page's "All teams" row and the
club rows under it (api/season_teams.py).

⚠️ THE CHECKS THAT MATTER (Isaac, 2026-10-19):
  • a finished season comes from player_season_teams, with its source;
  • a club value is sent only where it is verified (adds up to the season row);
    every other value is null, never 0 — Lahman's 1902 has no PA or CS;
  • a traded season with no exact split still names its clubs, every value null;
  • the current season is checked live against the nightly stints with the same
    rule, and a stint that stops adding up drops the breakdown.

Real rows (fixtures/season_teams_endpoint.json), verbatim from a local copy of
production with the approved dry run's club rows loaded: J. D. Martinez 2017
(Retrosheet), Jimmy Sheckard 1902 (Lahman), Minnie Minoso 1951 (no breakdown),
Luis Arraez 2026 (nightly). Needs an embedded Postgres (`pip install pgserver`);
FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_season_teams_endpoint.py
"""
import contextlib
import copy
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
from database.models import (Base, PlayerSeason, PlayerSeasonStint, PlayerSeasonTeam,  # noqa: E402
                             PitcherSeason, PitcherSeasonStint, PitcherSeasonTeam)

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "season_teams_endpoint.json")))
MARTINEZ, SHECKARD, MINOSO, ARRAEZ = 502110, 122100, 119176, 650333
TABLES = {"player_seasons": PlayerSeason, "player_season_stints": PlayerSeasonStint,
          "player_season_teams": PlayerSeasonTeam}

try:
    import pgserver
except ImportError:
    pgserver = None


def load(fx):
    """A fresh database holding `fx`'s rows; returns a get_session for it."""
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="season_teams_endpoint_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[m.__table__ for m in (*TABLES.values(), PitcherSeason,
                                                              PitcherSeasonStint, PitcherSeasonTeam)])
    with eng.begin() as c:
        for t, m in TABLES.items():
            cols = {col.name for col in m.__table__.columns}
            for r in fx[t]:
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
    return eng, get_session


def season(payload, year):
    return next((s for s in payload["batting"] if s["year"] == year), None)


def season_row(pid, year):
    return next(r for r in FX["player_seasons"] if r["player_id"] == pid and r["year"] == year)


if pgserver is None:
    check("pgserver installed (the endpoint reads Postgres)", False, "pip install pgserver")
else:
    eng, get_session = load(FX)
    patched = (main.connection.get_session, main.connection.db_available)
    main.connection.get_session = get_session
    main.connection.db_available = lambda: True
    try:
        print("Retrosheet: J. D. Martinez 2017")
        p = main.player_season_teams(MARTINEZ)
        s = season(p, 2017)
        check("source retrosheet, clubs DET then ARI", s and s["source"] == "retrosheet"
              and [t["team"] for t in s["teams"]] == ["DET", "ARI"], s)
        check("HR 16 + 29, the season's 45", [t["stats"]["HR"] for t in s["teams"]] == [16, 29]
              and season_row(MARTINEZ, 2017)["HR"] == 45)
        sent = [c for c in s["teams"][0]["stats"] if s["teams"][0]["stats"][c] is not None]
        check("every value sent adds up to the season row",
              all(sum(t["stats"][c] for t in s["teams"]) == season_row(MARTINEZ, 2017)[c] for c in sent), sent)
        check("no pitching seasons for a batter", p["pitching"] == [])

        print("Lahman: Jimmy Sheckard 1902")
        s = season(main.player_season_teams(SHECKARD), 1902)
        check("source lahman, clubs BLA then BRO", s and s["source"] == "lahman"
              and [t["team"] for t in s["teams"]] == ["BLA", "BRO"], s)
        check("PA and CS null on both clubs (Lahman doesn't record them) — never 0",
              all(t["stats"]["PA"] is None and t["stats"]["CS"] is None for t in s["teams"]))
        check("AB 15 + 486 = the season's 501",
              [t["stats"]["AB"] for t in s["teams"]] == [15, 486] and season_row(SHECKARD, 1902)["AB"] == 501)

        print("No breakdown: Minnie Minoso 1951")
        s = season(main.player_season_teams(MINOSO), 1951)
        check("no source, the clubs named (CLE, CHA)", s and s["source"] is None
              and [t["team"] for t in s["teams"]] == ["CLE", "CHA"], s)
        check("every value null", all(v is None for t in s["teams"] for v in t["stats"].values()))

        print("Current season: Luis Arraez 2026")
        s = season(main.player_season_teams(ARRAEZ), 2026)
        check("source nightly, clubs SFN then PHI", s and s["source"] == "nightly"
              and [t["team"] for t in s["teams"]] == ["SFN", "PHI"], s)
        check("G 105 + 46 = the season's 151",
              [t["stats"]["G"] for t in s["teams"]] == [105, 46] and season_row(ARRAEZ, 2026)["G"] == 151)
    finally:
        main.connection.get_session, main.connection.db_available = patched
    eng.dispose()

    print("a 2026 stint that stops adding up")
    bad = copy.deepcopy(FX)
    for r in bad["player_season_stints"]:
        if r["player_id"] == ARRAEZ and r["team"] == "PHI":
            r["H"] += 1
    eng, get_session = load(bad)
    main.connection.get_session, main.connection.db_available = get_session, (lambda: True)
    try:
        s = season(main.player_season_teams(ARRAEZ), 2026)
        check("no breakdown: clubs named, every value null",
              s and s["source"] is None and [t["team"] for t in s["teams"]] == ["SFN", "PHI"]
              and all(v is None for t in s["teams"] for v in t["stats"].values()), s)
    finally:
        main.connection.get_session, main.connection.db_available = patched
    eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
