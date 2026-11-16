# bdl26_fld.py — READ-ONLY. 2026 fielding features from the BDL download (no Retrosheet for 2026):
#   zr   zone range: each ball in play is charged to the position whose area it lands in (spray angle, distance,
#        launch angle); actual out minus league P(out | angle bin, distance bin, batted-ball type, EV bin)
#   tot  assists (infield) / putouts (outfield) from /stats, minus league rate × balls in play at the position
#   err  errors from /stats, same baseline
#   catt/ccs  catchers: steal attempts / caught stealing by opposing runners (from their /stats) vs league per inning
# The fielder at a position is the player whose span (bdl26_build SPANS) covers that inning.
# Output bdl26_fld.pkl: X[(our pid, 2026)][pos] = {feature: value, 'n': balls in play at that position, 'inn': innings}
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser('~/baseballstats-tools/data/bdl2026'))      # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import json, os, sys, math, pickle, collections
OUT = BDL_DIR; W = INPUTS
B = pickle.load(open(os.path.join(W, 'bdl26.pkl'), 'rb')); SP = B['SPANS']
from war.dbconn import conn
cur = conn().cursor(); cur.execute('SELECT bdl_id, player_id FROM players WHERE bdl_id IS NOT NULL')
B2P = collections.defaultdict(list)
for b, p in cur.fetchall(): B2P[b].append(p)
pid_of = lambda b: B2P[b][0] if len(B2P.get(b, [])) == 1 else None
FP = {'C': 2, '1B': 3, '2B': 4, '3B': 5, 'SS': 6, 'LF': 7, 'CF': 8, 'RF': 9}
G = {g['id']: g for g in json.load(open(os.path.join(OUT, 'games.json'))) if g.get('season_type') == 'regular' and g.get('status') == 'STATUS_FINAL'}
teams_of = collections.defaultdict(list)
for (gid, t) in SP: teams_of[gid].append(t)
def fielder(gid, fteam, pos, inn):
    c = [(s, b) for b, p, s, e in SP.get((gid, fteam), []) if FP.get(p) == pos and s <= inn]
    return max(c)[1] if c else None
from war.common import area, OUTS, INPLAY, bip_class, out_rates
bips = []                                                              # (gid, fielding team, inning, pos, out, key)
for line in open(os.path.join(OUT, 'pa.jsonl')):
    j = json.loads(line); gid = j['game_id']
    if gid not in G or len(teams_of.get(gid, [])) != 2: continue
    # batting team per half: the team whose spans contain the first batter of that half
    bat = {}
    for p in j['data']:
        if p['half_inning'] in bat: continue
        for t in teams_of[gid]:
            if any(b == p['batter_id'] for b, *_ in SP[(gid, t)]): bat[p['half_inning']] = t
    for p in j['data']:
        if p['result'] not in INPLAY or p['half_inning'] not in bat: continue
        q = [x for x in (p.get('pitches') or []) if x.get('hit_coordinate_x') is not None]
        if not q: continue
        c_ = bip_class(q[-1])
        if c_ is None: continue
        pos, key = c_
        fteam = [t for t in teams_of[gid] if t != bat[p['half_inning']]][0]
        bips.append((gid, fteam, p['inning'], pos, p['result'] in OUTS and p['result'] != 'Field Error', key))
pout = out_rates((o, k) for *_, o, k in bips)
X = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(float)))
miss = 0
for gid, ft, inn, pos, o, k in bips:
    b = fielder(gid, ft, pos, inn); pid = pid_of(b) if b else None
    if pid is None: miss += 1; continue
    F = X[(pid, 2026)][pos]; F['zr'] += o - pout(k); F['n'] += 1
print('balls in play charged', len(bips) - miss, 'unassigned', miss)
# innings at each position per player and game; main position per game for /stats totals
GP = collections.defaultdict(lambda: collections.defaultdict(float))   # (gid, bdl player) -> pos -> innings
for (gid, t), lst in SP.items():
    for b, p, s, e in lst:
        if p in FP: GP[(gid, b)][FP[p]] += min(e, 10.0) - s
catcher_of = {}
for (gid, t), lst in SP.items():
    cs_ = [(min(e, 10.0) - s, b) for b, p, s, e in lst if p == 'C']
    if cs_: catcher_of[(gid, t)] = max(cs_)[1]
tots = collections.defaultdict(lambda: [0.0, 0.0])                     # pid,pos -> (A or PO, E)
lgT = collections.defaultdict(lambda: [0.0, 0.0, 0.0])                  # pos -> (n, A/PO, E)
run = collections.defaultdict(lambda: [0.0, 0.0, 0.0])                  # catcher pid -> (innings, attempts, CS)
for line in open(os.path.join(OUT, 'stats.jsonl')):
    j = json.loads(line); gid = j['game_id']
    if gid not in G: continue
    for r in j['data']:
        b = r['player']['id']; tid = r['team']['id'] if isinstance(r.get('team'), dict) else None
        m = GP.get((gid, b))
        if m:
            tot_inn = sum(m.values()); pid = pid_of(b) if tot_inn > 0 else None
            for pos, inn in m.items():
                if pid is None or pos == 2: continue
                w = inn / tot_inn; v = (r.get('assists') or 0) if pos <= 6 else (r.get('putouts') or 0)
                tots[(pid, pos)][0] += w * v; tots[(pid, pos)][1] += w * (r.get('errors') or 0)
        att = (r.get('stolen_bases') or 0) + (r.get('caught_stealing') or 0)
        if att and tid is not None:
            opp = [t for t in teams_of.get(gid, []) if t != tid]
            if opp and (gid, opp[0]) in catcher_of:
                c = pid_of(catcher_of[(gid, opp[0])])
                if c: run[c][1] += att; run[c][2] += r.get('caught_stealing') or 0
for (gid, t), b in catcher_of.items():
    c = pid_of(b)
    if c: run[c][0] += GP[(gid, b)][2]
for (pid, s_), m in X.items():
    for pos, F in m.items():
        if (pid, pos) in tots: L = lgT[pos]; L[0] += F['n']; L[1] += tots[(pid, pos)][0]; L[2] += tots[(pid, pos)][1]
for (pid, s_), m in X.items():
    for pos, F in m.items():
        if (pid, pos) in tots and lgT[pos][0]:
            L = lgT[pos]; F['tot'] = tots[(pid, pos)][0] - F['n'] * L[1] / L[0]; F['err'] = tots[(pid, pos)][1] - F['n'] * L[2] / L[0]
li = sum(v[0] for v in run.values()); la_ = sum(v[1] for v in run.values()); lc = sum(v[2] for v in run.values())
for c, (inn, att, cs) in run.items():
    F = X[(c, 2026)][2]; F['inn'] = inn; F['n'] += inn
    F['catt'] = att - inn * la_ / li; F['ccs'] = cs - att * lc / la_
print('players', len(X), 'catchers', len(run), 'league attempts', la_, 'CS', lc)
pickle.dump({k: {p: dict(v) for p, v in m.items()} for k, m in X.items()}, open(os.path.join(W, 'bdl26_fld.pkl'), 'wb'))
