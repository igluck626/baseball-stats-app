# tz_build.py — READ-ONLY on Retrosheet duckdb. Total-Zone-style plays made vs chances, per (season, fielder, position),
# following Sean Smith's published description (statspeak 2007; SABR BRJ 2010):
#   * a chance = a ball in play the fielder is responsible for: the outs he fields first, the errors he makes, and the
#     HITS charged to him — infielders only on ground balls, outfielders on line drives and fly balls (rated separately)
#   * ground-ball hits: by hit location when Retrosheet has it (G56 → half 3B / half SS, G6 → SS ...), otherwise
#     single to LF → ½ 3B ½ SS, to CF → ½ 2B ½ SS, to RF → ½ 2B ½ 1B; ground-ball extra-base hits → 3B (LF) / 1B (RF)
#   * balls with no batted-ball type (most seasons before 1988): each hit is split grounder/air by the batter's own share
#     of infield outs that season, scaled so the league average matches the typed seasons (Smith: hits "apportioned using
#     the batter's out distribution"); untyped outs: assisted infield outs = grounders, unassisted = air (3U = 0.6 grounder)
#   * expected plays = chances × league out-rate for that (season, position, batter hand, type)
# Output tz_raw.pkl: rows (season, retro fielder id, fielding team, pos, type, made, chances, expected)
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
d.execute("""CREATE TEMP TABLE pl AS SELECT SEASON s, GAME_ID g, EVENT_ID e, PIT_ID pit, BAT_ID bid,
   CASE WHEN BAT_HOME_ID=1 THEN AWAY_TEAM_ID ELSE HOME_TEAM_ID END ft, CASE WHEN BAT_HOME_ID=1 THEN HOME_TEAM_ID ELSE AWAY_TEAM_ID END bt_team,
   coalesce(BAT_HAND_CD,'?') h, EVENT_CD cd, EVENT_TX tx, BAT_FLD_CD bfc, HOME_TEAM_ID ht
   FROM r.plays WHERE GAME_TYPE='R'""")
d.execute("CREATE TEMP TABLE pos AS SELECT g, bt_team team, bfc p, e, bid pid FROM pl WHERE bfc BETWEEN 2 AND 9")
sel = ', '.join(f'coalesce(b{p}.pid, a{p}.pid) f{p}' for p in range(3, 10))
joins = ' '.join(f"ASOF LEFT JOIN (SELECT * FROM pos WHERE p={p}) b{p} ON b{p}.g=x.g AND b{p}.team=x.ft AND b{p}.e <= x.e "
                 f"ASOF LEFT JOIN (SELECT * FROM pos WHERE p={p}) a{p} ON a{p}.g=x.g AND a{p}.team=x.ft AND a{p}.e >= x.e" for p in range(3, 10))
# z = first fielder (outs/errors) or the fielder who picked up the hit; loc = Retrosheet hit-location digits if any
d.execute(f"""CREATE TEMP TABLE b AS SELECT x.s, x.g, x.e, x.ft, x.bid, x.h, x.cd, x.tx, {sel},
   CASE WHEN x.cd IN (2,19) THEN regexp_extract(x.tx, '^([1-9])', 1) WHEN x.cd = 18 THEN regexp_extract(x.tx, '^E([1-9])', 1)
        ELSE nullif(regexp_extract(x.tx, '^[SDT]([1-9])', 1), '') END z,
   regexp_extract(x.tx, '/[GLFP]?([1-9]{{1,2}})', 1) loc,
   CASE WHEN regexp_matches(x.tx, '/(G|BG|GDP)([^A-Z]|$)|/G[1-9]') THEN 'G' WHEN regexp_matches(x.tx, '/(L|BL)([^A-Z]|$)|/L[1-9]') THEN 'L'
        WHEN regexp_matches(x.tx, '/(F|FL)([^A-Z]|$)|/F[1-9]') THEN 'F' WHEN regexp_matches(x.tx, '/(P|BP|IF)([^A-Z]|$)|/P[1-9]') THEN 'P' ELSE '?' END bty,
   (x.cd IN (2,19) AND length(regexp_extract(x.tx, '^([1-9]+)', 1)) >= 2) assisted
   FROM pl x {joins} WHERE x.cd IN (2,18,19,20,21,22) AND NOT x.tx LIKE '99%' AND x.tx NOT LIKE 'K%'""")
