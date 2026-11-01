#!/usr/bin/env python3
"""Every builder that can INSERT a season row labels its source (audit item 4,
Isaac 2026-11-01). The upsert only writes the columns an entry carries, so an
entry without "source" leaves a NEW row's source NULL — 345 current-season
rows on 2026-11-01, made by the nightly and the roster sync.

⚠️ THE CHECKS THAT MATTER:
  • nightly batter + pitcher entries, and the per-player BDL backfill: 'bdl'
  • the roster sync's team-only placeholder rows: 'bdl'
  • the Lahman loader's batting + pitching rows: 'lahman'

No network, no database: BDL calls and saves are replaced. Standalone, no pytest.
Run: <backend python> backend/tests/test_season_source_labels.py
"""
import contextlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "scripts"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "api")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
import data_service as ds                                         # noqa: E402
import nightly_update as nu                                       # noqa: E402
import lahman_load as ll                                          # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


print("current season (nightly)")
bat = nu._build_current_batter_entry(1, 2, 2026, bdl_stats={"G": 10, "AB": 30, "H": 9})
check("batter entry: source 'bdl'", bat and bat.get("source") == "bdl", bat)
pit = ds._build_pitcher_season_entry(1, 2026, mlb_api_stats={"G": 5, "IP": 10.0, "ER": 3})
check("pitcher entry: source 'bdl'", pit and pit.get("source") == "bdl", pit)

print("per-player BDL backfill")
real_get = ds._bdl_get_json
ds._bdl_get_json = lambda path, params: {"data": [{
    "season": 2024, "postseason": False, "team_name": "Boston Red Sox",
    "batting_gp": 10, "batting_ab": 30, "batting_h": 9, "batting_2b": 2, "batting_3b": 0, "batting_hr": 1,
    "pitching_gp": 5, "pitching_gs": 1, "pitching_w": 1, "pitching_l": 0, "pitching_ip": 10.0,
    "pitching_er": 3, "pitching_bb": 2, "pitching_k": 9}]}
try:
    b = ds._build_backfill_batter_entry(7, 2024)
    p = ds._build_backfill_pitcher_entry(7, 2024)
finally:
    ds._bdl_get_json = real_get
check("backfill batter entry: source 'bdl'", b and b.get("source") == "bdl", b)
check("backfill pitcher entry: source 'bdl'", p and p.get("source") == "bdl", p)

print("roster sync placeholders")


class _Q:
    def filter(self, *a): return self
    def first(self): return None


class _DB:
    def __init__(self): self.added = []
    def query(self, model): return _Q()
    def add(self, row): self.added.append(row)


db = _DB()
ds._apply_team_to_season_rows(db, player_id=1, year=2026, abbr="BOS", create_pitcher=True, create_batter=True)
check("both placeholder rows created, source 'bdl'",
      len(db.added) == 2 and all(r.source == "bdl" for r in db.added), [(type(r).__name__, r.source) for r in db.added])

print("Lahman loader")
saved = {"bat": [], "pit": []}


@contextlib.contextmanager
def _session():
    yield None


real = (ll.connection.get_session, ll.crud.save_player_seasons, ll.crud.save_pitcher_seasons)
ll.connection.get_session = _session
ll.crud.save_player_seasons = lambda db, m, rows: saved["bat"].extend(rows)
ll.crud.save_pitcher_seasons = lambda db, m, rows: saved["pit"].extend(rows)
try:
    ll._load_batting({"surhobj01": 122989}, set())
    ll._load_pitching({"sabatcc01": 282332}, set())
finally:
    ll.connection.get_session, ll.crud.save_player_seasons, ll.crud.save_pitcher_seasons = real
check("B.J. Surhoff's 19 batting seasons, every one 'lahman'",
      len(saved["bat"]) == 19 and all(r.get("source") == "lahman" for r in saved["bat"]), len(saved["bat"]))
check("CC Sabathia's pitching seasons, every one 'lahman'",
      saved["pit"] and all(r.get("source") == "lahman" for r in saved["pit"]), len(saved["pit"]))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if results and all(results) else 1)
