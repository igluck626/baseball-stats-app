"""The balldontlie season store for the nightly WAR update: one small compressed file per regular-season final.

Layout under ROOT/bdl<season>/:
  games.json           the season's whole /games list as of the last sync (replaced by each sync)
  games/<id>.json.gz   one game: lineups, plate appearances, per-player stat lines and plays, cut down to the fields
                       the WAR builders read (see compact_game), plus the game's /games fingerprint and fetch time
  index.json           a cache of each game file's fingerprint and fetch time (rebuilt from the files if lost)
Every file is written under a temporary name and renamed, so a crash leaves the old file or the new one, never half.

sync() walks the season's whole /games list (about 30 requests) instead of asking by date, because balldontlie files a
night game under the next UTC date. It fetches a game when it is a regular-season final and
  - it has no file yet, or
  - its /games fingerprint (status, runs/hits/errors, inning scores) changed since the file was fetched, or
  - it was played in the last `refetch_days` days: scorer corrections that leave the line score alone (a hit changed
    to an error is caught by the fingerprint; a fielder's name is not),
and, for the last two, it wasn't fetched or tried in the last `min_age_h` hours (so the 08:00/19:00 catch-up runs don't
repeat the night's work).
A game with any of its four feeds empty is saved but marked incomplete (2026's twelve Opening Day games on March 26-27
still had no lineups on 2026-11-25), and is fetched again each night while it was played in the last `incomplete_days`
days; after that it is used as it is and stays listed in the report. A response is never saved over a file that has
more of the four feeds filled, or with a feed 10% shorter than the file's (a cut-short answer). A game whose fetch
fails waits FAIL_BACKOFF_H hours before the next try; a games list with clearly fewer finals than last time is refused.
sync() stops at `max_requests` and reports what it left for the next run. It never touches a database.

load() trims each row to what the builders read (BUILDER_*), so a season fits in about half the memory; the files keep
more (every non-pitch play, every stat-line column) for later uses.

The command line (`python -m war.bdl_store sync|import|expand|status`) is for one-off runs and the 2026 rehearsal.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://api.balldontlie.io/mlb/v1/'
FAIL_BACKOFF_H = 6        # a game whose fetch failed isn't asked again for this long
SHRINK = 0.9              # a re-download with a feed under 90% of the stored rows is a cut-short answer: not used

# --- what a game file keeps ------------------------------------------------------------------------------------------
LINEUP_KEYS = ('id', 'batting_order', 'position', 'is_probable_pitcher', 'team_id')
PLAYER_KEYS = ('id', 'first_name', 'last_name', 'full_name', 'position')
TEAM_KEYS = ('id', 'abbreviation')
BIP_KEYS = ('pitch_number', 'hit_coordinate_x', 'hit_coordinate_y', 'hit_distance', 'launch_angle', 'exit_velocity')
# /plays rows that are only a pitch call; every other type (play results, inning and batter markers, steals, pickoffs,
# wild pitches, intentional walks ...) is kept, so an unknown new type is kept rather than lost
PITCH_CALLS = {'Ball', 'Foul Ball', 'Strike Looking', 'Strike Swinging', 'Bunted Foul', 'Foul Tip', 'Foul Tip Bunt',
               'Swinging Strike (Blocked)', 'Ball In Dirt'}
PITCH_CALL_PREFIXES = ('Ball - ', 'Strike Looking - ', 'Strike Swinging - ', 'Foul Ball - ')


# --- what load() hands the builders (bdl_season, bdl_fielding, bdl_runners, bdl_leverage read nothing else) ----------
BUILDER_PA = ('pa_number', 'half_inning', 'inning', 'batter_id', 'pitcher_id', 'result', 'runner_on_first',
              'runner_on_second', 'runner_on_third', 'outs', 'pitches')
BUILDER_STAT = ('assists', 'putouts', 'errors', 'stolen_bases', 'caught_stealing')
BUILDER_PLAY_TYPES = ('Play Result', 'Start Inning', 'Start Batter/Pitcher', 'End Batter/Pitcher')
BUILDER_PLAY = ('order', 'type', 'inning', 'inning_type', 'outs', 'batter_id', 'pitcher_id', 'away_score', 'home_score')
FEEDS = ('lineups', 'pa', 'stats', 'plays')


def for_builders(kind, rows):
    if kind == 'pa':
        return [_pick(p, BUILDER_PA) for p in rows]
    if kind == 'stats':
        return [dict(_pick(s, BUILDER_STAT), player=s.get('player'), **({'team': s['team']} if 'team' in s else {}))
                for s in rows]
    if kind == 'plays':                    # text only where bdl_runners reads it
        return [dict(_pick(x, BUILDER_PLAY), **({'text': x.get('text')} if x.get('type') == 'Play Result' else {}))
                for x in rows if x.get('type') in BUILDER_PLAY_TYPES]
    return rows


def filled(n):
    """How many of the four feeds have rows, from an index entry's counts."""
    return sum(1 for k in FEEDS if n.get(k))


