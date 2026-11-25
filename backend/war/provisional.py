"""Nightly provisional WAR for the current season: download → build → compute → check → (write).

Run by the nightly as a separate process (`python -m war.provisional --mode dry_run|write`), so its memory and CPU never
sit in the API process. It prints one JSON report as its last line. Exit 0 = done (written, unchanged, dry run, or
nothing to do); 2 = refused by a check (nothing written); 1 = error (nothing written).

Steps
  1. Only one run at a time: a Postgres advisory lock held for the whole run (a second run reports 'locked' and stops).
  2. Season window: from Opening Day to 3 days after the last scheduled regular-season day (api/season_phase.derive, on
     the game list the download just read). Outside it, nothing is computed or written. No rows before Opening Day.
  3. Download: war.bdl_store.sync into <root>/bdl<season> (new games, changed line scores, the last 3 days again).
  4. Build and compute from ONE database snapshot (REPEATABLE READ, READ ONLY), so a phase writing meanwhile (the
     19:00 catch-up) can't change the inputs halfway: the four balldontlie builders, the static inputs
     (war/static/current_season.json), compute(y0=S, current=S).
  5. Checks (see check()). REFUSE = nothing is written and the report says why. WARN = written, and listed.
     Players who can't be rated are left out and listed (Isaac 2026-11-25: write the rest, don't refuse the night).
  6. Unchanged = every row equals what's stored (floats within 1e-9): nothing is written.
  7. Write (mode 'write' only), one transaction: lock_timeout, statement_timeout, both freeze triggers present and
     enabled, LOCK TABLE, refuse if any row of this season and version is 'final'; DELETE the season's provisional
     rows of this version; INSERT the new ones; read back equal to what was submitted; every other row (other years,
     other versions) unchanged by count and content hash; commit. Then read back again on a fresh connection.

The version written is the settings file's (war/params/war_params_v2.json → 'v2'); v1 stays frozen at its 2026 rows.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from war import bdl_store, static_inputs  # noqa: E402
from war.builders import bdl_fielding, bdl_leverage, bdl_runners, bdl_season  # noqa: E402
from war.builders.bdl_common import bdl_maps  # noqa: E402

ET = ZoneInfo('America/New_York')
LOCK_KEY = 0x57415232                           # 'WAR2': the advisory lock that keeps runs from overlapping
DAYS_AFTER_LAST = 3                             # keep running this many days after the last regular-season day
OFFSEASON_MONTHS = (11, 12, 1, 2)               # no request at all in these months (Opening Day is never before March)

# checks: REFUSE below these (2026 for comparison, measured on the whole season)
MIN_SPAN_GAMES = 0.97        # games giving fielding spans / stored finals (2026: 2,414 / 2,429 = 99.4%)
MIN_PA_MAPPED = 0.97         # store plate appearances whose batter maps to one of our players (2026: see report)
MIN_FLD_BDL = 0.95           # position players' PA rated with balldontlie fielding (2026: see report)
MIN_RELIEF_LI = 0.95         # relief innings with an entry leverage (2026: see report)
MIN_PA_RATED = 0.98          # PA of rated hitter rows / player_seasons PA (2026: see report)
MIN_IP_RATED = 0.98          # IP of rated pitcher rows / pitcher_seasons IP
MAX_ABS_WAR = 15.0
MAX_MISSING_FINALS = 0.02    # finals without a stored file (a capped download), share of finals
MAX_NIGHT_CHANGE = 2.5       # biggest one-player change from the stored rows, when the code is the same
GAMELOG_LAG_DAYS = 1         # batting_gamelogs must reach the newest stored final's date minus this

COLS = ['player_id', 'year', 'role', 'method_version', 'war',
        'offense', 'defense', 'runs_batting', 'runs_baserunning', 'runs_double_plays', 'runs_replacement', 'runs_position',
        'runs_fielding', 'pa', 'pitcher_batting', 'pitcher_pa_share', 'fielding_source',
        'wins_above_avg', 'wins_replacement', 'ip', 'runs_allowed', 'expected_ra9', 'defense_runs', 'wins_linear',
        'entry_leverage', 'relief_ip_share',
        'runs_per_win', 'park_factor', 'league', 'data_tier', 'params_sha', 'run_id', 'computed_at']
META = {'run_id', 'computed_at', 'params_sha'}          # not compared when deciding whether anything changed
FLOAT_TOL = 1e-9


class Refuse(Exception):
    pass


def commit_id():
    c = os.environ.get('RAILWAY_GIT_COMMIT_SHA')
    if c:
        return c[:7]
    try:
        return subprocess.run(['git', '-C', BACKEND, 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return 'unknown'


def season_window(games, today):
    """(opening_day, last_regular_day) as dates from the season's game list, or (None, None)."""
    from api.season_phase import derive
    d = derive(games)
    f = lambda s: dt.date.fromisoformat(s) if s else None
    return f(d.get('opening_day')), f(d.get('last_regular_day'))


