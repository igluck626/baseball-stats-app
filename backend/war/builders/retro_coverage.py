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
    print(len(cov), 'seasons')
