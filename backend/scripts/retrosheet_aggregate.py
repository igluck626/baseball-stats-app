#!/usr/bin/env python3
"""Build the four committed Retrosheet season CSVs from the retrosplits daybyday files.

Writes, under backend/data/retrosheet/:
    retrosheet_batting_seasons.csv   one row per player-year (summed across teams)
    retrosheet_batting_stints.csv    one row per player-year-team, stint_order by first appearance
    retrosheet_pitching_seasons.csv
    retrosheet_pitching_stints.csv
which `retrosheet_ingest.py` then loads. Regular season only; MLBAM ids via
chadwick_retro_bridge.csv. Runs off Railway (the daybyday files are ~30 MB a season).

⚠️ ONE SOURCE PER GAME. From 1903 to 1972 a daybyday file can carry the same
player-game twice — once from the event file ('evt') and once from the box score
('box'), sometimes a third time from a deduced game ('ded'). Summing every row
double-counts. Each game is read from ONE source, preferring evt, then box, then
ded — the order the committed CSVs were built with.

⚠️ BATTING G IS EVERY APPEARANCE. `B_G` is 1 on any game a man appeared in:
batting, pinch-running, a defensive inning, or pitching without batting. Batting G
is the number of distinct games with `B_G` = 1 — the official count, what Lahman
and Baseball-Reference print and what balldontlie ships for the current season.
The CSVs used to count only games with a batting event (any nonzero batting
stat), which left Tommie Aaron's 1962 at 115 games against an official 141. That
older count is still written, as G_batted, because the streak and span gate
(`_complete_seasons`) compares it with the game-log rows, and the game logs hold
only the games with a batting event. `--g-rule legacy` writes the old count into
G to reproduce the earlier files.

A player-year gets a batting row only when some batting stat is nonzero, as
before — a pitcher who never batted gets none, however many games he pitched. Once
he qualifies, every team he appeared for that year counts toward G and gets a stint
row, including a team he only pitched for.

Usage:
    python3 retrosheet_aggregate.py --cache DIR [--first 1898] [--last 2025]
                                    [--out DIR] [--g-rule appearances|legacy]
"""
import argparse
import collections
import csv
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_RETRO_DIR = os.path.join(_HERE, "..", "data", "retrosheet")
_URL = "https://raw.githubusercontent.com/chadwickbureau/retrosplits/master/daybyday/playing-{year}.csv"
_SOURCE_RANK = {"evt": 0, "box": 1, "ded": 2}

BAT_COLS = ["G", "PA", "AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB", "SO", "SB", "CS",
            "IBB", "HBP", "SH", "SF", "GIDP", "TB"]
_BAT_SRC = {"PA": "B_PA", "AB": "B_AB", "R": "B_R", "H": "B_H", "doubles": "B_2B", "triples": "B_3B",
            "HR": "B_HR", "RBI": "B_RBI", "BB": "B_BB", "SO": "B_SO", "SB": "B_SB", "CS": "B_CS",
            "IBB": "B_IBB", "HBP": "B_HP", "SH": "B_SH", "SF": "B_SF", "GIDP": "B_GDP", "TB": "B_TB"}
PIT_COLS = ["W", "L", "G", "GS", "IP", "SO", "BB", "HR", "CG", "SHO", "SV", "H", "ER", "R", "IBB",
            "WP", "HBP", "BK", "BFP", "GF", "SH", "SF", "GIDP"]
_PIT_SRC = {"W": "P_W", "L": "P_L", "GS": "P_GS", "SO": "P_SO", "BB": "P_BB", "HR": "P_HR",
            "CG": "P_CG", "SHO": "P_SHO", "SV": "P_SV", "H": "P_H", "ER": "P_ER", "R": "P_R",
            "IBB": "P_IBB", "WP": "P_WP", "HBP": "P_HP", "BK": "P_BK", "BFP": "P_TBF", "GF": "P_GF",
            "SH": "P_SH", "SF": "P_SF", "GIDP": "P_GDP"}


def _i(v) -> int:
    try:
        return int(float(v)) if v not in ("", None) else 0
    except ValueError:
        return 0


def _playing_file(cache: str, year: int) -> str:
    path = os.path.join(cache, f"playing-{year}.csv")
    if not os.path.exists(path):
        tmp = path + ".part"
        urllib.request.urlretrieve(_URL.format(year=year), tmp)
        os.replace(tmp, path)
    return path


def _bridge() -> dict:
    with open(os.path.join(_RETRO_DIR, "chadwick_retro_bridge.csv"), encoding="utf-8-sig") as f:
        return {r["key_retro"]: int(r["key_mlbam"]) for r in csv.DictReader(f) if r["key_retro"] and r["key_mlbam"]}


