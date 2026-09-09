#!/usr/bin/env python3
"""Regenerate testdata/game-leaders.json from live BDL payloads.

The fixture is REAL payload, trimmed to the fields the Swift models
decode. Constructed data is what missed the interleaved steal and the
mid-inning substitution the first time these joins were written — both
are shapes nobody thinks to invent.

Expectations are computed HERE, by an independent implementation, so a
test comparing against them is comparing two readings of the same
payload rather than checking the app against itself.

Usage:  BDL_KEY=... python3 testdata/make-game-leaders-fixture.py
"""
import json, os, sys, urllib.parse, urllib.request
from collections import Counter, defaultdict

KEY = os.environ["BDL_KEY"]
BASE = "https://api.balldontlie.io/mlb/v1"

# 5059936 — an interleaved steal inside an at-bat (Top 8), a pinch hitter
#           and two relief changes mid-inning, and Halvorsen's 7-of-10.
# 5059818 — four batters batting TWICE in the bottom 8th, which is the
#           only reason the join needs a queue rather than a lookup.
GAMES = {"stealAndSubs": 5059936, "battedAround": 5059818}

PLAY_FIELDS = ("game_id order type text home_score away_score inning inning_type "
               "scoring_play score_value outs balls strikes batter_id pitcher_id "
               "pitch_type pitch_velocity trajectory").split()
PITCH_FIELDS = ("exit_velocity launch_angle hit_distance expected_batting_average "
                "is_barrel release_speed plate_speed pitch_type call_name description").split()


def get(path, params):
    qs = urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(f"{BASE}/{path}?{qs}",
                                 headers={"Authorization": KEY, "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())


def all_plays(gid):
    rows, cursor = [], None
    while True:
        p = {"game_id": gid, "per_page": 100}
        if cursor:
            p["cursor"] = cursor
        d = get("plays", p)
        rows += d["data"]
        cursor = (d.get("meta") or {}).get("next_cursor")
        if not cursor or len(d["data"]) < 100:
            break
    return rows


def half(raw):
    return "bottom" if "bot" in (raw or "").lower() else "top"


def build(gid):
    plays = [{k: p.get(k) for k in PLAY_FIELDS} for p in all_plays(gid)]
    pas = []
    for pa in get("plate_appearances", {"game_id": gid})["data"]:
        pas.append({
            "batter_id": pa.get("batter_id"), "pitcher_id": pa.get("pitcher_id"),
            "inning": pa["inning"], "half_inning": pa.get("half_inning"),
            "pa_number": pa["pa_number"], "result": pa.get("result"),
            "pitches": [{k: q.get(k) for k in PITCH_FIELDS} for q in (pa.get("pitches") or [])],
        })
    stats = get("stats", {"game_ids[]": gid, "per_page": 100})["data"]
    identity = {str(s["player"]["id"]): {"name": s["player"]["full_name"],
                                         "teamId": (s.get("team") or {}).get("id")}
                for s in stats if s.get("team")}
    team_ids = sorted({v["teamId"] for v in identity.values()})

    # --- expectations, computed independently of the Swift ---
    def rank(events, limit=10):
        return [{"playerId": p, "value": round(v, 1)}
                for p, v in sorted(events, key=lambda x: -x[1])[:limit]]

    hits, pitches = [], []
    for pa in pas:
        for q in pa["pitches"]:
            if q.get("exit_velocity") is not None and pa["batter_id"]:
                hits.append((pa["batter_id"], q["exit_velocity"]))
            if q.get("release_speed") is not None and pa["pitcher_id"]:
                pitches.append((pa["pitcher_id"], q["release_speed"]))

    def side(events, team):
        return rank([(p, v) for p, v in events
                     if identity.get(str(p), {}).get("teamId") == team], 3)

    # every plate appearance paired with the sentence that belongs to it,
    # by inning + half + batter consumed in order
    texts = defaultdict(list)
    for p in sorted(plays, key=lambda x: x["order"]):
        if p["type"] == "Play Result" and p.get("batter_id") is not None and p.get("text"):
            texts[(p["inning"], half(p["inning_type"]), p["batter_id"])].append(p["text"])
    cursor, sentences = defaultdict(int), []
    for pa in sorted(pas, key=lambda x: (x["inning"], x["pa_number"])):
        k = (pa["inning"], half(pa["half_inning"]), pa["batter_id"])
        i = cursor[k]; cursor[k] += 1
        got = texts[k][i] if i < len(texts[k]) else None
        sentences.append({"inning": pa["inning"], "half": half(pa["half_inning"]),
                          "batterId": pa["batter_id"], "paNumber": pa["pa_number"],
                          "occurrence": i, "sentence": got})

    repeats = [{"inning": k[0], "half": k[1], "batterId": k[2], "times": n}
               for k, n in Counter((pa["inning"], half(pa["half_inning"]), pa["batter_id"])
                                   for pa in pas).items() if n > 1]

    top = rank(pitches)
    owner = Counter(e["playerId"] for e in top).most_common(1)[0] if top else (None, 0)

    return {
        "gameId": gid, "plays": plays, "pas": pas, "identity": identity,
        "awayTeamId": team_ids[0] if len(team_ids) == 2 else None,
        "homeTeamId": team_ids[1] if len(team_ids) == 2 else None,
        "expected": {
            "topHits": rank(hits),
            "topPitches": top,
            "mostRowsOnePlayer": {"playerId": owner[0], "rows": owner[1]},
            "distinctInTopPitches": len({e["playerId"] for e in top}),
            "perSideHits": {"away": side(hits, team_ids[0]), "home": side(hits, team_ids[1])}
                           if len(team_ids) == 2 else {},
            "perSidePitches": {"away": side(pitches, team_ids[0]), "home": side(pitches, team_ids[1])}
                              if len(team_ids) == 2 else {},
            "sentences": sentences,
            "repeatBatters": repeats,
        },
    }


out = {label: build(gid) for label, gid in GAMES.items()}
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game-leaders.json")
with open(path, "w") as f:
    json.dump(out, f, separators=(",", ":"))
print(f"wrote {path} ({os.path.getsize(path):,} bytes)")
for label, d in out.items():
    e = d["expected"]
    print(f"  {label}: plays={len(d['plays'])} pas={len(d['pas'])} "
          f"repeats={len(e['repeatBatters'])} topOwner={e['mostRowsOnePlayer']}")
