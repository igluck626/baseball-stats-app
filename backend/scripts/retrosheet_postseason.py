#!/usr/bin/env python3
"""Retrosheet postseason game logs -> postseason_batting/pitching_gamelogs.

The retrosplits daybyday files the regular-season backfill already downloads
(`playing-YYYY.csv`) carry every postseason game too, as `season.phase`
F (Wild Card), D (Division Series), L (LCS) and W (World Series) — the
regular-season ingest drops them (`season.phase != "R"`). This writes them to
their OWN tables, so nothing here can reach a regular-season figure.

Box-score derived: ER comes straight from `P_ER`, never rebuilt from play
events (the source of the earlier ER corruption). Rows are de-duplicated with
the regular ingest's own rule (evt > box > ded, one per appearance) and mapped
to MLBAM through the same committed Chadwick bridge — the helpers are imported
from `retrosheet_gamelogs`, not copied.

DRY RUN BY DEFAULT: downloads (cached with --cache), parses and prints
per-season counts; nothing is written. `--write` inserts with ON CONFLICT DO
NOTHING against the partial unique indexes and must not be run without an
approved dry run.

Usage:
    python backend/scripts/retrosheet_postseason.py --from 1903 --to 2025 --cache /tmp/retro
    python backend/scripts/retrosheet_postseason.py --from 2025 --to 2025 --write
"""
import argparse
import collections
import csv
import datetime
import io
import os
import sys
from typing import Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, _SCRIPTS_DIR)
sys.path.insert(0, _BACKEND_DIR)

import retrosheet_gamelogs as rg                                  # noqa: E402

# retrosplits `season.phase` -> our round code. 'A' (All-Star) and 'R' are
# not postseason and never enter these tables.
ROUND = {"F": "WC", "D": "DS", "L": "CS", "W": "WS"}


def _score_map(teams_text: str) -> dict:
    """{(game.key, team.key): (team_runs, opp_runs, W/L/T)} for postseason games."""
    by_game: dict = collections.defaultdict(dict)
    for r in csv.DictReader(io.StringIO(teams_text)):
        if r.get("season.phase") not in ROUND:
            continue
        by_game[r["game.key"]][r["team.key"]] = (
            rg._i(r.get("B_R")), r.get("R_W"), r.get("R_L"), r.get("R_T"))
    out = {}
    for gk, teams in by_game.items():
        for t, (runs, w, l, ti) in teams.items():
            opp = next((v[0] for t2, v in teams.items() if t2 != t), None)
            out[(gk, t)] = (runs, opp, "W" if w == "1" else "L" if l == "1" else "T" if ti == "1" else None)
    return out


def parse_year(year: int, playing_text: str, teams_text: Optional[str],
               bridge: dict) -> tuple[list[dict], list[dict], set]:
    """(batting rows, pitching rows, unmapped retro ids) for one season's
    postseason. Unmapped players' rows are INCLUDED, with player_id None.
    Pure: no network, no database."""
    best: dict = {}
    for r in csv.DictReader(io.StringIO(playing_text)):
        if r.get("season.phase") not in ROUND:
            continue
        pk, gk = r.get("person.key"), r.get("game.key")
        if not pk or not gk:
            continue
        key = (pk, gk, r.get("slot"), r.get("seq"))
        pri = rg._SRC.get(r.get("game.source"), 0)
        if key not in best or pri > best[key][0]:
            best[key] = (pri, r)
    scores = _score_map(teams_text) if teams_text else {}
    bat, pit, unmapped = [], [], set()
    for _, r in best.values():
        # ⚠️ An unmapped player is KEPT: `player_id` NULL, the retro id always
        # set, resolved at read time once a mapping lands. Reported, not dropped.
        mlbam = bridge.get(r["person.key"])
        if mlbam is None:
            unmapped.add(r["person.key"])
        gk, team = r["game.key"], r.get("team.key")
        try:
            gd = datetime.date.fromisoformat(r.get("game.date") or "")
        except ValueError:
            gd = None
        team_score, opp_score, result = scores.get((gk, team), (None, None, None))
        common = {"player_id": mlbam, "bdl_player_id": None, "retro_player_id": r["person.key"],
                  "source": "retrosheet",
                  "game_id": "retro-" + gk, "game_date": gd, "season": year,
                  "round": ROUND[r["season.phase"]], "team": team,
                  "opponent": r.get("opponent.key"),
                  "home_away": "H" if r.get("team.alignment") == "1" else "A"}
        i = rg._i
        # The regular ingest's appearance gate, verbatim: from 2022 (universal
        # DH) an EMPTY batting row of a man who pitched is the non-batting
        # pitcher's line, not a batting appearance. Without it a two-way
        # starter (Ohtani, 2025) has TWO batting rows in one game — his real one
        # in slot 1 and an empty one in slot 0 — and the table keeps only one.
        has_outcome = any(i(r.get(c)) > 0 for c in rg._BAT_SIGNAL)
        pitched = i(r.get("P_G")) > 0 or i(r.get("P_OUT")) > 0
        if i(r.get("B_G")) > 0 and (has_outcome or not pitched or year < 2022):
            bat.append({**common, "result": result, "team_score": team_score, "opp_score": opp_score,
                        "PA": rg._effective_pa(i(r.get("B_PA")), i(r.get("B_AB")), i(r.get("B_BB")),
                                               i(r.get("B_HP")), i(r.get("B_SF")), i(r.get("B_SH"))),
                        "AB": i(r.get("B_AB")), "R": i(r.get("B_R")), "H": i(r.get("B_H")),
                        "doubles": i(r.get("B_2B")), "triples": i(r.get("B_3B")), "HR": i(r.get("B_HR")),
                        "RBI": i(r.get("B_RBI")), "BB": i(r.get("B_BB")), "IBB": i(r.get("B_IBB")),
                        "SO": i(r.get("B_SO")), "SB": i(r.get("B_SB")), "CS": i(r.get("B_CS")),
                        "HBP": i(r.get("B_HP")), "SF": i(r.get("B_SF")), "GIDP": i(r.get("B_GDP")),
                        "SH": i(r.get("B_SH"))})
        if i(r.get("P_G")) > 0 or i(r.get("P_OUT")) > 0:
            pit.append({**common, "result": rg._pitcher_decision(r),
                        "IP": round(i(r.get("P_OUT")) / 3, 3), "H": i(r.get("P_H")),
                        "R": i(r.get("P_R")), "ER": i(r.get("P_ER")), "BB": i(r.get("P_BB")),
                        "SO": i(r.get("P_SO")), "HR": i(r.get("P_HR")), "HBP": i(r.get("P_HP")),
                        "W": i(r.get("P_W")), "L": i(r.get("P_L")), "SV": i(r.get("P_SV")),
                        "GS": i(r.get("P_GS"))})
    return bat, pit, unmapped


