#!/usr/bin/env python3
"""Zero-PA game-log admission, the 2026 BDL ingest cutover, and the Hot/Cold
windows that must not count the newly admitted games.

⚠️ THE DISTINCTION BEING PINNED. A batting line with no PA, AB or BB is two
different things, and only the stat row itself can tell them apart:
  • no pitching line  → a defensive sub or pinch runner. MLB's game log lists
    the game, so we keep it (Seager 06-30, Foscue 05-29).
  • a pitching line   → a pitcher's pitching-only game. MLB's game log leaves
    it out, so we drop it (Ohtani without the DH).
A pitching line with IP 0 is kept when the pitcher faced a batter (Milner
06-25: BF 3), and dropped when he faced nobody.

The fixtures are real BDL `/stats` rows, trimmed to the fields the parsers
read.

Standalone, no pytest — matches test_streak_classify.py.
Run: python3 backend/tests/test_zero_pa_gamelogs.py
"""
import ast
import contextlib
import datetime
import logging
import os
import sys
from typing import Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..")
DATA_SERVICE = os.path.join(BACKEND, "api", "data_service.py")
CRUD = os.path.join(BACKEND, "database", "crud.py")


def extract(path, names, ns):
    """Exec the named top-level functions / assignments from `path` into
    `ns`, rather than importing a module that pulls in the whole app.
    Postponed annotations let `int | None` signatures load on 3.9."""
    tree = ast.parse(open(path).read())
    body = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in names:
            body.append(n)
        elif (isinstance(n, ast.Assign) and len(n.targets) == 1
              and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names):
            body.append(n)
    found = {getattr(n, "name", None) or n.targets[0].id for n in body}
    missing = set(names) - found
    assert not missing, f"not found in {path}: {missing}"
    future = ast.parse("from __future__ import annotations").body
    exec(compile(ast.Module(body=future + body, type_ignores=[]), path, "exec"), ns)
    return ns


results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not ok else ""))


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
ds = extract(DATA_SERVICE, {
    "_to_int", "_ip_str_to_decimal", "_batted", "_resolve_side",
    "_bdl_pitching_hbp", "_parse_bdl_batting_gamelog", "_parse_bdl_pitching_gamelog",
    "_BDL_INGEST_CUTOVER", "_before_bdl_cutover", "save_bdl_gamelogs_for_date",
    "_bdl_game_ctx",
}, {"datetime": datetime, "Optional": Optional, "log": logging.getLogger("t"),
    "_MLB_LOCAL_TZ": ZoneInfo("America/New_York")})
bat = ds["_parse_bdl_batting_gamelog"]
pit = ds["_parse_bdl_pitching_gamelog"]


def ctx(home, away, date):
    return {"game_id": "1", "game_date": datetime.date.fromisoformat(date),
            "season": 2026, "home_team_name": home, "home_team_display_name": home,
            "away_team_name": away, "away_team_display_name": away,
            "home_team_abbr": "H", "away_team_abbr": "A",
            "home_runs": 3, "away_runs": 2}


print("Batting admission")
# Seager, BDL game 5059049, TEX @ CLE 06-30: no PA shipped at all, AB 0, BB 0,
# no pitching line — a defensive appearance.
seager = {"team_name": "Texas Rangers", "ip": None, "batters_faced": None,
          "plate_appearances": None, "at_bats": 0, "bb": 0, "hits": 0, "runs": 0}
r = bat(seager, ctx("Cleveland Guardians", "Texas Rangers", "2026-06-30"))
check("Seager 06-30 (defensive, PA null) kept", r is not None)
check("  ...and its null PA is stored as shipped, not invented",
      r is not None and r["PA"] is None and r["AB"] == 0, r)

# Foscue, BDL game 5058625, KC @ TEX 05-29 ET: PA 0 shipped explicitly.
foscue = {"team_name": "Texas Rangers", "ip": None, "batters_faced": None,
          "plate_appearances": 0, "at_bats": 0, "bb": 0, "hits": 0, "runs": 0,
          "stolen_bases": 0}
check("Foscue 05-29 (PA 0, no pitching line) kept",
      bat(foscue, ctx("Texas Rangers", "Kansas City Royals", "2026-05-29")) is not None)

# Ohtani, BDL game 5058043: a start without the DH. The batting fields are
# null and the pitching line is present.
ohtani = {"team_name": "Los Angeles Dodgers", "ip": 6, "batters_faced": 22,
          "plate_appearances": None, "at_bats": None, "bb": None,
          "p_k": 7, "p_hits": 4, "p_bb": 1, "p_runs": 1, "er": 1, "p_hr": 0}
check("Ohtani pitching-only date excluded from batting",
      bat(ohtani, ctx("Los Angeles Dodgers", "San Francisco Giants", "2026-04-01")) is None)