# --- build and compute -------------------------------------------------------------------------------------------------
def build_inputs(root, season, cur, static_path=static_inputs.PATH, log=lambda *a: None):
    G, LU, PA, ST, PL, missing = bdl_store.load(root, season)
    if not G:
        raise Refuse('no regular-season finals in the game list')
    p_map, pp_map = bdl_maps(cur)
    li = static_inputs.load(static_path)
    tag = f'bdl{season % 100:02d}'
    b = bdl_season.build(G, LU, PA, ST, p_map, log=log)
    inputs = dict(li)
    inputs[f'{tag}.pkl'] = b
    inputs[f'{tag}_fld.pkl'] = bdl_fielding.build(G, PA, ST, b['SPANS'], p_map, season, log=log)
    inputs[f'{tag}_runs.pkl'] = bdl_runners.build(set(G), ST, PL, b['SPANS'], p_map, log=log)
    inputs[f'{tag}_li.pkl'] = bdl_leverage.build(set(G), PA, PL, pp_map, li['li_raw.pkl']['TABLE'], log=log)
    pa_rows = sum(len(v) for v in PA.values())
    pa_mapped = sum(c[0] for c in b['PBP'].values())
    days = [bdl_store.eastern_day(G[g]) for g in PA]
    diag = {'finals': len(G), 'stored': len(PA), 'missing': len(missing), 'span_games': b['games'],
            'pa_rows': pa_rows, 'pa_mapped': pa_mapped, 'newest_final_et': max(days).isoformat() if days else None}
    return inputs, diag


def _canon(o):
    """A canonical, JSON-able form of an input (tuple keys and sets sorted by repr, floats exact)."""
    if isinstance(o, dict):
        return sorted([repr(k), _canon(v)] for k, v in o.items())
    if isinstance(o, (list, tuple)):
        return [_canon(x) for x in o]
    if isinstance(o, (set, frozenset)):
        return sorted(repr(x) for x in o)
    if isinstance(o, float):
        return repr(o)
    return o


def input_hash(inputs):
    """12 hex of the sha256 of every input compute() was given: the run_id names the exact data behind a row."""
    h = hashlib.sha256()
    for k in sorted(inputs):
        h.update(k.encode())
        h.update(json.dumps(_canon(inputs[k])).encode())
    return h.hexdigest()[:12]


def to_rows(res, season, version, params_sha, run_id, computed_at):
    """compute()'s result for `season` as war_seasons rows (the same mapping as the v1/v2 loads' build_plan.py)."""
    rows = []
    num = lambda x: None if x is None else float(x)
    for (pid, y), v in res['hit'].items():
        if y != season:
            continue
        rows.append(dict(player_id=pid, year=y, role='hit', war=num(v['war']), offense=num(v['offense']),
                         defense=num(v['defense']), runs_batting=num(v['bat']), runs_baserunning=num(v['br']),
                         runs_double_plays=num(v['dp']), runs_replacement=num(v['rep']), runs_position=num(v['pos']),
                         runs_fielding=num(v['fld']), pa=int(v['PA']), pitcher_batting=bool(v['pitcher']),
                         pitcher_pa_share=num(v['pit_share']), fielding_source=v['fielding_source'] or None,
                         runs_per_win=num(v['rpw']), park_factor=num(v['park']), league=v['league'] or None))
    for (pid, y), v in res['pit'].items():
        if y != season:
            continue
        rows.append(dict(player_id=pid, year=y, role='pit', war=num(v['war']), wins_above_avg=num(v['waa']),
                         wins_replacement=num(v['rep']), ip=num(v['IP']), runs_allowed=num(v['R_']),
                         expected_ra9=num(v['xra9']), defense_runs=num(v['rdef']), wins_linear=num(v['lin']),
                         entry_leverage=num(v.get('gmli')), relief_ip_share=num(v['relief_ip_share']),
                         runs_per_win=num(v['rpw']), park_factor=num(v['park']), league=v['league'] or None))
    for r in rows:
        r.update(method_version=version, data_tier='provisional', params_sha=params_sha, run_id=run_id,
                 computed_at=computed_at)
        for c in COLS:
            r.setdefault(c, None)
    rows.sort(key=lambda r: (r['role'], r['player_id']))
    return rows


