#!/usr/bin/env python3
"""load_season_teams maps Lahman's per-club rows through the JUDGED bridge
(lahman_load.judge_lahman_ids plus data/lahman_bridge_judgments.json), never
through People.bbrefID.

⚠️ THE CHECK THAT MATTERS: along a few chains Lahman shifted bbrefID onto the
next man. Luther Gilyard (Lahman gilyalu01) carries bbrefID 'gillipe01', which
is Pen Gilliard's id, so keying by it filed Gilyard's 1937 club rows under
Gilliard (819382). The judged bridge files them under Gilyard (819376).

Real files: backend/data/lahman/People.csv and chadwick_mlb.csv, and the
judgments file. Standalone, no pytest.
Run: <backend python> backend/tests/test_season_teams_bridge.py
"""
import csv
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "scripts"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "api")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
import load_season_teams as lst                                    # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


people = {p["playerID"]: p for p in csv.DictReader(open(os.path.join(lst.LAHMAN, "People.csv"), encoding="utf-8-sig"))}
bridge = lst.lahman_bridge()

print("the shifted chain")
check("Lahman gilyalu01 carries Pen Gilliard's bbrefID (the trap)", people["gilyalu01"]["bbrefID"] == "gillipe01")
check("  ...and the judged bridge files Gilyard under Gilyard (819376), not Gilliard (819382)",
      bridge.get("gilyalu01") == 819376 and bridge.get("gillipe01") == 819382,
      (bridge.get("gilyalu01"), bridge.get("gillipe01")))
check("Fabré and Fabelo are not swapped", bridge.get("fabreis01") == 819596 and bridge.get("fabelju01") == 819598,
      (bridge.get("fabreis01"), bridge.get("fabelju01")))

print("the hand judgments")
check("Bill Harris: harribi05 -> 819165; the other Bill Harris (harribi06) maps to no one",
      bridge.get("harribi05") == 819165 and bridge.get("harribi06") is None)
check("Roy S. Williams: williro02 -> 816544", bridge.get("williro02") == 816544)
check("no two Lahman ids land on one player",
      len(set(bridge.values())) == len(bridge), len(bridge) - len(set(bridge.values())))

print("a judgment can't override the judge")
bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"judgments": [{"playerID": "gilyalu01", "mlbam": 819382, "why": "test"}]}, bad)
bad.close()
saved, lst.JUDGMENTS = lst.JUDGMENTS, bad.name
try:
    lst.lahman_bridge()
    check("a judgment contradicting the judged bridge is refused", False, "no error")
except SystemExit as exc:
    check("a judgment contradicting the judged bridge is refused", "contradicts" in str(exc), exc)
finally:
    lst.JUDGMENTS = saved
    os.unlink(bad.name)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
