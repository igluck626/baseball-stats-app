#!/usr/bin/env python3
"""The balldontlie season store for the nightly WAR update (backend/war/bdl_store.py), against a fake balldontlie.

⚠️ THE CHECKS THAT MATTER:
  • only regular-season finals are fetched; a game is fetched again when its line score changes, or when it was played
    in the last few days and its file is old enough (scorer corrections), and never twice in one night otherwise
  • a game with an empty feed is saved as incomplete and retried for a week (never longer); a response is never saved
    over a file with more of its feeds filled
  • the All-Star Game (filed by balldontlie as a regular-season final between two 'Unknown' teams) is left out
  • the request cap stops the run cleanly, says which games were left, and the next run picks them up
  • a cut-short answer (no data list, a shorter feed, a games list with far fewer finals) never replaces good data, and
    a game whose fetch keeps failing waits 6 hours between tries
  • the cut-down game file keeps every field the WAR builders read (the full-season check that the builders give
    identical results from it is a separate local run on the 1.5 GB download; see the commit message)

Standalone, no pytest, no network, no database. Run: <backend python> backend/tests/test_war_bdl_store.py
"""
import copy
import datetime as dt
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from war import bdl_store as bs  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


NOW = dt.datetime(2027, 5, 10, 15, 0, tzinfo=dt.timezone.utc)


def game(gid, date, status='STATUS_FINAL', season_type='regular', errors=0, allstar=False):
    unk = {'id': -1, 'abbreviation': 'UNK', 'display_name': 'Unknown'}
    return {'id': gid, 'date': f'{date}T00:00:00.000Z', 'status': status, 'season_type': season_type,
            'home_team': unk if allstar else {'id': 1, 'abbreviation': 'NYY', 'display_name': 'New York Yankees'},
            'away_team': unk if allstar else {'id': 2, 'abbreviation': 'BOS', 'display_name': 'Boston Red Sox'},
            'venue': 'Yankee Stadium',
            'home_team_data': {'runs': 3, 'hits': 7, 'errors': errors, 'inning_scores': [0, 1, 2]},
            'away_team_data': {'runs': 2, 'hits': 5, 'errors': 0, 'inning_scores': [2, 0, 0]}}


def pitch(n, x=None):
    return {'pitch_number': n, 'release_speed': 95.0, 'spin_rate': 2200, 'hit_coordinate_x': x,
            'hit_coordinate_y': None if x is None else 100.0, 'hit_distance': None if x is None else 250,
            'launch_angle': None if x is None else 20, 'exit_velocity': None if x is None else 99.0}


def feeds(gid):
    lu = [{'id': 1, 'game_id': gid, 'batting_order': 1, 'position': 'SS', 'is_probable_pitcher': False,
           'player': {'id': 10, 'first_name': 'A', 'last_name': 'B', 'full_name': 'A B', 'position': 'SS',
                      'dob': '2000-01-01', 'team': {'id': 9}}, 'team': {'id': 1, 'abbreviation': 'NYY', 'slug': 'x'}}]
    pa = [{'batter_id': 10, 'pitcher_id': 20, 'inning': 1, 'half_inning': 'top', 'pa_number': 1, 'outs': 0,
           'result': 'Single', 'runner_on_first': False, 'runner_on_second': False, 'runner_on_third': False,
           'pitches': [pitch(1), pitch(2, 120.0), pitch(3, 130.0), pitch(4)]}]
    st = [{'player': {'id': 10, 'first_name': 'A', 'last_name': 'B', 'full_name': 'A B', 'position': 'SS',
                      'height': '6\''}, 'team': {'id': 1, 'abbreviation': 'NYY', 'league': 'AL'}, 'game_id': gid,
           'assists': 3, 'putouts': 1, 'errors': 0, 'stolen_bases': 1, 'caught_stealing': 0, 'at_bats': 4}]
    pl = [{'game_id': gid, 'order': i, 'type': t, 'text': t, 'inning': 1, 'inning_type': 'Top', 'outs': 0,
           'batter_id': 10, 'pitcher_id': 20, 'away_score': 0, 'home_score': 0}
          for i, t in enumerate(['Start Inning', 'Start Batter/Pitcher', 'Ball', 'Strike Looking', 'Foul Ball',
                                 'Ball - Overturned', 'Automatic Ball (Pitch Timer Violation)', 'Automatic Ball - IBB',
                                 'Stolen Base', 'Single', 'Play Result', 'End Batter/Pitcher'])]
    return {'lineups': lu, 'plate_appearances': pa, 'stats': st, 'plays': pl}


