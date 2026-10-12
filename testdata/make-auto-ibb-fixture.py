#!/usr/bin/env python3
"""Regenerate backend/tests/fixtures/auto_ibb_2026.json: balldontlie data for the
games behind the automatic-intentional-walk stand-ins (team_contact.fill_automatic_ibbs).

balldontlie's /plate_appearances writes no row for an automatic intentional walk (no
pitch thrown), so pa_number skips one. These games each have one or more such holes,
plus one game whose intentional walk came after real pitches and so IS in the feed:

  15467381  2026-10-07 CLE @ CHW    hole 71, top 8th
  15467417  2026-10-10 CHW @ CLE    hole 59, bottom 8th
  15467418  2026-10-11 LAD @ MIL    holes 62 and 64, bottom 8th (two in one inning)
  5059902   the old missingPARow game, hole 71, top 10th
  15467377  2026-10-05 CHW @ CLE    a pitched intentional walk (in the feed), no hole

Trimmed like team_contact_2026_postseason.json — plate appearances with the in-play
pitch only and the runners at the start, /stats rows split by side — except that the
play stream keeps EVERY row (pitch rows included, five fields each): telling an
automatic walk from a pitched one needs the at-bat's pitch rows.

Usage:  BDL_KEY=... python3 testdata/make-auto-ibb-fixture.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GAMES = [15467381, 15467417, 15467418, 5059902, 15467377]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", "tests", "fixtures", "auto_ibb_2026.json")
BASE = "https://api.balldontlie.io/mlb/v1/"
PA_KEYS = ["batter_id", "half_inning", "inning", "pa_number", "pitch_count", "result",
           "runner_on_first", "runner_on_second", "runner_on_third"]
PITCH_KEYS = ["call_name", "exit_velocity", "expected_batting_average", "launch_angle"]
BOX_KEYS = ["at_bats", "bb", "caught_stealing", "doubles", "errors", "gidp", "hit_by_pitch", "hits",
            "hr", "k", "left_on_base", "pitch_count", "pitching_outs", "plate_appearances", "runs",
            "sac_bunts", "sac_flies", "stolen_bases", "triples"]
PLAY_KEYS = ["batter_id", "inning", "inning_type", "order", "outs", "text", "type"]


def get(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params, doseq=True)
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
    g = get(f"games/{gid}", {})["data"]
    pas = []
    for pa in paged("plate_appearances", {"game_id": gid}):
        q = in_play(pa)
        pas.append({**{k: pa.get(k) for k in PA_KEYS}, "pitches": [{k: q.get(k) for k in PITCH_KEYS}] if q else []})
    ids = {"away": g["away_team"]["id"], "home": g["home_team"]["id"]}
    stats = paged("stats", {"game_ids[]": gid})
    box = {side: [{**{k: r.get(k) for k in BOX_KEYS}, "player_id": (r.get("player") or {}).get("id")}
                  for r in stats if (r.get("team") or {}).get("id") == tid] for side, tid in ids.items()}
    plays = [{k: p.get(k) for k in PLAY_KEYS} for p in paged("plays", {"game_id": gid})]
    games[str(gid)] = {"date": g["date"], "away": g["away_team"]["abbreviation"], "home": g["home_team"]["abbreviation"],
                       "status": g["status"], "plate_appearances": pas, "box": box, "plays": plays}
    print(gid, g["away_team"]["abbreviation"], "@", g["home_team"]["abbreviation"], len(pas), "PAs", len(plays), "plays")

note = ("balldontlie data for the automatic-intentional-walk games (see testdata/make-auto-ibb-fixture.py), "
        f"fetched {time.strftime('%Y-%m-%d', time.gmtime())}: plate appearances (in-play pitch only, runners at the "
        "start), /stats rows by side, and the WHOLE play stream (pitch rows kept) trimmed to five fields.")
with open(OUT, "w") as f:
    json.dump({"note": note, "games": games}, f, separators=(",", ":"))
print("wrote", os.path.relpath(OUT), os.path.getsize(OUT), "bytes")
