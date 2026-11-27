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
    for i in range(100):
        c2.execute(BattingGameLog.__table__.insert().values(player_id=1, game_id=f'g{i}', game_date=dt.date(2027, 5, 1),
                                                            season=S, team_score=3))


def H(pa, src='balldontlie', pitcher=False):
    return {'PA': pa, 'pitcher': pitcher, 'fielding_source': src}


RES = {'hit': {(1, S): H(600), (4, S): H(400)}, 'pit': {(3, S): {'IP': 150.0, 'relief_ip_share': 0.5, 'gmli': 1.2}},
       'skipped': [], 'park_notes': {}}
DIAG = {'finals': 100, 'stored': 100, 'missing': 0, 'span_games': 99, 'no_lineups_or_pa': 0, 'pa_rows': 1000,
        'pa_mapped': 995, 'pa_in_span_games': 990, 'games_with_pa_and_plays': 100, 'newest_final_et': '2027-05-01'}
ROWS = [row(1, 'hit', 3.0), row(4, 'hit', 0.5), row(3, 'pit', 2.0)]
OLD = {(r['player_id'], r['role']): r for r in ROWS}


def run_check(res=RES, diag=DIAG, rows=ROWS, old=OLD, same_code=True, accept=False):
    c = psycopg2.connect(DSN); cur = c.cursor()
    try:
        return pv.check(res, rows, diag, cur, S, old, same_code, None, accept)
    finally:
        c.close()


refused = lambda r, word: any(word in x for x in r[0])
ref, warn, m = run_check()
check('a normal night passes', ref == [], ref)
check('and reports its measures', m['fielding_balldontlie_share'] == 1.0 and m['pa_rated_share'] == 1.0
      and m['inputs_through'] == '2027-05-01' and m['finals_vs_gamelog_games'] == [100, 100], m)
r2 = copy.deepcopy(RES); r2['hit'][(1, S)]['fielding_source'] = None
check('refuses when the fielding inputs didn\'t arrive (no balldontlie fielding)', refused(run_check(res=r2), 'fielding'))
r3 = copy.deepcopy(RES); r3['pit'][(3, S)]['gmli'] = None
check('refuses when the leverage inputs didn\'t arrive', refused(run_check(res=r3), 'leverage'))
check('refuses when our game logs are behind the download', refused(run_check(diag=dict(DIAG, newest_final_et='2027-05-04')), 'game logs end'))
check('refuses when the download is behind our game logs', refused(run_check(diag=dict(DIAG, newest_final_et='2027-04-28')), 'download ends'))
check('allows a one-day difference either way', run_check(diag=dict(DIAG, newest_final_et='2027-05-02'))[0] == []
      and run_check(diag=dict(DIAG, newest_final_et='2027-04-30'))[0] == [])
check('refuses when games with lineups give no fielding positions (beyond 10)', refused(run_check(diag=dict(DIAG, span_games=85)), 'fielding positions'))
check('refuses when too many plate appearances can\'t be mapped to players', refused(run_check(diag=dict(DIAG, pa_rows=12000, pa_mapped=11000)), 'balldontlie id'))
check('refuses when too many finals are missing (beyond 15)', refused(run_check(diag=dict(DIAG, finals=120, missing=20)), 'not downloaded'))
check('...but one missing game is a warning', run_check(diag=dict(DIAG, missing=1))[0] == [] and any('not downloaded' in w for w in run_check(diag=dict(DIAG, missing=1))[1]))
check('refuses when the download and our game logs disagree on the number of games', refused(run_check(diag=dict(DIAG, finals=140, stored=140)), 'game logs'))
r4 = copy.deepcopy(RES); del r4['hit'][(4, S)]
check('refuses when too much playing time is unrated', refused(run_check(res=r4), 'plate appearances are not rated'))
jump = [dict(ROWS[0], war=6.0)] + ROWS[1:]
check('refuses a 3-WAR jump in one night with the same code', refused(run_check(rows=jump), 'moved'))
check('...but not after a code change', run_check(rows=jump, same_code=False)[0] == [])
check('...and not with accept_change (reported instead)', run_check(rows=jump, accept=True)[0] == []
      and any('accept_change' in w for w in run_check(rows=jump, accept=True)[1]))
newbig = ROWS + [row(8, 'hit', 2.4)]
check('refuses a player with no row last night appearing at 2.4 WAR (wrong-id copies)', refused(run_check(rows=newbig), 'no row last night'))
check('a new player at a small WAR is fine', run_check(rows=ROWS + [row(8, 'hit', 0.2)])[0] == [])
check('refuses a non-finite value', refused(run_check(rows=[dict(ROWS[0], runs_per_win=float('nan'))] + ROWS[1:]), 'finite'))
check('refuses WAR beyond ±15', refused(run_check(rows=[dict(ROWS[0], war=16.0)] + ROWS[1:], same_code=False), 'beyond'))
many_old = {(i, 'hit'): row(i, 'hit', 1.0) for i in range(100, 130)}
many_old.update(OLD)
check('refuses when stored rows would disappear (more than 10, or 1%)', refused(run_check(old=many_old), 'disappear'))
r5 = copy.deepcopy(RES); r5['skipped'] = [(7, S, 'pit', 'no team or league recorded')]; r5['park_notes'] = {'ATH': 'last season\'s factor'}
ref, warn, m = run_check(res=r5)
check('an unrated player and a park fallback are listed, not refused (Isaac 2026-11-25)',
      ref == [] and any('unrated' in x for x in warn) and any('park factor for ATH' in x for x in warn), (ref, warn))