class Fake:
    """balldontlie in memory. /games and /plays are served 2 rows per page so pagination is exercised."""

    def __init__(self, games):
        self.games, self.feeds, self.log, self.fail = games, {}, [], {}

    def __call__(self, req, timeout):
        u = urllib.parse.urlparse(req.full_url)
        path = u.path.rsplit('/', 1)[-1]
        q = urllib.parse.parse_qs(u.query)
        self.log.append((path, q))
        key = (path, int((q.get('game_id') or q.get('game_ids[]') or ['0'])[0]))
        if self.fail.get(key):
            code = self.fail[key].pop(0)
            if code:
                raise urllib.error.HTTPError(req.full_url, code, 'x', {}, None)
        if path == 'games':
            rows, size = self.games, 2
        else:
            rows = self.feeds.get(key[1], feeds(key[1]))[path]
            size = 2 if path == 'plays' else 100
        start = int((q.get('cursor') or ['0'])[0])
        page = {'data': rows[start:start + size], 'meta': {}}
        if start + size < len(rows):
            page['meta']['next_cursor'] = start + size
        return io.BytesIO(json.dumps(page).encode())


def client(fake, cap=10_000):
    return bs.Client('k', cap, opener=fake, sleep=lambda s: None)


def fetched(fake):
    return sorted({int(q['game_id'][0]) for p, q in fake.log if p == 'plays'})


root = tempfile.mkdtemp()
G = [game(1, '2027-04-01'), game(2, '2027-05-08'), game(3, '2027-05-09'), game(4, '2027-05-09', 'STATUS_POSTPONED'),
     game(5, '2027-03-01', season_type='spring_training'), game(6, '2027-05-09', 'STATUS_IN_PROGRESS'),
     game(99, '2027-05-09', allstar=True)]

print('first night: every regular-season final, nothing else')
f = Fake(G)
r = bs.sync(root, 2027, client(f), now=NOW, log=lambda s: None)
check('fetches the three regular-season finals only', fetched(f) == [1, 2, 3], fetched(f))
check('report: 3 new, 3 saved, none missing', (r['new'], len(r['saved']), r['missing_finals']) == (3, 3, []), r)
check('games list paged through (7 games, 2 per page)', sum(p == 'games' for p, _ in f.log) == 4)
check('the All-Star Game is not a regular-season final', not bs.is_regular_final(G[-1]))
check('right parameter per endpoint (game_ids[] for lineups and stats, game_id for the other two)',
      all(('game_ids[]' in q) == (p in ('lineups', 'stats')) for p, q in f.log if p != 'games'))
check('inputs_through is the newest stored final', r['inputs_through'] == '2027-05-09', r['inputs_through'])
sdir = bs.season_dir(root, 2027)
check('no temporary files left behind', not any('.tmp' in n for d, _, fs in os.walk(sdir) for n in fs))

print('the cut-down game file')
rec = bs.read_game(sdir, 1)
types = [x['type'] for x in rec['plays']]
check('pitch calls dropped, events and markers kept (IBB, steal, play result)',
      types == ['Start Inning', 'Start Batter/Pitcher', 'Automatic Ball - IBB', 'Stolen Base', 'Single', 'Play Result',
                'End Batter/Pitcher'], types)
p = rec['pa'][0]
check('plate appearance keeps only the last pitch with a hit location', p['pitches'] == [
    {'pitch_number': 3, 'hit_coordinate_x': 130.0, 'hit_coordinate_y': 100.0, 'hit_distance': 250, 'launch_angle': 20,
     'exit_velocity': 99.0}], p['pitches'])
need_pa = {'pa_number', 'half_inning', 'inning', 'batter_id', 'pitcher_id', 'result', 'runner_on_first',
           'runner_on_second', 'runner_on_third', 'outs'}
check('plate appearance keeps the fields the builders read', need_pa <= set(p), need_pa - set(p))
s = rec['stats'][0]
check('stat line keeps names, position, team id and the counts', s['player'] == {
    'id': 10, 'first_name': 'A', 'last_name': 'B', 'full_name': 'A B', 'position': 'SS'} and s['team'] == {
    'id': 1, 'abbreviation': 'NYY'} and all(k in s for k in ('assists', 'putouts', 'errors', 'stolen_bases',
                                                             'caught_stealing', 'at_bats')), s)