# Same, but BDL ships the batting zeros instead of nulls — the drop must not
# depend on the nulls.
ohtani_zeros = dict(ohtani, plate_appearances=0, at_bats=0, bb=0)
check("  ...also when the batting fields ship as 0, not null",
      bat(ohtani_zeros, ctx("Los Angeles Dodgers", "San Francisco Giants", "2026-04-01")) is None)

# Null PA is 0, not "unknown so keep": with a pitching line and AB/BB 0, drop.
check("null PA handled as 0 (pitching line, AB 0, BB 0 -> dropped)",
      bat(dict(ohtani, at_bats=0, bb=0), ctx("Los Angeles Dodgers", "X", "2026-04-01")) is None)
# ...and null PA with a real AB still bats (19 such 2026 rows exist).
check("null PA with AB 4 kept",
      bat({"team_name": "T", "ip": None, "plate_appearances": None, "at_bats": 4, "bb": 0},
          ctx("T", "U", "2026-05-20")) is not None)

print("Pitching admission")
# Milner, BDL game 5058986, CHC @ NYM 06-25: no out recorded, three hits, BF 3.
milner = {"team_name": "Chicago Cubs", "ip": 0, "pitching_outs": 0, "batters_faced": 3,
          "p_k": 0, "p_hits": 3, "p_bb": 0, "pitching_hbp": 0, "p_runs": 1, "er": 1,
          "p_hr": 1, "wins": 0, "losses": 0, "saves": 0, "holds": 0}
r = pit(milner, ctx("New York Mets", "Chicago Cubs", "2026-06-25"))
check("Milner 06-25 (IP 0, BF 3) kept", r is not None)
check("  ...with IP 0 and his three hits", r is not None and r["IP"] == 0 and r["H"] == 3, r)

# Milner, BDL game 5058015, CHC @ PHI 04-14: a pitching line on which he faced
# nobody — IP 0, BF null, every count zero or null. A true defensive-only line.
phantom = {"team_name": "Chicago Cubs", "ip": 0, "pitching_outs": None,
           "batters_faced": None, "p_k": 0, "p_hits": 0, "p_bb": 0, "p_runs": 0,
           "er": 0, "p_hr": 0, "plate_appearances": None, "at_bats": None, "bb": None}
check("defensive-only pitcher line (IP 0, BF null) dropped from pitching",
      pit(phantom, ctx("Philadelphia Phillies", "Chicago Cubs", "2026-04-14")) is None)
check("  ...and from batting (it carries a pitching line)",
      bat(phantom, ctx("Philadelphia Phillies", "Chicago Cubs", "2026-04-14")) is None)
check("BF 0 explicitly shipped, IP 0, K 0 dropped",
      pit(dict(phantom, batters_faced=0), ctx("Philadelphia Phillies", "Chicago Cubs", "2026-04-14")) is None)
check("IP 0 with a strikeout still kept (unchanged rule)",
      pit(dict(phantom, p_k=1), ctx("Philadelphia Phillies", "Chicago Cubs", "2026-04-14")) is not None)

# ---------------------------------------------------------------------------
# The cutover guard, driven through save_bdl_gamelogs_for_date itself with its
# network and DB collaborators stubbed.
# ---------------------------------------------------------------------------
print("Cutover guard")


def bdl_game(gid, utc_iso):
    return {"id": gid, "date": utc_iso, "season": 2026,
            "home_team": {"id": 1, "name": "H", "display_name": "Home", "abbreviation": "H"},
            "away_team": {"id": 2, "name": "A", "display_name": "Away", "abbreviation": "A"},
            "home_team_data": {"runs": 1}, "away_team_data": {"runs": 0}}


def run_save(date_str, games, **kw):
    fetched_dates, fetched_games = [], []

    class _Conn:
        @staticmethod
        def db_available():
            return True

        @staticmethod
        @contextlib.contextmanager
        def get_session():
            yield None

    def fetch_games(d, finals_only):
        fetched_dates.append(d)
        return games

    def fetch_stats(gid, m, c):
        fetched_games.append(gid)
        return {}, {}

    ds.update(connection=_Conn, _get_bdl_key=lambda: "k",
              fetch_bdl_games_for_date=fetch_games, fetch_bdl_game_stats=fetch_stats,
              _bdl_to_mlbam_map=lambda db: {}, time=type("T", (), {"sleep": staticmethod(lambda s: None)}),
              _BDL_RATE_LIMIT_SLEEP=0)
    out = ds["save_bdl_gamelogs_for_date"](date_str, **kw)
    return out, fetched_dates, fetched_games


out, dates, _ = run_save("2026-05-18", [bdl_game(10, "2026-05-18T23:05:00.000Z")])
check("refuses 2026-05-18", out["status"] == "refused_pre_cutover" and dates == [], out)

