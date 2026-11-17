# lgstr2.py — READ-ONLY. League strength from switchers, stint level (within-season trades + season-to-season moves),
# hitters (OPS vs league) and pitchers (RA9 vs league, converted to OPS units with runs ∝ OPS^1.8). Log units.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
SEASON = int(_os.environ.get('WAR_SEASON', '2026'))                                                   # the balldontlie season
TAG = f'bdl{SEASON % 100:02d}'                                                                      # its input-file prefix
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser(f'~/baseballstats-tools/data/bdl{SEASON}'))   # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import os, sys, pickle, math, statistics
from collections import defaultdict
from war.dbconn import conn
cur = conn().cursor()
cur.execute('SELECT year, team_id, league FROM team_seasons'); TL = {(y, t): L for y, t, L in cur.fetchall()}
n = lambda v: float(v or 0)
cur.execute('SELECT player_id, year, team, "PA","AB","H",doubles,triples,"HR","BB","HBP","SF" FROM player_season_stints WHERE year BETWEEN 1900 AND %s', (SEASON,))
B = defaultdict(lambda: defaultdict(lambda: [0.0] * 9))      # B[(pid, y)][league] summed counts
for r in cur.fetchall():
    L = TL.get((r[1], r[2]))
    if L not in ('AL', 'NL', 'FL'): continue
    acc = B[(r[0], r[1])][L]
    for i, v in enumerate(r[3:12]): acc[i] += n(v)
cur.execute('SELECT player_id, year, team, "IP", "R" FROM pitcher_season_stints WHERE year BETWEEN 1900 AND %s', (SEASON,))
Pp = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
for pid, y, t, ip, R in cur.fetchall():
    L = TL.get((y, t))
    if L in ('AL', 'NL', 'FL'): a = Pp[(pid, y)][L]; a[0] += n(ip); a[1] += n(R)
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
def hrel(y, L, c): return math.log(ops(c) / (lgb[(y, L)][0] / lgb[(y, L)][1]))
def prel(y, L, c): return -math.log(max(0.3, 9 * c[1] / c[0]) / (9 * lgp[(y, L)][0] / lgp[(y, L)][1])) / 1.8
obs = defaultdict(list)       # obs[(y, A, B)] = (gain moving A->B, weight)
MINB, MINP = float(os.environ.get('MINB', '60')), float(os.environ.get('MINP', '20'))
for src, D, rel, mn in (('bat', B, hrel, MINB), ('pit', Pp, prel, MINP)):
    for (pid, y), m in D.items():
        if src == 'bat' and pid in pitcher_pid: continue
        ok = {L: c for L, c in m.items() if c[0] >= mn and (src == 'pit' or ops(c) > 0)}
        if len(ok) == 2:                                 # traded between leagues within the season
            (A, ca), (Bl, cb) = sorted(ok.items())
            obs[(y, A, Bl)].append((rel(y, Bl, cb) - rel(y, A, ca), min(ca[0], cb[0])))
        if len(ok) == 1:
            A, ca = next(iter(ok.items()))
            for dy in (1, 2):
                nx = D.get((pid, y + dy), {})
                okn = {L: c for L, c in nx.items() if c[0] >= mn}
                if len(okn) == 1:
                    Bl, cb = next(iter(okn.items()))
                    if Bl != A: obs[(y, A, Bl)].append((rel(y + dy, Bl, cb) - rel(y, A, ca), min(ca[0], cb[0]) / dy)); break
                if okn: break
W = int(os.environ.get('SW', '4'))
def wm(x): return sum(a * w for a, w in x) / sum(w for _, w in x)
STR = {}
for y in range(1900, SEASON + 1):
    pairs = {}
    for A, Bl in (('AL', 'NL'), ('AL', 'FL'), ('FL', 'NL')):
        ab = [t for yy in range(y - W, y + W + 1) for t in obs.get((yy, A, Bl), [])]
        ba = [t for yy in range(y - W, y + W + 1) for t in obs.get((yy, Bl, A), [])]
        if len(ab) >= 8 and len(ba) >= 8: pairs[(A, Bl)] = (wm(ab) - wm(ba)) / 2     # strength A − B
    g = pairs.get(('AL', 'NL'), 0.0); STR[(y, 'AL')], STR[(y, 'NL')] = g / 2, -g / 2
    if y in (1914, 1915) and ('AL', 'FL') in pairs: STR[(y, 'FL')] = STR[(y, 'AL')] - pairs[('AL', 'FL')]
cnt = defaultdict(int)
for (y, A, Bl), v in obs.items(): cnt[y // 10 * 10] += len(v)
print('observations per decade:', dict(sorted(cnt.items())))
for y in (1910, 1915, 1925, 1935, 1950, 1960, 1965, 1975, 1990, 2000, 2010, 2015, 2023):
    print(y, {L: round(STR.get((y, L), 0), 3) for L in ('AL', 'NL', 'FL') if (y, L) in STR})
pickle.dump(STR, open(os.path.join(INPUTS, 'lgstr.pkl'), 'wb'))
