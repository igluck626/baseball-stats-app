#!/usr/bin/env python3
"""Where the API's WAR comes from (api/war_source.py, approved 2026-11-18): the WAR_VERSION setting switches every
WAR read between the stored Baseball-Reference columns and our war_seasons table.

⚠️ THE CHECKS THAT MATTER:
  • setting OFF: every read is exactly the stored columns, as before (careers, leaderboards, /ask sums)
  • setting ON ('v1'): player seasons, careers, leader badges, season / all-time / career leaderboards, award voting
    and /ask sums all read war_seasons, with the missing-WAR rules:
      - a Negro Leagues season reads None and is left out of career totals
      - a hitting row with no plate appearances (a pitcher who never batted) reads 0.0
      - any other season with playing time and no war_seasons row is unknown: None, and so is its career total
  • WAR_off / WAR_def carry our offense / defense, which add up to WAR; WAA / runs_above_* are None under our WAR
  • served to one decimal, rounded the same way in Python and SQL, so badges and boards agree with the seasons shown
Synthetic rows on an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_war_source.py
"""
import contextlib
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
os.environ.pop("WAR_VERSION", None)
from sqlalchemy import create_engine, text                         # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
import data_service                                                # noqa: E402
import war_source                                                  # noqa: E402
from database import connection                                    # noqa: E402
from database.models import Base, Pitcher, PitcherSeason, Player, PlayerSeason, WarSeason   # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed", False, "pip install pgserver")
    sys.exit(1)

srv = pgserver.get_server(tempfile.mkdtemp(prefix="war_source_"), cleanup_mode="stop")
eng = create_engine(srv.get_uri())
Base.metadata.create_all(eng, tables=[Player.__table__, Pitcher.__table__, PlayerSeason.__table__,
                                      PitcherSeason.__table__, WarSeason.__table__])
NOW = datetime.datetime(2026, 11, 18)


def war(pid, y, role, w, o=None, d=None):
    return dict(player_id=pid, year=y, role=role, method_version="v1", war=w, offense=o, defense=d,
                data_tier="final", params_sha="x" * 64, run_id="test", computed_at=NOW)


with eng.begin() as c:
    for pid, name in ((1, "Hitter One"), (2, "Hitter Two"), (3, "Pitcher Three")):
        c.execute(Player.__table__.insert().values(player_id=pid, name=name))
    c.execute(Pitcher.__table__.insert().values(player_id=3, name="Pitcher Three"))
    for r in (dict(player_id=1, year=1930, team="NYA", league="AL", G=150, PA=600, AB=540, H=180, WAR=5.0, WAR_off=4.0, WAR_def=0.5, WAA=3.0),
              dict(player_id=1, year=1931, team="NYA", league="AL", G=140, PA=500, AB=450, H=140, WAR=4.0, WAR_off=3.5, WAR_def=0.2, WAA=2.0),
              dict(player_id=1, year=1932, team="HOM", league="NNL", G=60, PA=300, AB=270, H=90, WAR=2.0),
              dict(player_id=2, year=1930, team="BOS", league="AL", G=120, PA=400, AB=360, H=100, WAR=3.0),
              dict(player_id=3, year=1930, team="BOS", league="AL", G=35, PA=0, AB=0, H=0, WAR=0.0)):
        c.execute(PlayerSeason.__table__.insert().values(**r))
    c.execute(PitcherSeason.__table__.insert().values(player_id=3, year=1930, team="BOS", league="AL", G=35, GS=30, IP=200.0, WAR=7.0))
    for r in (war(1, 1930, "hit", 6.0, 5.0, 1.0), war(1, 1931, "hit", 3.46, 2.97, 0.49), war(3, 1930, "pit", 6.2)):
        c.execute(WarSeason.__table__.insert().values(**r))
S = sessionmaker(bind=eng)


@contextlib.contextmanager
def get_session():
    s = S()
    try:
        yield s
        s.commit()
    finally:
        s.close()


connection.get_session = get_session
connection.db_available = lambda: True
data_service._bio_dict = lambda *a, **k: {}
data_service._player_payload = lambda row, db, **k: {"player_id": row.player_id}
data_service._qualifier_thresholds = lambda db, year: (502, 162.0)
data_service._resolve_team_code = lambda team, league: team


