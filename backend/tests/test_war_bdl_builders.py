#!/usr/bin/env python3
"""The balldontlie-season WAR builders read from the compact store (war/bdl_store.py) give exactly what they give from
the original raw download, for all of 2026.

⚠️ THE CHECKS THAT MATTER:
  • bdl_season, bdl_fielding, bdl_runners and bdl_leverage built from the store (trimmed rows, /games order) equal the
    same builders run on the raw 1.5 GB download, value for value (not to rounding)
  • and equal the stored 2026 input files the published WAR was computed from, less one entry: the All-Star Game's
    ballpark under team 'UNK' in bdl26.pkl's VENUE, which is now left out (no WAR step reads it)

Needs this Mac's local files (the store at ~/baseballstats-tools/data/war_store, the raw download at
~/baseballstats-tools/data/bdl2026, the input files at ~/baseballstats-tools/scripts/war) and a read-only database
connection for the balldontlie id map; each part is SKIPPED, not passed, when its files are missing.
If only the stored-file comparison fails, first check whether the players' balldontlie ids changed since 2026-11-12.

Standalone, no pytest. Run: PGOPTIONS='-c default_transaction_read_only=on' <backend python> backend/tests/test_war_bdl_builders.py
"""
import copy
import os
import pickle
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
results, skipped = [], []

STORE = os.path.expanduser('~/baseballstats-tools/data/war_store')
RAW = os.path.expanduser('~/baseballstats-tools/data/bdl2026')
INPUTS = os.path.expanduser('~/baseballstats-tools/scripts/war')
FILES = ('bdl26.pkl', 'bdl26_fld.pkl', 'bdl26_runs.pkl', 'bdl26_li.pkl')


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def skip(label, why):
    skipped.append(label)
    print(f"  [SKIPPED] {label}  ({why})")


if not (os.path.isdir(os.path.join(STORE, 'bdl2026')) and os.path.exists(os.path.join(INPUTS, 'li_raw.pkl'))):
    skip('all checks', 'no local store or li_raw.pkl on this machine')
    print(f"\n0 passed, {len(skipped)} skipped")
    sys.exit(0)

from war import bdl_store as bs  # noqa: E402
from war.builders import bdl_fielding, bdl_leverage, bdl_runners, bdl_season  # noqa: E402
from war.builders.bdl_common import bdl_maps, load_raw  # noqa: E402
from war.dbconn import conn  # noqa: E402

MAPS = bdl_maps(conn().cursor())
TABLE = pickle.load(open(os.path.join(INPUTS, 'li_raw.pkl'), 'rb'))['TABLE']
quiet = lambda *a: None


def build_all(G, LU, PA, ST, PL):
    b = bdl_season.build(G, LU, PA, ST, MAPS[0], log=quiet)
    return {'bdl26.pkl': b,
            'bdl26_fld.pkl': bdl_fielding.build(G, PA, ST, b['SPANS'], MAPS[0], 2026, log=quiet),
            'bdl26_runs.pkl': bdl_runners.build(set(G), ST, PL, b['SPANS'], MAPS[0], log=quiet),
            'bdl26_li.pkl': bdl_leverage.build(set(G), PA, PL, MAPS[1], TABLE, log=quiet)}


print('2026 from the compact store')
G, LU, PA, ST, PL, missing = bs.load(STORE, 2026)
check('every 2026 final is in the store (2,429: the All-Star Game left out)', len(G) == 2429 and not missing,
      (len(G), missing[:5]))
S = build_all(G, LU, PA, ST, PL)
check('2,414 games give fielding spans, as in the published 2026 build', S['bdl26.pkl']['games'] == 2414,
      S['bdl26.pkl']['games'])
del G, LU, PA, ST, PL

print('the same builders on the raw download')
if os.path.exists(os.path.join(RAW, 'pa.jsonl')):
    R = build_all(*load_raw(RAW))
    for f in FILES:
        check(f'{f}: store and raw download identical', S[f] == R[f])
    del R
else:
    skip('store vs raw download', 'no raw download on this machine')

print('the stored input files the published 2026 WAR used')
if all(os.path.exists(os.path.join(INPUTS, f)) for f in FILES):
    for f in FILES:
        st = pickle.load(open(os.path.join(INPUTS, f), 'rb'))
        if f == 'bdl26.pkl':
            st = copy.deepcopy(st)
            check('the stored VENUE\'s only extra entry is the All-Star Game (UNK)',
                  set(st['VENUE']) - set(S[f]['VENUE']) == {'UNK'}, set(st['VENUE']) ^ set(S[f]['VENUE']))
            st['VENUE'].pop('UNK', None)
        check(f'{f}: identical to the stored file', S[f] == st)
else:
    skip('store vs stored input files', 'input files missing')

print(f"\n{sum(results)}/{len(results)} passed" + (f", {len(skipped)} skipped" if skipped else ""))
sys.exit(0 if all(results) else 1)
