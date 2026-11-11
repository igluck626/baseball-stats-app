#!/usr/bin/env python3
"""The manual admin season tools find Lahman rows through the JUDGED bridge and
label every row they create (queued after audit items 2 + 4, Isaac 2026-11-01).

⚠️ THE CHECKS THAT MATTER:
  • reload-player-lahman loads by the judged Lahman playerID, never the bio's
    bbref_id. Matt Manning's bbref id is mannima02, which is MAX Manning's
    Lahman playerID: keyed by bbref id, Matt got Max's 1939-41 Negro-League
    seasons.
  • No judged playerID (held, or not in Lahman): 409, nothing written.
  • repair-swapped-player stops BEFORE ANY WRITE when the judged bridge
    disagrees with correct_bbref (its phase D deletes every season row first).
  • reset-player-season / seed-pitcher-season stubs say 'bdl'; Lahman rows say
    'lahman'.

Real Lahman + Chadwick files; no database (sessions are replaced, and a
session opened where none may be fails the check). Standalone, no pytest.
Run: <backend python> backend/tests/test_admin_season_tools.py
"""
import contextlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "scripts")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
from fastapi import HTTPException                                  # noqa: E402
import main                                                        # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


class _Row:
    def __init__(self, **kw): self.__dict__.update(kw)


class _Q:
    def __init__(self, first=None): self._first = first
    def filter(self, *a): return self
    def first(self): return self._first
    def delete(self, **kw): return 0
    def count(self): return 0


class _DB:
    """Answers bio lookups from `bios` {Model: row}; records adds and saves."""
    def __init__(self, bios=None): self.bios, self.added = bios or {}, []
    def query(self, model): return _Q(self.bios.get(model))
    def get(self, model, pk): return self.bios.get(model)
    def add(self, row): self.added.append(row)
    def delete(self, row): pass
    def commit(self): pass


def patched(db, saved=None, forbid=False):
    @contextlib.contextmanager
    def session():
        if forbid:
            raise AssertionError("a session was opened")
        yield db
    main.connection.get_session = session
    main.connection.db_available = lambda: True
    if saved is not None:
        main.crud.save_player_seasons = lambda d, pid, rows: saved.setdefault("bat", []).extend(rows)
        main.crud.save_pitcher_seasons = lambda d, pid, rows: saved.setdefault("pit", []).extend(rows)


real = (main.connection.get_session, main.connection.db_available,
        main.crud.save_player_seasons, main.crud.save_pitcher_seasons)
try:
    print("judged ids")
    check("Matt Manning 666159 -> ['mannima01']", main._judged_lahman_ids(666159) == ["mannima01"],
          main._judged_lahman_ids(666159))
    check("B.J. Surhoff 122989 -> ['surhobj01']", main._judged_lahman_ids(122989) == ["surhobj01"],
          main._judged_lahman_ids(122989))

    print("reload-player-lahman")
    saved: dict = {}
    patched(_DB({main.Pitcher: _Row(bbref_id="mannima02")}), saved)
    out = main.admin_reload_player_lahman(666159)
    years = out["years_pitching"]
    check("Matt Manning loads by mannima01, not his bbref id mannima02",
          out["lahman_id"] == "mannima01" and years and min(years) >= 2019, out)
    check("no 1939-41 Negro-League season (Max Manning's) on Matt", not set(years) & {1939, 1940, 1941}, years)
    check("every reloaded row says 'lahman'",
          saved.get("pit") and all(r.get("source") == "lahman" for r in saved["pit"]), saved.get("pit", [])[:1])

    unmapped = 999999999
    saved = {}
    patched(_DB({main.Player: _Row(bbref_id="mannima01")}), saved)
    try:
        main.admin_reload_player_lahman(unmapped)
        check("no judged playerID: 409", False, "no stop")
    except HTTPException as e:
        check("no judged playerID: 409, even with a bbref_id that IS a Lahman id", e.status_code == 409, e.detail)
    check("... and nothing saved", not saved, saved)

    print("repair-swapped-player")
    patched(_DB(), forbid=True)
    try:
        main.admin_repair_swapped_player(real_mlbam=666159, correct_bbref="mannima02", bogus_mlbam=None,
                                         position_type="pitcher", position=None, skip_lahman_reload=False)
        check("bridge disagrees: 409", False, "no stop")
    except HTTPException as e:
        check("Matt Manning with correct_bbref mannima02: 409", e.status_code == 409, e.detail)
    except AssertionError as e:
        check("Matt Manning with correct_bbref mannima02: stops before any session", False, e)

    print("stub rows")
    db = _DB()
    patched(db)
    main.admin_reset_player_season(1, season=2026, team_code="ATH")
    check("reset-player-season stub says 'bdl'", len(db.added) == 1 and db.added[0].source == "bdl",
          [getattr(r, "source", None) for r in db.added])
    db = _DB()
    patched(db)
    main.admin_seed_pitcher_season(1, year=2026, team_code="CLE")
    check("seed-pitcher-season stub says 'bdl'", len(db.added) == 1 and db.added[0].source == "bdl",
          [getattr(r, "source", None) for r in db.added])
finally:
    (main.connection.get_session, main.connection.db_available,
     main.crud.save_player_seasons, main.crud.save_pitcher_seasons) = real

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if results and all(results) else 1)
