#!/usr/bin/env python3
"""The nightly provisional WAR (backend/war/provisional.py): its guarded write, its checks and its run lock, on an
embedded Postgres with the real war_seasons table and freeze triggers (`pip install pgserver`; FAILS without it).

⚠️ THE CHECKS THAT MATTER:
  • the write replaces exactly the season's provisional rows of the version it writes: other seasons, other versions
    and final rows are untouched, and what is stored equals what was submitted, value for value
  • it refuses (and changes nothing) when the season has a final row, or a freeze trigger is missing or disabled
  • each check refuses the night it is meant to: fielding or leverage inputs missing, game logs behind the
    play-by-play, rows disappearing, a one-night jump with unchanged code, non-finite values
  • a second run while one holds the lock stops at once ('locked')

Standalone, no pytest, no network. Run: <backend python> backend/tests/test_war_provisional.py
(without the read-only PGOPTIONS: it builds its own local database)
"""
import copy
import datetime as dt
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


try:
    import pgserver
except ImportError:
    check('pgserver installed', False, 'pip install pgserver')
    sys.exit(1)
import psycopg2  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from database.models import Base, BattingGameLog, PitcherSeason, PlayerSeason, WarSeason  # noqa: E402
from war import provisional as pv  # noqa: E402

srv = pgserver.get_server(tempfile.mkdtemp(prefix='war_prov_'), cleanup_mode='stop')
DSN = srv.get_uri()
eng = create_engine(DSN)
Base.metadata.create_all(eng, tables=[WarSeason.__table__, PlayerSeason.__table__, PitcherSeason.__table__,
                                      BattingGameLog.__table__])
S, V = 2027, 'v2'
T0 = '2027-05-01T15:00:00'


def row(pid, role, war, year=S, version=V, tier='provisional', run_id='v2-aaaaaaa-111111111111', **kw):
    r = {c: None for c in pv.COLS}
    r.update(player_id=pid, year=year, role=role, method_version=version, war=war, data_tier=tier,
             params_sha='p' * 64, run_id=run_id, computed_at=T0, league='AL', runs_per_win=10.0, park_factor=1.0)
    if role == 'hit':
        r.update(offense=war * 0.8, defense=war * 0.2, pa=500, pitcher_batting=False, fielding_source='balldontlie')
    else:
        r.update(ip=150.0, wins_above_avg=war - 1, wins_replacement=1.0)
    r.update(kw)
    return r


def sql(q, args=()):
    c = psycopg2.connect(DSN)
    c.autocommit = True
    cur = c.cursor()
    cur.execute("SET TIME ZONE 'UTC'")
    cur.execute(q, args)
    out = cur.fetchall() if cur.description else None
    c.close()
    return out


def insert(rows):
    c = psycopg2.connect(DSN)
    cur = c.cursor()
    for r in rows:
        cur.execute(f"INSERT INTO war_seasons ({', '.join(pv.COLS)}) VALUES ({', '.join(['%s'] * len(pv.COLS))})",
                    tuple(r[k] for k in pv.COLS))
    c.commit()
    c.close()


def snapshot(where):
    return sql(f"SELECT md5(string_agg(md5(x::text), '' ORDER BY md5(x::text))), count(*) FROM war_seasons x WHERE {where}")[0]


print('the guarded write')
insert([row(1, 'hit', 3.0), row(2, 'hit', 1.5), row(3, 'pit', 2.0),                       # tonight's season, v2
        row(1, 'hit', 2.9, version='v1'), row(3, 'pit', 2.1, version='v1'),               # same season, another version
        row(1, 'hit', 4.0, year=2026, tier='final'), row(3, 'pit', 5.0, year=2026, tier='final')])
others = snapshot(f"NOT (year = {S} AND method_version = '{V}')")
new = [row(1, 'hit', 3.25, run_id='v2-bbbbbbb-222222222222', computed_at='2027-05-02T15:00:00'),
       row(3, 'pit', 1.875, run_id='v2-bbbbbbb-222222222222', computed_at='2027-05-02T15:00:00', entry_leverage=1.31),
       row(4, 'hit', 0.1 + 0.2, run_id='v2-bbbbbbb-222222222222', computed_at='2027-05-02T15:00:00', fielding_source=None)]