lu = rec['lineups'][0]
check('lineup keeps game team (not the player\'s current team), slot and position',
      lu['team']['id'] == 1 and lu['batting_order'] == 1 and lu['position'] == 'SS' and 'team' not in lu['player'], lu)
need_play = {'order', 'type', 'text', 'inning', 'inning_type', 'outs', 'batter_id', 'pitcher_id', 'away_score',
             'home_score'}
check('plays keep the fields the builders read', all(need_play <= set(x) for x in rec['plays']))

print('later the same day: nothing is fetched again')
f = Fake(G)
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(hours=4), log=lambda s: None)
check('no game re-fetched', fetched(f) == [] and r['requests'] == 4, (fetched(f), r['requests']))

print('next night: the re-fetch window and a changed line score')
G2 = copy.deepcopy(G)
G2[0] = game(1, '2027-04-01', errors=1)                 # an old game's line score changes (hit scored as an error)
G2[5] = game(6, '2027-05-09')                            # the in-progress game is now final
G2.append(game(7, '2027-05-10'))
f = Fake(G2)
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=1), log=lambda s: None)
check('fetches the two new finals, the changed game and the last 3 days\' games', fetched(f) == [1, 2, 3, 6, 7],
      fetched(f))
check('report splits them: 2 new, 1 changed, 2 re-fetched', (r['new'], r['changed'], r['refetch']) == (2, 1, 2), r)
check('the changed game\'s file has the new fingerprint', bs.read_game(sdir, 1)['fingerprint'] == bs.fingerprint(G2[0]))
check('order: new games first, then changed, then the re-fetch window', r['saved'] == [6, 7, 1, 2, 3], r['saved'])

print('an empty feed: saved as incomplete, retried, never saved over fuller data')
G3 = G2 + [game(8, '2027-05-11')]
f = Fake(G3)
f.feeds[8] = dict(feeds(8), plate_appearances=[])
f.feeds[7] = dict(feeds(7), plays=[])                   # game 7 is in the re-fetch window and comes back emptier
before7 = bs.read_game(sdir, 7)
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=2), log=lambda s: None)
check('a new game with no plate appearances is saved and listed as incomplete',
      8 in r['saved'] and r['incomplete_finals'] == [(8, {'pa': 0})] and r['missing_finals'] == [], r)
check('an existing full file is kept when its re-fetch comes back emptier',
      bs.read_game(sdir, 7) == before7 and [g for g, _ in r['kept']] == [7], r['kept'])
f = Fake(G3)
f.feeds[7] = dict(feeds(7), plays=[])
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=2, hours=3), log=lambda s: None)
check('and not asked for again in the same day', 7 not in fetched(f), fetched(f))

print('retries, failures and the request cap')
f = Fake(G3)
f.fail[('lineups', 8)] = [429, 0]                         # rate limited once, then fine
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=3), log=lambda s: None)
check('a 429 is retried and the game saved', 8 in r['saved'], r)
check('the incomplete game, filled in on its re-fetch, is complete', r['incomplete_finals'] == [], r['incomplete_finals'])
G35 = G3 + [game(9, '2027-05-12'), game(10, '2027-05-12')]
f = Fake(G35)
f.fail[('stats', 9)] = [404]
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=4, hours=1), log=lambda s: None)
check('a 404 on one game is reported and the next game still saved',
      [g for g, _ in r['failed']] == [9] and 10 in r['saved'], (r['failed'], r['saved']))
G4 = G35 + [game(i, '2027-05-13') for i in range(11, 16)]
f = Fake(G4)
r = bs.sync(root, 2027, client(f, cap=20), now=NOW + dt.timedelta(days=5), log=lambda s: None)
check('the cap stops the run and says what was left', r['capped'] and r['requests'] <= 20 and r['left'], r)
left = set(r['left'])
f = Fake(G4)
r2 = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=5, hours=1), log=lambda s: None)
check('the next run picks up the left games', left <= set(r2['saved']) and r2['missing_finals'] == [],
      (left, r2['saved'], r2['missing_finals']))

print('an incomplete game is retried for a week, no longer')
G5 = G4 + [game(30, '2027-05-01')]
f = Fake(G5)
f.feeds[30] = dict(feeds(30), lineups=[])
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=5, hours=2), log=lambda s: None)
check('saved as incomplete (no lineups)', 30 in r['saved'] and r['incomplete_finals'] == [(30, {'lineups': 0})], r)
f = Fake(G5)
f.feeds[30] = dict(feeds(30), lineups=[])
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=7), log=lambda s: None)
check('played 05-01, so on 05-17 it is past the 7-day window: not fetched again, still listed',
      30 not in fetched(f) and r['incomplete_finals'] == [(30, {'lineups': 0})], (fetched(f), r['incomplete_finals']))