def aggregate_year(path: str, year: int, bridge: dict, g_rule: str = "appearances"):
    """(batting_stints, pitching_stints) for one season: {(mlbam, team): {...}} with the
    bookkeeping keys _first / _last (appearance dates) and _bat (any batting stat)."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["season.phase"] == "R":
                rows.append(r)
    best: dict = {}
    for r in rows:
        g = r["game.key"]
        if g not in best or _SOURCE_RANK.get(r["game.source"], 9) < _SOURCE_RANK.get(best[g], 9):
            best[g] = r["game.source"]

    bat: dict = {}
    pit: dict = {}
    for r in rows:
        if r["game.source"] != best[r["game.key"]]:
            continue                                   # one source per game
        mlbam = bridge.get(r["person.key"])
        if mlbam is None:
            continue
        key = (mlbam, r["team.key"])
        when = (r["game.date"], _i(r["game.number"]))
        b = bat.setdefault(key, {"_games": set(), "_batted": set(), "_first": when, "_last": when, "_bat": False,
                                 **{c: 0 for c in BAT_COLS if c != "G"}})
        b["_first"], b["_last"] = min(b["_first"], when), max(b["_last"], when)
        for c, src in _BAT_SRC.items():
            b[c] += _i(r[src])
        if any(_i(r[src]) for src in _BAT_SRC.values()):
            b["_bat"] = True
            b["_batted"].add(r["game.key"])
        if _i(r["B_G"]):
            b["_games"].add(r["game.key"])
        if _i(r["P_G"]):
            p = pit.setdefault(key, {"_games": set(), "_first": when, "_last": when, "_outs": 0,
                                     **{c: 0 for c in PIT_COLS if c not in ("G", "IP")}})
            p["_first"], p["_last"] = min(p["_first"], when), max(p["_last"], when)
            p["_games"].add(r["game.key"])
            p["_outs"] += _i(r["P_OUT"])
            for c, src in _PIT_SRC.items():
                p[c] += _i(r[src])
    for b in bat.values():
        b["G_batted"] = len(b["_batted"])
        b["G"] = len(b["_games"]) if g_rule == "appearances" else b["G_batted"]
    for p in pit.values():
        p["G"] = len(p["_games"])
        p["IP"] = round(p["_outs"] / 3, 3)
    return bat, pit


def _season_rows(stints: dict, cols: list, year: int, keep, extra=(), g_rule: str = "appearances"):
    """Collapse team stints into (season_rows, stint_rows) for one year.

    stint_order and the season row's team are worked out over EVERY team the man
    appeared for that year, before `keep` drops any: a pitcher traded mid-season who
    batted only for his second club keeps stint_order 2 there, and his season row
    names the club of his last game."""
    by_player = collections.defaultdict(list)
    for (mlbam, team), s in stints.items():
        by_player[mlbam].append((team, s))
    seasons, stint_rows = [], []
    for mlbam, every in by_player.items():
        every.sort(key=lambda ts: ts[1]["_first"])
        order_of = {team: n for n, (team, _s) in enumerate(every, 1)}
        last_team = max(every, key=lambda ts: ts[1]["_last"])[0]
        # `keep` decides the PLAYER-YEAR, not each team: a pitcher traded in July who
        # batted only for his second club still appeared for his first, and those
        # games are his (Lahman lists both teams). The legacy rule kept only the
        # teams he batted for, so that is what it reproduces.
        if not any(keep(s) for _team, s in every):
            continue
        items = [(team, s) for team, s in every if g_rule == "appearances" or keep(s)]
        tot = {c: 0 for c in list(cols) + list(extra)}
        for team, s in items:
            order = order_of[team]
            row = {"player_id": mlbam, "year": year, "team": team}
            for c in list(cols) + list(extra):
                row[c] = s[c]
                tot[c] += s[c]
            row["stint_order"] = order
            stint_rows.append(row)
        if "IP" in tot:
            tot["IP"] = round(sum(s["_outs"] for _, s in items) / 3, 3)
        seasons.append({"player_id": mlbam, "year": year, "team": last_team, "league": "", **tot})
    return seasons, stint_rows


def build(cache: str, first: int, last: int, g_rule: str):
    bridge = _bridge()
    out = {"bat_seasons": [], "bat_stints": [], "pit_seasons": [], "pit_stints": []}
    for year in range(first, last + 1):
        bat, pit = aggregate_year(_playing_file(cache, year), year, bridge, g_rule)
        s, st = _season_rows(bat, BAT_COLS, year, keep=lambda b: b["_bat"], extra=("G_batted",), g_rule=g_rule)
        out["bat_seasons"] += s; out["bat_stints"] += st
        s, st = _season_rows(pit, PIT_COLS, year, keep=lambda p: True)
        out["pit_seasons"] += s; out["pit_stints"] += st
        print(f"  {year}: {len(bat)} batting stints, {len(pit)} pitching stints", flush=True)
    return out


def write(out: dict, out_dir: str, g_rule: str):
    os.makedirs(out_dir, exist_ok=True)
    gb = ["G_batted"] if g_rule == "appearances" else []
    specs = {
        "retrosheet_batting_seasons.csv":  ("bat_seasons", ["player_id", "year", "team", "league"] + BAT_COLS + gb),
        "retrosheet_batting_stints.csv":   ("bat_stints",  ["player_id", "year", "team"] + BAT_COLS + gb + ["stint_order"]),
        "retrosheet_pitching_seasons.csv": ("pit_seasons", ["player_id", "year", "team", "league"] + PIT_COLS),
        "retrosheet_pitching_stints.csv":  ("pit_stints",  ["player_id", "year", "team"] + PIT_COLS + ["stint_order"]),
    }
    for name, (key, cols) in specs.items():
        rows = sorted(out[key], key=lambda r: (r["player_id"], r["year"], r.get("stint_order", 0)))
        with open(os.path.join(out_dir, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {name}: {len(rows)} rows")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", required=True, help="directory holding (or receiving) playing-YYYY.csv")
    ap.add_argument("--first", type=int, default=1898)
    ap.add_argument("--last", type=int, default=2025)
    ap.add_argument("--out", default=_RETRO_DIR)
    ap.add_argument("--g-rule", choices=("appearances", "legacy"), default="appearances")
    a = ap.parse_args()
    write(build(a.cache, a.first, a.last, a.g_rule), a.out, a.g_rule)
    return 0


if __name__ == "__main__":
    sys.exit(main())
