#!/usr/bin/env python3
"""Diff a fresh battery run against golden_baseline.json. Prints ONLY changed
cells, skipping keys marked unstable for that question. Exit code 1 if any
non-advisory change is found (so it can gate a merge phase in CI).

Usage: python3 diff_battery.py results_new.json [golden_baseline.json]
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)


def main():
    new = json.load(open(sys.argv[1]))
    golden_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "golden_baseline.json")
    golden = json.load(open(golden_path))
    gq = golden["questions"]

    changed = 0
    advisory = 0
    for qid in sorted(gq):
        g = gq[qid]
        gcell = g["cell"]
        unstable = set(g.get("unstable", []))
        ncell = (new.get(qid) or {}).get("cell", {})
        keys = sorted(set(gcell) | set(ncell))
        for k in keys:
            if gcell.get(k) == ncell.get(k):
                continue
            if k in unstable:
                advisory += 1
                continue
            changed += 1
            print(f"CHANGED {qid} [{k}]  {g['q'][:55]}")
            print(f"    golden: {gcell.get(k)!r}")
            print(f"    new:    {ncell.get(k)!r}")

    print(f"\n{changed} non-advisory change(s); {advisory} advisory (unstable) diff(s) skipped.")
    sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
