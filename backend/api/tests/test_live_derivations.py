"""Phase 1 derivation tests — prove the plays-only derivations in
`live_service` reproduce the known-correct linescore/hits/runs for a real
finished game (5059068, PHI@PIT, 2026-07-01) BEFORE the refactor touches a live
game. See docs/live_redesign_phase1.md §2.

Run:  cd backend/api && python3 -m unittest tests.test_live_derivations -v

`live_service` imports `data_service` at module load (which pulls pandas + the
DB). None of the pure derivations need it, so we stub `data_service` with the
handful of attributes the module references, keeping the test hermetic.
"""

import datetime
import json
import os
import sys
import types
import unittest

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/api
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

# --- Stub data_service before importing live_service ----------------------
_stub = types.ModuleType("data_service")
_stub._BDL_RATE_LIMIT_SLEEP = 0.0
_stub._BDL_TO_LAHMAN_TEAM_MAP = {21: "PHI", 22: "PIT"}
_stub._MLB_LOCAL_TZ = datetime.timezone.utc
_stub._bdl_get_json = lambda *a, **k: {}
sys.modules["data_service"] = _stub

import live_service as ls  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "game_5059068_plays.json")

# Known-correct output (PIT = away, bats top; PHI = home, bats bottom).
EXPECTED_GRID_PIT = [0, 0, 2, 0, 2, 0, 2, 0, 0]
EXPECTED_GRID_PHI = [0, 5, 1, 2, 0, 0, 0, 2, None]   # bottom-9th never batted -> null
EXPECTED_HITS_PIT = 12
EXPECTED_HITS_PHI = 11
EXPECTED_RUNS_PIT = 6
EXPECTED_RUNS_PHI = 10


def _load_plays():
    with open(FIXTURE) as f:
        return json.load(f)["plays"]


def _minimal_game():
    """Just enough of the /games object for assemble_unified (identity + errors);
    everything else comes from the play feed."""
    return {
        "id": 5059068,
        "status": "STATUS_FINAL",
        "season": 2026,
        "season_type": "regular",
        "away_team": {"id": 22, "abbreviation": "PIT", "name": "Pirates",
                      "location": "Pittsburgh", "league": "National", "division": "Central"},
        "home_team": {"id": 21, "abbreviation": "PHI", "name": "Phillies",
                      "location": "Philadelphia", "league": "National", "division": "East"},
        "away_team_data": {"errors": 1},   # games-sourced (KEEP)
        "home_team_data": {"errors": 0},
    }


class TestGridDerivation(unittest.TestCase):
    def setUp(self):
        self.plays = sorted(_load_plays(), key=lambda p: p.get("order") or 0)
        self.grid = ls._derive_grid(self.plays)

    def test_grid_matches_known_linescore_exactly(self):
        away = [row["away"] for row in self.grid]
        home = [row["home"] for row in self.grid]
        self.assertEqual(away, EXPECTED_GRID_PIT)
        self.assertEqual(home, EXPECTED_GRID_PHI)

    def test_un_batted_bottom_9th_is_null_not_zero(self):
        ninth = self.grid[8]
        self.assertEqual(ninth["num"], 9)
        self.assertIsNone(ninth["home"], "PHI never batted bottom-9th -> must be null")
        self.assertEqual(ninth["away"], 0, "PIT batted top-9th scoreless -> 0, not null")

    def test_cells_are_int_or_none_never_string(self):
        for row in self.grid:
            for side in ("away", "home"):
                self.assertTrue(row[side] is None or isinstance(row[side], int),
                                f"cell {row} must be int|null, never a string 'x'")

    def test_grid_has_at_least_nine_innings(self):
        self.assertGreaterEqual(len(self.grid), 9)


class TestHitsAndRuns(unittest.TestCase):
    def setUp(self):
        self.plays = sorted(_load_plays(), key=lambda p: p.get("order") or 0)

    def test_hits_match_known_totals(self):
        away_hits, home_hits = ls._derive_hits(self.plays)
        self.assertEqual(away_hits, EXPECTED_HITS_PIT)
        self.assertEqual(home_hits, EXPECTED_HITS_PHI)

    def test_runs_totals_match_and_equal_grid_sum(self):
        state = ls._derive_state(self.plays)
        self.assertEqual(state["away_runs"], EXPECTED_RUNS_PIT)
        self.assertEqual(state["home_runs"], EXPECTED_RUNS_PHI)
        # cross-check: totals equal the sum of per-inning grid deltas
        grid = ls._derive_grid(self.plays)
        self.assertEqual(sum(r["away"] or 0 for r in grid), EXPECTED_RUNS_PIT)
        self.assertEqual(sum(r["home"] or 0 for r in grid), EXPECTED_RUNS_PHI)


