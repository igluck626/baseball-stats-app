#!/usr/bin/env python3
"""`_last_season_played` — what the nightly stamps into `mlb_last_season` when
balldontlie marks a player inactive.

⚠️ THE CHECK THAT MATTERS: an inactive player whose last real season was 2023
is stamped 2023, not `current_year - 1` (2025). The blind stamp recorded
seasons nobody played (Dany Jiménez, Joely Rodríguez, José Rodríguez and Chad
Smith all read 2025). A season row with no games is not a season; a player with
no season rows is left unchanged rather than stamped with a guess.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_last_season_stamp.py
"""
import inspect
import os
import sys
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import data_service as ds                                         # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def last(bat, pit):
    """Run the real rule with the season rows stubbed: lists of (year, G)."""
    ds.crud.get_player_seasons = lambda db, pid: [NS(year=y, G=g) for y, g in bat]
    ds.crud.get_pitcher_seasons = lambda db, pid: [NS(year=y, G=g) for y, g in pit]
    return ds._last_season_played(db=None, player_id=1)


_orig = (ds.crud.get_player_seasons, ds.crud.get_pitcher_seasons)
try:
    print("the last season with games")
    check("inactive pitcher whose last real season is 2023 -> 2023, not 2025",
          last([], [(2021, 30), (2022, 41), (2023, 12)]) == 2023)
    check("Joely Rodríguez (2016-2024, released before 2026) -> 2024", last([(2024, 14)], [(2016, 12), (2024, 14)]) == 2024)
    check("batting and pitching both count (two-way: last batting 2024, last pitching 2022)",
          last([(2023, 50), (2024, 60)], [(2022, 9)]) == 2024)
    check("a season row with 0 games is not a season (2025 placeholder ignored)",
          last([(2023, 40), (2025, 0)], []) == 2023)
    check("a NULL games count is not a season either", last([(2025, None), (2022, 5)], []) == 2022)
    check("no season rows at all -> None (the caller leaves the field unchanged)", last([], []) is None)
    check("only empty rows -> None", last([(2025, 0)], [(2025, 0)]) is None)
finally:
    ds.crud.get_player_seasons, ds.crud.get_pitcher_seasons = _orig

print("the nightly uses it")
src = inspect.getsource(ds.sync_player_active_status_from_bdl)
check("the stamp no longer writes current_year - 1", "mlb_last_season = current_year - 1" not in src)
check("the stamp writes _last_season_played's answer and skips a player with none",
      "_last_season_played(db, player_id)" in src and 'counts["retire_skipped_no_seasons"] += 1' in src)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
