"""Writes coverage.json: the share of each season's games that Retrosheet play-by-play covers (from the Retrosheet DuckDB's
coverage table). compute.py uses it to turn play-by-play plate appearances into innings for seasons with partial coverage.
Run locally (needs the DuckDB): <python> -m war.builders.retro_coverage [--duckdb PATH] [--out DIR]"""
import argparse
import json
import os

import duckdb

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--duckdb', default=os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))
    ap.add_argument('--out', default=os.environ.get('WAR_INPUTS', os.path.expanduser('~/baseballstats-tools/scripts/war')))
    a = ap.parse_args()
    d = duckdb.connect(a.duckdb, read_only=True)
    cov = {int(s): pct / 100 for s, _, _, pct in d.sql('SELECT * FROM coverage').fetchall()}
    json.dump(cov, open(os.path.join(a.out, 'coverage.json'), 'w'), indent=0, sort_keys=True)
    # per league: games with play-by-play (by the home team's league, from Lahman) / games played (Lahman team games ÷ 2)
    import csv
    lah = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'lahman', 'Teams.csv')
    lg, played = {}, {}
    for r in csv.DictReader(open(lah, encoding='utf-8-sig')):
        y = int(r['yearID']); lg[(y, r['teamIDretro'])] = r['lgID']; played[(y, r['lgID'])] = played.get((y, r['lgID']), 0) + int(r['G']) / 2
    pbp = {}
    for s, ht, n in d.sql("SELECT SEASON, HOME_TEAM_ID, count(DISTINCT GAME_ID) FROM plays WHERE GAME_TYPE='R' OR GAME_TYPE IS NULL GROUP BY 1, 2").fetchall():
        L = lg.get((s, ht))
        if L: pbp[(s, L)] = pbp.get((s, L), 0) + n
    covl = {}
    for (s, L), n in pbp.items():
        if played.get((s, L)): covl.setdefault(str(s), {})[L] = min(1.0, n / played[(s, L)])
    json.dump(covl, open(os.path.join(a.out, 'coverage_league.json'), 'w'), indent=0, sort_keys=True)
    print(len(cov), 'seasons;', '1944', covl.get('1944'), '1914', covl.get('1914'))
