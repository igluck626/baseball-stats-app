"""A READ-ONLY Postgres connection for local runs of the WAR builders and the history calculation.

Uses DATABASE_URL when set, otherwise the path-only file ~/.baseballstats_dburl. The session is set read-only, so nothing
run through this connection can write.
"""
import os

import psycopg2


def conn():
    url = os.environ.get("DATABASE_URL") or open(os.path.expanduser("~/.baseballstats_dburl")).read().strip()
    c = psycopg2.connect(url)
    c.set_session(readonly=True)
    return c