w = pv.write(DSN, S, V, new, log=lambda *a: None)
check('deletes the 3 stored provisional rows and inserts the 3 new ones', (w['deleted'], w['inserted']) == (3, 3), w)
c = psycopg2.connect(DSN); cur = c.cursor(); cur.execute("SET TIME ZONE 'UTC'")
got = pv.read_rows(cur, S, V); c.close()
check('what is stored equals what was submitted, value for value (0.1 + 0.2 kept bit-exact)',
      pv._equal_exact(got, new) is None, pv._equal_exact(got, new))
check('player 2, not in tonight\'s result, is gone from the season', (2, 'hit') not in got)
check('other versions and other seasons untouched (content hash)', snapshot(f"NOT (year = {S} AND method_version = '{V}')") == others)
check('fresh read-back reported equal', w['fresh_read_back'] == 'equal')

print('refusals leave everything as it was')
insert([row(9, 'hit', 1.0, tier='final')])
before = snapshot('true')
try:
    pv.write(DSN, S, V, new, log=lambda *a: None)
    check('refuses a season with a final row', False, 'no refusal')
except pv.Refuse as e:
    check('refuses a season with a final row', 'final' in str(e), str(e))
check('...and nothing changed', snapshot('true') == before)
try:
    sql(f"DELETE FROM war_seasons WHERE player_id = 9 AND year = {S}")
    check('the database itself refuses to delete a final row (freeze trigger)', False, 'deleted')
except psycopg2.Error as e:
    check('the database itself refuses to delete a final row (freeze trigger)', 'final' in str(e), str(e)[:80])
sql('ALTER TABLE war_seasons DISABLE TRIGGER war_seasons_freeze_rows')
sql(f"DELETE FROM war_seasons WHERE player_id = 9 AND year = {S}")          # test cleanup, trigger off
before = snapshot('true')
try:
    pv.write(DSN, S, V, new, log=lambda *a: None)
    check('refuses while a freeze trigger is disabled', False, 'no refusal')
except pv.Refuse as e:
    check('refuses while a freeze trigger is disabled', 'trigger' in str(e), str(e))
check('...and nothing changed', snapshot('true') == before)
sql('ALTER TABLE war_seasons ENABLE TRIGGER war_seasons_freeze_rows')
w = pv.write(DSN, S, V, new, log=lambda *a: None)
check('with the trigger back, the same write goes through', w['inserted'] == 3, w)

print('deciding whether anything changed')
c = psycopg2.connect(DSN); cur = c.cursor(); cur.execute("SET TIME ZONE 'UTC'")
old = pv.read_rows(cur, S, V); c.close()
same = [dict(r, war=r['war'] + 1e-12, run_id='v2-ccccccc-333333333333', computed_at='2027-05-03T15:00:00') for r in new]
check('float noise and a new run_id/time are not a change', pv.diff_rows(same, old) == ([], [], []), pv.diff_rows(same, old))
moved = copy.deepcopy(new); moved[0]['war'] += 0.01; moved = moved[:2] + [row(5, 'pit', 0.5)]
check('a moved value, a new player and a dropped one are', pv.diff_rows(moved, old) == ([(5, 'pit')], [(4, 'hit')], [(1, 'hit')]),
      pv.diff_rows(moved, old))

print('the checks')
with eng.begin() as c2:
    c2.execute(PlayerSeason.__table__.insert().values(player_id=1, year=S, team='NYA', league='AL', PA=1000))
    c2.execute(PitcherSeason.__table__.insert().values(player_id=3, year=S, team='NYA', league='AL', IP=150.0))
    c2.execute(BattingGameLog.__table__.insert().values(player_id=1, game_id='g1', game_date=dt.date(2027, 5, 1), season=S))


def H(pa, src='balldontlie', pitcher=False):
    return {'PA': pa, 'pitcher': pitcher, 'fielding_source': src}


RES = {'hit': {(1, S): H(600), (4, S): H(400)}, 'pit': {(3, S): {'IP': 150.0, 'relief_ip_share': 0.5, 'gmli': 1.2}},
       'skipped': []}
DIAG = {'finals': 100, 'stored': 100, 'missing': 0, 'span_games': 99, 'pa_rows': 1000, 'pa_mapped': 995,
        'newest_final_et': '2027-05-01'}
