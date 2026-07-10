"""Backfill historical per-game game logs (1898-1999) from Retrosheet daybyday.

Runtime download + aggregate on Railway (NO committed CSV — the historical
daybyday is ~282 MB). For each year it pulls `playing-YYYY.csv` (per-player-
per-game) and `teams-YYYY.csv` (per-team-per-game, carries the final scores)
from the Chadwick retrosplits GitHub mirror, dedupes multi-source rows the same
way the season ingest does (evt>box>ded, appearance key), maps the retro person
id to MLBAM via the committed bridge, and writes one gamelog row per appearance
into batting_gamelogs / pitching_gamelogs.

game_id = "retro-" + game.key (e.g. "retro-BOS195004180") — the prefix keeps
these distinct from the BDL (bare id) and MLB Stats API ("mlb-…") rows, so the
(player_id, game_id) PK never collides and the write is ON CONFLICT DO NOTHING.
"""

import csv
import datetime
import io
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

from database import connection, crud                                      # noqa: E402
from database.models import BattingGameLog, PitchingGameLog                # noqa: E402

_BRIDGE = os.path.join(_BACKEND_DIR, "data", "retrosheet", "chadwick_retro_bridge.csv")
_BASE = "https://raw.githubusercontent.com/chadwickbureau/retrosplits/master/daybyday"
_PLAYING_URL = _BASE + "/playing-{year}.csv"
_TEAMS_URL = _BASE + "/teams-{year}.csv"

_SRC = {"evt": 3, "box": 2, "ded": 1}
_BATCH = 2000    # gamelog rows per INSERT statement

log = logging.getLogger(__name__)

# Any batting-outcome column > 0 marks a batting appearance (B_G excluded — it's
# 1 even on a pure pitching appearance). Mirrors the season-ingest dead-ball gate.
_BAT_SIGNAL = ["B_PA", "B_AB", "B_R", "B_H", "B_2B", "B_3B", "B_HR", "B_RBI",
               "B_BB", "B_SO", "B_SB", "B_CS", "B_HP", "B_SH", "B_SF", "B_GDP",
               "B_IBB"]


def _i(v) -> int:
    if v in (None, ""):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


def _io(v):
    """int-or-None — for era-gated fields (pitches/strikes) where an unrecorded
    value should stay NULL, not read as 0."""
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _effective_pa(raw_pa: int, ab: int, bb: int, hbp: int, sf: int, sh: int) -> int:
    """Raw B_PA when recorded, else the AB+BB+HBP+SF+SH identity (Retrosheet
    doesn't record B_PA pre-~1957). Same rule as the season ingest."""
    return raw_pa if raw_pa > 0 else (ab + bb + hbp + sf + sh)


def _pitcher_decision(r) -> str:
    """The PITCHER's decision from the daybyday per-game flags — this is what
    the app stores in a pitching row's `result` (the DEC column + season W-L),
    NOT the team's win/loss. Matches the existing encoding (S/W/L/ND); daybyday
    has no holds pre-2000, so "H" never applies and a no-decision falls to ND."""
    if _i(r.get("P_W")) == 1:
        return "W"
    if _i(r.get("P_L")) == 1:
        return "L"
    if _i(r.get("P_SV")) == 1:
        return "S"
    return "ND"


def _set_state(state, lock, **kw):
    if state is None:
        return
    if lock is not None:
        with lock:
            state.update(kw)
    else:
        state.update(kw)


