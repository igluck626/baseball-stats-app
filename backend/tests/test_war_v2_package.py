#!/usr/bin/env python3
"""Our own WAR, version 2 (approved 2026-11-19): version 1 with one change — a pitcher's runs saved become wins at the
runs-per-win of his own games (the published pitcher runs-per-win formula: ((18 − IP/G) × league RA9 + (IP/G) × his
RA9) / 18, + 2, × 1.5, the league rate in his parks) instead of v1's blend of league runs-per-win and Pythagenpat. It closes v1's gap on the best
starters (aces since 2010 were about 0.8 wins low).

⚠️ THE CHECKS THAT MATTER:
  • war_params_v2.json differs from war_params_v1.json only in version, frozen_at and the four runs-per-win settings —
    v2 is v1 plus exactly this change, and v1's file is never edited (its stored seasons are final)
  • every hitter season is identical in v1 and v2; only pitchers move
  • pitcher wins above average still sum to zero within every league-season
  • a fresh v2 run matches tests/fixtures/war_v2_golden.json (when the input files are on this machine; SKIPPED, not
    passed, without them)

Standalone, no pytest. Run: <backend python> backend/tests/test_war_v2_package.py
"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


PDIR = os.path.join(BACKEND, 'war', 'params')
P1 = json.load(open(os.path.join(PDIR, 'war_params_v1.json')))
P2 = json.load(open(os.path.join(PDIR, 'war_params_v2.json')))

print("v2's settings are v1's plus one change")
check('version is v2', P2.get('version') == 'v2', P2.get('version'))
check('runs-to-wins model is the pitcher runs-per-win formula', P2['scalars'].get('pitcher_wins_model') == 'dynamic_rpw')
check('v1 keeps its blend (its stored seasons are final)', P1['scalars'].get('pitcher_wins_model', 'blend') == 'blend')
diff_top = sorted(k for k in set(P1) | set(P2) if k != 'scalars' and P1.get(k) != P2.get(k))
check('top level differs only in version and frozen_at', diff_top == ['frozen_at', 'version'], diff_top)
diff_sc = sorted(k for k in set(P1['scalars']) | set(P2['scalars']) if P1['scalars'].get(k) != P2['scalars'].get(k))
check('settings differ only in the four runs-per-win settings', diff_sc == ['dyn_rpw_add', 'dyn_rpw_mult', 'dyn_rpw_park_adjust', 'pitcher_wins_model'], diff_sc)

print("a fresh v2 run")
inputs = os.environ.get('WAR_INPUTS', os.path.expanduser('~/baseballstats-tools/scripts/war'))
have = all(os.path.exists(os.path.join(inputs, f)) for f in ('bat_raw.pkl', 'fld2_raw.pkl', 'tz_raw.pkl', 'coverage.json',
                                                            'coverage_league.json', 'li_raw.pkl', 'bdl26.pkl', 'bdl26_li.pkl'))
if not have:
    print(f'  [SKIPPED] input files not found in {inputs} (run on the machine that holds them)')
else:
    from war import golden
    from war.compute import compute
    from war.dbconn import conn
    season = int(os.environ.get('WAR_SEASON', '2026'))
    r2 = compute(conn().cursor(), inputs, params_path=os.path.join(PDIR, 'war_params_v2.json'), current=season)
    r1 = compute(conn().cursor(), inputs, params_path=os.path.join(PDIR, 'war_params_v1.json'), current=season)
    check('labelled v2', r2['params'] == 'v2', r2['params'])
    diffs = golden.compare(golden.summary(r2), json.load(open(os.path.join(HERE, 'fixtures', 'war_v2_golden.json'))))
    check('matches tests/fixtures/war_v2_golden.json', not diffs, diffs[:8])
    check('same seasons as v1 (hitters and pitchers)', set(r1['hit']) == set(r2['hit']) and set(r1['pit']) == set(r2['pit']))
    moved = [k for k in r1['hit'] if r1['hit'][k] != r2['hit'][k]]
    check('every hitter season is identical in v1 and v2', not moved, moved[:5])
    pw = defaultdict(float)
    for (p, y), v in r2['pit'].items():
        if v['IP'] > 0: pw[(y, v['league'])] += v['waa']
    check('pitcher wins above average sum to zero within every league-season', max(abs(x) for x in pw.values()) < 1e-6,
          max(abs(x) for x in pw.values()))
    rep_same = all(abs(r1['pit'][k]['rep'] - r2['pit'][k]['rep']) < 1e-12 for k in r1['pit'])
    check('replacement credit is unchanged (only wins above average moves)', rep_same)
    ace = r2['pit'][(650911, 2026)]['war'] - r1['pit'][(650911, 2026)]['war']      # Cristopher Sanchez 2026, the case that prompted v2
    # +0.30 with the league rate in his parks (Citizens Bank Park plays a little hitter-friendly); +0.48 without it
    check('an ace gains: Cristopher Sanchez 2026 is higher in v2', ace > 0.2, round(ace, 2))
    tot = lambda r: sum(v['war'] for v in r['pit'].values())
    check('league-wide pitcher WAR is unchanged to within 0.1% (centering holds the totals)', abs(tot(r2) - tot(r1)) / tot(r1) < 0.001,
          (round(tot(r1), 1), round(tot(r2), 1)))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
