# zfld_build.py — READ-ONLY. Zone-based range (1986+ seasons with Retrosheet hit locations): for each ball in play in zone z with
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
#  type t, expected outs for position q = league P(out | season, z, t) x q's share of those outs; credit = actual - expected.
d.execute(r"""CREATE TEMP TABLE zb AS SELECT s, f2, f3, f4, f5, f6, f7, f8, f9, bty,
   nullif(regexp_extract(tx, '/([GLFP]|BG|BP|BL|FL)?([1-9]{1,2}[LMDSXF]{0,2})(/|\.|$|\+|-)', 2), '') zn,
   CASE WHEN cd IN (2,19) THEN regexp_extract(tx, '^([1-9])', 1) END outby
   FROM pf WHERE cd IN (2,18,19,20,21,22) AND NOT tx LIKE '99%'""")
d.execute("DELETE FROM zb WHERE zn IS NULL")
d.execute("""CREATE TEMP TABLE zr AS SELECT s, zn, bty, count(*) n, count(outby) o,
   """ + ', '.join(f"sum((outby = '{q}')::int) o{q}" for q in range(1, 10)) + " FROM zb GROUP BY 1, 2, 3")
cols = ', '.join(f"sum(CASE WHEN zb.f{q} IS NOT NULL THEN (zb.outby = '{q}')::int - zr.o{q}::double / zr.n END) FILTER (WHERE zb.f{q} = fid) r{q}" for q in range(2, 10))
res = {}
for q in range(2, 10):
    rows = d.sql(f"""SELECT zb.s, zb.f{q}, sum((coalesce(zb.outby, '') = '{q}')::int - zr.o{q}::double / zr.n), sum(zr.o{q}::double / zr.n), count(*)
                     FROM zb JOIN zr USING (s, zn, bty) WHERE zb.f{q} IS NOT NULL GROUP BY 1, 2""").fetchall()
    for s_, fid, r_, e_, n_ in rows: res[(s_, fid, q)] = (r_, e_, n_)
    print('pos', q, len(rows), T())
pickle.dump(res, open(os.path.join(INPUTS, 'zfld_raw.pkl'), 'wb'))
print(d.sql("SELECT s, count(*) FROM zb GROUP BY 1 ORDER BY 1").fetchall()[::3])