ROWS = [row(1, 'hit', 3.0), row(4, 'hit', 0.5), row(3, 'pit', 2.0)]
OLD = {(r['player_id'], r['role']): r for r in ROWS}


def run_check(res=RES, diag=DIAG, rows=ROWS, old=OLD, same_code=True):
    c = psycopg2.connect(DSN); cur = c.cursor()
    try:
        return pv.check(res, rows, diag, cur, S, old, same_code, None)
    finally:
        c.close()


ref, warn, m = run_check()
check('a normal night passes', ref == [], ref)
check('and reports its measures', m['fielding_balldontlie_share'] == 1.0 and m['pa_rated_share'] == 1.0 and m['inputs_through'] == '2027-05-01', m)
r2 = copy.deepcopy(RES); r2['hit'][(1, S)]['fielding_source'] = None
check('refuses when the fielding inputs didn\'t arrive (no balldontlie fielding)', any('fielding_balldontlie' in x for x in run_check(res=r2)[0]))
r3 = copy.deepcopy(RES); r3['pit'][(3, S)]['gmli'] = None
check('refuses when the leverage inputs didn\'t arrive', any('relief_ip_with_leverage' in x for x in run_check(res=r3)[0]))
check('refuses when our game logs are behind the play-by-play',
      any('game logs end' in x for x in run_check(diag=dict(DIAG, newest_final_et='2027-05-04'))[0]))
check('allows game logs one day behind', run_check(diag=dict(DIAG, newest_final_et='2027-05-02'))[0] == [])
check('refuses when too few games give fielding spans', any('span_games' in x for x in run_check(diag=dict(DIAG, span_games=90))[0]))
check('refuses when too many plate appearances can\'t be mapped to players', any('pa_mapped' in x for x in run_check(diag=dict(DIAG, pa_mapped=900))[0]))
check('refuses when too many finals are missing', any('missing_finals' in x for x in run_check(diag=dict(DIAG, missing=5))[0]))
r4 = copy.deepcopy(RES); del r4['hit'][(4, S)]
check('refuses when too much playing time is unrated', any('pa_rated' in x for x in run_check(res=r4)[0]))
jump = [dict(ROWS[0], war=6.0)] + ROWS[1:]
check('refuses a 3-WAR jump in one night with the same code', any('moved' in x for x in run_check(rows=jump)[0]))
check('...but not after a code change (reported instead)', run_check(rows=jump, same_code=False)[0] == [])
check('refuses a non-finite value', any('non-finite' in x or 'not finite' in x for x in
                                      run_check(rows=[dict(ROWS[0], runs_per_win=float('nan'))] + ROWS[1:])[0]))
check('refuses WAR beyond ±15', any('beyond' in x for x in run_check(rows=[dict(ROWS[0], war=16.0)] + ROWS[1:], same_code=False)[0]))
many_old = {(i, 'hit'): row(i, 'hit', 1.0) for i in range(100, 130)}
many_old.update(OLD)
check('refuses when stored rows would disappear (more than 10, or 1%)', any('disappear' in x for x in run_check(old=many_old)[0]))
r5 = copy.deepcopy(RES); r5['skipped'] = [(7, S, 'pit', 'no team or league recorded')]
ref, warn, m = run_check(res=r5)
check('an unrated player is listed, not refused (Isaac 2026-11-25)', ref == [] and any('unrated' in x for x in warn), (ref, warn))

print('one run at a time')
holder = psycopg2.connect(DSN); holder.autocommit = True
holder.cursor().execute('SELECT pg_advisory_lock(%s)', (pv.LOCK_KEY,))
rep = pv.run(S, 'dry_run', tempfile.mkdtemp(), DSN, client=None, now=dt.datetime(2027, 5, 2, 15, tzinfo=dt.timezone.utc))
check('a second run stops at once while the lock is held', rep['status'] == 'locked', rep)
holder.close()
rep = pv.run(S, 'dry_run', tempfile.mkdtemp(), DSN, client=None, now=dt.datetime(2027, 12, 2, 15, tzinfo=dt.timezone.utc))
check('in the offseason months it stops before any request or database work', rep['status'] == 'offseason', rep)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