class TestAssembleContract(unittest.TestCase):
    def setUp(self):
        self.unified = ls.assemble_unified(
            _minimal_game(), stats=[], plays=_load_plays(), pas=[], lineup=None,
        )

    def test_top_level_shape_preserved(self):
        for key in ("game_id", "fetched_at", "status", "season", "season_type",
                    "summary", "linescore", "situation", "plays", "scoring_plays",
                    "batting", "pitching"):
            self.assertIn(key, self.unified)

    def test_linescore_is_plays_derived(self):
        ls_block = self.unified["linescore"]
        self.assertEqual([r["away"] for r in ls_block["innings"]], EXPECTED_GRID_PIT)
        self.assertEqual([r["home"] for r in ls_block["innings"]], EXPECTED_GRID_PHI)
        self.assertEqual(ls_block["away_runs"], EXPECTED_RUNS_PIT)
        self.assertEqual(ls_block["home_runs"], EXPECTED_RUNS_PHI)
        self.assertEqual(ls_block["away_hits"], EXPECTED_HITS_PIT)
        self.assertEqual(ls_block["home_hits"], EXPECTED_HITS_PHI)
        self.assertIsNone(ls_block["innings"][8]["home"])   # x/blank cell

    def test_errors_kept_from_games_feed(self):
        ls_block = self.unified["linescore"]
        self.assertEqual(ls_block["away_errors"], 1)   # from away_team_data
        self.assertEqual(ls_block["home_errors"], 0)

    def test_summary_runs_and_hits_match_linescore(self):
        summ = self.unified["summary"]
        self.assertEqual(summ["away"]["runs"], EXPECTED_RUNS_PIT)
        self.assertEqual(summ["home"]["runs"], EXPECTED_RUNS_PHI)
        self.assertEqual(summ["away"]["hits"], EXPECTED_HITS_PIT)
        self.assertEqual(summ["home"]["hits"], EXPECTED_HITS_PHI)

    def test_situation_present_with_batter_pitcher_keys(self):
        sit = self.unified["situation"]
        for key in ("batter", "pitcher", "on_first", "on_second", "on_third"):
            self.assertIn(key, sit)


class TestEnrichedListPayload(unittest.TestCase):
    def test_summary_from_unified_adds_batter_pitcher_lastplay(self):
        unified = ls.assemble_unified(
            _minimal_game(), stats=[], plays=_load_plays(), pas=[], lineup=None,
        )
        card = ls._summary_from_unified(unified)
        # existing contract keys still present
        for key in ("game_id", "fetched_at", "status", "is_live", "inning",
                    "inning_half", "outs", "away", "home"):
            self.assertIn(key, card)
        # additive enrichment
        for key in ("batter", "pitcher", "last_play"):
            self.assertIn(key, card)
        # last_play is the text of the final play row
        self.assertEqual(card["last_play"], unified["plays"][-1]["text"])


FIXTURE_5059076 = os.path.join(os.path.dirname(__file__), "fixtures",
                               "game_5059076_plays.json")


class TestHitSuffixMatching(unittest.TestCase):
    """Guards the suffix-based hit classifier (`_is_hit_type`) that replaced the
    exact {Single, Double, Triple, Home Run} set — see the 5059076 smoke check
    where a 'Bunt Single' was undercounted."""

    def test_prefixed_hit_variants_count(self):
        for t in ("Single", "Double", "Triple", "Home Run",
                  "Bunt Single", "Infield Single", "Ground Rule Double",
                  "Inside The Park Home Run",
                  "  bunt single  ", "GROUND RULE DOUBLE"):  # trim + case-insensitive
            self.assertTrue(ls._is_hit_type(t), f"{t!r} should count as a hit")

    def test_non_hits_do_not_count(self):
        for t in ("Double Play", "Triple Play",          # the obvious false positives
                  "Sacrifice Fly", "Ground Out", "Fly Out", "Line Out", "Pop Out",
                  "Batters Fielders Choice - Runner Out", "Strike Swinging",
                  "Stolen Base", "Play Result", "Start Inning", "", None):
            self.assertFalse(ls._is_hit_type(t), f"{t!r} should NOT count as a hit")

    def test_double_and_triple_play_excluded_in_derive_hits(self):
        plays = [
            {"order": 1, "inning": 1, "inning_type": "Top",    "type": "Bunt Single"},
            {"order": 2, "inning": 1, "inning_type": "Top",    "type": "Single"},
            {"order": 3, "inning": 1, "inning_type": "Bottom", "type": "Double Play"},
            {"order": 4, "inning": 1, "inning_type": "Bottom", "type": "Triple Play"},
            {"order": 5, "inning": 1, "inning_type": "Bottom", "type": "Double"},
        ]
        away, home = ls._derive_hits(plays)
        self.assertEqual(away, 2, "Bunt Single + Single count for away")
        self.assertEqual(home, 1, "only Double counts; Double/Triple Play excluded")