G4 = G5

print('cut-short answers and repeated failures')
class NoData(Fake):
    def __call__(self, req, timeout):
        if '/plays' in req.full_url and 'game_id=12' in req.full_url:
            return io.BytesIO(json.dumps({'meta': {}}).encode())         # a 200 with no data list
        return super().__call__(req, timeout)
G6 = G4 + [game(40, '2027-05-17')]
f = NoData(G6 + [game(12, '2027-05-17')])
before12 = bs.read_game(sdir, 12)
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=8), log=lambda s: None)
check('a response without a data list is a failure, not an empty feed', 12 in [g for g, _ in r['failed']], r['failed'])
f = Fake(G6)
f.feeds[13] = dict(feeds(13), plays=feeds(13)['plays'][:3])           # game 13 re-fetched with plays cut short
idx13 = bs.load_index(sdir)[13]['n']['plays']
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=8, hours=1), log=lambda s: None, refetch_days=30)
check('a re-download with a feed 10% shorter than the file is not used', 13 in [g for g, _ in r['kept']]
      and bs.load_index(sdir)[13]['n']['plays'] == idx13, (r['kept'], idx13))
f = Fake(G6)
f.fail[('stats', 41)] = [404]
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=9), log=lambda s: None, refetch_days=0)
G7 = G6 + [game(41, '2027-05-18')]
f = Fake(G7); f.fail[('stats', 41)] = [404, 404]
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=9), log=lambda s: None, refetch_days=0)
f = Fake(G7)
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=9, hours=2), log=lambda s: None, refetch_days=0)
check('a game whose fetch failed waits 6 hours before the next try', 41 not in fetched(f) and r['backing_off'] >= 1,
      (fetched(f), r.get('backing_off')))
r = bs.sync(root, 2027, client(Fake(G7)), now=NOW + dt.timedelta(days=9, hours=7), log=lambda s: None, refetch_days=0)
check('...and is fetched after it', 41 in r['saved'], r['saved'])
try:
    bs.sync(root, 2027, client(Fake([g for g in G7 if g['id'] > 20])), now=NOW + dt.timedelta(days=10), log=lambda s: None)
    check('a games list with far fewer finals than last time is refused', False, 'accepted')
except RuntimeError as e:
    check('a games list with far fewer finals than last time is refused', 'not using it' in str(e), str(e))
check('...and the saved list is kept', sum(map(bs.is_regular_final, json.load(open(os.path.join(sdir, 'games.json'))))) ==
      sum(map(bs.is_regular_final, G7)))
G4 = G7

print('reading the store')
Gl, LU, PA, ST, PL, missing = bs.load(root, 2027)
check('G holds regular-season finals only', sorted(Gl) == sorted(g['id'] for g in G4 if bs.is_regular_final(g)))
check('every final has its four feeds', set(LU) == set(PA) == set(ST) == set(PL) == set(Gl) and missing == [])
pl = PL[1]
check('builders get the four play types they read, text only on play results',
      [x['type'] for x in pl] == ['Start Inning', 'Start Batter/Pitcher', 'Play Result', 'End Batter/Pitcher']
      and [('text' in x) for x in pl] == [False, False, True, False], pl)
check('builders get the plate-appearance and stat-line fields they read, nothing more',
      set(PA[1][0]) == set(bs.BUILDER_PA) and set(ST[1][0]) == set(bs.BUILDER_STAT) | {'player', 'team'},
      (set(PA[1][0]), set(ST[1][0])))
full = bs.load(root, 2027, trim=False)[4][1]
check('trim=False returns the file rows as stored', full == bs.read_game(sdir, 1)['plays'])
os.remove(os.path.join(sdir, 'index.json'))
idx = bs.load_index(sdir)
check('the index is rebuilt from the game files when lost', sorted(idx) == sorted(Gl), sorted(idx))
f = Fake(G4)
r = bs.sync(root, 2027, client(f), now=NOW + dt.timedelta(days=10, hours=1), log=lambda s: None, refetch_days=0)
check('and a rebuilt index fetches nothing extra', fetched(f) == [], fetched(f))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
