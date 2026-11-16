# bdl_sweep.py — read-only BDL download for own-WAR 2026: games, lineups, plate appearances, per-game stat lines.
# One request at a time; resumable (skips games already saved); backs off on 429.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser('~/baseballstats-tools/data/bdl2026'))      # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import json, os, re, sys, time, urllib.request, urllib.parse
OUT = BDL_DIR
KEY = os.environ['BDL_KEY']                     # the backend's balldontlie key (same variable the server uses)
BASE = 'https://api.balldontlie.io/mlb/v1/'
n_req = 0
def get(path, params):
    global n_req
    url = BASE + path + '?' + urllib.parse.urlencode(params, doseq=True)
    for attempt in range(8):
        time.sleep(0.7)
        try:
            n_req += 1
            with urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': KEY}), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500: time.sleep(10 * (attempt + 1)); continue
            raise
        except Exception: time.sleep(10 * (attempt + 1))
    raise RuntimeError('gave up ' + path)
def games():
    f = os.path.join(OUT, 'games.json')
    if os.path.exists(f): return json.load(open(f))
    rows, cur = [], None
    while True:
        p = {'seasons[]': 2026, 'per_page': 100}
        if cur: p['cursor'] = cur
        d = get('games', p); rows += d['data']; cur = d.get('meta', {}).get('next_cursor')
        if not cur: break
    json.dump(rows, open(f, 'w')); return rows
G = games()
reg = [g for g in G if g.get('season_type') == 'regular' and g.get('status') == 'STATUS_FINAL']
print(len(G), 'games;', len(reg), 'regular-season finals', flush=True)
for kind, path, pk in (('lineups', 'lineups', 'game_ids[]'), ('pa', 'plate_appearances', 'game_id')):
    fn = os.path.join(OUT, kind + '.jsonl'); done = set()
    if os.path.exists(fn): done = {json.loads(l)['game_id'] for l in open(fn)}
    with open(fn, 'a') as fo:
        for i, g in enumerate(reg):
            if g['id'] in done: continue
            d = get(path, {pk: g['id'], 'per_page': 100})
            fo.write(json.dumps({'game_id': g['id'], 'data': d['data'], 'meta': d.get('meta')}) + '\n'); fo.flush()
            if i % 200 == 0: print(kind, i, len(reg), n_req, flush=True)
fn = os.path.join(OUT, 'stats.jsonl'); done = set()
if os.path.exists(fn): done = {json.loads(l)['game_id'] for l in open(fn)}
with open(fn, 'a') as fo:
    for i, g in enumerate(reg):
        if g['id'] in done: continue
        rows, cur = [], None
        while True:
            p = {'game_ids[]': g['id'], 'per_page': 100}
            if cur: p['cursor'] = cur
            d = get('stats', p); rows += d['data']; cur = d.get('meta', {}).get('next_cursor')
            if not cur: break
        fo.write(json.dumps({'game_id': g['id'], 'data': rows}) + '\n'); fo.flush()
        if i % 200 == 0: print('stats', i, len(reg), n_req, flush=True)
print('DONE', n_req, 'requests', flush=True)
