#!/usr/bin/env python3
"""Regenerate testdata/live-leaders.json: the server's live Game Leaders top 3
(backend/api/game_leaders_live.py) for the games in game-leaders.json, and for a
small constructed tie case.

Both suites read it. backend/tests/test_game_leaders_live.py checks the server
still produces exactly this; GameLeadersTests checks the client's FINAL ranking,
cut to three, comes out in the same order — so a game's last live top 3 and its
final top 3 agree when no event is missing.

The tie case is fed OUT of game order: a ranking that leaned on feed order (a
stable sort) instead of the shared tie-break would fail it.

Usage:  python3 testdata/make-live-leaders-fixture.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend", "api"))
import game_leaders_live as gl  # noqa: E402


def pitch(speed=None, ev=None):
    return {"release_speed": speed, "exit_velocity": ev, "pitch_type": "4-Seam Fastball" if speed else None}


# Four 99.5s and three 101.0s: the earlier event must rank first every time.
TIES = {
    "awayTeamId": 1, "homeTeamId": 2,
    "identity": {"10": {"name": "Pitcher Ten", "teamId": 2}, "11": {"name": "Pitcher Eleven", "teamId": 1},
                 "20": {"name": "Batter Twenty", "teamId": 1}, "21": {"name": "Batter TwentyOne", "teamId": 2},
                 "22": {"name": "Batter TwentyTwo", "teamId": 1}},
    "pas": [   # deliberately not in game order
        {"batter_id": 20, "pitcher_id": 10, "inning": 2, "half_inning": "top", "pa_number": 8,
         "result": "Double", "pitches": [pitch(99.5, 103.0)]},
        {"batter_id": 22, "pitcher_id": 10, "inning": 2, "half_inning": "top", "pa_number": 7,
         "result": "Single", "pitches": [pitch(99.5, 101.0), pitch(97.0)]},
        {"batter_id": 21, "pitcher_id": 11, "inning": 1, "half_inning": "bottom", "pa_number": 4,
         "result": "Single", "pitches": [pitch(99.5, 101.0)]},
        {"batter_id": 20, "pitcher_id": 10, "inning": 1, "half_inning": "top", "pa_number": 1,
         "result": "Lineout", "pitches": [pitch(98.0), pitch(99.5, 101.0)]},
    ],
}


def top3(pas, identity, away, home):
    names = {int(k): v["name"] for k, v in identity.items()}
    board = gl.build(pas, names, away_id=away, home_id=home) or {c: [] for c in gl.CATEGORIES}
    return {c: [[e["player_id"], e["inning"], e["half"], e["pa_number"], e["pitch_index"], e["value"]]
                for e in board[c]] for c in gl.CATEGORIES}


def fixture():
    games = json.load(open(os.path.join(HERE, "game-leaders.json")))
    out = {"games": {}, "ties": dict(TIES)}
    for name, g in games.items():
        out["games"][name] = {"top3": top3(g["pas"], g["identity"], g["awayTeamId"], g["homeTeamId"])}
    out["ties"]["top3"] = top3(TIES["pas"], TIES["identity"], TIES["awayTeamId"], TIES["homeTeamId"])
    # One whole board in the shape the snapshot ships, for the client's decode test.
    g = games["stealAndSubs"]
    out["board"] = gl.build(g["pas"], {int(k): v["name"] for k, v in g["identity"].items()},
                            away_id=g["awayTeamId"], home_id=g["homeTeamId"])
    return out


if __name__ == "__main__":
    path = os.path.join(HERE, "live-leaders.json")
    json.dump(fixture(), open(path, "w"), indent=1)
    print("wrote", path)