class TestBuntSingleRegression(unittest.TestCase):
    """Locks in the live-smoke finding: the 5059076 fixture contains a real
    'Bunt Single' that MUST be counted, and the suffix rule reconciles with the
    /games hits total the exact set missed."""

    def setUp(self):
        with open(FIXTURE_5059076) as f:
            blob = json.load(f)
        self.plays = blob["plays"]
        self.expected = blob["expected"]

    def test_fixture_actually_contains_a_bunt_single(self):
        self.assertTrue(
            any("bunt single" in (p.get("type") or "").lower() for p in self.plays),
            "fixture must contain a Bunt Single or the regression isn't guarded",
        )

    def test_hits_match_games_reference_including_bunt_single(self):
        away, home = ls._derive_hits(self.plays)
        self.assertEqual(away, self.expected["hits_away_PIT"])
        self.assertEqual(home, self.expected["hits_home_PHI"])
        # the suffix rule recovers the hit the old exact set dropped
        self.assertGreater(away, self.expected["old_exact_set_hits_away_PIT"])
        # and reconciles with the independent /games hits total
        self.assertEqual(away, self.expected["games_ref_hits_away_PIT"])


class TestPitchCountExtraction(unittest.TestCase):
    """`_pitch_counts_from_pas`: `pitcher_pitch_count` is a CUMULATIVE running
    total per pitcher on each pitch, so a pitcher's game total is the MAX across
    their pitches (== last pitch's value), never the sum. Empty/absent counts
    yield NO map entry -> pc None downstream (not 0)."""

    def _pas(self):
        # Two pitchers, interleaved PAs. pitcher_pitch_count climbs monotonically
        # per pitcher (10 -> then p1's next PA continues at 13/15). game_pitch_count
        # is the interleaved whole-game count and must NOT be used.
        return [
            {"pitcher_id": 1, "pitches": [
                {"pitcher_pitch_count": 3,  "game_pitch_count": 3},
                {"pitcher_pitch_count": 7,  "game_pitch_count": 7},
                {"pitcher_pitch_count": 10, "game_pitch_count": 12},
            ]},
            {"pitcher_id": 2, "pitches": [
                {"pitcher_pitch_count": 4,  "game_pitch_count": 8},
                {"pitcher_pitch_count": 9,  "game_pitch_count": 15},
            ]},
            {"pitcher_id": 1, "pitches": [
                {"pitcher_pitch_count": 13, "game_pitch_count": 18},
                {"pitcher_pitch_count": 15, "game_pitch_count": 20},
            ]},
        ]

    def test_max_per_pitcher_not_sum(self):
        pc = ls._pitch_counts_from_pas(self._pas())
        # p1 last cumulative = 15 (NOT sum 3+7+10+13+15=48); p2 = 9 (NOT 4+9=13)
        self.assertEqual(pc[1], 15)
        self.assertEqual(pc[2], 9)

    def test_uses_pitcher_not_game_pitch_count(self):
        pc = ls._pitch_counts_from_pas(self._pas())
        # game_pitch_count maxes are 20 (p1) / 15 (p2); must NOT be those.
        self.assertNotEqual(pc[1], 20)
        self.assertNotEqual(pc[2], 15)

    def test_empty_pitches_yields_no_entry(self):
        pas = [
            {"pitcher_id": 5, "pitches": []},          # empty array
            {"pitcher_id": 6},                          # no pitches key
            {"pitcher_id": 7, "pitches": [{"balls": 0}]},  # pitch w/o the count
            {"pitches": [{"pitcher_pitch_count": 4}]},  # no pitcher_id
        ]
        pc = ls._pitch_counts_from_pas(pas)
        # None of these produce a count -> absent from map (so pc None, not 0).
        self.assertNotIn(5, pc)
        self.assertNotIn(6, pc)
        self.assertNotIn(7, pc)
        self.assertEqual(pc, {})

    def test_pit_row_attaches_pc_none_when_absent(self):
        stat = {"ip": "2.0", "player": {"id": 99, "full_name": "Test P"}}
        # pitcher 99 not in the map -> pc None (not 0, not missing)
        row = ls._pit_row(stat, {1: 15})
        self.assertIn("pc", row)
        self.assertIsNone(row["pc"])
        # present -> attached
        row2 = ls._pit_row(stat, {99: 42})
        self.assertEqual(row2["pc"], 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
