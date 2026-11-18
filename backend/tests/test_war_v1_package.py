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
SIGN = dict(range=1, dp=1, tot=1, err=-1, zr=1, rbp=1, tzp=1, tder=1, xb=-1, kill=1, cs=1, pb=-1, catt=-1, ccs=1, cwp=-1, cpo=1)
wrong = [(k, f, round(c, 3)) for k, d in P['fld_retro']['coef'].items() for f, c in d.items() if c * SIGN[f] < -1e-12]
check('every fielding weight has its sensible sign (more plays good, errors bad)', not wrong, wrong[:6])
SIGN_T = dict(A=1, PO=1, DP=1, E=-1)
wrong_t = [(g, f, c) for g, d in P['fld_totals']['coef'].items() for f, c in d.items() if c * SIGN_T[f] < -1e-12]
check('season-totals fielding weights have sensible signs too', not wrong_t, wrong_t)
check('position values from the 1870s on', min(int(e) for e in P['positions']['by_era']) == 1870)
for s in ('start_to_relief_ip_ratio', 'pre1910_team_def_scale', 'team_der_weight'):
    check(f'setting {s} lives in the settings file', s in P['scalars'])
check('replacement shares add to about 1,000 WAR a season', 0.95 <= P['scalars']['hit_rep_share'] + P['scalars']['pit_rep_share'] <= 1.0)
for s in ('hit_rep_share', 'pit_rep_share', 'war_per_season_games', 'center_fielding', 'center_positions', 'center_pitcher_waa',
          'reliever_leverage', 'reliever_rep_weight', 'rep_pool_leagues', 'league_strength_k_1900'):
    check(f'scalar {s} present', s in P['scalars'])

print("a fresh run matches the reviewed numbers")
inputs = os.environ.get('WAR_INPUTS', os.path.expanduser('~/baseballstats-tools/scripts/war'))
have = all(os.path.exists(os.path.join(inputs, f)) for f in ('bat_raw.pkl', 'fld2_raw.pkl', 'tz_raw.pkl', 'coverage.json',
                                                            'coverage_league.json', 'li_raw.pkl', 'bdl26.pkl', 'bdl26_li.pkl'))
if not have:
    print(f'  [SKIPPED] input files not found in {inputs} (run on the machine that holds them)')
else:
    from war import golden
    from war.compute import compute
    from war.dbconn import conn
    season = int(os.environ.get('WAR_SEASON', '2026'))       # the balldontlie (current) season the golden file was made with
    res = compute(conn().cursor(), inputs, current=season)
    diffs = golden.compare(golden.summary(res), json.load(open(os.path.join(HERE, 'fixtures', 'war_v1_golden.json'))))
    check('matches tests/fixtures/war_v1_golden.json', not diffs, diffs[:8])
    check('every unrated season is listed with a reason, and only a handful', len(res['skipped']) <= 10 and all(len(x) == 4 for x in res['skipped']),
          res['skipped'][:5])
    from collections import defaultdict
    lt = defaultdict(lambda: [0.0, 0.0])
    for (p, y), v in res['hit'].items():
        if not v['pitcher']: lt[(y, v['league'])][0] += v['fld']; lt[(y, v['league'])][1] += v['pos']
    worst = max(abs(x) for t in lt.values() for x in t[:2])
    check('fielding and position runs sum to zero within every league-season', worst < 1e-6, worst)
    pw = defaultdict(float)
    for (p, y), v in res['pit'].items():
        if v['IP'] > 0: pw[(y, v['league'])] += v['waa']
    check('pitcher wins above average sum to zero within every league-season', max(abs(x) for x in pw.values()) < 1e-6, max(abs(x) for x in pw.values()))
    bt = defaultdict(float)
    for (p, y), v in res['hit'].items():
        if not v['pitcher']: bt[(y, v['league'])] += v['bat']
    check('batting runs of position players sum to zero within every league-season', max(abs(x) for x in bt.values()) < 1e-6, max(abs(x) for x in bt.values()))
    check('rows with no playing time are listed with a reason, not dropped', all(len(x) == 4 and x[3] for x in res['empty']), res['empty'][:3])
    ls = res['lahman_stints']
    check('pre-1898 traded players: every Lahman match confirmed against our season rows', ls['bat_confirmed'] > 500 and ls['pit_confirmed'] > 100
          and not ls['bat_unconfirmed'] and not ls['pit_unconfirmed'], ls)
    c_ = conn().cursor()
    c_.execute('SELECT player_id, year, "ER" FROM pitcher_seasons WHERE "IP" > 0 AND "R" IS NULL AND "ER" IS NOT NULL')
    nr = [(k, er) for *k, er in c_.fetchall() if tuple(k) in res['pit']]
    check('a blank runs-allowed falls back to earned runs, never zero', all(res['pit'][tuple(k)]['R_'] == er for k, er in nr), nr[:3])
    zp = [(k, v['war']) for k, v in res['hit'].items() if v['PA'] == 0]
    check('players with no plate appearances (pinch runners, defensive subs) are rated', len(zp) > 100 and any(abs(w) > 0.05 for _, w in zp), len(zp))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
