# bdl_plays.py — read-only BDL download (Isaac approved 2026-11-12): /plays for 2026 regular-season finals, for fielder
# names on each play (double plays, outfield assists) and runner advances. One request at a time; resumable.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
SEASON = int(_os.environ.get('WAR_SEASON', '2026'))                                                   # the balldontlie season
TAG = f'bdl{SEASON % 100:02d}'                                                                      # its input-file prefix
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser(f'~/baseballstats-tools/data/bdl{SEASON}'))   # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, 'bdl_download.py')).read(); ns = {}; exec(src[:src.index('def games():')], ns); get = ns['get']
OUT = BDL_DIR
G = json.load(open(os.path.join(OUT, 'games.json')))
reg = [g for g in G if g.get('season_type') == 'regular' and g.get('status') == 'STATUS_FINAL']
fn = os.path.join(OUT, 'plays.jsonl'); done = set()
if os.path.exists(fn): done = {json.loads(l)['game_id'] for l in open(fn)}
print(len(reg), 'games;', len(done), 'already saved', flush=True)
with open(fn, 'a') as fo:
    for i, g in enumerate(reg):
        if g['id'] in done: continue
        rows, cur = [], None
        while True:
            p = {'game_id': g['id'], 'per_page': 100}
            if cur: p['cursor'] = cur
            d = get('plays', p); rows += d['data']; cur = d.get('meta', {}).get('next_cursor')
            if not cur: break
        fo.write(json.dumps({'game_id': g['id'], 'data': rows}) + '\n'); fo.flush()
        if i % 100 == 0: print('plays', i, len(reg), ns['n_req'], flush=True)
print('DONE', ns['n_req'], 'requests', flush=True)
