"""Golden summary of a WAR v1 run, so any change to the calculation, its settings or its inputs shows up in a test.

summary(result) -> a small JSON-able dict: row counts, per-decade WAR totals, and every 200th hitter / every 150th pitcher
season (in a fixed order) with all of its pieces. tests/test_war_v1_package.py compares a fresh run against
tests/fixtures/war_v1_golden.json; regenerate the fixture only when a change is intended (and say so in the commit).
"""
from collections import defaultdict

FIELDS_HIT = ('bat', 'br', 'dp', 'rep', 'pos', 'fld', 'rpw', 'offense', 'defense', 'war')
FIELDS_PIT = ('waa', 'rep', 'war', 'IP')


def summary(res):
    out = {'version': res['params'], 'counts': {'hit': len(res['hit']), 'pit': len(res['pit'])}, 'decades': {}, 'sample': {'hit': {}, 'pit': {}}}
    dec = defaultdict(float)
    for side in ('hit', 'pit'):
        for (pid, y), v in res[side].items():
            dec[f'{side}:{y // 10 * 10}'] += v['war']
    out['decades'] = {k: round(v, 6) for k, v in sorted(dec.items())}
    for side, step, fields in (('hit', 200, FIELDS_HIT), ('pit', 150, FIELDS_PIT)):
        for i, k in enumerate(sorted(res[side])):
            if i % step == 0:
                out['sample'][side][f'{k[0]}:{k[1]}'] = {f: round(res[side][k][f], 6) for f in fields}
    return out


def compare(a, b, tol=1e-5):
    """List of human-readable differences between two summaries (empty = same)."""
    diffs = []
    if a['counts'] != b['counts']:
        diffs.append(f"row counts {a['counts']} vs {b['counts']}")
    for k in sorted(set(a['decades']) | set(b['decades'])):
        if abs(a['decades'].get(k, 0) - b['decades'].get(k, 0)) > 1e-3:
            diffs.append(f"decade total {k}: {a['decades'].get(k)} vs {b['decades'].get(k)}")
    for side in ('hit', 'pit'):
        for key in sorted(set(a['sample'][side]) | set(b['sample'][side])):
            x, y = a['sample'][side].get(key), b['sample'][side].get(key)
            if x is None or y is None:
                diffs.append(f'{side} {key}: missing on one side'); continue
            bad = [f for f in x if abs(x[f] - y[f]) > tol]
            if bad:
                diffs.append(f'{side} {key}: ' + ', '.join(f'{f} {x[f]} vs {y[f]}' for f in bad))
    return diffs
