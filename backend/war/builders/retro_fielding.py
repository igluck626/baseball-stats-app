# fld2_build.py — READ-ONLY on Retrosheet duckdb. Per (season, fielder, position) fielding components:
#  range (3–9): plays made vs expected; expected = league rate by batter hand × batted-ball type, and for untyped balls
#               × the pitcher's infield/outfield tendency (his own season, shrunk toward league)
#  dp (3–6): double plays he took part in vs expected, on ground-ball-out chances with a runner on first, < 2 outs
#  arm (7–9): extra bases taken / runners thrown out on singles and doubles to his zone vs league
#  catcher (2): caught stealing vs league rate on steal attempts; passed balls vs league rate per play
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
d = duckdb.connect(); d.execute("SET threads=8")
d.execute(f"ATTACH '{RETRO}' AS r (READ_ONLY)")
d.execute("""CREATE TEMP TABLE pl AS SELECT SEASON s, GAME_ID g, EVENT_ID e,
   CASE WHEN BAT_HOME_ID=1 THEN AWAY_TEAM_ID ELSE HOME_TEAM_ID END ft, CASE WHEN BAT_HOME_ID=1 THEN HOME_TEAM_ID ELSE AWAY_TEAM_ID END bt_team,
   PIT_ID pit, coalesce(BAT_HAND_CD,'?') h, EVENT_CD cd, EVENT_TX tx, OUTS_CT o, BAT_FLD_CD bfc, BAT_ID bid,
   BASE1_RUN_ID IS NOT NULL r1, BASE2_RUN_ID IS NOT NULL r2, RUN1_DEST_ID d1, RUN2_DEST_ID d2, DP_FL dp,
   (coalesce(RUN1_SB_FL,false)::int + coalesce(RUN2_SB_FL,false)::int + coalesce(RUN3_SB_FL,false)::int) sb,
   (coalesce(RUN1_CS_FL,false)::int + coalesce(RUN2_CS_FL,false)::int + coalesce(RUN3_CS_FL,false)::int) cs
   FROM r.plays WHERE GAME_TYPE='R'""")
print('plays', d.sql('SELECT count(*) FROM pl').fetchone(), T())
d.execute("CREATE TEMP TABLE pos AS SELECT g, bt_team team, bfc p, e, bid pid FROM pl WHERE bfc BETWEEN 2 AND 9")
sel = ', '.join(f'coalesce(b{p}.pid, a{p}.pid) f{p}' for p in range(2, 10))
joins = ' '.join(f"ASOF LEFT JOIN (SELECT * FROM pos WHERE p={p}) b{p} ON b{p}.g=x.g AND b{p}.team=x.ft AND b{p}.e <= x.e "
                 f"ASOF LEFT JOIN (SELECT * FROM pos WHERE p={p}) a{p} ON a{p}.g=x.g AND a{p}.team=x.ft AND a{p}.e >= x.e" for p in range(2, 10))
d.execute(f"""CREATE TEMP TABLE pf AS SELECT x.*, {sel},
   CASE WHEN tx LIKE '99%' THEN NULL WHEN cd IN (2,19) THEN regexp_extract(tx, '^([1-9])', 1)
        WHEN cd = 18 THEN regexp_extract(tx, '^E([1-9])', 1)
        WHEN cd IN (20,21,22) THEN coalesce(nullif(regexp_extract(tx, '^[SDT]([1-9])', 1), ''), nullif(regexp_extract(tx, '/[GLFP]?([1-9])', 1), '')) END z,
   CASE WHEN regexp_matches(tx, '/(G|BG|GDP)([^A-Z]|$)|/G[1-9]') THEN 'G' WHEN regexp_matches(tx, '/(L|BL)([^A-Z]|$)|/L[1-9]') THEN 'L'
        WHEN regexp_matches(tx, '/(F|P|BP|FL|IF)([^A-Z]|$)|/[FP][1-9]') THEN 'F' ELSE '?' END bty
   FROM pl x {joins}""")
