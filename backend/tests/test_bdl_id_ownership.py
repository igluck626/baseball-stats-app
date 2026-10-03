#!/usr/bin/env python3
"""A balldontlie id belongs to one of our players. Never two.

⚠️ WHY THIS EXISTS. Every stats writer goes per player: our id -> its bdl_id ->
balldontlie's line. When two of our ids carry the same bdl_id, both are written
the same line, every night. That is how a retired José Ramírez (542432, a
pitcher, last pitched 2018) came to hold the first 60 games of the Guardians'
José Ramírez (608070), and how two retired Josh Smiths held the Blue Jays Josh
Smith's April. The reverse map the game-log writer uses (bdl_id -> our id)
then keeps whichever row the query happened to return last, so a re-pull under
a corrected stamp lands the same games on the other id and neither copy is
ever removed.

The second stamp came from the bootstrap matcher behind
/admin/build-bdl-player-mapping and /admin/retry-unmapped-bdl-players: it walks
OUR rows with no bdl_id, finds the balldontlie player by name and side, and
stamps him — without asking whether that balldontlie id is already ours on
another row. Run today, it would stamp 965 on 542432 and 225 on both retired
Josh Smiths again (checked against the live search, 2026-10-02).

Standalone, no pytest, offline (in-memory SQLite + a stubbed matcher).
Run: <backend python> backend/tests/test_bdl_id_ownership.py
"""
import contextlib
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
from sqlalchemy import create_engine                              # noqa: E402
from sqlalchemy.orm import sessionmaker                           # noqa: E402
import data_service as ds                                         # noqa: E402
from database.models import Base, Pitcher, Player                 # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def fresh_db(rows):
    """rows: (table, player_id, name, bdl_id, mlb_debut)."""
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng, tables=[Player.__table__, Pitcher.__table__])
    S = sessionmaker(bind=eng)
    with S() as s:
        for t, pid, name, bdl, debut in rows:
            s.add((Player if t == "players" else Pitcher)(player_id=pid, name=name, bdl_id=bdl, mlb_debut=debut))
        s.commit()

    @contextlib.contextmanager
    def get_session():
        s = S()
        try:
            yield s
            s.commit()
        finally:
            s.close()
    return S, get_session


def stamps(S):
    with S() as s:
        return {("players", r.player_id): r.bdl_id for r in s.query(Player)} | \
               {("pitchers", r.player_id): r.bdl_id for r in s.query(Pitcher)}


# What the live balldontlie search returns for these names today.
MATCH = {"Jose Ramirez": {"batter": 965, "pitcher": 3355}, "Josh Smith": {"batter": 225, "pitcher": 225},
         "Gavin Newman": {"batter": 4242, "pitcher": 4242}}

_orig = (ds.connection.get_session, ds.connection.db_available, ds._get_bdl_key, ds._bdl_match_one_player, ds.time.sleep)
try:
    ds.connection.db_available = lambda: True
    ds._get_bdl_key = lambda: "test"
    ds.time.sleep = lambda s: None
    ds._bdl_match_one_player = lambda full_name, side, mlb_debut=None: (
        (MATCH[full_name][side], "matched", []) if full_name in MATCH else (None, "unmatched", []))

    print("the bootstrap matcher does not stamp an id that is already ours")
    S, gs = fresh_db([
        ("players", 608070, "Jose Ramirez", 965, 2013),     # the Guardians' José Ramírez, mapped
        ("players", 542432, "Jose Ramirez", None, 2014),    # the retired pitcher's phantom batter row
        ("pitchers", 542432, "Jose Ramirez", None, 2014),
        ("players", 669701, "Josh Smith", 225, 2022),       # the Blue Jays' Josh Smith, mapped
        ("players", 595001, "Josh Smith", None, 2015),      # retired Josh Smith (b.1987)
        ("pitchers", 595001, "Josh Smith", None, 2015),
        ("players", 605479, "Josh Smith", None, 2019),      # retired Josh D. Smith (b.1989)
        ("pitchers", 605479, "Josh Smith", None, 2019),
        ("players", 700001, "Gavin Newman", None, 2026),    # a genuinely new, unmapped player
    ])
    ds.connection.get_session = gs
    ds.build_bdl_player_mapping(since_year=2010)
    st = stamps(S)
    check("542432's batter row is NOT stamped 965 (608070 holds it)", st[("players", 542432)] is None)
    check("595001 / 605479 are NOT stamped 225 on either side (669701 holds it)",
          all(st[(t, p)] is None for t in ("players", "pitchers") for p in (595001, 605479)))
    check("608070 and 669701 keep their ids", st[("players", 608070)] == 965 and st[("players", 669701)] == 225)
    check("a free id is still stamped (Gavin Newman -> 4242)", st[("players", 700001)] == 4242)
    check("an id free of other owners is still stamped (542432's pitcher row -> 3355)", st[("pitchers", 542432)] == 3355)

    print("one run cannot hand the same free id to two of our rows")
    S, gs = fresh_db([("players", 595001, "Josh Smith", None, 2015), ("players", 605479, "Josh Smith", None, 2019)])
    ds.connection.get_session = gs
    ds.build_bdl_player_mapping(since_year=2010)
    st = stamps(S)
    check("at most one of the two Josh Smiths gets 225", sum(v == 225 for v in st.values()) <= 1)

    print("a writer never resolves a shared id")
    S, gs = fresh_db([("players", 542432, "Jose Ramirez", 965, 2014), ("players", 608070, "Jose Ramirez", 965, 2013),
                      ("players", 671277, "Luis Garcia", 125, 2019)])
    with S() as s:
        m = ds._bdl_to_mlbam_map(s)
    check("bdl 965 on two of our ids maps to neither (no arbitrary winner)", 965 not in m)
    check("an unshared id still maps (125 -> 671277)", m.get(125) == 671277)
    check("a two-way player's id on both sides of ONE id still maps",
          (lambda S2: ds._bdl_to_mlbam_map(S2()))(fresh_db([("players", 660271, "Shohei Ohtani", 70, 2018), ("pitchers", 660271, "Shohei Ohtani", 70, 2018)])[0]).get(70) == 660271)
finally:
    (ds.connection.get_session, ds.connection.db_available, ds._get_bdl_key, ds._bdl_match_one_player, ds.time.sleep) = _orig

print("the per-player writers drop a shared id too")
NIGHTLY = os.path.join(HERE, "..", "scripts", "nightly_update.py")
src = open(NIGHTLY).read()
check("the nightly builds its per-player bdl maps through the shared-id filter",
      src.count("_without_shared_bdl_ids(") >= 3)
check("the name-only stamper that started this is gone", not hasattr(ds, "_stamp_bdl_id_by_name"))
main_src = open(os.path.join(HERE, "..", "api", "main.py")).read()
seg = main_src[main_src.index("def admin_set_bdl_id"):main_src.index("def admin_set_player_active")]
check("/admin/set-bdl-id refuses an id another player holds", "_bdl_id_owners(" in seg and "409" in seg)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
