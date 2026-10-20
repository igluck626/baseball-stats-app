#!/usr/bin/env python3
"""A postseason line stored before its player was mapped is attributed once the
mapping lands (`postseason_ingest.resolve_unmapped`), and the ingest maps
through the balldontlie id aliases.

⚠️ THE CHECKS THAT MATTER:
  • An unmapped player's lines are stored with player_id NULL; when his id is
    mapped later, resolve sets player_id on exactly those rows, and a second
    run changes nothing.
  • An alias id's line resolves to the player it is an alias of.
  • A player's own id and an alias with a line in the same game count as ONE
    appearance: the line with more known fields is attributed, the other stays
    stored with player_id NULL (never summed, never deleted), and resolve never
    turns it into a second appearance.

Real game: CHW @ HOU, AL Wild Card Game 1, 2026-09-29
(fixtures/bdl_postseason_game_15457178.json, balldontlie wire shape). Mac
Hendrickson (bdl 6113194) is the unmapped pitcher. The alias case reuses Shane
Smith's real line under a second id, with fewer fields known, the way Seth
Halvorsen's two records arrived (data/bdl_id_aliases.json).
Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_postseason_resolve.py
"""
import contextlib
import copy
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
from sqlalchemy import create_engine, text                         # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
import postseason_ingest as pi                                     # noqa: E402
from database import crud                                          # noqa: E402
from database.models import Base, PostseasonBattingGameLog, PostseasonPitchingGameLog   # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FIX = json.load(open(os.path.join(HERE, "fixtures", "bdl_postseason_game_15457178.json")))
HENDRICKSON, ALIAS = 6113194, 999001
ids = sorted({s["player"]["id"] for s in FIX["stats"]})
everyone = {b: 900000 + i for i, b in enumerate(ids)}
before = {b: p for b, p in everyone.items() if b != HENDRICKSON}

print("the alias rule in build_rows")
smith = next(s for s in FIX["stats"] if s["player"]["last_name"] == "Smith" and s.get("ip") is not None)
sparse = copy.deepcopy(smith)
sparse["player"]["id"] = ALIAS
for k in ("pitch_count", "strikes", "batters_faced", "wild_pitches", "pitching_hbp"):
    sparse[k] = None
amap = {**before, ALIAS: before[smith["player"]["id"]]}
_, pit2, _ = pi.build_rows(FIX["game"], "WC", FIX["stats"] + [sparse], amap)
mine = [r for r in pit2 if r["bdl_player_id"] in (smith["player"]["id"], ALIAS)]
check("own id and alias in one game: both lines stored", len(mine) == 2, mine)
check("  ...the more-known line (his own) is attributed, the alias line is not",
      [r["bdl_player_id"] for r in mine if r["player_id"] is not None] == [smith["player"]["id"]], mine)

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the postseason tables are Postgres)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="ps_resolve_"), cleanup_mode="stop")
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[PostseasonBattingGameLog.__table__, PostseasonPitchingGameLog.__table__])
    S = sessionmaker(bind=eng)

    @contextlib.contextmanager
    def session():
        s = S()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    def count(sql):
        with session() as db:
            return db.execute(text(sql)).scalar()

    print("an unmapped player, mapped later")
    bat, pit, unmapped = pi.build_rows(FIX["game"], "WC", FIX["stats"] + [sparse], amap)
    with session() as db:
        crud.bulk_insert_postseason(db, PostseasonBattingGameLog, bat)
        crud.bulk_insert_postseason(db, PostseasonPitchingGameLog, pit)
    check("stored with player_id NULL: Hendrickson and the alias line",
          count("SELECT count(*) FROM postseason_pitching_gamelogs WHERE player_id IS NULL") == 2
          and unmapped == {HENDRICKSON})
    with session() as db:
        out = pi.resolve_unmapped(db, before)
    check("nothing to resolve while he is still unmapped (the alias line's player is taken)",
          out == {"postseason_batting_gamelogs": 0, "postseason_pitching_gamelogs": 0}, out)
    now = {**everyone, ALIAS: everyone[smith["player"]["id"]]}
    with session() as db:
        out = pi.resolve_unmapped(db, now)
    check("his mapping lands: exactly his pitching row is attributed",
          out == {"postseason_batting_gamelogs": 0, "postseason_pitching_gamelogs": 1}, out)
    check("  ...to his player id", count(f"SELECT player_id FROM postseason_pitching_gamelogs "
                                         f"WHERE bdl_player_id = {HENDRICKSON}") == everyone[HENDRICKSON])
    check("the alias line stays unattributed: one appearance for Smith",
          count(f"SELECT count(*) FROM postseason_pitching_gamelogs WHERE player_id = "
                f"{everyone[smith['player']['id']]}") == 1
          and count(f"SELECT player_id FROM postseason_pitching_gamelogs WHERE bdl_player_id = {ALIAS}") is None)
    with session() as db:
        out = pi.resolve_unmapped(db, now)
    check("a second run changes nothing", sum(out.values()) == 0, out)

    print("an alias id alone")
    with session() as db:
        db.execute(text(f"DELETE FROM postseason_pitching_gamelogs WHERE bdl_player_id = {smith['player']['id']}"))
    with session() as db:
        out = pi.resolve_unmapped(db, now)
    check("with no line under his own id, the alias line resolves to him",
          out["postseason_pitching_gamelogs"] == 1
          and count(f"SELECT player_id FROM postseason_pitching_gamelogs WHERE bdl_player_id = {ALIAS}")
          == everyone[smith["player"]["id"]], out)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
