#!/usr/bin/env python3
"""lahman_load._load_chadwick_bridge — the bridge every Lahman loader and admin
backfill uses — is keyed by Lahman playerID through the judge, plus the hand
judgments file (audit item 2, Isaac 2026-11-01).

⚠️ THE CHECKS THAT MATTER:
  • Lahman playerID and Baseball-Reference id differ, and along some names
    they are SWAPPED: Lahman mannima01 is Matt Manning (bbref mannima02), and
    mannima02 is Max Manning (bbref mannima01). Keying by bbref id gave Matt
    Max's 1939-41 rows and two All-Star selections.
  • Ids with punctuation on Baseball-Reference (surhob.01, sabatc.01) still map.
  • The three hand-judged held ids (data/lahman_bridge_judgments.json) map, so
    a reload keeps their rows; a judgment contradicting the judge stops it.

Real files: backend/data/lahman/People.csv, chadwick_mlb.csv and the
judgments file. Standalone, no pytest.
Run: <backend python> backend/tests/test_lahman_bridge_judged.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "scripts"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
import lahman_load as ll                                          # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


bridge = ll._load_chadwick_bridge()

print("swapped ids follow Lahman's playerID, never the bbref id")
for pid, want, who in (("mannima01", 666159, "Matt Manning"), ("mannima02", 818363, "Max Manning"),
                       ("brownbe01", 676962, "Ben Brown"), ("brownbe02", 820350, "Benny Brown"),
                       ("harriwi01", 407483, "Willie Harris"), ("harriwi02", 501789, "Will Harris")):
    check(f"{pid} -> {who} {want}", bridge.get(pid) == want, bridge.get(pid))

print("punctuated Baseball-Reference ids")
check("surhobj01 -> B.J. Surhoff 122989", bridge.get("surhobj01") == 122989, bridge.get("surhobj01"))
check("sabatcc01 -> CC Sabathia 282332", bridge.get("sabatcc01") == 282332, bridge.get("sabatcc01"))

print("hand judgments")
for pid, want in (("harribi05", 819165), ("williro02", 816544), ("fabelju01", 819598)):
    check(f"{pid} -> {want}", bridge.get(pid) == want, bridge.get(pid))

bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"judgments": [{"playerID": "mannima01", "mlbam": 818363}]}, bad); bad.close()
real = ll.JUDGMENTS_JSON
ll.JUDGMENTS_JSON = bad.name
try:
    ll._load_chadwick_bridge()
    check("a judgment contradicting the judge stops the load", False, "no stop")
except SystemExit as e:
    check("a judgment contradicting the judge stops the load", "contradicts" in str(e), e)
finally:
    ll.JUDGMENTS_JSON = real
    os.unlink(bad.name)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if results and all(results) else 1)
