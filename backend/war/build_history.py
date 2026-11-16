"""Run the WAR v1 calculation for every covered season and save the result to a local file. Reads only; writes nothing to
the database (storing results is a separate, approved step).

  <backend python> -m war.build_history [--inputs DIR] [--out FILE] [--golden]

--golden also writes tests/fixtures/war_v1_golden.json from this run (only when a calculation change is intended).
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
GOLDEN = os.path.join(os.path.dirname(HERE), 'tests', 'fixtures', 'war_v1_golden.json')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--inputs', default=os.environ.get('WAR_INPUTS', os.path.expanduser('~/baseballstats-tools/scripts/war')))
    ap.add_argument('--out', default=os.path.expanduser('~/baseballstats-tools/scripts/war/war_v1_history.pkl'))
    ap.add_argument('--golden', action='store_true')
    a = ap.parse_args()
    t = time.time()
    res = compute(conn().cursor(), a.inputs)
    pickle.dump(res, open(a.out, 'wb'))
    print(f"hitter seasons {len(res['hit'])}, pitcher seasons {len(res['pit'])}, {time.time() - t:.0f}s → {a.out}")
    if a.golden:
        json.dump(golden.summary(res), open(GOLDEN, 'w'), indent=1, sort_keys=True)
        print('golden fixture written:', GOLDEN)
