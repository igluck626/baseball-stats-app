"""Semantics tests for `data_service.recalculate_pitching_counting`.

The aggregator delegates entirely to a raw SQL UPDATE, so the branch that
matters — CURRENT-season OVERWRITE vs PAST-season COALESCE (fill-null-only) — is
expressed in the generated SQL. We execute that real SQL against an in-memory
`sqlite3` DB (its `SUM(COALESCE(...))` / `UPDATE ... WHERE EXISTS` / table-
qualified `COALESCE(pitcher_seasons."R", ...)` all behave like the production
Postgres path — verified), so this exercises the ACTUAL fill/overwrite result,
not just the SQL string.

`data_service` pulls pandas / pybaseball / sqlalchemy / dotenv at import (and
reads ORM columns), none needed by this pure-SQL function — so we stub those
heavy deps before importing, and stub sqlalchemy's `text` to return the raw SQL
string so a sqlite-backed fake session can run it. Mirrors how
`test_backfill_selection` / `test_live_derivations` stub their deps. Run:
    cd backend/api && python3 -m unittest tests.test_pitching_counting -v
"""

import os
import sqlite3
import sys
import types
import unittest

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/api
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

# --- Stub the heavy third-party + DB deps data_service pulls at import -------
class _AnyAttrModule(types.ModuleType):
    def __getattr__(self, name):
        return object


sys.modules.setdefault("pandas", _AnyAttrModule("pandas"))
sys.modules.setdefault("requests", types.ModuleType("requests"))

_pyb = types.ModuleType("pybaseball")
_pyb.cache = types.SimpleNamespace(enable=lambda: None)
sys.modules.setdefault("pybaseball", _pyb)

_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dotenv)

# sqlalchemy: `text` must return the raw SQL string so the sqlite fake db can
# execute it (the rest are unused by recalculate_pitching_counting).
_sa = types.ModuleType("sqlalchemy")
for _attr in ("and_", "case", "func", "or_"):
    setattr(_sa, _attr, lambda *a, **k: None)
_sa.text = lambda s=None, *a, **k: s
sys.modules.setdefault("sqlalchemy", _sa)
_sa_orm = types.ModuleType("sqlalchemy.orm")
_sa_orm.aliased = lambda *a, **k: None
sys.modules.setdefault("sqlalchemy.orm", _sa_orm)

_db = types.ModuleType("database")
sys.modules.setdefault("database", _db)
_conn = types.ModuleType("database.connection")
_crud = types.ModuleType("database.crud")
sys.modules.setdefault("database.connection", _conn)
sys.modules.setdefault("database.crud", _crud)
_db.connection = _conn
_db.crud = _crud


class _FakeTable:
    columns = []


class _FakeModel:
    __table__ = _FakeTable()


_models = types.ModuleType("database.models")
_models.PitcherSeason = _FakeModel
_models.PlayerSeason = _FakeModel
sys.modules.setdefault("database.models", _models)

import data_service as ds  # noqa: E402


class _SqliteDB:
    """Minimal stand-in for the SQLAlchemy session: forwards `.execute(sql,
    params)` to a sqlite3 connection. `recalculate_pitching_counting` only uses
    `db.execute(...).rowcount`, which a sqlite3 cursor provides."""

    def __init__(self, con):
        self.con = con

    def execute(self, sql, params=None):
        return self.con.execute(sql, params or {})


class _CapturingSqliteDB(_SqliteDB):
    """Records every SQL passed to `execute` (and still runs it), so tests can
    assert on the GENERATED SQL structure — the only way to catch a regression
    to the Postgres-broken fill-null form, since sqlite happily fills BOTH the
    broken `COALESCE(<table>."F", …)` form and the fixed bare-subquery form."""

    def __init__(self, con):
        super().__init__(con)
        self.sqls: list = []

    def execute(self, sql, params=None):
        self.sqls.append(str(sql))
        return super().execute(sql, params)