def _pick(d, keys):
    return {k: d[k] for k in keys if k in d}


def _sub(d, keys):
    return _pick(d, keys) if isinstance(d, dict) else d


def compact_lineup(x):
    r = _pick(x, LINEUP_KEYS)
    r['player'] = _sub(x.get('player'), PLAYER_KEYS)
    if 'team' in x:
        r['team'] = _sub(x['team'], TEAM_KEYS)
    return r


def compact_pa(p):
    """Every plate-appearance field except the pitch list, which is cut to the last pitch with a hit location."""
    r = {k: v for k, v in p.items() if k != 'pitches'}
    hit = [x for x in (p.get('pitches') or []) if x.get('hit_coordinate_x') is not None]
    r['pitches'] = [_pick(hit[-1], BIP_KEYS)] if hit else []
    return r


def compact_stat(s):
    r = {k: v for k, v in s.items() if k not in ('player', 'team', 'game_id')}
    r['player'] = _sub(s.get('player'), PLAYER_KEYS)
    if 'team' in s:
        r['team'] = _sub(s['team'], TEAM_KEYS)
    return r


def is_pitch_call(t):
    return t in PITCH_CALLS or 'Pitch Timer' in (t or '') or (t or '').startswith(PITCH_CALL_PREFIXES)


def compact_play(x):
    return {k: v for k, v in x.items() if k != 'game_id'}


def compact_game(lineups, pa, stats, plays):
    return {'lineups': [compact_lineup(x) for x in lineups],
            'pa': [compact_pa(p) for p in pa],
            'stats': [compact_stat(s) for s in stats],
            'plays': [compact_play(x) for x in plays if not is_pitch_call(x.get('type'))]}


def fingerprint(g):
    """What /games says about a game that changes when it finishes or the line score is corrected."""
    side = lambda d: {k: (d or {}).get(k) for k in ('runs', 'hits', 'errors', 'inning_scores')}
    key = {'status': g.get('status'), 'season_type': g.get('season_type'), 'date': g.get('date'),
           'home': side(g.get('home_team_data')), 'away': side(g.get('away_team_data'))}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def eastern_day(g):
    """The game's date on MLB's (Eastern) calendar, as api/season_phase.eastern_date reads balldontlie's timestamps."""
    from zoneinfo import ZoneInfo
    t = dt.datetime.fromisoformat((g.get('date') or '').replace('Z', '+00:00'))
    return t.astimezone(ZoneInfo('America/New_York')).date()


def is_regular_final(g):
    """A finished regular-season game between two real teams. balldontlie files the All-Star Game as a regular-season
    final with both teams 'Unknown' (id -1): that's why its 2026 list had 2,430 finals to our 2,429."""
    team_ok = lambda t: isinstance(t, dict) and (t.get('id') or 0) > 0
    return (g.get('season_type') == 'regular' and g.get('status') == 'STATUS_FINAL'
            and team_ok(g.get('home_team')) and team_ok(g.get('away_team')))


# --- files -----------------------------------------------------------------------------------------------------------
def season_dir(root, season):
    return os.path.join(root, f'bdl{season}')


def _game_path(sdir, gid):
    return os.path.join(sdir, 'games', f'{gid}.json.gz')