print('the season\'s first nights')
# Opening Day 2026: 12 games, 10 of them with no lineups from balldontlie; the hitters of those games get no fielding
od = dict(DIAG, finals=12, stored=12, span_games=2, no_lineups_or_pa=10, pa_rows=900, pa_mapped=890,
          pa_in_span_games=150, games_with_pa_and_plays=12)
r6 = copy.deepcopy(RES); r6['hit'] = {(1, S): H(100), (4, S): H(500, src=None)}
c3 = psycopg2.connect(DSN); c3.autocommit = True; cur3 = c3.cursor()
cur3.execute("DELETE FROM batting_gamelogs WHERE game_id NOT IN (SELECT game_id FROM batting_gamelogs ORDER BY game_id LIMIT 12)")
cur3.execute('UPDATE player_seasons SET "PA" = 600 WHERE year = %s', (S,))
ref, warn, m = run_check(res=r6, diag=od, old={})
check('Opening Day with ten lineup-less games is written, with warnings (a refusal would blank every career)',
      ref == [] and any('no lineups' in w for w in warn), (ref, warn))
check('...a fielding step that produced nothing is still refused on Opening Day',
      refused(run_check(res=dict(r6, hit={(1, S): H(100, src=None), (4, S): H(500, src=None)}), diag=od, old={}), 'fielding'))
c3.close()
print('a feed outage mid-season')
# 1500 games, the newest 150 with no plate appearances: the fielding and leverage shares can't see it (their expected
# shares are built from the same games), so the count of feed-less games itself must refuse
mid = dict(DIAG, finals=1500, stored=1500, span_games=1350, no_lineups_or_pa=150, pa_rows=105300, pa_mapped=105300,
           pa_in_span_games=105300, games_with_pa_and_plays=1350)
check('150 of 1500 games without feeds is refused (an outage, not Opening Day\'s twelve)',
      refused(run_check(diag=mid), 'feed outage'))
check('22 of 1500 (2026\'s twelve plus a lagging night) is a warning', not any(
    'feed outage' in r for r in run_check(diag=dict(mid, span_games=1478, no_lineups_or_pa=22))[0]))
print('what a night that passed does')
check('nothing differs → unchanged (no write), in either mode', pv.outcome('write', False) == 'unchanged'
      and pv.outcome('dry_run', False) == 'unchanged')
check('changes → write in write mode, dry_run otherwise', pv.outcome('write', True) == 'write' and pv.outcome('dry_run', True) == 'dry_run')
check('rewrite (rehearsal) writes identical rows, but only in write mode', pv.outcome('write', False, True) == 'write'
      and pv.outcome('dry_run', False, True) == 'unchanged')
print('one run at a time')
holder = psycopg2.connect(DSN); holder.autocommit = True
holder.cursor().execute('SELECT pg_advisory_lock(%s)', (pv.LOCK_KEY,))
rep = pv.run(S, 'dry_run', tempfile.mkdtemp(), DSN, client=None, now=dt.datetime(2027, 5, 2, 15, tzinfo=dt.timezone.utc))
check('a second run stops at once while the lock is held', rep['status'] == 'locked', rep)
holder.close()
rep = pv.run(S, 'dry_run', tempfile.mkdtemp(), DSN, client=None, now=dt.datetime(2027, 12, 2, 15, tzinfo=dt.timezone.utc))
check('in the offseason months it stops before any request or database work', rep['status'] == 'offseason', rep)
import json as _json
root = tempfile.mkdtemp()
os.makedirs(os.path.join(root, f'bdl{S}'))
od_games = [{'id': i, 'date': '2027-03-26T23:00:00.000Z', 'status': 'STATUS_SCHEDULED', 'season_type': 'regular',
             'home_team': {'id': i, 'abbreviation': f'T{i}'}, 'away_team': {'id': 100 + i, 'abbreviation': f'U{i}'}}
            for i in range(1, 16)]
_json.dump(od_games, open(os.path.join(root, f'bdl{S}', 'games.json'), 'w'))
rep = pv.run(S, 'dry_run', root, DSN, client=None, now=dt.datetime(2027, 3, 26, 20, tzinfo=dt.timezone.utc))
check('Opening Day before the first final: nothing to do, not a refusal', rep['status'] == 'no_games_yet', rep)
check('a \'locked\' run doesn\'t overwrite the saved report', pv.save_report(root, {'status': 'locked'}) and
      not os.path.exists(os.path.join(root, 'reports', 'latest.json')))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
