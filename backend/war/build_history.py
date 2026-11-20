"""Run the WAR calculation for every covered season and save the result to a local file. Reads only; writes nothing to
the database (storing results is a separate, approved step).

  <backend python> -m war.build_history [--version v1|v2] [--inputs DIR] [--out FILE] [--golden]

--version picks the frozen settings file war/params/war_params_<version>.json (default v2, the version
the app serves).
--golden also writes tests/fixtures/war_<version>_golden.json from this run (only when a calculation change is intended).
"""
import argparse
import json
import os
import pickle
import time

from war import golden
from war.compute import compute
from war.dbconn import conn

HERE = os.path.dirname(os.path.abspath(__file__))

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--inputs', default=os.environ.get('WAR_INPUTS', os.path.expanduser('~/baseballstats-tools/scripts/war')))
    ap.add_argument('--version', default='v2', help='which frozen settings file: war/params/war_params_<version>.json')
    ap.add_argument('--out', help='default ~/baseballstats-tools/scripts/war/war_<version>_history.pkl')
    ap.add_argument('--golden', action='store_true')
    ap.add_argument('--season', type=int, default=int(os.environ.get('WAR_SEASON', '2026')),
                    help='the balldontlie (current) season; 0 = none, every season from Retrosheet up to --through')
    ap.add_argument('--through', type=int, help="last season, when --season 0 (the 'final' recompute)")
    a = ap.parse_args()
    params = os.path.join(HERE, 'params', f'war_params_{a.version}.json')
    a.out = a.out or os.path.expanduser(f'~/baseballstats-tools/scripts/war/war_{a.version}_history.pkl')
    GOLDEN = os.path.join(os.path.dirname(HERE), 'tests', 'fixtures', f'war_{a.version}_golden.json')
    t = time.time()
    res = compute(conn().cursor(), a.inputs, params_path=params, current=a.season or None, through=a.through)
    assert res['params'] == a.version, (res['params'], a.version)
    pickle.dump(res, open(a.out, 'wb'))
    print(f"hitter seasons {len(res['hit'])}, pitcher seasons {len(res['pit'])}, skipped {len(res['skipped'])}, "
          f"no playing time {len(res['empty'])}, {time.time() - t:.0f}s → {a.out}")
    for s in res['skipped']: print('  skipped:', s)
    print('  pre-1898 traded players from Lahman:', res['lahman_stints'])
    if a.golden:
        json.dump(golden.summary(res), open(GOLDEN, 'w'), indent=1, sort_keys=True)
        print('golden fixture written:', GOLDEN)
