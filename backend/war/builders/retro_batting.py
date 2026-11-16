# bat_build.py — READ-ONLY on Retrosheet duckdb. Per season: run-expectancy event values (single, double, triple, HR,
# unintentional walk, HBP, out). Per (season, batter): baserunning beyond steals (extra bases taken / outs on the bases
# as a runner on singles, doubles and outs in play, vs league for the same event, base and outs) and grounded-into-DP
# vs league rate on chances (runner on first, < 2 outs).
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser('~/baseballstats-tools/data/bdl2026'))      # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import duckdb, os, pickle, time
t0 = time.time(); T = lambda: f'{time.time() - t0:.0f}s'
d = duckdb.connect(); d.execute("SET threads=8")
d.execute(f"ATTACH '{RETRO}' AS r (READ_ONLY)")
d.execute("""CREATE TEMP TABLE p AS SELECT SEASON s, GAME_ID g, INN_CT i, BAT_HOME_ID hb, EVENT_ID e, EVENT_CD cd, EVENT_TX tx, OUTS_CT o,
   BAT_ID bid, BASE1_RUN_ID b1, BASE2_RUN_ID b2, BASE3_RUN_ID b3, BAT_DEST_ID bd, RUN1_DEST_ID d1, RUN2_DEST_ID d2, RUN3_DEST_ID d3,
   EVENT_OUTS_CT eo, DP_FL dp,
   (BAT_DEST_ID>=4)::int + (RUN1_DEST_ID>=4)::int + (RUN2_DEST_ID>=4)::int + (RUN3_DEST_ID>=4)::int runs,
   o*8 + (b1 IS NOT NULL)::int + 2*(b2 IS NOT NULL)::int + 4*(b3 IS NOT NULL)::int st0,
   CASE WHEN o + EVENT_OUTS_CT >= 3 THEN 24 ELSE (o+EVENT_OUTS_CT)*8
        + ((BAT_DEST_ID=1) OR (RUN1_DEST_ID=1))::int + 2*((BAT_DEST_ID=2) OR (RUN1_DEST_ID=2) OR (RUN2_DEST_ID=2))::int
        + 4*((BAT_DEST_ID=3) OR (RUN1_DEST_ID=3) OR (RUN2_DEST_ID=3) OR (RUN3_DEST_ID=3))::int END st1
   FROM r.plays WHERE GAME_TYPE='R'""")
d.execute("CREATE TEMP TABLE p2 AS SELECT *, sum(runs) OVER (PARTITION BY g, i, hb ORDER BY e ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) rest FROM p")
d.execute("CREATE TEMP TABLE re AS SELECT s, st0 st, avg(rest) v FROM p2 GROUP BY 1, 2 UNION ALL SELECT DISTINCT s, 24, 0.0 FROM p2")
d.execute("""CREATE TEMP TABLE val AS SELECT p2.s, cd, tx, bid, coalesce(a.v, 0) - b.v + runs rv, p2.* EXCLUDE (s, cd, tx, bid)
   FROM p2 JOIN re b ON b.s=p2.s AND b.st=p2.st0 LEFT JOIN re a ON a.s=p2.s AND a.st=p2.st1""")
print('run values', T())
LW = {}
for s, cls, v, n in d.sql("""SELECT s, CASE cd WHEN 20 THEN 'S' WHEN 21 THEN 'D' WHEN 22 THEN 'T' WHEN 23 THEN 'HR' WHEN 14 THEN 'BB' WHEN 16 THEN 'HBP'
       WHEN 3 THEN 'K' WHEN 2 THEN 'O' WHEN 18 THEN 'ROE' END c, avg(rv), count(*) FROM val WHERE cd IN (2,3,14,16,18,20,21,22,23) GROUP BY 1, 2""").fetchall():
    LW.setdefault(s, {})[cls] = v
print('weights', T(), {k: round(v, 3) for k, v in LW[1965].items()})
# baserunning: runner advancement vs league on singles, doubles, outs in play; dest 0 = out on the bases
d.execute("""CREATE TEMP TABLE run AS
   SELECT s, cd, 1 base, o, b1 rid, d1 dst FROM p WHERE b1 IS NOT NULL AND cd IN (20,21)
   UNION ALL SELECT s, cd, 2, o, b2, d2 FROM p WHERE b2 IS NOT NULL AND cd IN (20,21)
   UNION ALL SELECT s, cd, 2, o, b2, d2 FROM p WHERE false""")
BR = d.sql("""WITH x AS (SELECT *, CASE WHEN dst = 0 THEN 0 ELSE least(dst, 4) - base END adv, (dst = 0)::int wo FROM run),
   l AS (SELECT s, cd, base, o, avg(adv) FILTER (WHERE wo = 0) ea, avg(wo) eo FROM x GROUP BY ALL)
   SELECT s, rid, sum(CASE WHEN wo = 0 THEN adv - ea ELSE 0 END), sum(wo - eo), count(*) FROM x JOIN l USING (s, cd, base, o) GROUP BY ALL""").fetchall()
print('baserunning', T())
DP = d.sql("""WITH x AS (SELECT s, bid, (cd = 2 AND dp AND regexp_matches(tx, 'GDP'))::int gdp FROM p WHERE b1 IS NOT NULL AND o < 2
                 AND cd IN (2,3,14,15,16,18,19,20,21,22,23)), l AS (SELECT s, avg(gdp) r FROM x GROUP BY 1)
   SELECT s, bid, sum(gdp) - sum(r), count(*) FROM x JOIN l USING (s) GROUP BY ALL""").fetchall()
PA = d.sql("SELECT s, bid, count(*) FROM p WHERE cd IN (2,3,14,15,16,17,18,19,20,21,22,23) GROUP BY ALL").fetchall()
pickle.dump(dict(LW=LW, BR=BR, DP=DP, PA=PA), open(os.path.join(INPUTS, 'bat_raw.pkl'), 'wb')); print('done', T())