class TestRecalculatePitchingCounting(unittest.TestCase):

    CURRENT_YEAR = 2026

    def setUp(self):
        self._orig_current_year = ds._current_year
        ds._current_year = lambda: self.CURRENT_YEAR  # pin so past/current are deterministic

        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            'CREATE TABLE pitcher_seasons '
            '(player_id INT, year INT, "R" INT, "HBP" INT)'
        )
        self.con.execute(
            'CREATE TABLE pitching_gamelogs '
            '(player_id INT, game_id TEXT, season INT, "R" INT, "HBP" INT)'
        )
        self.db = _SqliteDB(self.con)

    def tearDown(self):
        ds._current_year = self._orig_current_year
        self.con.close()

    def _season_row(self, player_id, year):
        return self.con.execute(
            'SELECT "R", "HBP" FROM pitcher_seasons WHERE player_id=? AND year=?',
            (player_id, year),
        ).fetchone()

    def _add_gamelogs(self, player_id, season, rows):
        # rows: list of (game_id, R, HBP)
        self.con.executemany(
            'INSERT INTO pitching_gamelogs VALUES (?,?,?,?,?)',
            [(player_id, gid, season, r, hbp) for (gid, r, hbp) in rows],
        )

    # 1. CURRENT season OVERWRITES from the game-log sum (even a stale value).
    def test_current_season_overwrites(self):
        # null-R/HBP row (repair-built, e.g. Cade Smith) + gamelogs summing 16/1.
        self.con.execute('INSERT INTO pitcher_seasons VALUES (1, 2026, NULL, NULL)')
        self._add_gamelogs(1, 2026, [("g1", 2, 1), ("g2", 0, 0), ("g3", 14, 0)])
        # and a STALE non-null row to prove overwrite (not just fill).
        self.con.execute('INSERT INTO pitcher_seasons VALUES (2, 2026, 99, 42)')
        self._add_gamelogs(2, 2026, [("g4", 3, 1), ("g5", 2, 0)])

        updated = ds.recalculate_pitching_counting(self.db, 2026)

        self.assertEqual(self._season_row(1, 2026), (16, 1))   # filled from sum
        self.assertEqual(self._season_row(2, 2026), (5, 1))    # stale 99/42 OVERWRITTEN
        self.assertEqual(updated, 2)

    # 2. PAST season with EXISTING legacy values is PRESERVED (the protection).
    def test_past_season_preserves_legacy(self):
        # legacy R=99, HBP=7; gamelogs sum to a DIFFERENT 5/1 — must NOT clobber.
        self.con.execute('INSERT INTO pitcher_seasons VALUES (1, 2024, 99, 7)')
        self._add_gamelogs(1, 2024, [("g1", 3, 1), ("g2", 2, 0)])

        ds.recalculate_pitching_counting(self.db, 2024)

        self.assertEqual(self._season_row(1, 2024), (99, 7))   # legacy untouched

    # 3. PAST season with NULL values IS filled from the sum (fill-null works).
    def test_past_season_fills_null(self):
        self.con.execute('INSERT INTO pitcher_seasons VALUES (1, 2024, NULL, NULL)')
        self._add_gamelogs(1, 2024, [("g1", 3, 1), ("g2", 2, 0)])

        ds.recalculate_pitching_counting(self.db, 2024)

        self.assertEqual(self._season_row(1, 2024), (5, 1))    # filled

    # 4. EXISTS guard: a row with no game logs is left ALONE (not zeroed).
    def test_row_without_gamelogs_untouched(self):
        self.con.execute('INSERT INTO pitcher_seasons VALUES (1, 2026, NULL, NULL)')
        # no gamelogs for player 1
        updated = ds.recalculate_pitching_counting(self.db, 2026)
        self.assertEqual(self._season_row(1, 2026), (None, None))  # not set to 0
        self.assertEqual(updated, 0)

    # player_id filter targets a single pitcher.
    def test_player_id_filter(self):
        self.con.execute('INSERT INTO pitcher_seasons VALUES (1, 2026, NULL, NULL)')
        self.con.execute('INSERT INTO pitcher_seasons VALUES (2, 2026, NULL, NULL)')
        self._add_gamelogs(1, 2026, [("g1", 4, 1)])
        self._add_gamelogs(2, 2026, [("g2", 9, 2)])

        updated = ds.recalculate_pitching_counting(self.db, 2026, player_id=1)

        self.assertEqual(self._season_row(1, 2026), (4, 1))       # targeted
        self.assertEqual(self._season_row(2, 2026), (None, None)) # untouched
        self.assertEqual(updated, 1)

    # PAST season, MIXED nulls: fill the null field, PRESERVE the non-null
    # sibling. This is why fill-null must be per-field — a single combined
    # null-guard would either skip R or clobber the legacy HBP.
    def test_past_season_mixed_fills_null_preserves_sibling(self):
        # R null (needs fill), HBP=9 legacy (must survive). Gamelog sums: R=5, HBP=1.
        self.con.execute('INSERT INTO pitcher_seasons VALUES (1, 2024, NULL, 9)')
        self._add_gamelogs(1, 2024, [("g1", 3, 1), ("g2", 2, 0)])

        ds.recalculate_pitching_counting(self.db, 2024)

        self.assertEqual(self._season_row(1, 2024), (5, 9))  # R filled, HBP legacy kept

    # ---- Structural guards on the GENERATED SQL (catch a regression to the
    # ---- Postgres-broken self-referential COALESCE form that sqlite can't) ----

    def test_fillnull_sql_is_per_field_bare_subquery_no_self_coalesce(self):
        # NOTE: sqlite fills BOTH the broken `COALESCE(pitcher_seasons."F", …)`
        # form and the fixed bare-subquery form, so the semantic tests above pass
        # on the broken code too. THIS is the regression guard: it asserts the
        # fill-null path emits one bare-subquery UPDATE PER FIELD with a
        # `WHERE "F" IS NULL` guard and NO target-table self-reference in SET.
        self.con.execute('INSERT INTO pitcher_seasons VALUES (1, 2024, NULL, NULL)')
        self._add_gamelogs(1, 2024, [("g1", 5, 1)])
        cap = _CapturingSqliteDB(self.con)

        ds.recalculate_pitching_counting(cap, 2024)  # past -> fill-null

        # one UPDATE per field
        self.assertEqual(len(cap.sqls), len(ds._PITCHING_COUNTING_FIELDS))
        for sql in cap.sqls:
            self.assertIn("IS NULL", sql)                          # WHERE null-guard
            self.assertNotIn("COALESCE(pitcher_seasons", sql)      # NOT the broken self-ref SET
            self.assertRegex(sql, r'SET\s+"\w+"\s*=\s*\(SELECT SUM')  # bare-subquery SET

    def test_overwrite_sql_is_single_combined_unchanged(self):
        self.con.execute('INSERT INTO pitcher_seasons VALUES (1, 2026, NULL, NULL)')
        self._add_gamelogs(1, 2026, [("g1", 4, 1)])
        cap = _CapturingSqliteDB(self.con)

        ds.recalculate_pitching_counting(cap, 2026)  # current -> overwrite

        self.assertEqual(len(cap.sqls), 1)               # single combined UPDATE
        sql = cap.sqls[0]
        self.assertNotIn("IS NULL", sql)                 # no per-field null-guard
        self.assertNotIn("COALESCE(pitcher_seasons", sql)  # bare subquery, no self-ref


