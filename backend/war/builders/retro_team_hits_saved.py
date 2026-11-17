# tder_build.py — READ-ONLY on Retrosheet duckdb. Team hits saved on balls in play per (season, fielding team), park-adjusted:
# expected hits = Σ league hit rate on balls in play × park factor (visiting teams' hit rate in that park vs league, ±2 seasons,
# shrunk with K balls). Home runs, strikeouts, walks excluded. Output tder_raw.pkl {(season, team): (balls, hits, expected, expected_park)}
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
SEASON = int(_os.environ.get('WAR_SEASON', '2026'))                                                   # the balldontlie season
TAG = f'bdl{SEASON % 100:02d}'                                                                      # its input-file prefix
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser(f'~/baseballstats-tools/data/bdl{SEASON}'))   # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import duckdb, os, pickle
K = float(os.environ.get('DK', '2000'))
d = duckdb.connect(); d.execute("SET threads=8")
d.execute(f"ATTACH '{RETRO}' AS r (READ_ONLY)")
d.execute("""CREATE TEMP TABLE b AS SELECT SEASON s, HOME_TEAM_ID ht, CASE WHEN BAT_HOME_ID=1 THEN AWAY_TEAM_ID ELSE HOME_TEAM_ID END ft,
   (EVENT_CD IN (20,21,22))::int hit FROM r.plays WHERE GAME_TYPE='R' AND EVENT_CD IN (2,18,19,20,21,22) AND NOT EVENT_TX LIKE '99%'""")
d.execute("CREATE TEMP TABLE lg AS SELECT s, avg(hit) r FROM b GROUP BY 1")
d.execute("CREATE TEMP TABLE pk1 AS SELECT s, ht, sum(hit) h, count(*) n FROM b WHERE ft <> ht GROUP BY 1, 2")
d.execute(f"""CREATE TEMP TABLE pk AS SELECT a.s, a.ht, (sum(p.h) + {K} * avg(l.r)) / (sum(p.n * l.r) + {K} * avg(l.r)) pf
   FROM (SELECT DISTINCT s, ht FROM pk1) a JOIN pk1 p ON p.ht = a.ht AND p.s BETWEEN a.s - 2 AND a.s + 2 JOIN lg l ON l.s = p.s GROUP BY 1, 2""")
rows = d.sql("""SELECT b.s, ft, count(*), sum(hit), sum(lg.r), sum(lg.r * coalesce(pf, 1)) FROM b JOIN lg USING (s) LEFT JOIN pk USING (s, ht) GROUP BY 1, 2""").fetchall()
pickle.dump({(s, t): (n, h, e, ep) for s, t, n, h, e, ep in rows}, open(os.path.join(INPUTS, 'tder_raw.pkl'), 'wb'))
print(len(rows), 'BOS 1967 park factor', d.sql("SELECT round(pf,3) FROM pk WHERE s=1967 AND ht='BOS'").fetchone(), 'PIT 1960', d.sql("SELECT round(pf,3) FROM pk WHERE s=1960 AND ht='PIT'").fetchone())
