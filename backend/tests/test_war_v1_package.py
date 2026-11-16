#!/usr/bin/env python3
"""Our own WAR (backend/war, version 1): the package is self-contained, never reads Baseball-Reference columns, and still
produces the reviewed numbers.

⚠️ THE CHECKS THAT MATTER:
  • No file in backend/war names a stored Baseball-Reference column (WAR, WAR_off, WAR_def, WAA, runs_above_*): the
    calculation must keep working after those columns are deleted.
  • The frozen settings file has every section compute.py reads, with option B (one Total Zone scale from 2003) on.
  • When the input files are present (a machine with the Retrosheet-derived inputs; WAR_INPUTS or the default tools
    folder), a fresh run matches tests/fixtures/war_v1_golden.json: row counts, per-decade totals, and ~1,000 sampled
    seasons with every piece. Without the inputs that check is reported as SKIPPED, not passed silently.

Standalone, no pytest. Run: <backend python> backend/tests/test_war_v1_package.py
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


print("the WAR package never names a Baseball-Reference column")
PAT = re.compile(r'"WAR"|\bWAR_off\b|\bWAR_def\b|"WAA"|\bruns_above_(rep|avg)\b')
hits = []
for root, dirs, files in os.walk(os.path.join(BACKEND, 'war')):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            for n, line in enumerate(open(os.path.join(root, f), encoding='utf-8'), 1):
                if PAT.search(line):
                    hits.append(f'{os.path.relpath(os.path.join(root, f), BACKEND)}:{n}')
check('no stored-WAR column names in backend/war', not hits, hits[:10])

print("frozen settings")
P = json.load(open(os.path.join(BACKEND, 'war', 'params', 'war_params_v1.json')))
need = {'version', 'positions', 'fld_retro', 'fld_bdl', 'fld_totals', 'bat_delta', 'scalars'}
check('every section present', need <= set(P), sorted(need - set(P)))
check('version is v1', P.get('version') == 'v1', P.get('version'))
check('option B on (Total Zone weights from 2003)', P['fld_retro'].get('use_tz_weights_from_2003') == 1)
check('fielding weights for 4 eras × 8 positions', len(P['fld_retro']['coef']) == 32, len(P['fld_retro']['coef']))
for s in ('hit_rep_share', 'pit_rep_share', 'war_per_season_games', 'center_fielding', 'rep_pool_leagues', 'league_strength_k_1900'):
    check(f'scalar {s} present', s in P['scalars'])

print("a fresh run matches the reviewed numbers")
inputs = os.environ.get('WAR_INPUTS', os.path.expanduser('~/baseballstats-tools/scripts/war'))
have = all(os.path.exists(os.path.join(inputs, f)) for f in ('bat_raw.pkl', 'fld2_raw.pkl', 'tz_raw.pkl', 'coverage.json', 'bdl26.pkl'))
if not have:
    print(f'  [SKIPPED] input files not found in {inputs} (run on the machine that holds them)')
else:
    from war import golden
    from war.compute import compute
    from war.dbconn import conn
    res = compute(conn().cursor(), inputs)
    diffs = golden.compare(golden.summary(res), json.load(open(os.path.join(HERE, 'fixtures', 'war_v1_golden.json'))))
    check('matches tests/fixtures/war_v1_golden.json', not diffs, diffs[:8])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