def _load_bridge() -> dict:
    bridge = {}
    with open(_BRIDGE, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r.get("key_retro") and r.get("key_mlbam"):
                try:
                    bridge[r["key_retro"]] = int(r["key_mlbam"])
                except (TypeError, ValueError):
                    continue
    return bridge


def _download_csv(url: str, retries: int = 5) -> Optional[str]:
    """Fetch a daybyday CSV with exponential backoff on 429/5xx/transient
    errors. Returns None on a genuine 404 (missing year) or after exhausting
    retries (caller logs + continues)."""
    delay = 2.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "baseball-stats-app/retrosheet-gamelogs"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            log.warning("  %s -> HTTP %s (attempt %d/%d)", url, exc.code, attempt + 1, retries)
        except Exception as exc:  # noqa: BLE001 - network transient
            log.warning("  %s -> %s (attempt %d/%d)", url, exc, attempt + 1, retries)
        time.sleep(delay)
        delay = min(delay * 2, 30.0)
    return None


def _build_score_map(teams_text: str) -> dict:
    """{(game.key, team.key): (team_score, opp_score, result)} from teams-YYYY.
    B_R is the team's runs; result from the R_W/R_L/R_T flags."""
    by_game: dict[str, dict] = defaultdict(dict)
    for r in csv.DictReader(io.StringIO(teams_text)):
        if r.get("season.phase") != "R":
            continue
        by_game[r["game.key"]][r["team.key"]] = (
            _i(r.get("B_R")), r.get("R_W"), r.get("R_L"), r.get("R_T"),
        )
    out: dict = {}
    for gk, teams in by_game.items():
        for t, (runs, w, l, ti) in teams.items():
            opp_runs = next((rr[0] for t2, rr in teams.items() if t2 != t), None)
            result = "W" if w == "1" else "L" if l == "1" else "T" if ti == "1" else None
            out[(gk, t)] = (runs, opp_runs, result)
    return out


def _parse_date(s: str):
    try:
        return datetime.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _ingest_year(year: int, bridge: dict, state, lock,
                 bat_model=BattingGameLog, pit_model=PitchingGameLog,
                 appearance_gate: bool = False) -> dict:
    playing = _download_csv(_PLAYING_URL.format(year=year))
    if not playing:
        log.warning("year %d: playing file missing/unreachable — skipped", year)
        return {"year": year, "status": "missing", "batting": 0, "pitching": 0, "unmapped": 0}

    teams = _download_csv(_TEAMS_URL.format(year=year))
    scores = _build_score_map(teams) if teams else {}
    if not teams:
        log.warning("year %d: teams file unreachable — scores left null", year)

    # Dedup multi-source rows to one per appearance (same rule as 3a).
    best: dict = {}
    for r in csv.DictReader(io.StringIO(playing)):
        if r.get("season.phase") != "R":
            continue
        rk = r.get("person.key"); gk = r.get("game.key")
        if not rk or not gk or len((r.get("game.date") or "")) < 4:
            continue
        pri = _SRC.get(r.get("game.source"), 0)
        key = (rk, gk, r.get("slot"), r.get("seq"))
        cur = best.get(key)
        if cur is None or pri > cur[0]:
            best[key] = (pri, r)

    bat_rows: list[dict] = []
    pit_rows: list[dict] = []
    unmapped = set()
    for _pri, r in best.values():
        mlbam = bridge.get(r["person.key"])
        if mlbam is None:
            unmapped.add(r["person.key"])
            continue
        gk = r["game.key"]; team = r.get("team.key")
        gd = _parse_date(r.get("game.date"))
        ha = "H" if r.get("team.alignment") == "1" else "A"
        team_score, opp_score, result = scores.get((gk, team), (None, None, None))
        gid = "retro-" + gk

        # Gate: appearance (B_G>0, includes 0-outcome reliever/sub/PR lines — a
        # SUPERSET of the DB's batting keys, used for the 2000-2025 replacement)
        # vs outcome (any batting stat > 0 — the leaner historical 1898-1999
        # default that drops empty appearances). In the appearance gate, still
        # drop EMPTY pitcher-appearance batting rows (B_G>0 but no batting stat
        # AND the player pitched this game) — those are the DH-era non-batting
        # pitcher lines the live data omits; keep empty non-pitcher subs.
        has_outcome = any(_i(r.get(c)) > 0 for c in _BAT_SIGNAL)
        pitched = _i(r.get("P_G")) > 0 or _i(r.get("P_OUT")) > 0
        if appearance_gate:
            # Drop empty pitcher-appearance rows only for 2022+ (universal DH):
            # pre-2022 live carries the same empty reliever rows, so keep them.
            bat_included = _i(r.get("B_G")) > 0 and (has_outcome or not pitched or year < 2022)
        else:
            bat_included = has_outcome
        if bat_included:
            pa = _effective_pa(_i(r.get("B_PA")), _i(r.get("B_AB")), _i(r.get("B_BB")),
                               _i(r.get("B_HP")), _i(r.get("B_SF")), _i(r.get("B_SH")))
            bat_rows.append({
                "player_id": mlbam, "game_id": gid, "game_date": gd, "season": year,
                "opponent": r.get("opponent.key"), "home_away": ha, "result": result,
                "team_score": team_score, "opp_score": opp_score,
                "PA": pa, "AB": _i(r.get("B_AB")), "R": _i(r.get("B_R")), "H": _i(r.get("B_H")),
                "doubles": _i(r.get("B_2B")), "triples": _i(r.get("B_3B")), "HR": _i(r.get("B_HR")),
                "RBI": _i(r.get("B_RBI")), "BB": _i(r.get("B_BB")), "IBB": _i(r.get("B_IBB")),
                "SO": _i(r.get("B_SO")), "SB": _i(r.get("B_SB")), "CS": _i(r.get("B_CS")),
                "HBP": _i(r.get("B_HP")), "SF": _i(r.get("B_SF")), "GIDP": _i(r.get("B_GDP")),
                "SH": _i(r.get("B_SH")), "LOB": None,   # per-player LOB not in daybyday
            })
        if _i(r.get("P_G")) > 0 or _i(r.get("P_OUT")) > 0:
            # result on a PITCHING row is the pitcher's DECISION (not the team
            # W/L that batting rows carry) — the app's DEC column + season W-L.
            pit_rows.append({
                "player_id": mlbam, "game_id": gid, "game_date": gd, "season": year,
                "opponent": r.get("opponent.key"), "home_away": ha,
                "result": _pitcher_decision(r),
                "IP": round(_i(r.get("P_OUT")) / 3, 3), "H": _i(r.get("P_H")),
                "R": _i(r.get("P_R")), "ER": _i(r.get("P_ER")), "BB": _i(r.get("P_BB")),
                "SO": _i(r.get("P_SO")), "HR": _i(r.get("P_HR")), "HBP": _i(r.get("P_HP")),
                "WP": _i(r.get("P_WP")),
                "pitches": _io(r.get("P_PITCH")), "strikes": _io(r.get("P_STRIKE")),
            })

    bw = _write(bat_model, bat_rows)
    pw = _write(pit_model, pit_rows)
    _set_state(state, lock, current_year=year)
    log.info("year %d: batting %d, pitching %d, unmapped %d", year, bw, pw, len(unmapped))
    return {"year": year, "status": "ok", "batting": bw, "pitching": pw, "unmapped": len(unmapped)}


def _write(model, rows: list[dict]) -> int:
    written = 0
    for i in range(0, len(rows), _BATCH):
        with connection.get_session() as db:
            written += crud.bulk_insert_gamelogs(db, model, rows[i:i + _BATCH])
    return written


def run(year_from: int = 1898, year_to: int = 1999, state=None, lock=None,
        bat_model=BattingGameLog, pit_model=PitchingGameLog,
        appearance_gate: bool = False) -> dict:
    connection.init_db()
    log.info("=" * 52)
    log.info("Retrosheet gamelog backfill — %d..%d (target=%s, appearance_gate=%s)",
             year_from, year_to, bat_model.__tablename__, appearance_gate)
    log.info("=" * 52)
    bridge = _load_bridge()

    per_year = []
    tot_bat = tot_pit = tot_unmapped = 0
    for year in range(year_from, year_to + 1):
        _set_state(state, lock, phase="ingesting", current_year=year)
        try:
            res = _ingest_year(year, bridge, state, lock,
                               bat_model=bat_model, pit_model=pit_model,
                               appearance_gate=appearance_gate)
        except Exception as exc:  # noqa: BLE001 - one bad year shouldn't kill the run
            log.exception("year %d FAILED: %s", year, exc)
            res = {"year": year, "status": "error", "error": str(exc),
                   "batting": 0, "pitching": 0, "unmapped": 0}
        per_year.append(res)
        tot_bat += res["batting"]; tot_pit += res["pitching"]; tot_unmapped += res["unmapped"]
        _set_state(state, lock, batting_written=tot_bat, pitching_written=tot_pit,
                   unmapped_skipped=tot_unmapped, years_done=len(per_year))

    summary = {
        "year_from": year_from, "year_to": year_to,
        "batting_written": tot_bat, "pitching_written": tot_pit,
        "unmapped_skipped": tot_unmapped,
        "years": per_year,
    }
    _set_state(state, lock, phase="done", summary=summary)
    log.info("Retrosheet gamelog backfill done: bat %d, pit %d", tot_bat, tot_pit)
    return summary


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    p = argparse.ArgumentParser(description="Backfill historical Retrosheet game logs.")
    p.add_argument("--from", dest="year_from", type=int, default=1898)
    p.add_argument("--to", dest="year_to", type=int, default=1999)
    args = p.parse_args()
    run(year_from=args.year_from, year_to=args.year_to)
