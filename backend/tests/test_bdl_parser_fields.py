#!/usr/bin/env python3
"""The nightly's balldontlie /stats parsers read every per-game field they store.

⚠️ THE CHECKS THAT MATTER: `left_on_base`, `wild_pitches` and `strikes` are
documented and shipped on /stats (every line of every game sampled 2026-10-17)
but were hardcoded None, so every balldontlie-sourced row lacked LOB, WP and
strikes. They are read now.

Real lines (fixtures/bdl_stats_fields.json), kept whole.
Standalone, no pytest. Run: <backend python> backend/tests/test_bdl_parser_fields.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
import data_service as ds                                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "bdl_stats_fields.json")))
ctx = ds._bdl_game_ctx(FX["game"])
b = ds._parse_bdl_batting_gamelog(FX["batter"], ctx)
p = ds._parse_bdl_pitching_gamelog(FX["pitcher"], ctx)
check("batting: LOB is balldontlie's left_on_base", b["LOB"] == FX["batter"]["left_on_base"], (b["LOB"],))
check("batting: PA, HBP, SF, SH, CS, IBB, GIDP still read",
      (b["PA"], b["HBP"], b["SF"], b["SH"], b["CS"], b["IBB"], b["GIDP"]) == tuple(
          FX["batter"][k] for k in ("plate_appearances", "hit_by_pitch", "sac_flies", "sac_bunts",
                                    "caught_stealing", "intentional_walks", "gidp")))
check("pitching: WP is balldontlie's wild_pitches", p["WP"] == FX["pitcher"]["wild_pitches"], (p["WP"],))
check("pitching: strikes is balldontlie's strikes", p["strikes"] == FX["pitcher"]["strikes"], (p["strikes"],))
check("pitching: HBP still read (pitching_hbp)", p["HBP"] == FX["pitcher"]["pitching_hbp"], (p["HBP"],))
missing = dict(FX["pitcher"], wild_pitches=None, strikes=None)
p2 = ds._parse_bdl_pitching_gamelog(missing, ctx)
check("a line without them (no detailed box score yet) stores NULL, not 0",
      p2["WP"] is None and p2["strikes"] is None)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
