# bdl26_build.py — READ-ONLY. From the BDL 2026 download: innings by position (lineups + batting-slot substitutions),
# batting-feed counts (PA, reached on error, GIDP chances) per player. Output bdl26.pkl keyed by OUR player_id.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser('~/baseballstats-tools/data/bdl2026'))      # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import json, os, sys, pickle, collections
OUT = BDL_DIR
from war.dbconn import conn
cur = conn().cursor(); cur.execute('SELECT bdl_id, player_id FROM players WHERE bdl_id IS NOT NULL')
B2P = collections.defaultdict(list)
for b, p in cur.fetchall(): B2P[b].append(p)
amb = {b for b, v in B2P.items() if len(v) > 1}
G = {g['id']: g for g in json.load(open(os.path.join(OUT, 'games.json'))) if g.get('season_type') == 'regular' and g.get('status') == 'STATUS_FINAL'}
def load(kind):
    f = os.path.join(OUT, kind + '.jsonl')
    return {j['game_id']: j['data'] for j in map(json.loads, open(f)) if j['game_id'] in G} if os.path.exists(f) else {}
LU, PA, ST = load('lineups'), load('pa'), load('stats')
FPOS = {'C': 2, '1B': 3, '2B': 4, '3B': 5, 'SS': 6, 'LF': 7, 'CF': 8, 'RF': 9, 'DH': 10}
INN = collections.defaultdict(lambda: collections.defaultdict(float)); GS = collections.defaultdict(int)
prof = {}
for gid, rows in ST.items():
    for r in rows: prof[r['player']['id']] = r['player'].get('position')
games_used = 0
STAT = collections.Counter()
SPANS = collections.defaultdict(list)
for gid in set(LU) & set(PA):
    lu = [x for x in LU[gid] if x.get('batting_order')]
    pas = sorted(PA[gid], key=lambda x: x['pa_number'])
    if len(lu) < 18 or not pas: continue
    teams = collections.defaultdict(dict)            # team id -> slot -> (bdl player, position)
    for x in lu: teams[x['team']['id'] if isinstance(x.get('team'), dict) else x.get('team_id')][x['batting_order']] = (x['player']['id'], x['position'])
    # which team bats in each half: match the first PA batter to a lineup
    half_team = {}
    for p in pas:
        for t, sl in teams.items():
            if any(p['batter_id'] == v[0] for v in sl.values()): half_team.setdefault(p['half_inning'], t)
    if len(half_team) < 2: continue
    games_used += 1
    for half, t in half_team.items():
        seq = [p for p in pas if p['half_inning'] == half]
        sl = teams[t]
        occ = {k: [(v[0], v[1], 1.0)] for k, v in sl.items()}   # slot -> [(player, pos, entry inning)]
        last_inn = {k: 1 for k in sl}
        ptr, prev_b = 1, None
        for p in seq:
            b = p['batter_id']
            if b == prev_b: STAT['repeat'] += 1; last_inn[ptr - 1 or 9] = p['inning']; continue   # same batter again (non-PA row / inning ended mid-PA)
            where = [k for k, lst in occ.items() if lst[-1][0] == b]
            if where:
                k = where[0]
                if k != ptr: STAT['resync'] += 1
            else:
                k = ptr
                if k not in occ: ptr = k % 9 + 1; continue
                prev_pos = occ[k][-1][1]
                pp = prof.get(b)
                if prev_pos == 'DH': pos = 'DH'
                elif pp in FPOS and pp != 'DH' and not (pp in ('LF', 'CF', 'RF') and prev_pos in ('LF', 'CF', 'RF')): pos = pp
                else: pos = prev_pos
                occ[k].append((b, pos, (last_inn[k] + p['inning']) / 2.0)); STAT['sub'] += 1
            last_inn[k] = p['inning']; ptr = k % 9 + 1; prev_b = b
        for k, lst in occ.items():
            for j, (b, pos, e) in enumerate(lst):
                x_ = lst[j + 1][2] if j + 1 < len(lst) else 10.0
                SPANS[(gid, t)].append((b, pos, e, x_ if j + 1 < len(lst) else 99.0))
                if pos in FPOS and b not in amb and b in B2P:
                    INN[B2P[b][0]][FPOS[pos]] += max(0.0, x_ - e)
                    if j == 0: GS[(B2P[b][0], FPOS[pos])] += 1
if os.environ.get('LUONLY') == '1':               # lineups only (before the PA download): each start = 8.0 innings
    INN.clear(); GS.clear(); games_used = 0
    for gid, lu in LU.items():
        games_used += 1
        for x in lu:
            b = x['player']['id']
            if x.get('batting_order') and x['position'] in FPOS and b not in amb and b in B2P:
                INN[B2P[b][0]][FPOS[x['position']]] += 8.0; GS[(B2P[b][0], FPOS[x['position']])] += 1
print(dict(STAT)); print('games used', games_used, 'of', len(G), '; players with innings', len(INN))
# batting-feed counts per batter
PBP = collections.defaultdict(lambda: [0, 0, 0, 0])    # PA, reached on error, GIDP, GIDP chances
for gid, pas in PA.items():
    for p in pas:
        b = p['batter_id']
        if b in amb or b not in B2P: continue
        c = PBP[B2P[b][0]]; c[0] += 1; c[1] += p['result'] == 'Field Error'
        ch = p['runner_on_first'] and p['outs'] < 2; c[3] += ch; c[2] += ch and p['result'] in ('GIDP', 'Grounded Into DP')
pickle.dump(dict(INN={k: dict(v) for k, v in INN.items()}, GS=dict(GS), PBP=dict(PBP), games=games_used, SPANS=dict(SPANS), HALF={}), open(os.path.join(INPUTS, 'bdl26.pkl'), 'wb'))
print(collections.Counter(p['result'] for pas in PA.values() for p in pas).most_common(40))
