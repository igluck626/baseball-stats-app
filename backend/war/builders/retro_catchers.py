# cat_build.py — READ-ONLY. Catcher running-game & blocking features per (season, catcher), each vs expected given the pitcher:
#  att (steal attempts per opportunity), cs (caught per attempt), wp, pb (per PA with runners on), po (pickoffs)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser('~/baseballstats-tools/data/bdl2026'))      # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import duckdb, os, pickle
d = duckdb.connect(); d.execute("SET threads=8")
d.execute(f"ATTACH '{RETRO}' AS r (READ_ONLY)")
d.execute("""CREATE TEMP TABLE pl AS SELECT SEASON s, GAME_ID g, EVENT_ID e, EVENT_CD cd, PIT_ID pit, BAT_ID bid, BAT_FLD_CD bfc,
   CASE WHEN BAT_HOME_ID=1 THEN AWAY_TEAM_ID ELSE HOME_TEAM_ID END ft, CASE WHEN BAT_HOME_ID=1 THEN HOME_TEAM_ID ELSE AWAY_TEAM_ID END bt_team,
   (BASE1_RUN_ID IS NOT NULL AND BASE2_RUN_ID IS NULL) OR (BASE2_RUN_ID IS NOT NULL AND BASE3_RUN_ID IS NULL) opp,
   BASE1_RUN_ID IS NOT NULL OR BASE2_RUN_ID IS NOT NULL OR BASE3_RUN_ID IS NOT NULL ron,
   (coalesce(RUN1_SB_FL,false)::int + coalesce(RUN2_SB_FL,false)::int + coalesce(RUN3_SB_FL,false)::int) sb,
   (coalesce(RUN1_CS_FL,false)::int + coalesce(RUN2_CS_FL,false)::int + coalesce(RUN3_CS_FL,false)::int) cs,
   coalesce(WP_FL,false)::int wp, (EVENT_CD = 10)::int pb, (EVENT_CD = 8)::int po
   FROM r.plays WHERE GAME_TYPE='R'""")
d.execute("CREATE TEMP TABLE pos AS SELECT g, bt_team team, e, bid pid FROM pl WHERE bfc = 2")
d.execute("""CREATE TEMP TABLE t AS SELECT x.*, coalesce(b.pid, a.pid) c FROM pl x
   ASOF LEFT JOIN pos b ON b.g = x.g AND b.team = x.ft AND b.e <= x.e ASOF LEFT JOIN pos a ON a.g = x.g AND a.team = x.ft AND a.e >= x.e WHERE x.ron""")
# expected per pitcher-season (his rate with all catchers, shrunk to league with 200 opportunities), then sum (actual - expected) per catcher
q = """WITH lg AS (SELECT s, sum(sb+cs)/sum(opp::int) la, sum(cs)/nullif(sum(sb+cs),0) lc, avg(wp) lw, avg(pb) lp, avg(po) lo FROM t GROUP BY 1),
 pr AS (SELECT t.s, pit, (sum(sb+cs) + 200*la)/(sum(opp::int)+200) pa_, (sum(cs) + 30*lc)/(sum(sb+cs)+30) pc, (sum(wp)+200*lw)/(count(*)+200) pw,
        (sum(po)+200*lo)/(count(*)+200) ppo FROM t JOIN lg USING (s) GROUP BY t.s, pit, la, lc, lw, lo)
 SELECT t.s, c, sum(opp::int), sum(sb+cs) - sum(opp::int * pa_), sum(cs) - sum((sb+cs) * pc), sum(wp) - sum(pw), sum(pb) - count(*) * any_value(lp),
        sum(po) - sum(ppo), count(*)
 FROM t JOIN pr USING (s, pit) JOIN lg USING (s) WHERE c IS NOT NULL GROUP BY 1, 2"""
res = d.sql(q).fetchall()
pickle.dump({(r[0], r[1]): r[2:] for r in res}, open(os.path.join(INPUTS, 'cat_raw.pkl'), 'wb')); print(len(res))