def _fetch(url: str, cache: Optional[str]) -> Optional[str]:
    if cache:
        path = os.path.join(cache, os.path.basename(url))
        if os.path.exists(path):
            return open(path).read()
    text = rg._download_csv(url)
    if text and cache:
        os.makedirs(cache, exist_ok=True)
        open(os.path.join(cache, os.path.basename(url)), "w").write(text)
    return text


def _counts() -> dict:
    from sqlalchemy import text
    from database import connection
    with connection.get_session() as db:
        return {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                for t in ("postseason_batting_gamelogs", "postseason_pitching_gamelogs")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="year_from", type=int, default=1903)
    ap.add_argument("--to", dest="year_to", type=int, default=2025)
    ap.add_argument("--cache", default=None, help="Keep downloaded files here.")
    ap.add_argument("--write", action="store_true", help="Insert. Without it: dry run.")
    args = ap.parse_args()
    bridge = rg._load_bridge()
    before = _counts() if args.write else {}
    print("season\tgames\tbat_rows\tpit_rows\tunmapped\trounds")
    totals = collections.Counter()
    for year in range(args.year_from, args.year_to + 1):
        playing = _fetch(rg._PLAYING_URL.format(year=year), args.cache)
        if not playing:
            print(f"{year}\tMISSING")
            continue
        teams = _fetch(rg._TEAMS_URL.format(year=year), args.cache)
        bat, pit, unmapped = parse_year(year, playing, teams, bridge)
        if not bat and not pit:
            continue                       # no postseason that year (1904, 1994)
        games = len({r["game_id"] for r in bat + pit})
        rounds = ",".join(f"{k}:{v}" for k, v in sorted(collections.Counter(
            r["round"] for r in {x["game_id"]: x for x in bat + pit}.values()).items()))
        print(f"{year}\t{games}\t{len(bat)}\t{len(pit)}\t{len(unmapped)}\t{rounds}", flush=True)
        totals.update(games=games, bat=len(bat), pit=len(pit), unmapped=len(unmapped), seasons=1)
        if args.write:
            from database import connection, crud
            from database.models import PostseasonBattingGameLog, PostseasonPitchingGameLog
            with connection.get_session() as db:
                crud.bulk_insert_postseason(db, PostseasonBattingGameLog, bat)
                crud.bulk_insert_postseason(db, PostseasonPitchingGameLog, pit)
    print(f"TOTAL\t{totals['games']}\t{totals['bat']}\t{totals['pit']}\t{totals['unmapped']}\t{totals['seasons']} seasons")
    if not args.write:
        print("DRY RUN — nothing written.")
        return 0
    # ⚠️ Read back, per table: ON CONFLICT DO NOTHING skips silently, so the
    # writer's own count proves nothing. Stored must equal submitted on a
    # first load (a re-run adds 0 by design — compare against `before`).
    after = _counts()
    ok = True
    for t, key in (("postseason_batting_gamelogs", "bat"), ("postseason_pitching_gamelogs", "pit")):
        added = after[t] - before[t]
        print(f"{t}: before {before[t]}, after {after[t]}, added {added}, submitted {totals[key]}")
        ok &= added == totals[key] or added == 0
    if not ok:
        print("⚠️ STORED ≠ SUBMITTED — rows were skipped by a conflict. Investigate before trusting the load.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
