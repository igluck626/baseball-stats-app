# lgstr_pre1900.py — READ-ONLY. League strength for 1871–1899 (NA, NL, AA, UA, PL) from players who switched leagues,
# the same method as lgstr2.py (which covers 1900+ and is left untouched). Strength is measured relative to the NL, so the
# newer, weaker leagues get less replacement credit and the season total falls (as B-Ref's does). Output lgstr_pre1900.pkl {(year, league): log strength}.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser('~/baseballstats-tools/data/bdl2026'))      # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import os, sys, pickle, math
from collections import defaultdict
LG = ('NA', 'NL', 'AA', 'UA', 'PL')
import csv
n = lambda v: float(v or 0)
LAH = LAHMAN       # stints by league come from Lahman (our stints table starts later)
TL = {}
for r in csv.DictReader(open(os.path.join(LAH, 'Teams.csv'), encoding='utf-8-sig')):
    if int(r['yearID']) <= 1901: TL[(int(r['yearID']), r['teamID'])] = r['lgID']
B = defaultdict(lambda: defaultdict(lambda: [0.0] * 9))
for r in csv.DictReader(open(os.path.join(LAH, 'Batting.csv'), encoding='utf-8-sig')):
    y = int(r['yearID'])
    if y > 1901 or r['lgID'] not in LG: continue
    pa = n(r['AB']) + n(r['BB']) + n(r['HBP']) + n(r['SF']) + n(r['SH'])
    acc = B[(r['playerID'], y)][r['lgID']]
    for i, v in enumerate((pa, n(r['AB']), n(r['H']), n(r['2B']), n(r['3B']), n(r['HR']), n(r['BB']), n(r['HBP']), n(r['SF']))): acc[i] += v
Pp = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
for r in csv.DictReader(open(os.path.join(LAH, 'Pitching.csv'), encoding='utf-8-sig')):
    y = int(r['yearID'])
    if y > 1901 or r['lgID'] not in LG: continue
    a = Pp[(r['playerID'], y)][r['lgID']]; a[0] += n(r['IPouts']) / 3; a[1] += n(r['R'])
def ops(c):
    PA, AB, H, D, T, HR, BB, HBP, SF = c
    return (H + BB + HBP) / max(1, AB + BB + HBP + SF) + (H + D + 2 * T + 3 * HR) / max(1, AB)
pitcher_pid = {k[0] for k, m in Pp.items() if sum(v[0] for v in m.values()) >= 20}
lgb = defaultdict(lambda: [0.0, 0.0]); lgp = defaultdict(lambda: [0.0, 0.0])
for (pid, y), m in B.items():
    if pid in pitcher_pid: continue
    for L, c in m.items(): lgb[(y, L)][0] += ops(c) * c[0]; lgb[(y, L)][1] += c[0]
for (pid, y), m in Pp.items():
    for L, c in m.items(): lgp[(y, L)][0] += c[1]; lgp[(y, L)][1] += c[0]
hrel = lambda y, L, c: math.log(ops(c) / (lgb[(y, L)][0] / lgb[(y, L)][1]))
prel = lambda y, L, c: -math.log(max(0.3, 9 * c[1] / c[0]) / (9 * lgp[(y, L)][0] / lgp[(y, L)][1])) / 1.8
obs = defaultdict(list)
for src, D, rel, mn in (('bat', B, hrel, 60.0), ('pit', Pp, prel, 20.0)):
    for (pid, y), m in D.items():
        if src == 'bat' and pid in pitcher_pid: continue
        ok = {L: c for L, c in m.items() if c[0] >= mn and (src == 'pit' or ops(c) > 0)}
        if len(ok) == 2:
            (A, ca), (Bl, cb) = sorted(ok.items()); obs[(y, A, Bl)].append((rel(y, Bl, cb) - rel(y, A, ca), min(ca[0], cb[0])))
        if len(ok) == 1:
            A, ca = next(iter(ok.items()))
            for dy in (1, 2):
                okn = {L: c for L, c in D.get((pid, y + dy), {}).items() if c[0] >= mn}
                if len(okn) == 1:
                    Bl, cb = next(iter(okn.items()))
                    if Bl != A: obs[(y, A, Bl)].append((rel(y + dy, Bl, cb) - rel(y, A, ca), min(ca[0], cb[0]) / dy)); break
                if okn: break
wm = lambda x: sum(a * w for a, w in x) / sum(w for _, w in x)
STR = {}
for y in range(1871, 1900):
    active = sorted({L for (yy, t), L in TL.items() if yy == y and L in LG})
    if len(active) < 2: continue
    rel_nl = {'NL': 0.0}
    for L in active:
        if L == 'NL': continue
        W = 4
        ab = [t for yy in range(y - W, y + W + 1) for t in obs.get((yy, *sorted((L, 'NL'))), [])]
        ba = [t for yy in range(y - W, y + W + 1) for t in obs.get((yy, *sorted((L, 'NL'))[::-1]), [])]
        if len(ab) >= 8 and len(ba) >= 8:
            a_, b_ = sorted((L, 'NL')); s = (wm(ab) - wm(ba)) / 2          # strength a_ − b_
            rel_nl[L] = s if a_ == L else -s
    for L, v in rel_nl.items(): STR[(y, L)] = v       # relative to the NL (the established league), not centred
for y in (1876, 1882, 1884, 1886, 1890, 1891):
    print(y, {L: round(v, 3) for (yy, L), v in STR.items() if yy == y})
pickle.dump(STR, open(os.path.join(INPUTS, 'lgstr_pre1900.pkl'), 'wb'))
if os.environ.get('DEBUG'):
    from collections import Counter
    print('obs', Counter((k[1], k[2]) for k in obs for _ in obs[k]).most_common(10))
    print('stints by league', Counter(L for m in B.values() for L in m).most_common())
