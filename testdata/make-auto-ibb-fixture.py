#!/usr/bin/env python3
"""Regenerate backend/tests/fixtures/auto_ibb_2026.json: balldontlie data for the
games behind the automatic-intentional-walk stand-ins (team_stats.fill_automatic_ibbs),
and backend/tests/fixtures/bdl_stats_15467418.json: LAD @ MIL's /stats rows untrimmed.

balldontlie's /plate_appearances writes no row for an automatic intentional walk (no
pitch thrown), so pa_number skips one. These games each have one or more such holes,
plus one game whose intentional walk came after real pitches and so IS in the feed:

  15467381  2026-10-07 CLE @ CHW    hole 71, top 8th
  15467417  2026-10-10 CHW @ CLE    hole 59, bottom 8th
  15467418  2026-10-11 LAD @ MIL    holes 62 and 64, bottom 8th (two in one inning)
  5059902   the old missingPARow game, hole 71, top 10th
  15467377  2026-10-05 CHW @ CLE    a pitched intentional walk (in the feed), no hole

Trimmed by DROPPING fields only — every field kept has balldontlie's own name and
nesting (a /stats row's player id stays at player.id, its team at team.id), and no
field is added. Per game: the /games object; /plate_appearances with each row's
top-level fields and only its in-play pitch; the /stats rows in one list, as the
endpoint returns them; and the WHOLE play stream (pitch rows included — telling an
automatic walk from a pitched one needs them).

Usage:  BDL_KEY=... python3 testdata/make-auto-ibb-fixture.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GAMES = [15467381, 15467417, 15467418, 5059902, 15467377]
FULL_STATS = 15467418
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", "tests", "fixtures")
OUT = os.path.join(FIXTURES, "auto_ibb_2026.json")
OUT_STATS = os.path.join(FIXTURES, f"bdl_stats_{FULL_STATS}.json")
BASE = "https://api.balldontlie.io/mlb/v1/"
GAME_KEYS = ["id", "date", "status", "postseason"]
TEAM_KEYS = ["id", "abbreviation"]
PA_DROP = ["pitches"]
PITCH_KEYS = ["call_name", "exit_velocity", "expected_batting_average", "launch_angle"]
STAT_KEYS = ["at_bats", "bb", "caught_stealing", "doubles", "errors", "gidp", "hit_by_pitch", "hits",
             "hr", "intentional_walks", "k", "left_on_base", "pitch_count", "pitching_outs", "plate_appearances",
             "runs", "sac_bunts", "sac_flies", "stolen_bases", "triples", "team_name", "game_id"]
PLAYER_KEYS = ["id", "full_name"]
PLAY_KEYS = ["batter_id", "inning", "inning_type", "order", "outs", "text", "type"]


def get(path, params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"Authorization": os.environ["BDL_KEY"]})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + 3 * attempt)


def paged(path, params):
    rows, cursor = [], None
    for _ in range(15):
        page = get(path, dict(params, per_page=100, **({"cursor": cursor} if cursor else {})))
        rows += page.get("data") or []
        cursor = (page.get("meta") or {}).get("next_cursor")
        if not cursor:
            return rows
    sys.exit(f"{path} {params}: more than 15 pages")


def pick(d, keys):
    """The listed fields d actually has — never one it doesn't."""
    return {k: d[k] for k in keys if k in d}


def in_play(pa):
    for q in pa.get("pitches") or []:
        if q.get("exit_velocity") is not None or q.get("expected_batting_average") is not None:
            return q
    for q in pa.get("pitches") or []:
        if (q.get("call_name") or "").lower().startswith("in play"):
            return q
    return None


games = {}
for gid in GAMES:
    g = get(f"games/{gid}", None)["data"]
    game = {**pick(g, GAME_KEYS), "away_team": pick(g["away_team"], TEAM_KEYS), "home_team": pick(g["home_team"], TEAM_KEYS)}
    pas = []
    for pa in paged("plate_appearances", {"game_id": gid}):
        q = in_play(pa)
        pas.append({**{k: v for k, v in pa.items() if k not in PA_DROP}, "pitches": [pick(q, PITCH_KEYS)] if q else []})
    raw_stats = paged("stats", {"game_ids[]": gid})
    stats = [{**pick(r, STAT_KEYS), "player": pick(r.get("player") or {}, PLAYER_KEYS), "team": pick(r.get("team") or {}, TEAM_KEYS)}
             for r in raw_stats]
    if gid == FULL_STATS:
        with open(OUT_STATS, "w") as f:
            json.dump({"note": f"balldontlie /stats?game_ids[]={gid}, every row and field exactly as served, fetched "
                               f"{time.strftime('%Y-%m-%d', time.gmtime())} (testdata/make-auto-ibb-fixture.py).",
                       "data": raw_stats}, f, separators=(",", ":"))
        print("wrote", os.path.relpath(OUT_STATS), os.path.getsize(OUT_STATS), "bytes")
    plays = [pick(p, PLAY_KEYS) for p in paged("plays", {"game_id": gid})]
    games[str(gid)] = {"game": game, "plate_appearances": pas, "stats": stats, "plays": plays}
    print(gid, game["away_team"]["abbreviation"], "@", game["home_team"]["abbreviation"], len(pas), "PAs",
          len(stats), "stat rows", len(plays), "plays")

note = ("balldontlie data for the automatic-intentional-walk games (see testdata/make-auto-ibb-fixture.py), "
        f"fetched {time.strftime('%Y-%m-%d', time.gmtime())}, trimmed by dropping fields only (names and nesting as "
        "served): the /games object, /plate_appearances (top-level fields, in-play pitch only), the /stats rows, "
        "and the WHOLE play stream (pitch rows kept).")
with open(OUT, "w") as f:
    json.dump({"note": note, "games": games}, f, separators=(",", ":"))
print("wrote", os.path.relpath(OUT), os.path.getsize(OUT), "bytes")