def read_rows(cur, season, version):
    cur.execute(f"SELECT {', '.join(COLS)} FROM war_seasons WHERE year = %s AND method_version = %s", (season, version))
    return {(r[0], r[2]): dict(zip(COLS, r)) for r in cur.fetchall()}


def same_value(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) <= FLOAT_TOL
    return a == b


def diff_rows(new, old):
    """(added keys, removed keys, changed keys) between new rows (list) and stored rows ({(pid, role): row})."""
    nk = {(r['player_id'], r['role']): r for r in new}
    added = sorted(set(nk) - set(old))
    removed = sorted(set(old) - set(nk))
    changed = sorted(k for k in set(nk) & set(old)
                     if any(not same_value(nk[k][c], old[k][c]) for c in COLS if c not in META))
    return added, removed, changed


# --- checks ------------------------------------------------------------------------------------------------------------
def check(res, rows, diag, cur, season, old, same_code, store_rep):
    """(refusals, warnings, measures). Each measure is a number in the report, so the nightly log shows how close a
    night came to a threshold, and the season's first nights can be compared with 2026's."""
    refuse, warn, m = [], [], {}

    def need(name, value, ok, why):
        m[name] = round(value, 4) if isinstance(value, float) else value
        if not ok:
            refuse.append(f'{name} = {m[name]}: {why}')

    if store_rep is not None:
        if store_rep.get('capped') or store_rep.get('failed'):
            warn.append(f"download incomplete: capped={store_rep.get('capped')}, failed={len(store_rep.get('failed') or [])}, "
                        f"left={len(store_rep.get('left') or [])}")
        if store_rep.get('incomplete_finals'):
            warn.append(f"{len(store_rep['incomplete_finals'])} finals with an empty feed (used as they are): "
                        f"{store_rep['incomplete_finals'][:10]}")
    f = diag['finals']
    need('missing_finals_share', diag['missing'] / f, diag['missing'] / f <= MAX_MISSING_FINALS,
         'too many finished games not downloaded')
    if diag['missing']:
        warn.append(f"{diag['missing']} finals not downloaded yet")
    need('span_games_share', diag['span_games'] / max(1, diag['stored']), diag['span_games'] / max(1, diag['stored']) >= MIN_SPAN_GAMES,
         'too few games could be followed for fielding positions')
    need('pa_mapped_share', diag['pa_mapped'] / max(1, diag['pa_rows']), diag['pa_mapped'] / max(1, diag['pa_rows']) >= MIN_PA_MAPPED,
         'too many plate appearances by batters with no (or two) players under their balldontlie id')
    hit = [v for (p, y), v in res['hit'].items() if y == season]
    pos_pa = sum(float(v['PA']) for v in hit if not v['pitcher'])
    bdl_pa = sum(float(v['PA']) for v in hit if not v['pitcher'] and v['fielding_source'] == 'balldontlie')
    need('fielding_balldontlie_share', bdl_pa / max(1.0, pos_pa), bdl_pa / max(1.0, pos_pa) >= MIN_FLD_BDL,
         "too little of position players' playing time has balldontlie fielding (the fielding inputs didn't arrive)")
    pit = [v for (p, y), v in res['pit'].items() if y == season]
    rel = sum(float(v['IP']) * float(v['relief_ip_share']) for v in pit)
    rel_li = sum(float(v['IP']) * float(v['relief_ip_share']) for v in pit if v.get('gmli') is not None)
    need('relief_ip_with_leverage_share', rel_li / max(1.0, rel), rel_li / max(1.0, rel) >= MIN_RELIEF_LI,
         'too few relief innings have an entry leverage (the leverage inputs didn\'t arrive)')
    cur.execute('SELECT coalesce(sum(CASE WHEN coalesce("PA", 0) = 0 AND "AB" > 0 THEN "AB" + coalesce("BB",0) + '
                'coalesce("HBP",0) + coalesce("SF",0) + coalesce("SH",0) ELSE "PA" END), 0) FROM player_seasons '
                'WHERE year = %s AND coalesce(league, \'\') IN (\'AL\', \'NL\')', (season,))
    db_pa = float(cur.fetchone()[0] or 0)
    rated_pa = sum(float(v['PA']) for v in hit)
    need('pa_rated_share', rated_pa / max(1.0, db_pa), rated_pa / max(1.0, db_pa) >= MIN_PA_RATED,
         'too much of the season\'s playing time is not rated')
    cur.execute('SELECT coalesce(sum("IP"), 0) FROM pitcher_seasons WHERE year = %s AND coalesce(league, \'\') IN (\'AL\', \'NL\')',
                (season,))
    db_ip = float(cur.fetchone()[0] or 0)
    rated_ip = sum(float(v['IP']) for v in pit)
    need('ip_rated_share', rated_ip / max(1.0, db_ip), rated_ip / max(1.0, db_ip) >= MIN_IP_RATED,
         'too many of the season\'s innings are not rated')
    cur.execute('SELECT max(game_date) FROM batting_gamelogs WHERE season = %s', (season,))
    gl = cur.fetchone()[0]
    newest = dt.date.fromisoformat(diag['newest_final_et']) if diag['newest_final_et'] else None
    m['gamelogs_through'] = gl.isoformat() if gl else None
    m['inputs_through'] = diag['newest_final_et']
    if newest and (gl is None or gl < newest - dt.timedelta(days=GAMELOG_LAG_DAYS)):
        refuse.append(f'our game logs end {gl}, balldontlie finals reach {newest}: the season totals are behind the '
                      'play-by-play (an earlier nightly phase failed?)')
    bad = [(r['player_id'], r['role'], r['war']) for r in rows
           if r['war'] is None or not math.isfinite(r['war']) or abs(r['war']) > MAX_ABS_WAR]
    if bad:
        refuse.append(f'WAR missing, not finite or beyond ±{MAX_ABS_WAR}: {bad[:5]}')
    nofin = [(r['player_id'], r['role'], c) for r in rows for c in COLS
             if isinstance(r[c], float) and not math.isfinite(r[c])]
    if nofin:
        refuse.append(f'non-finite values: {nofin[:5]}')
    nolg = [(r['player_id'], r['role']) for r in rows if not r['league'] or len(r['league']) > 3]
    if nolg:
        refuse.append(f'rows without a league: {nolg[:5]}')
    if not rows:
        refuse.append('no rows')
    m['rows'] = len(rows)
    m['war_sum'] = round(sum(r['war'] for r in rows if r['war'] is not None), 2)
    skipped = [s for s in res['skipped'] if s[1] == season]
    m['skipped'] = len(skipped)
    if skipped:
        warn.append(f'{len(skipped)} player seasons left unrated: {skipped[:10]}')
    if old:
        nk = {(r['player_id'], r['role']): r['war'] for r in rows}
        deltas = sorted(((abs(nk[k] - o['war']), k) for k, o in old.items() if k in nk), reverse=True)
        m['biggest_change'] = (round(deltas[0][0], 3), list(deltas[0][1])) if deltas else None
        lost = [k for k in old if k not in nk]
        m['rows_dropped'] = len(lost)
        if lost:
            warn.append(f'{len(lost)} stored rows not in tonight\'s result: {lost[:10]}')
        if len(lost) > max(10, 0.01 * len(old)):
            refuse.append(f'{len(lost)} of {len(old)} stored rows would disappear')
        if same_code and deltas and deltas[0][0] > MAX_NIGHT_CHANGE:
            refuse.append(f'a player moved {deltas[0][0]:.2f} WAR in one night with the same code: {deltas[0][1]}')
    return refuse, warn, m


