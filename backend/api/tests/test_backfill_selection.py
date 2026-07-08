"""Regular-vs-postseason selection tests for the BDL historical backfill.

`data_service._pick_regular_season_row` must pick the REGULAR-season row from a
BDL `/season_stats` response even when the postseason row is listed FIRST (the
Cade Smith 2024 shape), and must never fall back to a postseason line.

`data_service` imports pandas / pybaseball / sqlalchemy / dotenv at module load
(and reads ORM columns), which aren't needed by this pure helper — so we stub
those heavy deps before importing, mirroring how `test_live_derivations` stubs
`data_service` itself. Run:
    cd backend/api && python3 -m unittest tests.test_backfill_selection -v
"""

import os
import sys
import types
import unittest

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/api
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

# --- Stub the heavy third-party + DB deps data_service pulls at import -------
# The backfill selector is pure (list[dict] -> dict|None); none of these are
# exercised by it, so minimal stand-ins are enough to let the module import.
class _AnyAttrModule(types.ModuleType):
    """Stub module returning a dummy for ANY attribute — covers pandas symbols
    referenced in data_service's type annotations at import (e.g. pd.DataFrame),
    none of which the pure backfill selector actually uses."""
    def __getattr__(self, name):  # noqa: D401
        return object


sys.modules.setdefault("pandas", _AnyAttrModule("pandas"))
sys.modules.setdefault("requests", types.ModuleType("requests"))

# pybaseball calls `pybaseball.cache.enable()` at data_service import.
_pyb = types.ModuleType("pybaseball")
_pyb.cache = types.SimpleNamespace(enable=lambda: None)
sys.modules.setdefault("pybaseball", _pyb)

_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dotenv)

# Minimal sqlalchemy surface used at data_service load time.
_sa = types.ModuleType("sqlalchemy")
for _attr in ("and_", "case", "func", "or_"):
    setattr(_sa, _attr, lambda *a, **k: None)
_sa.text = lambda *a, **k: None
sys.modules.setdefault("sqlalchemy", _sa)
_sa_orm = types.ModuleType("sqlalchemy.orm")
_sa_orm.aliased = lambda *a, **k: None
sys.modules.setdefault("sqlalchemy.orm", _sa_orm)

# database.* — connection/crud are only referenced inside functions; models
# expose column lists read at import, so give them empty column sets.
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


# Field values exactly as BDL returns them (confirmed from the raw response):
#   {"postseason": true,  "season_type": "postseason"}
#   {"postseason": false, "season_type": "regular"}
def _pit(postseason, ip, gp):
    return {"postseason": postseason, "season_type":
            "postseason" if postseason else "regular",
            "pitching_ip": ip, "pitching_gp": gp}


class TestRegularSeasonSelection(unittest.TestCase):

    def test_picks_regular_when_postseason_is_first(self):
        # Cade Smith 2024 shape: postseason row FIRST, regular second.
        rows = [_pit(True, 10, 9), _pit(False, 75.1, 74)]
        s = ds._pick_regular_season_row(rows, "pitching_ip")
        self.assertIs(s["postseason"], False)
        self.assertEqual(s["pitching_ip"], 75.1)
        self.assertEqual(s["pitching_gp"], 74)

    def test_picks_regular_when_regular_is_first(self):
        # Cade Smith 2025 shape: regular first, postseason second.
        rows = [_pit(False, 73.2, 76), _pit(True, 3.1, 3)]
        s = ds._pick_regular_season_row(rows, "pitching_ip")
        self.assertEqual(s["pitching_gp"], 76)

    def test_postseason_only_returns_none(self):
        # No regular row -> never fall back to a playoff line.
        rows = [_pit(True, 10, 9)]
        self.assertIsNone(ds._pick_regular_season_row(rows, "pitching_ip"))

    def test_legacy_single_row_without_flag_is_kept(self):
        # Old shape: no `postseason` key at all -> keep the row (back-compat).
        rows = [{"pitching_ip": 40.1, "pitching_gp": 38}]
        s = ds._pick_regular_season_row(rows, "pitching_ip")
        self.assertEqual(s["pitching_gp"], 38)

    def test_no_row_with_stat_returns_none(self):
        rows = [{"batting_ab": 12}]  # pitching query, no pitching_ip anywhere
        self.assertIsNone(ds._pick_regular_season_row(rows, "pitching_ip"))

    def test_batter_side_uses_same_rule(self):
        rows = [
            {"postseason": True,  "batting_ab": 4},
            {"postseason": False, "batting_ab": 500},
        ]
        s = ds._pick_regular_season_row(rows, "batting_ab")
        self.assertEqual(s["batting_ab"], 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