def _write_atomic(path, data, gz=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp{os.getpid()}'
    raw = json.dumps(data, separators=(',', ':')).encode()
    with open(tmp, 'wb') as f:
        f.write(gzip.compress(raw, 6, mtime=0) if gz else raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_game(sdir, gid):
    with gzip.open(_game_path(sdir, gid), 'rt') as f:
        return json.load(f)


def _entry(rec):
    n = {k: len(rec[k]) for k in FEEDS}
    return {'fingerprint': rec['fingerprint'], 'fetched_at': rec['fetched_at'], 'date': rec.get('date'), 'n': n,
            'complete': filled(n) == len(FEEDS)}


def save_game(sdir, gid, game_row, data, fetched_at):
    rec = {'game_id': gid, 'fingerprint': fingerprint(game_row), 'fetched_at': fetched_at,
           'date': game_row.get('date'), **data}
    _write_atomic(_game_path(sdir, gid), rec, gz=True)
    return _entry(rec)


def load_index(sdir):
    """{game id: {fingerprint, fetched_at, date, n}} for every game file present; files missing from the cached index
    (or an unreadable index) are read directly."""
    gdir = os.path.join(sdir, 'games')
    have = {int(f.split('.')[0]) for f in os.listdir(gdir) if f.endswith('.json.gz')} if os.path.isdir(gdir) else set()
    try:
        cached = {int(k): v for k, v in json.load(open(os.path.join(sdir, 'index.json'))).items()}
    except (OSError, ValueError):
        cached = {}
    idx = {g: cached[g] for g in have if g in cached}
    for g in have - set(idx):
        idx[g] = _entry(read_game(sdir, g))
    return idx


def save_index(sdir, idx):
    _write_atomic(os.path.join(sdir, 'index.json'), {str(k): v for k, v in sorted(idx.items())})


def load(root, season, trim=True):
    """The store as the builders read it: (G, LU, PA, ST, PL, missing). G = {id: /games row} for regular-season finals
    in the saved games list; the four others = {id: rows} for the finals that have a file; missing = finals without.
    trim=True cuts rows to what the builders read (for_builders); trim=False returns the files' rows as they are.
    Everything is in the /games list's order, the order the original download wrote its files in, so the builders' sums
    run in the same order and the results match a build from that download exactly (not just to rounding)."""
    sdir = season_dir(root, season)
    G = {g['id']: g for g in json.load(open(os.path.join(sdir, 'games.json'))) if is_regular_final(g)}
    LU, PA, ST, PL, missing = {}, {}, {}, {}, []
    cut = for_builders if trim else (lambda kind, rows: rows)
    for gid in G:
        if not os.path.exists(_game_path(sdir, gid)):
            missing.append(gid)
            continue
        r = read_game(sdir, gid)
        LU[gid], PA[gid], ST[gid], PL[gid] = (cut(k, r[k]) for k in FEEDS)
    return G, LU, PA, ST, PL, missing


# --- balldontlie -----------------------------------------------------------------------------------------------------
class Client:
    """One request at a time, `pause` seconds apart (the key is shared with the app), backing off on 429 and 5xx.
    Counts requests; refuses to start one past `max_requests` (raises Budget)."""

    class Budget(Exception):
        pass

    def __init__(self, key, max_requests, pause=0.7, timeout=60, opener=None, sleep=time.sleep):
        self.key, self.max, self.pause, self.timeout = key, max_requests, pause, timeout
        self.opener = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))
        self.sleep, self.n = sleep, 0

    def get(self, path, params):
        url = BASE + path + '?' + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(6):
            if self.n >= self.max:
                raise Client.Budget(f'request cap {self.max} reached')
            self.sleep(self.pause)
            self.n += 1
            try:
                with self.opener(urllib.request.Request(url, headers={'Authorization': self.key}), self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
                pass
            self.sleep(10 * (attempt + 1))
        raise RuntimeError(f'balldontlie {path}: no answer after 6 tries')

    def all_pages(self, path, params):
        rows, cursor = [], None
        while True:
            p = dict(params, per_page=100, **({'cursor': cursor} if cursor else {}))
            d = self.get(path, p)
            if not isinstance(d, dict) or not isinstance(d.get('data'), list):
                raise ValueError(f'balldontlie {path}: a response without a data list')
            rows += d['data']
            cursor = (d.get('meta') or {}).get('next_cursor')
            if not cursor:
                return rows


def fetch_game(client, gid):
    """The four feeds for one game (parameter names differ per endpoint: game_ids[] vs game_id)."""
    return (client.all_pages('lineups', {'game_ids[]': gid}),
            client.all_pages('plate_appearances', {'game_id': gid}),
            client.all_pages('stats', {'game_ids[]': gid}),
            client.all_pages('plays', {'game_id': gid}))


def plan(games, idx, now, refetch_days=3, min_age_h=18, incomplete_days=7):
    """Which regular-season finals to fetch, in order: new games, changed fingerprints, then the re-fetch window
    (games from the last `refetch_days` days, and incomplete games from the last `incomplete_days` days)."""
    cutoff = (now - dt.timedelta(days=refetch_days)).date().isoformat()
    cutoff_inc = (now - dt.timedelta(days=incomplete_days)).date().isoformat()
    stale = (now - dt.timedelta(hours=min_age_h)).isoformat()
    new, changed, recent = [], [], []
    for g in sorted((g for g in games if is_regular_final(g)), key=lambda g: (g.get('date') or '', g['id'])):
        have = idx.get(g['id'])
        day = (g.get('date') or '')[:10]
        if have is None:
            new.append(g)
            continue
        if max(have['fetched_at'], have.get('tried_at', '')) >= stale:      # fetched or tried in the last min_age_h
            continue
        if have['fingerprint'] != fingerprint(g):
            changed.append(g)
        elif day >= cutoff or (not have.get('complete') and day >= cutoff_inc):
            recent.append(g)
    return new, changed, recent


def sync(root, season, client, now=None, refetch_days=3, min_age_h=18, incomplete_days=7, log=print):
    """Bring the store up to date. Returns a report dict; raises only if the /games list itself can't be read."""
    now = now or dt.datetime.now(dt.timezone.utc)
    sdir = season_dir(root, season)
    os.makedirs(os.path.join(sdir, 'games'), exist_ok=True)
    games = client.all_pages('games', {'seasons[]': season})
    gpath = os.path.join(sdir, 'games.json')
    try:
        before = sum(map(is_regular_final, json.load(open(gpath))))
    except (OSError, ValueError):
        before = 0
    now_n = sum(map(is_regular_final, games))
    if now_n < before - max(5, 0.01 * before):          # a cut-short list must not replace a full one
        raise RuntimeError(f'balldontlie listed {now_n} regular-season finals, {before} last time: not using it')
    _write_atomic(gpath, games)
    idx = load_index(sdir)
    fpath = os.path.join(sdir, 'failures.json')
    try:
        fails = {int(k): v for k, v in json.load(open(fpath)).items()}
    except (OSError, ValueError):
        fails = {}
    retry_after = (now - dt.timedelta(hours=FAIL_BACKOFF_H)).isoformat()
    new, changed, recent = plan(games, idx, now, refetch_days, min_age_h, incomplete_days)
    rep = {'season': season, 'listed': len(games), 'finals': sum(map(is_regular_final, games)),
           'new': len(new), 'changed': len(changed), 'refetch': len(recent), 'saved': [], 'kept': [],
           'failed': [], 'left': [], 'capped': False}
    todo = [g for g in new + changed + recent if fails.get(g['id'], '') < retry_after]
    rep['backing_off'] = len(new + changed + recent) - len(todo)
    for i, g in enumerate(todo):
        gid = g['id']
        try:
            lu, pa, st, pl = fetch_game(client, gid)
        except Client.Budget:
            rep['capped'] = True
            rep['left'] = [x['id'] for x in todo[i:]]
            break
        except Exception as e:                                    # one game's failure doesn't stop the rest
            rep['failed'].append((gid, f'{type(e).__name__}: {e}'))
            fails[gid] = now.isoformat()                          # and isn't asked again for FAIL_BACKOFF_H hours
            _write_atomic(fpath, {str(k): v for k, v in fails.items()})
            continue
        if fails.pop(gid, None):
            _write_atomic(fpath, {str(k): v for k, v in fails.items()})
        n = {'lineups': len(lu), 'pa': len(pa), 'stats': len(st), 'plays': len(pl)}
        old_n = idx[gid]['n'] if gid in idx else {}
        shrank = [k for k in FEEDS if old_n.get(k) and n[k] < SHRINK * old_n[k]]
        if gid in idx and (filled(old_n) > filled(n) or shrank):   # never replace fuller data with emptier
            idx[gid]['tried_at'] = now.isoformat()                 # but don't ask again before min_age_h
            save_index(sdir, idx)
            rep['kept'].append((gid, n))
            continue
        idx[gid] = save_game(sdir, gid, g, compact_game(lu, pa, st, pl), now.isoformat())
        save_index(sdir, idx)
        rep['saved'].append(gid)
    finals = {g['id'] for g in games if is_regular_final(g)}
    rep['stored_finals'] = len(finals & set(idx))
    rep['missing_finals'] = sorted(finals - set(idx))
    rep['incomplete_finals'] = sorted((g, {k: v for k, v in idx[g]['n'].items() if not v})
                                      for g in finals & set(idx) if not idx[g].get('complete'))
    rep['requests'] = client.n
    rep['inputs_through'] = max((g.get('date') or '')[:10] for g in games if g['id'] in idx and g['id'] in finals) \
        if finals & set(idx) else None
    log(json.dumps({k: (v if not isinstance(v, list) or len(v) <= 20 else f'{len(v)} games') for k, v in rep.items()}))
    return rep


# --- the old raw download (backend/war/builders/bdl_download.py layout) ----------------------------------------------
def import_raw(raw_dir, root, season, fetched_at=None):
    """Compact an existing raw download (games.json + lineups/pa/stats/plays .jsonl) into the store, game by game.
    fetched_at defaults to the raw plays file's modification time."""
    sdir = season_dir(root, season)
    games = json.load(open(os.path.join(raw_dir, 'games.json')))
    by_id = {g['id']: g for g in games}
    finals = {g['id'] for g in games if is_regular_final(g)}
    fetched_at = fetched_at or dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(raw_dir, 'plays.jsonl')),
                                                         dt.timezone.utc).isoformat()

    def stream(kind, fn):
        for line in open(os.path.join(raw_dir, kind + '.jsonl')):
            j = json.loads(line)
            if j['game_id'] in finals:
                yield j['game_id'], [fn(x) for x in j['data']]

    part = {'lineups': dict(stream('lineups', compact_lineup)), 'stats': dict(stream('stats', compact_stat)),
            'pa': dict(stream('pa', compact_pa))}
    idx = {}
    for gid, pl in stream('plays', compact_play):
        data = {'lineups': part['lineups'].get(gid, []), 'pa': part['pa'].get(gid, []),
                'stats': part['stats'].get(gid, []), 'plays': [x for x in pl if not is_pitch_call(x.get('type'))]}
        idx[gid] = save_game(sdir, gid, by_id[gid], data, fetched_at)
    _write_atomic(os.path.join(sdir, 'games.json'), games)
    save_index(sdir, idx)
    return {'finals': len(finals), 'imported': len(idx), 'missing': sorted(finals - set(idx))}