# --- write -------------------------------------------------------------------------------------------------------------
def _others_hash(cur, season, version):
    cur.execute("SELECT count(*), md5(string_agg(md5(x::text), '' ORDER BY md5(x::text))) FROM war_seasons x "
                "WHERE NOT (year = %s AND method_version = %s)", (season, version))
    return cur.fetchone()


def _norm(row):
    d = dict(row)
    if isinstance(d.get('computed_at'), dt.datetime):
        d['computed_at'] = d['computed_at'].replace(tzinfo=None).isoformat()
    return d


def _equal_exact(got, rows):
    want = {(r['player_id'], r['role']): r for r in rows}
    if set(got) != set(want):
        return f'keys differ: {len(set(got) ^ set(want))}'
    for k, r in want.items():
        g = _norm(got[k])
        for c in COLS:
            if g[c] != r[c]:
                return f'{k} {c}: stored {g[c]!r}, submitted {r[c]!r}'
    return None


def write(dsn, season, version, rows, log=print):
    """The guarded replacement of the season's provisional rows. Returns a dict; raises Refuse (rolled back)."""
    import psycopg2
    from psycopg2.extras import execute_batch
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("SET lock_timeout = '10s'")
        cur.execute("SET statement_timeout = '120s'")
        cur.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = 'war_seasons'::regclass AND NOT tgisinternal "
                    "AND tgenabled <> 'D'")
        trg = {r[0] for r in cur.fetchall()}
        if not {'war_seasons_freeze_rows', 'war_seasons_freeze_truncate'} <= trg:
            raise Refuse(f'freeze triggers missing or disabled (found {sorted(trg)})')
        cur.execute('LOCK TABLE war_seasons IN SHARE ROW EXCLUSIVE MODE')
        cur.execute("SELECT data_tier, count(*) FROM war_seasons WHERE year = %s AND method_version = %s GROUP BY 1",
                    (season, version))
        tiers = dict(cur.fetchall())
        if tiers.get('final'):
            raise Refuse(f'{season} {version} has {tiers["final"]} final rows: a finalized season is never rewritten')
        before = _others_hash(cur, season, version)
        cur.execute("DELETE FROM war_seasons WHERE year = %s AND method_version = %s AND data_tier = 'provisional'",
                    (season, version))
        deleted = cur.rowcount
        if deleted != tiers.get('provisional', 0):
            raise Refuse(f'deleted {deleted}, expected {tiers.get("provisional", 0)}')
        execute_batch(cur, f"INSERT INTO war_seasons ({', '.join(COLS)}) VALUES ({', '.join(['%s'] * len(COLS))})",
                      [tuple(r[c] for c in COLS) for r in rows], page_size=1000)
        got = read_rows(cur, season, version)
        bad = _equal_exact(got, rows)
        if bad:
            raise Refuse(f'read-back differs inside the transaction: {bad}')
        if _others_hash(cur, season, version) != before:
            raise Refuse('rows of other seasons or versions changed')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    conn2 = psycopg2.connect(dsn)                    # never trust the writer's own return: read back fresh
    try:
        cur2 = conn2.cursor()
        cur2.execute("SET TIME ZONE 'UTC'")
        bad = _equal_exact(read_rows(cur2, season, version), rows)
        after = _others_hash(cur2, season, version)
    finally:
        conn2.close()
    if bad:
        raise RuntimeError(f'COMMITTED, but the fresh read-back differs: {bad}')
    if after != before:
        raise RuntimeError('COMMITTED, but other rows differ on the fresh read-back')
    return {'deleted': deleted, 'inserted': len(rows), 'fresh_read_back': 'equal', 'others_unchanged': before[0]}


