#!/usr/bin/env python3
"""The MLB Stats API is not used anywhere in this app.

⚠️ THE CHECK THAT MATTERS: no backend source file names the host. The last two
uses — the /people/search MLBAM-id lookup in the nightly's discover pass and
the /admin/backfill-mlb-gamelogs-async historical loader — were removed; a new
player now waits for the Chadwick mapping (scripts/chadwick_catchup.py).

Standalone, no pytest. Run: <backend python> backend/tests/test_no_mlb_stats_api.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
HOST = "statsapi" + ".mlb.com"          # split so this file doesn't match itself

hits = []
for root, dirs, files in os.walk(BACKEND):
    dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv", "venv", "data")]
    for f in files:
        if f.endswith((".py", ".sh", ".toml", ".cfg", ".txt", ".yml", ".yaml")):
            path = os.path.join(root, f)
            with open(path, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if HOST in line:
                        hits.append(f"{os.path.relpath(path, BACKEND)}:{n}")
ok = not hits
print(f"  [{'PASS' if ok else 'FAIL'}] no backend file names {HOST}" + ("" if ok else f"  ({hits[:10]})"))
print(f"\n{int(ok)}/1 passed")
sys.exit(0 if ok else 1)