def expand(root, season, out_dir):
    """Write the store back out in the raw layout (only the kept fields), so the builders can read it unchanged."""
    G, LU, PA, ST, PL, missing = load(root, season, trim=False)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'games.json'), 'w') as f:
        json.dump(json.load(open(os.path.join(season_dir(root, season), 'games.json'))), f)
    for kind, d in (('lineups', LU), ('pa', PA), ('stats', ST), ('plays', PL)):
        with open(os.path.join(out_dir, kind + '.jsonl'), 'w') as f:
            for gid in d:
                f.write(json.dumps({'game_id': gid, 'data': d[gid]}) + '\n')
    return {'games': len(PA), 'missing': missing}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('command', choices=('sync', 'import', 'expand', 'status'))
    ap.add_argument('--season', type=int, required=True)
    ap.add_argument('--root', required=True, help='store root (the season folder bdl<season> goes inside it)')
    ap.add_argument('--raw', help='import: the raw download folder')
    ap.add_argument('--out', help='expand: where to write the raw-layout files')
    ap.add_argument('--max-requests', type=int, default=1500)
    ap.add_argument('--refetch-days', type=int, default=3)
    ap.add_argument('--incomplete-days', type=int, default=7)
    a = ap.parse_args(argv)
    if a.command == 'sync':
        key = os.environ.get('BDL_KEY')
        if not key:
            sys.exit('BDL_KEY is not set')
        rep = sync(a.root, a.season, Client(key, a.max_requests), refetch_days=a.refetch_days,
                   incomplete_days=a.incomplete_days)
        sys.exit(0 if not (rep['capped'] or rep['failed']) else 3)
    if a.command == 'import':
        print(json.dumps(import_raw(a.raw, a.root, a.season)))
    elif a.command == 'expand':
        print(json.dumps(expand(a.root, a.season, a.out)))
    else:
        sdir = season_dir(a.root, a.season)
        idx = load_index(sdir)
        games = json.load(open(os.path.join(sdir, 'games.json')))
        finals = {g['id'] for g in games if is_regular_final(g)}
        print(json.dumps({'finals': len(finals), 'stored': len(finals & set(idx)), 'missing': sorted(finals - set(idx)),
                          'incomplete': sorted(g for g in finals & set(idx) if not idx[g].get('complete')),
                          'newest_fetch': max((v['fetched_at'] for v in idx.values()), default=None)}))


if __name__ == '__main__':
    main()