# The 05-19 UTC bucket: one 05-19 afternoon game, one 05-18 ET night game
# (01:10Z on the 19th is 21:10 ET on the 18th).
bucket = [bdl_game(20, "2026-05-19T17:05:00.000Z"), bdl_game(21, "2026-05-19T01:10:00.000Z")]
out, dates, gids = run_save("2026-05-19", bucket)
check("allows 2026-05-19", out["status"] == "ok" and dates == ["2026-05-19"], out)
check("  ...but skips the 05-18 ET night game inside that UTC bucket",
      gids == [20] and out["skipped_pre_cutover"] == 1, (gids, out))

out, dates, gids = run_save("2026-05-18", [bdl_game(10, "2026-05-18T23:05:00.000Z")],
                            allow_pre_cutover=True)
check("override flag lets 05-18 through", out["status"] == "ok" and gids == [10], out)
out, dates, _ = run_save("2025-05-01", [])
check("other seasons are out of scope (2025-05-01 not refused)", out["status"] == "ok", out)

# ---------------------------------------------------------------------------
# Hot/Cold windows, against in-memory SQLite with the real models.
# ---------------------------------------------------------------------------
print("Hot/Cold windows")
sys.path.insert(0, BACKEND)
from sqlalchemy import create_engine                       # noqa: E402
from sqlalchemy.orm import Session                          # noqa: E402
from database.models import BattingGameLog, PitchingGameLog  # noqa: E402

engine = create_engine("sqlite://")
BattingGameLog.__table__.create(engine)
PitchingGameLog.__table__.create(engine)
cr = extract(CRUD, {"get_batting_gamelogs", "get_pitching_gamelogs"}, {})
exec("from sqlalchemy import func, or_\nfrom sqlalchemy.orm import Session", cr)
cr.update(BattingGameLog=BattingGameLog, PitchingGameLog=PitchingGameLog)

d0 = datetime.date(2026, 9, 1)
with Session(engine) as db:
    # Sixteen batted games, then the most recent one a 0-PA pinch-running game.
    for i in range(16):
        db.add(BattingGameLog(player_id=1, game_id=f"g{i}", season=2026,
                              game_date=d0 + datetime.timedelta(days=i),
                              PA=4, AB=4, BB=0, H=1))
    db.add(BattingGameLog(player_id=1, game_id="pr", season=2026,
                          game_date=d0 + datetime.timedelta(days=20),
                          PA=0, AB=0, BB=0, H=0, R=1))
    # A null-PA row that did bat must stay in the window.
    db.add(BattingGameLog(player_id=2, game_id="n", season=2026, game_date=d0,
                          PA=None, AB=3, BB=0, H=1))
    for i in range(3):
        db.add(PitchingGameLog(player_id=3, game_id=f"p{i}", season=2026,
                               game_date=d0 + datetime.timedelta(days=i), IP=1.0, SO=1))
    db.add(PitchingGameLog(player_id=3, game_id="ip0", season=2026,
                           game_date=d0 + datetime.timedelta(days=5), IP=0.0, H=3))
    db.add(PitchingGameLog(player_id=3, game_id="bf0", season=2026,
                           game_date=d0 + datetime.timedelta(days=9), IP=0.0))
    db.commit()

    win = cr["get_batting_gamelogs"](db, 1, season=2026, last_n=15, batted_only=True)
    ids = [g.game_id for g in win]
    check("Hot/Cold skips the 0-PA row", "pr" not in ids, ids)
    check("  ...and still returns fifteen batted games (g1..g15)",
          len(win) == 15 and ids[-1] == "g1", ids)
    recent = cr["get_batting_gamelogs"](db, 1, season=2026, last_n=15)
    check("Recent Games still shows the 0-PA game first",
          recent[0].game_id == "pr", [g.game_id for g in recent][:3])
    check("null-PA row with AB 3 counts as batted",
          len(cr["get_batting_gamelogs"](db, 2, season=2026, batted_only=True)) == 1)
    pw = [g.game_id for g in cr["get_pitching_gamelogs"](db, 3, season=2026, faced_batter_only=True)]
    check("pitching window keeps IP 0 with hits, skips a line with no batter faced",
          "ip0" in pw and "bf0" not in pw, pw)

# The heat functions must actually ask for the filtered windows.
src = open(DATA_SERVICE).read()
tree = ast.parse(src)
fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def passes_kw(fn_name, callee, kw):
    for node in ast.walk(fns[fn_name]):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == callee):
            return any(k.arg == kw and isinstance(k.value, ast.Constant)
                       and k.value.value is True for k in node.keywords)
    return False


check("_compute_batter_heat passes batted_only=True",
      passes_kw("_compute_batter_heat", "get_batting_gamelogs", "batted_only"))
check("_compute_pitcher_heat passes faced_batter_only=True",
      passes_kw("_compute_pitcher_heat", "get_pitching_gamelogs", "faced_batter_only"))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
