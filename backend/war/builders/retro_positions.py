# retro_positions.py — READ-ONLY on Retrosheet duckdb. Plate appearances by fielding position (BAT_FLD_CD: 1–9 fielding,
# 10 designated hitter) per (Retrosheet batter id, season). compute.py uses them to split outfield innings into LF/CF/RF and
# to find time at DH. Output posPA.pkl {(retro id, season): {position code: plate appearances}}.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
import os, pickle
from collections import defaultdict
import duckdb
d = duckdb.connect(RETRO, read_only=True)
POSPA = defaultdict(dict)
for bid, s, f, n_ in d.sql("SELECT BAT_ID, SEASON, BAT_FLD_CD, count(*) FROM plays WHERE GAME_TYPE='R' OR GAME_TYPE IS NULL GROUP BY 1,2,3").fetchall():
    POSPA[(bid, s)][f] = n_
pickle.dump(dict(POSPA), open(os.path.join(INPUTS, 'posPA.pkl'), 'wb')); print(len(POSPA))
