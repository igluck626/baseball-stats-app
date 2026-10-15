#!/usr/bin/env python3
"""`GET /news?team=` — the Athletics' news is found under the app's code.

⚠️ THE CHECK THAT MATTERS: news rows for the Athletics are stored as "OAK"
(news_service._TEAM_SLUGS) and the app asks for "ATH", its catalog's code
(BDLTeamIds.swift). Without the alias /news returned no articles for them and
Home hid the Team News section. "OAK" must keep working, other teams must be
untouched, and league-wide news must still carry every team.

Runs the real handler and the real SQL on an embedded local Postgres
(`pip install pgserver`); without it the check is reported SKIPPED.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_news_team_alias.py
"""
import contextlib
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
from sqlalchemy import create_engine, text                         # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
import main                                                        # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def session_factory(S):
    @contextlib.contextmanager
    def get_session():                 # as connection.get_session: commit on a clean exit
        s = S()
        try:
            yield s
            s.commit()
        finally:
            s.close()
    return get_session


try:
    import pgserver
except ImportError:
    pgserver = None

print("GET /news?team= on Postgres")
if pgserver is None:
    print("  [SKIPPED] pgserver not installed — the alias was NOT checked")
    sys.exit(0)

srv = pgserver.get_server(tempfile.mkdtemp(prefix="news_alias_"), cleanup_mode="stop")
eng = create_engine(srv.get_uri())
main.connection.get_session = session_factory(sessionmaker(bind=eng))
main.connection.db_available = lambda: True
main.news_service._ensure_images = lambda codes: None   # no feed fetch from a test
main.news_service.ensure_news_table()
now = datetime.datetime(2026, 10, 15, 12, tzinfo=datetime.timezone.utc)
with main.connection.get_session() as db:
    for i, (code, title) in enumerate([("OAK", "A's prospect shows off"), ("LAN", "Dodgers rotation set")]):
        db.execute(text(
            "INSERT INTO news_articles (source, source_name, team_code, title, summary, url, published_at, fetched_at) "
            "VALUES ('mlb', 'MLB.com', :c, :t, NULL, :u, :p, :p)"),
            {"c": code, "t": title, "u": f"https://example.invalid/{i}", "p": now - datetime.timedelta(hours=i)})


def titles(team):
    return [a["title"] for a in main.news(team=team, limit=20)["articles"]]


check("ATH (the app's code) finds the Athletics' articles", titles("ATH") == ["A's prospect shows off"], titles("ATH"))
check("OAK still finds them", titles("OAK") == ["A's prospect shows off"], titles("OAK"))
check("another team is untouched", titles("LAN") == ["Dodgers rotation set"], titles("LAN"))
check("league-wide carries both", sorted(titles(None)) == ["A's prospect shows off", "Dodgers rotation set"], titles(None))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
