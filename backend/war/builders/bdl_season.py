# bdl26_build.py — READ-ONLY. From the BDL 2026 download: innings by position (lineups + batting-slot substitutions),
# batting-feed counts (PA, reached on error, GIDP chances) per player. Output bdl26.pkl keyed by OUR player_id.
# build() is the calculation (called by the nightly update); the command line reads WAR_BDL_DIR and writes the pickle.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
SEASON = int(_os.environ.get('WAR_SEASON', '2026'))                                                   # the balldontlie season
TAG = f'bdl{SEASON % 100:02d}'                                                                      # its input-file prefix
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser(f'~/baseballstats-tools/data/bdl{SEASON}'))   # balldontlie season download
import os, pickle, collections

FPOS = {'C': 2, '1B': 3, '2B': 4, '3B': 5, 'SS': 6, 'LF': 7, 'CF': 8, 'RF': 9, 'DH': 10}


def build(G, LU, PA, ST, B2P, luonly=False, log=print):
    """G {id: game} regular-season finals; LU/PA/ST {id: rows} for those games; B2P balldontlie id → [our player_ids]
    (players table). Returns the bdlYY.pkl dict: INN, GS, PBP, games (games used), SPANS, HALF, VENUE."""
    amb = {b for b, v in B2P.items() if len(v) > 1}
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
    if luonly:                                        # lineups only (before the PA download): each start = 8.0 innings
        INN.clear(); GS.clear(); games_used = 0
        for gid, lu in LU.items():
            games_used += 1
            for x in lu:
                b = x['player']['id']
                if x.get('batting_order') and x['position'] in FPOS and b not in amb and b in B2P:
                    INN[B2P[b][0]][FPOS[x['position']]] += 8.0; GS[(B2P[b][0], FPOS[x['position']])] += 1
    log(dict(STAT)); log('games used', games_used, 'of', len(G), '; players with innings', len(INN))
    # batting-feed counts per batter
    PBP = collections.defaultdict(lambda: [0, 0, 0, 0])    # PA, reached on error, GIDP, GIDP chances
    for gid, pas in PA.items():
        for p in pas:
            b = p['batter_id']
            if b in amb or b not in B2P: continue
            c = PBP[B2P[b][0]]; c[0] += 1; c[1] += p['result'] == 'Field Error'
            ch = p['runner_on_first'] and p['outs'] < 2; c[3] += ch; c[2] += ch and p['result'] in ('GIDP', 'Grounded Into DP')
    # each team's main home ballpark this season (balldontlie venue), so the current season's park factor only borrows last
    # season when the team played in the same park
    _v = collections.defaultdict(collections.Counter)
    for g_ in G.values():
        ht_ = g_['home_team'].get('abbreviation') if isinstance(g_.get('home_team'), dict) else None
        if ht_ and g_.get('venue'): _v[ht_][g_['venue']] += 1
    VENUE = {t_: c_.most_common(1)[0][0] for t_, c_ in _v.items()}
    return dict(INN={k: dict(v) for k, v in INN.items()}, GS=dict(GS), PBP=dict(PBP), games=games_used, SPANS=dict(SPANS), HALF={}, VENUE=VENUE)


if __name__ == '__main__':
    from war.dbconn import conn
    from war.builders.bdl_common import bdl_maps, load_raw
    G, LU, PA, ST, _PL = load_raw(BDL_DIR)
    out = build(G, LU, PA, ST, bdl_maps(conn().cursor())[0], luonly=os.environ.get('LUONLY') == '1')
    pickle.dump(out, open(os.path.join(INPUTS, f'{TAG}.pkl'), 'wb'))
    print(collections.Counter(p['result'] for pas in PA.values() for p in pas).most_common(40))
