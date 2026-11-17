# rb_build.py — READ-ONLY on Retrosheet duckdb. "Batter-distribution range": for every ball in play while a fielder is at
# position p, expected outs = the BATTER's career share of balls in play turned into outs by position p (shrunk toward the
# league by batter hand, K balls), scaled so each season's league total matches; made = 1 if p fielded the out first.
# Smith/B-Ref describe pre-2003 Total Zone as apportioning by "the batter's career out distribution"; this is that idea
# applied to outs, for seasons with no batted-ball types. Park-adjusted with visiting-fielder factors like tz_build.py.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
SEASON = int(_os.environ.get('WAR_SEASON', '2026'))                                                   # the balldontlie season
TAG = f'bdl{SEASON % 100:02d}'                                                                      # its input-file prefix
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser(f'~/baseballstats-tools/data/bdl{SEASON}'))   # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import duckdb, os, pickle, time
t0 = time.time(); T = lambda: f'{time.time() - t0:.0f}s'
K = float(os.environ.get('RBK', '200')); KP = float(os.environ.get('RBKP', '300')); USEP = os.environ.get('RBPIT', '1') == '1'
d = duckdb.connect(); d.execute("SET threads=8")
d.execute(f"ATTACH '{RETRO}' AS r (READ_ONLY)")
d.execute("""CREATE TEMP TABLE pl AS SELECT SEASON s, GAME_ID g, EVENT_ID e, BAT_ID bid, PIT_ID pid_p, HOME_TEAM_ID ht,
   CASE WHEN BAT_HOME_ID=1 THEN AWAY_TEAM_ID ELSE HOME_TEAM_ID END ft, CASE WHEN BAT_HOME_ID=1 THEN HOME_TEAM_ID ELSE AWAY_TEAM_ID END bt_team,
   coalesce(BAT_HAND_CD,'?') h, EVENT_CD cd, EVENT_TX tx, BAT_FLD_CD bfc FROM r.plays WHERE GAME_TYPE='R'""")
d.execute("CREATE TEMP TABLE pos AS SELECT g, bt_team team, bfc p, e, bid pid FROM pl WHERE bfc BETWEEN 2 AND 9")
sel = ', '.join(f'coalesce(b{p}.pid, a{p}.pid) f{p}' for p in range(3, 10))
joins = ' '.join(f"ASOF LEFT JOIN (SELECT * FROM pos WHERE p={p}) b{p} ON b{p}.g=x.g AND b{p}.team=x.ft AND b{p}.e <= x.e "
                 f"ASOF LEFT JOIN (SELECT * FROM pos WHERE p={p}) a{p} ON a{p}.g=x.g AND a{p}.team=x.ft AND a{p}.e >= x.e" for p in range(3, 10))
d.execute(f"""CREATE TEMP TABLE b AS SELECT x.s, x.g, x.ht, x.ft, x.bid, x.pid_p, x.h, {sel},
   CASE WHEN x.cd IN (2,19) THEN TRY_CAST(regexp_extract(x.tx, '^([1-9])', 1) AS INT) END z
   FROM pl x {joins} WHERE x.cd IN (2,18,19,20,21,22) AND NOT x.tx LIKE '99%'""")
print('balls in play', d.sql('SELECT count(*) FROM b').fetchone(), T())
out = []
for p in range(3, 10):
    d.execute(f"""CREATE OR REPLACE TEMP TABLE c AS SELECT s, ht, ft, bid, pid_p, h, f{p} fid, coalesce(z = {p}, false)::int made FROM b""")
    d.execute(f"""CREATE OR REPLACE TEMP TABLE lh AS SELECT h, avg(made) r FROM c GROUP BY 1""")
    d.execute(f"""CREATE OR REPLACE TEMP TABLE bs AS SELECT bid, h, (sum(made) + {K} * any_value(lh.r)) / (count(*) + {K}) sh FROM c JOIN lh USING (h) GROUP BY 1, 2""")
    # pitcher's career share for this position (shrunk), combined with the batter's by log5-style ratio to the league
    d.execute(f"""CREATE OR REPLACE TEMP TABLE lall AS SELECT avg(made) r FROM c""")
    d.execute(f"""CREATE OR REPLACE TEMP TABLE ps AS SELECT pid_p, (sum(made) + {KP} * any_value(lall.r)) / (count(*) + {KP}) / any_value(lall.r) prel FROM c, lall GROUP BY 1""")
    d.execute(f"""CREATE OR REPLACE TEMP TABLE c2 AS SELECT c.*, bs.sh * CASE WHEN {USEP} THEN coalesce(ps.prel, 1) ELSE 1 END sh FROM c JOIN bs USING (bid, h) LEFT JOIN ps USING (pid_p)""")
    d.execute(f"""CREATE OR REPLACE TEMP TABLE sf AS SELECT s, avg(made) / avg(sh) k FROM c2 GROUP BY 1""")
    d.execute(f"""CREATE OR REPLACE TEMP TABLE c3 AS SELECT c2.*, sh * sf.k x FROM c2 JOIN sf USING (s)""")
    d.execute(f"""CREATE OR REPLACE TEMP TABLE pk1 AS SELECT s, ht, sum(made) m, sum(x) x FROM c3 WHERE ft <> ht GROUP BY 1, 2""")
    d.execute(f"""CREATE OR REPLACE TEMP TABLE pk AS SELECT a.s, a.ht, (sum(b.m) + 300) / (sum(b.x) + 300) pf FROM (SELECT DISTINCT s, ht FROM pk1) a
                  JOIN pk1 b ON b.ht = a.ht AND b.s BETWEEN a.s - 2 AND a.s + 2 GROUP BY 1, 2""")
    rows = d.sql(f"""SELECT s, fid, ft, {p}, sum(made), count(*), sum(x), sum(x * coalesce(pf, 1)) FROM c3 LEFT JOIN pk USING (s, ht) WHERE fid IS NOT NULL GROUP BY 1, 2, 3""").fetchall()
    out += rows; print('pos', p, len(rows), T())
pickle.dump(out, open(os.path.join(INPUTS, 'rb_raw.pkl'), 'wb'))