print('fielders joined', T())
d.execute("CREATE TEMP TABLE bip AS SELECT *, z IN ('3','4','5','6') AND cd IN (2,19) if_out, z IN ('7','8','9') AND cd IN (2,19) of_out FROM pf WHERE cd IN (2,18,19,20,21,22) AND NOT tx LIKE '99%'")
# pitcher tendency: share of his fielded outs made by infielders vs league, shrunk with 100 outs of league
d.execute("""CREATE TEMP TABLE ptend AS WITH a AS (SELECT s, pit, sum(if_out::int) i, sum(of_out::int) f FROM bip GROUP BY 1,2),
   l AS (SELECT s, sum(i)/(sum(i)+sum(f)) li FROM a GROUP BY 1)
   SELECT a.s, pit, ((i + 100*li)/(i+f+100))/li fi, ((f + 100*(1-li))/(i+f+100))/(1-li) fo FROM a JOIN l USING (s)""")
out = {}
for p in range(3, 10):
    grp = 'fi' if p <= 6 else 'fo'
    q = f"""WITH b AS (SELECT bip.*, CASE WHEN bty = '?' THEN coalesce(t.{grp}, 1) ELSE 1 END adj, (z = '{p}' AND cd IN (2,19))::int made
                       FROM bip LEFT JOIN ptend t USING (s, pit) WHERE f{p} IS NOT NULL),
       rt AS (SELECT s, h, bty, sum(made) / sum(adj) r FROM b GROUP BY ALL)
       SELECT s, f{p}, ft, sum(made), sum(adj * r), count(*) FROM b JOIN rt USING (s, h, bty) GROUP BY ALL"""
    out[('range', p)] = d.sql(q).fetchall(); print('range', p, T())
for p in range(3, 7):
    q = f"""WITH b AS (SELECT *, (dp AND regexp_matches(split_part(tx, '/', 1), '{p}'))::int inv FROM pf
                       WHERE r1 AND o < 2 AND cd IN (2,19) AND z IN ('1','3','4','5','6') AND bty IN ('G','?') AND f{p} IS NOT NULL),
       rt AS (SELECT s, avg(inv) r FROM b GROUP BY 1)
       SELECT s, f{p}, ft, sum(inv), sum(r), count(*) FROM b JOIN rt USING (s) GROUP BY ALL"""
    out[('dp', p)] = d.sql(q).fetchall(); print('dp', p, T())
for p in range(7, 10):
    # runner on 1st (single: extra = reaches 3rd/home; double: extra = scores) and on 2nd (single: extra = scores); kill = out (dest 0)
    q = f"""WITH c AS (
        SELECT s, f{p} f, ft, cd, 1 base, o, (CASE WHEN cd=20 THEN d1>=3 ELSE d1>=4 END)::int x, (d1=0)::int k FROM pf WHERE cd IN (20,21) AND z='{p}' AND r1 AND f{p} IS NOT NULL
        UNION ALL SELECT s, f{p}, ft, cd, 2, o, (d2>=4)::int, (d2=0)::int FROM pf WHERE cd=20 AND z='{p}' AND r2 AND f{p} IS NOT NULL),
       rt AS (SELECT s, cd, base, o, avg(x) rx, avg(k) rk FROM c GROUP BY ALL)
       SELECT s, f, ft, sum(x), sum(rx), sum(k), sum(rk), count(*) FROM c JOIN rt USING (s, cd, base, o) GROUP BY ALL"""
    out[('arm', p)] = d.sql(q).fetchall(); print('arm', p, T())
q = """WITH c AS (SELECT * FROM pf WHERE f2 IS NOT NULL), l AS (SELECT s, sum(cs)/nullif(sum(sb+cs),0) csr, avg((cd=10)::int) pbr FROM c GROUP BY 1)
       SELECT s, f2, ft, sum(cs), sum((sb+cs)*csr), sum((cd=10)::int), sum(pbr), count(*), sum(sb+cs) FROM c JOIN l USING (s) GROUP BY ALL"""
out[('catch', 2)] = d.sql(q).fetchall(); print('catch', T())
pickle.dump(out, open(os.path.join(INPUTS, 'fld2_raw.pkl'), 'wb')); print('done', T())
