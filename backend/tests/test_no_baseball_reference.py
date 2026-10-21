#!/usr/bin/env python3
"""Nothing in the server fetches from Baseball-Reference, and nothing refreshes
WAR. Stored WAR (and the WAR-derived columns) stays as it is and keeps
displaying.

⚠️ THE CHECKS THAT MATTER:
  • no backend file names the host, its daily WAR files, or the scraping
    library we used to reach it — the nightly, both catch-ups, the
    discover-phase backfill and the admin endpoints (bulk-load, backfill-war,
    backfill-player-history, bref-war-status) all used to download the two
    WAR files;
  • balldontlie's season WAR is Baseball-Reference's WAR redistributed (equal
    to our stored bWAR for 749 of 902 batters, 2026-10-21), so it isn't a
    replacement source either: a balldontlie season row carrying WAR builds a
    season entry with no WAR key, so the column-additive upsert keeps the
    stored value.

Standalone, no pytest. Run: <backend python> backend/tests/test_no_baseball_reference.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(BACKEND, "scripts"), os.path.join(BACKEND, "api"), BACKEND]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


# Split so this file doesn't match itself.
NEEDLES = ["baseball-reference" + ".com", "war_daily" + "_", "py" + "baseball"]

print("no server file reaches Baseball-Reference")
hits = {n: [] for n in NEEDLES}
for root, dirs, files in os.walk(BACKEND):
    dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv", "venv", "data")]
    for f in files:
        if f.endswith((".py", ".sh", ".toml", ".cfg", ".txt", ".yml", ".yaml")) or f == "Dockerfile":
            path = os.path.join(root, f)
            with open(path, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    low = line.lower()
                    for needle in NEEDLES:
                        if needle in low:
                            hits[needle].append(f"{os.path.relpath(path, BACKEND)}:{n}")
for needle in NEEDLES:
    check(f"no backend file names {needle!r}", not hits[needle], hits[needle][:10])

print("WAR is never written from a balldontlie season row")
import data_service as ds                                          # noqa: E402
import nightly_update as nu                                        # noqa: E402

gone = [f for f in ("_download_bref_war_csv", "_bwar_bat_all", "_bwar_pitch_all", "_bwar_bat_all_meta",
                    "_bwar_pitch_all_meta", "get_last_war_hash", "_player_bwar_batting_seasons",
                    "_player_bwar_pitching_seasons", "_batting_bref", "_pitching_bref") if hasattr(ds, f)]
check("the WAR download helpers are gone", not gone, gone)

bat_row = {"batting_gp": 150, "batting_ab": 560, "batting_h": 170, "batting_2b": 30, "batting_3b": 2,
           "batting_hr": 25, "batting_r": 90, "batting_rbi": 88, "batting_bb": 60, "batting_so": 120,
           "batting_sb": 5, "batting_avg": 0.304, "batting_obp": 0.38, "batting_slg": 0.51,
           "batting_ops": 0.89, "batting_war": 4.7}
pit_row = {"pitching_gp": 30, "pitching_gs": 30, "pitching_w": 12, "pitching_l": 8, "pitching_sv": 0,
           "pitching_ip": "180.1", "pitching_h": 150, "pitching_er": 60, "pitching_hr": 18, "pitching_bb": 50,
           "pitching_k": 200, "pitching_era": 3.0, "pitching_whip": 1.11, "pitching_k_per_9": 9.98,
           "pitching_war": 5.2}
bat = ds._parse_bdl_batter_row(bat_row)
pit = ds._parse_bdl_pitcher_row(pit_row)
check("a balldontlie batter row carrying batting_war parses with no WAR", "WAR" not in bat, bat.get("WAR"))
check("a balldontlie pitcher row carrying pitching_war parses with no WAR", "WAR" not in pit, pit.get("WAR"))

WAR_COLS = {"WAR", "WAR_off", "WAR_def", "WAA", "OPS_plus", "ERA_plus", "runs_above_avg", "runs_above_rep"}
be = nu._build_current_batter_entry(1, 2, 2026, bdl_stats={**bat, "WAR": 4.7})
pe = nu._build_current_pitcher_entry(1, 2, 2026, bdl_stats={**pit, "WAR": 5.2})
check("the nightly batter entry writes no WAR column, and no team / league",
      be is not None and not (WAR_COLS | {"team", "league"}) & set(be), sorted(set(be or {})))
check("  ...but it still carries the season's counting stats", be and be.get("H") == 170 and be.get("HR") == 25, be)
check("the nightly pitcher entry writes no WAR column, and no team / league",
      pe is not None and not (WAR_COLS | {"team", "league"}) & set(pe), sorted(set(pe or {})))
check("  ...but it still carries the season's counting stats", pe and pe.get("SO") == 200 and pe.get("W") == 12, pe)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
