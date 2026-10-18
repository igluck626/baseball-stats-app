#!/usr/bin/env python3
"""The nightly's per-team (stint) derivation and the roster's team-only lines
under Isaac's "activity" rule (2026-10-16).

⚠️ THE CHECKS THAT MATTER:
  • A per-team value is NULL when any game behind it has the cell unknown and
    shows activity (AB or BB above 0; every pitching game). It used to be the
    silent 0 of `int(x or 0)`: Collins's Royals row read PA 484, Heuer's
    Guardians row HBP 0 and WP 0.
  • An unknown on a no-AB/no-BB game reads as 0 (Billy Cook keeps a number).
  • TB is unknown when doubles or triples are.
  • A team group with no batting lines (a pitcher's pitching-only games) sums
    to 0, never NULL — a bare SUM's NULL once blanked 930 rows' AB, Ohtani's
    Dodgers row among them.
  • The roster: a team line with an unknown, or a season row with one, never
    "adds up" — it falls back to "All teams", never a number on a silent 0.

Real rows (fixtures/stint_activity.json): the three players' 2026 game logs,
one real row from the opposing side of each of their games, their season rows.
Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_stint_activity.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "scripts")]
os.environ.pop("DATABASE_URL", None)

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "stint_activity.json")))
COLLINS, COOK, HEUER = 686555, 695257, 676051

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the derivation is Postgres SQL)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="stint_activity_"), cleanup_mode="stop")
    os.environ["DATABASE_URL"] = srv.get_uri()
    from sqlalchemy import create_engine                            # noqa: E402
    from sqlalchemy.orm import Session                               # noqa: E402
    from database.models import Base, BattingGameLog, PitchingGameLog  # noqa: E402
    import nightly_update as nu                                      # noqa: E402
    import team_page                                                 # noqa: E402
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[BattingGameLog.__table__, PitchingGameLog.__table__])
    with eng.begin() as c:
        for m, rows in ((BattingGameLog, FX["own_batting"] + FX["opponent_rows"]), (PitchingGameLog, FX["own_pitching"])):
            cols = {col.name for col in m.__table__.columns}
            for r in rows:
                c.execute(m.__table__.insert().values({k: v for k, v in r.items() if k in cols}))
    with Session(eng) as db:
        bat, _ = nu._derive_stints(db, 2026, False)
        pit, _ = nu._derive_stints(db, 2026, True)
    by = {(r["player_id"], r["team"]): r for r in bat + pit}

    print("per-team rows")
    col = by.get((COLLINS, "KCA"))
    check("Collins (Royals): PA, HBP, SF, SB, CS, doubles unknown — not the silent 0 behind PA 484",
          col is not None and all(col[k] is None for k in ("PA", "HBP", "SF", "SB", "CS", "doubles")), col)
    check("Collins: AB, H, HR (never unknown) are still sums, and TB is unknown with doubles",
          col["AB"] is not None and col["H"] is not None and col["TB"] is None, (col["AB"], col["TB"]))
    cook = by.get((COOK, "PIT"))
    want = sum((r["PA"] or 0) for r in FX["own_batting"] if r["player_id"] == COOK)
    check("Cook (Pirates): his unknowns are all on no-AB/no-BB games — PA is a number",
          cook is not None and cook["PA"] is not None and cook["PA"] <= want, (cook and cook["PA"], want))
    heu = [r for r in pit if r["player_id"] == HEUER]
    check("Heuer: an unknown HBP in a game he pitched makes his team row's HBP unknown (was 0)",
          heu and all(r["HBP"] is None for r in heu), [(r["team"], r["HBP"]) for r in heu])
    check("Heuer: WP, never shipped by balldontlie, is unknown (was 0)",
          heu and all(r["WP"] is None for r in heu), [(r["team"], r["WP"]) for r in heu])
    check("Heuer: innings and earned runs still sum", heu and all(r["IP"] is not None and r["ER"] is not None for r in heu))
    hb = [r for r in bat if r["player_id"] == HEUER]
    check("Heuer's batting row (pitching games only, no batting lines): AB and H are 0, not unknown",
          hb and all(r["AB"] == 0 and r["H"] == 0 and r["PA"] == 0 for r in hb), [(r["team"], r["AB"], r["PA"]) for r in hb])

    print("roster team-only lines")
    std = lambda **kw: {"standard": {"AB": 100, "H": 30, "doubles": 5, "triples": 1, "HR": 4, "RBI": 12,
                                     "BB": 10, "HBP": 2, "SF": 1, **kw}, "advanced": {}}
    st = lambda team, **kw: {"team": team, "AB": 50, "H": 15, "doubles": 2, "triples": 0, "HR": 2, "RBI": 6,
                             "BB": 5, "HBP": 1, "SF": 0, "G": 20, **kw}
    two = [st("NYA"), st("BOS", doubles=3, triples=1, HBP=1, SF=1)]
    scope, line = team_page.roster_stats("NYA", two, std(), pitcher=False)
    check("two teams that add up: this team's line", scope == "team" and line["g"] == 20, (scope, line))
    scope, _ = team_page.roster_stats("NYA", [two[0], dict(two[1], HBP=None)], std(), pitcher=False)
    check("one team row with an unknown HBP: falls back to All teams", scope == "all_teams", scope)
    scope, _ = team_page.roster_stats("NYA", two, std(HBP=None), pitcher=False)
    check("a season row with an unknown HBP: falls back to All teams (it used to pass as 0 = 0)",
          scope == "all_teams", scope)
    eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
