# roe_build.py — READ-ONLY on Retrosheet duckdb. Times each batter reached on an error (EVENT_CD 18), regular season.
# (Saved 2026-11-13 from the inline command that first wrote roe.pkl on 2026-11-11, so every v1 input has a builder.)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
SEASON = int(_os.environ.get('WAR_SEASON', '2026'))                                                   # the balldontlie season
TAG = f'bdl{SEASON % 100:02d}'                                                                      # its input-file prefix
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser(f'~/baseballstats-tools/data/bdl{SEASON}'))   # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import duckdb, os, pickle
d = duckdb.connect(RETRO, read_only=True)
roe = d.sql("SELECT SEASON, BAT_ID, count(*) FROM plays WHERE GAME_TYPE='R' AND EVENT_CD=18 GROUP BY 1,2").fetchall()
out = os.path.join(INPUTS, 'roe.pkl')
pickle.dump(roe, open(out, 'wb')); print(len(roe))
