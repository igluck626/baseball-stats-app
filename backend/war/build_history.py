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
    ap.add_argument('--season', type=int, default=int(os.environ.get('WAR_SEASON', '2026')),
                    help='the balldontlie (current) season; 0 = none, every season from Retrosheet up to --through')
    ap.add_argument('--through', type=int, help="last season, when --season 0 (the 'final' recompute)")
    a = ap.parse_args()
    t = time.time()
    res = compute(conn().cursor(), a.inputs, current=a.season or None, through=a.through)
    pickle.dump(res, open(a.out, 'wb'))
    print(f"hitter seasons {len(res['hit'])}, pitcher seasons {len(res['pit'])}, skipped {len(res['skipped'])}, {time.time() - t:.0f}s → {a.out}")
    for s in res['skipped']: print('  skipped:', s)
    if a.golden:
        json.dump(golden.summary(res), open(GOLDEN, 'w'), indent=1, sort_keys=True)
        print('golden fixture written:', GOLDEN)
