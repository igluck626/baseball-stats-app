# bdl_leverage.py — READ-ONLY. Reliever entry leverage for the balldontlie season, on the same scale as retro_leverage.py:
# each relief entrance's game state (inning, top/bottom, outs, runners, home lead) looked up in the Retrosheet LI table.
#   entrance  = a 'Start Batter/Pitcher' play whose pitcher is not his team's first pitcher of the game
#   outs/score before = the previous batter's final values in that half-inning (balldontlie stamps the after-values)
#   runners before   = the plate-appearance feed's runner flags for that batter and pitcher
# Output bdlYY_li.pkl {our player_id: (relief appearances, sum of entry LI)}
# build() is the calculation (called by the nightly update); the command line reads WAR_BDL_DIR and writes the pickle.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
SEASON = int(_os.environ.get('WAR_SEASON', '2026'))                                                   # the balldontlie season
TAG = f'bdl{SEASON % 100:02d}'                                                                      # its input-file prefix
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser(f'~/baseballstats-tools/data/bdl{SEASON}'))   # balldontlie season download
import collections, os, pickle


def build(G, PA, PL, B2P, TABLE, log=print):
    """G regular-season final ids; PA/PL {id: rows} plate appearances and plays; B2P balldontlie id → [our player_ids]
    from the players AND pitchers tables; TABLE the Retrosheet LI table (li_raw.pkl's TABLE).
    Returns the bdlYY_li.pkl dict {our player_id: (relief appearances, sum of entry LI)}."""
    RUN = {}                                                # (game, inning, half, batter, pitcher) → runner state before the PA
    for gid, rows in PA.items():
        for p in sorted(rows, key=lambda x: x['pa_number']):
            k = (gid, p['inning'], (p['half_inning'] or '').lower()[:3], p['batter_id'], p['pitcher_id'])
            RUN.setdefault(k, int(bool(p['runner_on_first'])) + 2 * int(bool(p['runner_on_second'])) + 4 * int(bool(p['runner_on_third'])))
    LI = collections.defaultdict(lambda: [0, 0.0]); stat = collections.Counter()
    for gid, data in PL.items():
        if gid not in G: continue
        rows = sorted(data, key=lambda x: x['order'])
        seen = {'top': [], 'bot': []}                       # pitchers used by the team fielding in that half
        prev = {'outs': 0, 'away': 0, 'home': 0, 'half': None}
        for x in rows:
            half = (x.get('inning_type') or '').lower()[:3]
            if x['type'] == 'Start Inning': prev.update(outs=0, half=half); continue
            if x['type'] == 'End Batter/Pitcher':
                prev.update(outs=x.get('outs') or 0, away=x.get('away_score') or 0, home=x.get('home_score') or 0); continue
            if x['type'] != 'Start Batter/Pitcher' or half not in seen or x.get('pitcher_id') is None: continue
            used = seen[half]; pit = x['pitcher_id']
            if pit in used: continue
            used.append(pit)
            if len(used) == 1: continue                     # the starter
            b = RUN.get((gid, x['inning'], half, x.get('batter_id'), pit))
            if b is None: stat['no runner state'] += 1; continue
            key = (min(x['inning'], 9), int(half == 'bot'), min(prev['outs'], 2), b, max(-6, min(6, prev['home'] - prev['away'])))
            li = TABLE.get(key)
            if li is None: stat['state not in table'] += 1; continue
            ps = B2P.get(pit, [])
            if len(ps) != 1: stat['pitcher not mapped'] += 1; continue
            LI[ps[0]][0] += 1; LI[ps[0]][1] += li; stat['entrances'] += 1
    log(dict(stat), 'relievers', len(LI))
    return {p: tuple(v) for p, v in LI.items()}


if __name__ == '__main__':
    from war.dbconn import conn
    from war.builders.bdl_common import bdl_maps, load_raw
    G, _LU, PA, _ST, PL = load_raw(BDL_DIR)
    TABLE = pickle.load(open(os.path.join(INPUTS, 'li_raw.pkl'), 'rb'))['TABLE']
    out = build(set(G), PA, PL, bdl_maps(conn().cursor())[1], TABLE)
    pickle.dump(out, open(os.path.join(INPUTS, f'{TAG}_li.pkl'), 'wb'))