def seasons_of(pid, role):
    with get_session() as db:
        model = PlayerSeason if role == "hit" else PitcherSeason
        rows = db.query(model).filter(model.player_id == pid).order_by(model.year).all()
        conv = data_service._db_row_to_season if role == "hit" else data_service._db_pitcher_row_to_season
        ss = [conv(r) for r in rows]
        war_source.apply_to_seasons(db, pid, ss, role)
    return ss


def board(stat_mode, is_batter=True, league=None, year=1930, stat="WAR"):
    if stat_mode == "season":
        r = data_service._leaderboard_season(stat, year, is_batter, 10, league, None)
    elif stat_mode == "all_time":
        r = data_service._leaderboard_all_time(stat, is_batter, 10, league, None)
    else:
        r = data_service._leaderboard_career(stat=stat, is_batter=is_batter, limit=10, league=league, team=None)
    return [(x["player"]["player_id"], round(x["value"], 2) if x["value"] is not None else None) for x in r["leaders"]]


def ask_sum(pid, table):
    if war_source.active_version():
        val, unk = war_source.ask_sql(table)
    else:
        val, unk = '"WAR"', "FALSE"
    with eng.connect() as c:
        return c.execute(text(f"SELECT COALESCE(SUM({val}), 0), {unk} FROM {table} WHERE player_id = :p"),
                         {"p": pid, "war_v": war_source.active_version()}).fetchone()


print("setting OFF: the stored columns, unchanged")
check("no active version", war_source.active_version() is None)
s1 = seasons_of(1, "hit")
check("seasons read the stored WAR", [s["WAR"] for s in s1] == [5.0, 4.0, 2.0], [s["WAR"] for s in s1])
check("career WAR is the stored sum", data_service._batting_career_totals(s1)["WAR"] == 11.0)
check("career totals say the stored figures (no source)", data_service._batting_career_totals(s1)["WAR_source"] is None)
check("season board ranks stored WAR", board("season") == [(1, 5.0), (2, 3.0), (3, 0.0)], board("season"))
check("oWAR board ranks the stored offense column when the setting is off", board("season", stat="oWAR") == [(1, 4.0)], board("season", stat="oWAR"))
check("career board sums stored WAR", board("career")[:2] == [(1, 11.0), (2, 3.0)], board("career"))
with get_session() as db:
    bat = db.query(PlayerSeason).filter_by(player_id=1, year=1930).one()
    check("award voting reads the stored WAR", war_source.row_war(db, bat, "hit") == 5.0)

print("setting ON (v1): our WAR everywhere")
os.environ["WAR_VERSION"] = "v1"
s1 = seasons_of(1, "hit")
check("seasons read our WAR; Negro Leagues season is None", [s["WAR"] for s in s1] == [6.0, 3.5, None], [s["WAR"] for s in s1])
check("WAR_off / WAR_def are our offense / defense", (s1[0]["WAR_off"], s1[0]["WAR_def"]) == (5.0, 1.0))
check("Baseball-Reference-only fields are None", s1[0]["WAA"] is None)
check("each season says which WAR it is", all(s["WAR_source"] == "v1" for s in s1))
check("served to one decimal; defense = WAR − offense so the parts add up (3.46 / 2.97 → 3.5 = 3.0 + 0.5)",
      (s1[1]["WAR"], s1[1]["WAR_off"], s1[1]["WAR_def"]) == (3.5, 3.0, 0.5), (s1[1]["WAR"], s1[1]["WAR_off"], s1[1]["WAR_def"]))
ct = data_service._batting_career_totals(s1)
check("career WAR leaves the hidden season out: 6.0 + 3.5", ct["WAR"] == 9.5, ct["WAR"])
check("career totals say which WAR they are", ct["WAR_source"] == "v1", ct.get("WAR_source"))
check("career offense + defense add up to career WAR", ct["WAR_off"] + ct["WAR_def"] == ct["WAR"], (ct["WAR_off"], ct["WAR_def"]))
s2 = seasons_of(2, "hit")
check("a season with playing time and no figure is unknown", s2[0]["WAR"] is None)
check("…and so is the career total over it", data_service._batting_career_totals(s2)["WAR"] is None)
s3 = seasons_of(3, "hit")
check("a career of only Negro Leagues seasons has no WAR (None, as the SQL sum gives)",
      war_source.career_total([{"year": 1932, "league": "NNL", "WAR": None}], "WAR") is None)