class TestBattingCountingSharesFix(unittest.TestCase):
    """The batting aggregator routes through the SAME `_apply_counting_aggregation`
    engine, so it gets the identical fix. Confirm its fill-null SQL is also the
    safe bare-subquery + WHERE-null form (not the Postgres-broken self-ref)."""

    def setUp(self):
        self._orig = ds._current_year
        ds._current_year = lambda: 2026
        self.con = sqlite3.connect(":memory:")
        self.con.execute('CREATE TABLE player_seasons (player_id INT, year INT, "IBB" INT, "GIDP" INT, "SF" INT, "PA" INT, "H" INT, "HBP" INT, "CS" INT, "SH" INT)')
        self.con.execute('CREATE TABLE batting_gamelogs (player_id INT, game_id TEXT, season INT, "IBB" INT, "GIDP" INT, "SF" INT, "PA" INT, "H" INT, "HBP" INT, "CS" INT, "SH" INT)')

    def tearDown(self):
        ds._current_year = self._orig
        self.con.close()

    def test_batting_fillnull_sql_is_bare_subquery_no_self_coalesce(self):
        self.con.execute('INSERT INTO player_seasons VALUES (1, 2024, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)')
        self.con.execute('INSERT INTO batting_gamelogs VALUES (1, "g1", 2024, 2, 1, 0, 4, 1, 0, 0, 0)')
        cap = _CapturingSqliteDB(self.con)

        ds.recalculate_batting_counting(cap, 2024)  # past -> fill-null

        self.assertEqual(len(cap.sqls), len(ds._BATTING_COUNTING_FIELDS))
        for sql in cap.sqls:
            self.assertIn("IS NULL", sql)
            self.assertNotIn("COALESCE(player_seasons", sql)  # NOT the broken self-ref
            self.assertRegex(sql, r'SET\s+"\w+"\s*=\s*\(SELECT SUM')
        # and it actually fills the null IBB from the gamelog sum in sqlite
        ibb = self.con.execute('SELECT "IBB" FROM player_seasons WHERE player_id=1').fetchone()[0]
        self.assertEqual(ibb, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