print('balls in play', d.sql('SELECT count(*) FROM b').fetchone(), T())
# grounder probability for untyped balls: batter's infield-out share that season vs league, calibrated on typed seasons
d.execute("""CREATE TEMP TABLE bshare AS WITH o AS (SELECT s, bid, sum((z IN ('1','3','4','5','6') AND assisted)::int) gi, count(*) n
               FROM b WHERE cd IN (2,19) AND z IS NOT NULL AND z<>'' GROUP BY 1,2),
   l AS (SELECT s, sum(gi)/sum(n) lg FROM o GROUP BY 1)
   SELECT o.s, bid, ((gi + 50*lg)/(n + 50)) / lg rel FROM o JOIN l USING (s)""")
# typed seasons: P(grounder | hit kind) league-wide, by hit kind = (cd, z)
d.execute("""CREATE TEMP TABLE pg AS SELECT cd, z, avg((bty='G')::int) p FROM b WHERE bty <> '?' AND cd IN (20,21,22) AND s BETWEEN 1988 AND 2025 GROUP BY 1,2""")
# expand every ball into (pos, type, weight, made)
d.execute("""CREATE TEMP TABLE ch AS
WITH t AS (SELECT b.*, coalesce(least(1.0, greatest(0.0, pg.p * coalesce(bs.rel, 1))), 0.5) pgr FROM b LEFT JOIN pg USING (cd, z) LEFT JOIN bshare bs USING (s, bid)),
-- outs and errors: the first fielder
oe AS (SELECT s, g, e, ft, h, TRY_CAST(z AS INT) pos,
          CASE WHEN bty <> '?' THEN (CASE WHEN bty = 'P' THEN 'F' ELSE bty END)
               WHEN TRY_CAST(z AS INT) >= 7 THEN 'F' WHEN assisted OR cd = 18 THEN 'G' WHEN z = '3' THEN 'G3' ELSE 'A' END ty,
          (cd IN (2,19))::int made, f3, f4, f5, f6, f7, f8, f9 FROM t WHERE cd IN (2,18,19) AND z BETWEEN '3' AND '9'),
oe2 AS (SELECT s, g, e, ft, h, pos, CASE WHEN ty = 'G3' THEN 'G' ELSE ty END ty, CASE WHEN ty = 'G3' THEN 0.6 ELSE 1.0 END w, made, f3, f4, f5, f6, f7, f8, f9 FROM oe
        WHERE NOT (pos <= 6 AND ty IN ('A', 'L', 'F')) AND NOT (pos >= 7 AND ty = 'G')),
-- hits: grounder share (typed: 0/1; untyped: pgr) to infielders, air share to the outfielder who fielded it
hg AS (SELECT t.*, CASE WHEN bty = 'G' THEN 1.0 WHEN bty IN ('L','F','P') THEN 0.0 ELSE pgr END gw FROM t WHERE cd IN (20,21,22) AND z IS NOT NULL),
gsplit AS (
  SELECT hg.*, sp.pos, sp.frac FROM hg, LATERAL (SELECT * FROM (VALUES
     (5, CASE WHEN z='5' AND (loc='' OR loc IS NULL) THEN 1.0 WHEN loc IN ('5','15','25') THEN 1.0 WHEN loc IN ('56','6','46') THEN (CASE WHEN loc='56' THEN 0.5 ELSE 0 END) WHEN loc IN ('3','34','4','1') THEN 0
              WHEN z='7' AND cd=20 THEN 0.5 WHEN z='7' THEN 1.0 ELSE 0 END),
     (6, CASE WHEN z='6' AND (loc='' OR loc IS NULL) THEN 1.0 WHEN loc='56' THEN 0.5 WHEN loc IN ('6','46') THEN (CASE WHEN loc='46' THEN 0.5 ELSE 1.0 END) WHEN loc IN ('5','3','34','4','1','15','25') THEN 0
              WHEN z='7' AND cd=20 THEN 0.5 WHEN z='8' THEN 0.5 ELSE 0 END),
     (4, CASE WHEN z='4' AND (loc='' OR loc IS NULL) THEN 1.0 WHEN loc='46' THEN 0.5 WHEN loc IN ('4') THEN 1.0 WHEN loc='34' THEN 0.5 WHEN loc IN ('5','56','6','3','1','15','25') THEN 0
              WHEN z='8' THEN 0.5 WHEN z='9' AND cd=20 THEN 0.5 ELSE 0 END),
     (3, CASE WHEN z='3' AND (loc='' OR loc IS NULL) THEN 1.0 WHEN loc IN ('3','13','23') THEN 1.0 WHEN loc='34' THEN 0.5 WHEN loc IN ('5','56','6','46','4','1','15','25') THEN 0
              WHEN z='9' AND cd=20 THEN 0.5 WHEN z='9' THEN 1.0 ELSE 0 END)) v(pos, frac)) sp WHERE sp.frac > 0),
hits AS (
  SELECT s, g, e, ft, h, pos, 'G' ty, gw * frac w, 0 made, f3, f4, f5, f6, f7, f8, f9 FROM gsplit WHERE gw > 0
  UNION ALL
  SELECT s, g, e, ft, h, TRY_CAST(z AS INT), CASE WHEN bty = 'L' THEN 'L' ELSE 'F' END, 1.0 - gw, 0, f3, f4, f5, f6, f7, f8, f9 FROM hg WHERE z BETWEEN '7' AND '9' AND gw < 1)
SELECT * FROM oe2 UNION ALL SELECT * FROM hits""")
print('chance rows', d.sql('SELECT count(*), sum(w) FROM ch').fetchone(), T())
# league out-rate per (season, pos, hand, type); air types L/F kept separate for outfielders; untyped-era outfield = 'F'
d.execute("""CREATE TEMP TABLE ch2 AS SELECT *, CASE pos WHEN 3 THEN f3 WHEN 4 THEN f4 WHEN 5 THEN f5 WHEN 6 THEN f6 WHEN 7 THEN f7 WHEN 8 THEN f8 WHEN 9 THEN f9 END fid FROM ch""")
d.execute("CREATE TEMP TABLE lr AS SELECT s, pos, h, ty, sum(made*w)/sum(w) r FROM ch2 GROUP BY ALL")
# park factor per (park = home team, position, type): VISITING fielders' plays made in that park vs their league expectation,
# pooled over the season ±2 years at the same home team, shrunk toward 1 with K expected plays (Smith: home/road park factors)
K = float(os.environ.get('TZK', '150')); PW = int(os.environ.get('TZPW', '2'))
d.execute("CREATE TEMP TABLE ch3 AS SELECT ch2.*, gm.ht, lr.r FROM ch2 JOIN (SELECT DISTINCT g, ht FROM pl) gm USING (g) JOIN lr USING (s, pos, h, ty)")
d.execute("""CREATE TEMP TABLE pk1 AS SELECT s, ht, pos, ty, sum(made*w) m, sum(w*r) x FROM ch3 WHERE ft <> ht GROUP BY ALL""")
d.execute(f"""CREATE TEMP TABLE pk AS SELECT a.s, a.ht, a.pos, a.ty, (sum(b.m) + {K}) / (sum(b.x) + {K}) pf
   FROM (SELECT DISTINCT s, ht, pos, ty FROM pk1) a JOIN pk1 b ON b.ht = a.ht AND b.pos = a.pos AND b.ty = a.ty AND b.s BETWEEN a.s - {PW} AND a.s + {PW} GROUP BY ALL""")
print('park factors', d.sql("SELECT count(*), min(pf), max(pf) FROM pk").fetchone(), T())
rows = d.sql("""SELECT s, fid, ft, pos, ty, sum(made*w), sum(w), sum(w*r), sum(w*r*coalesce(pf, 1)) FROM ch3 LEFT JOIN pk USING (s, ht, pos, ty)
                  WHERE fid IS NOT NULL GROUP BY ALL""").fetchall()
print('Fenway LF fly-ball factor 1963-68:', d.sql("SELECT s, round(pf,3) FROM pk WHERE ht='BOS' AND pos=7 AND ty='F' AND s BETWEEN 1963 AND 1968 ORDER BY s").fetchall())
pickle.dump(rows, open(os.path.join(INPUTS, 'tz_raw.pkl'), 'wb'))
print('rows', len(rows), T())