# --- the run -----------------------------------------------------------------------------------------------------------
def run(season, mode, root, dsn, client=None, now=None, force=False, params_path=None, log=print):
    from war.compute import DEFAULT_PARAMS, compute
    import psycopg2
    t0 = time.time()
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(ET).date()
    params_path = params_path or DEFAULT_PARAMS
    rep = {'season': season, 'mode': mode, 'started': now.isoformat(), 'commit': commit_id(), 'status': None}
    if not force and today.month in OFFSEASON_MONTHS:
        return dict(rep, status='offseason')
    lock = psycopg2.connect(dsn)
    lock.autocommit = True
    try:
        lc = lock.cursor()
        lc.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_KEY,))
        if not lc.fetchone()[0]:
            return dict(rep, status='locked')
        if client is not None:
            store_rep = bdl_store.sync(root, season, client, now=now, log=lambda s: None)
            rep['download'] = {k: (v if not isinstance(v, list) or len(v) <= 20 else f'{len(v)} games')
                               for k, v in store_rep.items()}
        else:
            store_rep = None
        games = json.load(open(os.path.join(bdl_store.season_dir(root, season), 'games.json')))
        opening, last = season_window(games, today)
        rep['window'] = [opening and opening.isoformat(), last and last.isoformat()]
        if not force and (opening is None or today < opening or last is None
                          or today > last + dt.timedelta(days=DAYS_AFTER_LAST)):
            return dict(rep, status='outside_season')
        conn = psycopg2.connect(dsn)
        try:
            conn.set_session(isolation_level='REPEATABLE READ', readonly=True)
            cur = conn.cursor()
            cur.execute("SET TIME ZONE 'UTC'")
            inputs, diag = build_inputs(root, season, cur)
            res = compute(cur, None, params_path=params_path, y0=season, current=season, inputs=inputs)
            version = res['params']
            params_sha = hashlib.sha256(open(params_path, 'rb').read()).hexdigest()
            run_id = f"{version}-{rep['commit']}-{input_hash(inputs)}"
            computed_at = now.replace(microsecond=0, tzinfo=None).isoformat()
            rows = to_rows(res, season, version, params_sha, run_id, computed_at)
            old = read_rows(cur, season, version)
            same_code = bool(old) and all(o['run_id'].split('-')[1:2] == [rep['commit']] for o in old.values())
            refuse, warn, measures = check(res, rows, diag, cur, season, old, same_code, store_rep)
        finally:
            conn.close()
        added, removed, changed = diff_rows(rows, old)
        rep.update(version=version, run_id=run_id, inputs=diag, measures=measures, warnings=warn,
                   changes={'added': len(added), 'removed': len(removed), 'changed': len(changed)})
        if any(o['data_tier'] == 'final' for o in old.values()):
            refuse.append(f'{season} {version} is finalized')
        if refuse:
            return dict(rep, status='refused', refusals=refuse, seconds=round(time.time() - t0, 1))
        if not (added or removed or changed):
            return dict(rep, status='unchanged', seconds=round(time.time() - t0, 1))
        if mode != 'write':
            return dict(rep, status='dry_run', seconds=round(time.time() - t0, 1))
        rep['write'] = write(dsn, season, version, rows, log=log)
        return dict(rep, status='written', seconds=round(time.time() - t0, 1))
    except Refuse as e:
        return dict(rep, status='refused', refusals=[str(e)], seconds=round(time.time() - t0, 1))
    finally:
        lock.close()                                  # releases the advisory lock


