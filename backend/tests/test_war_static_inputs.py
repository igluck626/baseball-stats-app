#!/usr/bin/env python3
"""The nightly's small static inputs (war/static/current_season.json) give the same current-season WAR as the full
Retrosheet-derived input files.

⚠️ THE CHECKS THAT MATTER:
  • the JSON holds exactly the pieces of bat_raw / tder_raw / li_raw / lgstr it says it does (bit-exact floats)
  • compute(y0=2026, current=2026) on the JSON plus the four balldontlie files equals the same run on the whole input
    folder, row for row and value for value
  • the JSON is small (under 300 KB) and was exported from the input files on this machine (source hashes match)

Needs this Mac's input files (~/baseballstats-tools/scripts/war) and a read-only database connection; SKIPPED, not
passed, without the input files.

Standalone, no pytest. Run: PGOPTIONS='-c default_transaction_read_only=on' <backend python> backend/tests/test_war_static_inputs.py
"""
import hashlib
import json
import os
import pickle
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
results = []
W = os.path.expanduser('~/baseballstats-tools/scripts/war')


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


from war import static_inputs as si  # noqa: E402

doc = json.load(open(si.PATH))
print('the file')
check('under 300 KB', os.path.getsize(si.PATH) < 300 * 1024, os.path.getsize(si.PATH))
if not os.path.exists(os.path.join(W, 'li_raw.pkl')):
    print('  [SKIPPED] comparisons with the input files (none on this machine)')
    print(f"\n{sum(results)}/{len(results)} passed, comparisons skipped")
    sys.exit(0 if all(results) else 1)

pk = lambda n: pickle.load(open(os.path.join(W, n), 'rb'))
check('exported from the input files on this machine (source hashes match)',
      all(doc['source_sha256'][n] == hashlib.sha256(open(os.path.join(W, n), 'rb').read()).hexdigest() for n in si.SOURCES),
      'the input files changed since the export: re-run `python -m war.static_inputs export`')
S_ = si.load()
last = doc['retrosheet_through']
check('run values = bat_raw LW for the last Retrosheet season', S_['bat_raw.pkl']['LW'] == {last: pk('bat_raw.pkl')['LW'][last]})
check('team defense park factors = tder_raw for that season', S_['tder_raw.pkl'] == {k: v for k, v in pk('tder_raw.pkl').items() if k[0] == last})
check('leverage table = li_raw TABLE', S_['li_raw.pkl']['TABLE'] == pk('li_raw.pkl')['TABLE'])
check('league strength = lgstr after that season', S_['lgstr.pkl'] == {k: v for k, v in pk('lgstr.pkl').items() if k[0] > last})

print('compute for 2026: static file vs the whole input folder')
from war.compute import compute  # noqa: E402
from war.dbconn import conn  # noqa: E402
inputs = dict(S_)
for n in ('bdl26.pkl', 'bdl26_fld.pkl', 'bdl26_runs.pkl', 'bdl26_li.pkl'):
    inputs[n] = pk(n)
cur = conn().cursor()
a = compute(cur, W, y0=2026, current=2026)
b = compute(cur, None, y0=2026, current=2026, inputs=inputs)
for k in ('hit', 'pit', 'skipped', 'empty'):
    check(f'{k}: identical ({len(a[k])} rows)', a[k] == b[k])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