check("a pitcher who never batted reads batting WAR 0.0", s3[0]["WAR"] == 0.0, s3[0]["WAR"])
p3 = seasons_of(3, "pit")
check("pitching WAR reads ours", p3[0]["WAR"] == 6.2 and data_service._pitching_career_totals(p3)["WAR"] == 6.2)
check("season board ranks our WAR (unrated season left off)", board("season") == [(1, 6.0)], board("season"))
check("all-time board ranks our WAR", board("all_time") == [(1, 6.0), (1, 3.5)], board("all_time"))
check("career board: hidden season left out, unknown career dropped, never-batted 0.0",
      board("career") == [(1, 9.5), (3, 0.0)], board("career"))
check("oWAR / dWAR season boards: offense, and defense = WAR − offense", (board("season", stat="oWAR"), board("season", stat="dWAR"))
      == ([(1, 5.0)], [(1, 1.0)]), (board("season", stat="oWAR"), board("season", stat="dWAR")))
check("oWAR all-time board uses the rounded offense (2.97 → 3.0)", board("all_time", stat="oWAR") == [(1, 5.0), (1, 3.0)], board("all_time", stat="oWAR"))
check("oWAR / dWAR career boards add the seasons shown, hidden left out (5.0+3.0, 1.0+0.5)",
      (board("career", stat="oWAR"), board("career", stat="dWAR")) == ([(1, 8.0), (3, 0.0)], [(1, 1.5), (3, 0.0)]),
      (board("career", stat="oWAR"), board("career", stat="dWAR")))
check("pitching boards read ours", board("season", is_batter=False) == [(3, 6.2)], board("season", is_batter=False))
with get_session() as db:
    ex = data_service._leader_extremes(db, PlayerSeason, 1930, "AL", data_service._LEADER_BATTING_STATS, 502, 162.0)
    check("leader badge extreme is our WAR", ex["WAR"] == 6.0, ex["WAR"])
    check("other badge extremes are untouched by the join", ex["H"] == 180, ex["H"])
    bat = db.query(PlayerSeason).filter_by(player_id=1, year=1930).one()
    check("award voting reads our WAR", war_source.row_war(db, bat, "hit") == 6.0)
v, unk = ask_sum(1, "player_seasons")
check("/ask career sum: hidden season left out, known", (round(float(v), 2), unk) == (9.5, False), (v, unk))
v, unk = ask_sum(2, "player_seasons")
check("/ask career sum over an unrated season is unknown", unk is True, (v, unk))
with eng.connect() as c_:                               # all-hidden scope: unknown, never 0.0
    val_, unk_ = war_source.ask_sql("player_seasons")
    r_ = c_.execute(text(f"SELECT COALESCE(SUM({val_}), 0), {unk_} FROM player_seasons WHERE player_id = 1 AND year = 1932"),
                    {"war_v": "v1"}).fetchone()
check("/ask over only Negro Leagues seasons is unknown, not 0.0", r_[1] is True, r_)
check("no -0.0: a small negative rounds to 0.0", str(war_source.r1(-0.04)) == "0.0", war_source.r1(-0.04))
check("pitchers' stored Baseball-Reference WAR_def is not served under our WAR",
      seasons_of(3, "pit")[0].get("WAR_def") is None)
os.environ["WAR_VERSION"] = "V1"
war_source._known.update(at=0.0, versions=None)
check("a WAR_VERSION the table doesn't hold falls back to the stored columns", war_source.active_version() is None)
check("…so pages show the stored WAR, never blanks", [s["WAR"] for s in seasons_of(1, "hit")] == [5.0, 4.0, 2.0])
os.environ["WAR_VERSION"] = "v1"
v, unk = ask_sum(3, "pitcher_seasons")
check("/ask pitching sum reads ours", round(float(v), 2) == 6.2 and unk is False, (v, unk))

print("setting OFF again: back to the stored columns")
os.environ["WAR_VERSION"] = ""
check("seasons read the stored WAR again", [s["WAR"] for s in seasons_of(1, "hit")] == [5.0, 4.0, 2.0])
check("season board ranks stored WAR again", board("season") == [(1, 5.0), (2, 3.0), (3, 0.0)], board("season"))

print(f"\n{sum(results)}/{len(results)} passed")
srv.cleanup()
sys.exit(0 if all(results) else 1)