def main(argv=None):
    ap = argparse.ArgumentParser(description='Nightly provisional WAR for the current season')
    ap.add_argument('--mode', choices=('dry_run', 'write'), default='dry_run')
    ap.add_argument('--season', type=int, help='default: this year (Eastern)')
    ap.add_argument('--root', default=os.environ.get('WAR_STORE', '/data/war'), help='balldontlie store root')
    ap.add_argument('--no-download', action='store_true', help='use the store as it is (rehearsals)')
    ap.add_argument('--force', action='store_true', help='ignore the season window (rehearsals)')
    ap.add_argument('--max-requests', type=int, default=1500)
    a = ap.parse_args(argv)
    season = a.season or dt.datetime.now(ET).year
    dsn = os.environ.get('DATABASE_URL') or open(os.path.expanduser('~/.baseballstats_dburl')).read().strip()
    client = None
    if not a.no_download:
        key = os.environ.get('BDL_KEY')
        if not key:
            print(json.dumps({'season': season, 'status': 'error', 'error': 'BDL_KEY is not set'}))
            return 1
        client = bdl_store.Client(key, a.max_requests)
    try:
        rep = run(season, a.mode, a.root, dsn, client=client, force=a.force)
    except Exception as e:
        import traceback
        rep = {'season': season, 'mode': a.mode, 'status': 'error', 'error': f'{type(e).__name__}: {e}',
               'traceback': traceback.format_exc()[-2000:]}
    print(json.dumps(rep, default=str))
    return {'refused': 2, 'error': 1}.get(rep['status'], 0)


if __name__ == '__main__':
    sys.exit(main())
