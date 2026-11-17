# retro_leverage.py — READ-ONLY on Retrosheet duckdb. How much pressure each reliever's entrances carried: the leverage index
# (LI) of the game state when he came in, averaged over his relief appearances in a season ("gmLI").
#   win expectancy WE(state) = share of games the home team won from that state (inning capped at 9, top/bottom, outs,
#                              runners, home lead capped at ±6), measured from Retrosheet itself
#   LI(state) = average |change in WE| over the plays from that state ÷ the average over all plays (1 = a typical moment)
#   a relief appearance = a pitcher's first play in a game when he is not his team's first pitcher of that game
# Output li_raw.pkl {(retro pitcher id, season): (relief appearances, sum of entry LI)}, plus 'TABLE': {state: LI}
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
import os, pickle, time
import duckdb
t0 = time.time(); T = lambda: f'{time.time() - t0:.0f}s'
d = duckdb.connect(); d.execute("SET threads=8")
d.execute(f"ATTACH '{RETRO}' AS r (READ_ONLY)")
d.execute("""CREATE TEMP TABLE p AS SELECT SEASON s, GAME_ID g, EVENT_ID e, INN_CT inn, BAT_HOME_ID bh, OUTS_CT o, PIT_ID pit,
   (BASE1_RUN_ID IS NOT NULL)::int + 2*(BASE2_RUN_ID IS NOT NULL)::int + 4*(BASE3_RUN_ID IS NOT NULL)::int b,
   HOME_SCORE_CT hs, AWAY_SCORE_CT as_,
   (BAT_DEST_ID>=4)::int + (RUN1_DEST_ID>=4)::int + (RUN2_DEST_ID>=4)::int + (RUN3_DEST_ID>=4)::int runs
   FROM r.plays WHERE GAME_TYPE='R' AND NOT EVENT_TX LIKE 'NP%'""")
# final score per game = the last play's score plus the runs scored on it
d.execute("""CREATE TEMP TABLE fin AS SELECT g, arg_max(hs + CASE WHEN bh=1 THEN runs ELSE 0 END, e) fh, arg_max(as_ + CASE WHEN bh=0 THEN runs ELSE 0 END, e) fa FROM p GROUP BY 1""")
d.execute("""CREATE TEMP TABLE st AS SELECT p.*, (f.fh > f.fa)::int hw, least(inn, 9) i9, greatest(-6, least(6, hs - as_)) dif FROM p JOIN fin f USING (g) WHERE f.fh <> f.fa""")
d.execute("CREATE TEMP TABLE we AS SELECT i9, bh, o, b, dif, avg(hw) w, count(*) n FROM st GROUP BY ALL")
d.execute("""CREATE TEMP TABLE st2 AS SELECT st.*, we.w w0, coalesce(lead(we.w) OVER (PARTITION BY g ORDER BY e), any_value(hw) OVER (PARTITION BY g)) w1
   FROM st JOIN we USING (i9, bh, o, b, dif)""")
avg_all = d.sql("SELECT avg(abs(w1 - w0)) FROM st2").fetchone()[0]
d.execute(f"CREATE TEMP TABLE li AS SELECT i9, bh, o, b, dif, avg(abs(w1 - w0)) / {avg_all} li FROM st2 GROUP BY ALL")
print('states', d.sql('SELECT count(*) FROM li').fetchone(), 'mean |ΔWE|', round(avg_all, 4), T())
# relief entrances: the first play of each pitcher in a game, when an earlier pitcher of his team already pitched that game
d.execute("""CREATE TEMP TABLE firsts AS SELECT g, s, bh, pit, min(e) e FROM st GROUP BY ALL""")
d.execute("""CREATE TEMP TABLE ent AS SELECT f.*, row_number() OVER (PARTITION BY g, bh ORDER BY e) k FROM firsts f""")
rows = d.sql("""SELECT ent.s, ent.pit, count(*), sum(li.li) FROM ent JOIN st ON st.g = ent.g AND st.e = ent.e
                JOIN li ON li.i9 = st.i9 AND li.bh = st.bh AND li.o = st.o AND li.b = st.b AND li.dif = st.dif
                WHERE ent.k > 1 GROUP BY 1, 2""").fetchall()
out = {(pit, s): (n, l) for s, pit, n, l in rows}
out['TABLE'] = {tuple(r[:5]): r[5] for r in d.sql('SELECT * FROM li').fetchall()}
pickle.dump(out, open(os.path.join(INPUTS, 'li_raw.pkl'), 'wb'))
print('pitcher-seasons with relief entrances', len(rows), T())
print('closer check, Rivera 2004:', [(k, round(v[1] / v[0], 2), v[0]) for k, v in out.items() if k != 'TABLE' and k[0] == 'rivem002' and k[1] in (2004, 2008)])
