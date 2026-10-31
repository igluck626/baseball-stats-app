#!/usr/bin/env python3
"""The Lahman loader stores IP as decimal thirds (66.667), like every other IP
column — never rounded to tenths (66.7) (Isaac, 2026-10-30).

A tenths row displays correctly on its own (.3 reads as .1), but a total summed
from tenths comes up short: Ken Brett's three postseason lines are 4, 7 and 7
outs, six innings, and stored as 1.3 + 2.3 + 2.3 they summed to 5.9 — shown as
5.2. Real rows from data/lahman/PitchingPost.csv.

⚠️ THE CHECKS THAT MATTER:
  • `_pitching_derived` (season rows) stores IPouts / 3 to three places.
  • `_load_postseason_pitching` does the same, and Brett's lines sum to 6.0.
  • Every stored fraction is .0, .333 or .667.

Standalone, no pytest. Run: <backend python> backend/tests/test_lahman_ip_thirds.py
"""
import contextlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "scripts"), os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
import lahman_load                                                 # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def thirds(ip):
    """A stored IP whose fraction is .0, .333 or .667, to the stored precision."""
    return ip is not None and abs(ip * 3 - round(ip * 3)) < 0.01 and round(ip, 3) == ip


print("season rows (_pitching_derived)")
for outs, want in ((200, 66.667), (199, 66.333), (201, 67.0)):
    ip = lahman_load._pitching_derived(outs, 0, 0, 0, 0, 0, 0)["IP"]
    check(f"{outs} outs -> {want}", ip == want and thirds(ip), ip)
check("no outs -> None", lahman_load._pitching_derived(0, 0, 0, 0, 0, 0, 0)["IP"] is None)

print("postseason rows (_load_postseason_pitching)")
HEADER = "playerID,yearID,round,teamID,lgID,W,L,G,GS,CG,SHO,SV,IPouts,H,ER,HR,BB,SO,BAOpp,ERA,IBB,WP,HBP,BK,BFP,GF,R,SH,SF,GIDP"
BRETT = [
    "brettke01,1967,WS,BOS,AL,0,0,2,0,0,0,0,4,0,0,0,1,1,0,0,0,0,0,0,5,2,0,0,0,0",
    "brettke01,1974,NLCS,PIT,NL,0,0,1,0,0,0,0,7,3,2,1,2,1,0.333,7.71,0,0,0,0,11,0,2,0,0,0",
    "brettke01,1975,NLCS,PIT,NL,0,0,2,0,0,0,0,7,1,0,0,0,1,0.143,0,0,0,0,1,7,0,0,0,0,0",
]
saved = []


@contextlib.contextmanager
def fake_session():
    yield None


with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
    fh.write("\n".join([HEADER, *BRETT]) + "\n")
real = (lahman_load.PITCHING_POST_CSV, lahman_load.connection.get_session,
        lahman_load.crud.save_player_postseason_pitching)
lahman_load.PITCHING_POST_CSV = fh.name
lahman_load.connection.get_session = fake_session
lahman_load.crud.save_player_postseason_pitching = lambda db, rows: saved.extend(rows)
try:
    lahman_load._load_postseason_pitching({"brettke01": 1})
finally:
    (lahman_load.PITCHING_POST_CSV, lahman_load.connection.get_session,
     lahman_load.crud.save_player_postseason_pitching) = real
    os.unlink(fh.name)

ips = [r["IP"] for r in saved]
check("Brett's three lines: 1.333, 2.333, 2.333", ips == [1.333, 2.333, 2.333], ips)
check("  ...every one a third", all(thirds(ip) for ip in ips), ips)
check("  ...and they sum to six innings (18 outs), not 5.9",
      round(sum(ip * 3 for ip in ips)) == 18 and abs(sum(ips) - 6.0) < 0.01, sum(ips))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if results and all(results) else 1)
