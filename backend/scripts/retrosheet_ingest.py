"""Ingest the committed, off-Railway-aggregated Retrosheet CSVs.

Retrosheet becomes authoritative for <= previous-season RAW COUNTING stats,
OVERWRITING both Lahman (pre-2008) and BDL (2008+) counting on the combined
player_seasons / pitcher_seasons rows — while the additive ON CONFLICT upsert
preserves the separate BRef WAR/OPS+ column-overlay (those columns are never
in our dict). Rates are computed INLINE here, mirroring lahman_load's
_batting_derived / _pitching_derived exactly. The per-team stint CSVs land in
the companion player_season_stints / pitcher_season_stints tables.

Source of truth for identity/mapping is already baked into the CSVs (MLBAM
player_id via the Chadwick register bridge); this module only reads + upserts.
"""

import csv
import datetime
import logging
import os
import sys
from collections import defaultdict
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup — mirror lahman_load so `database` + api imports resolve.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

from database import connection, crud                                      # noqa: E402
from database.models import PitcherSeason, PlayerSeason, TeamSeason        # noqa: E402
import lahman_load                                                         # noqa: E402  (reuse the derived-rate helpers)

RETRO_DIR   = os.path.join(_BACKEND_DIR, "data", "retrosheet")
BAT_SEASONS = os.path.join(RETRO_DIR, "retrosheet_batting_seasons.csv")
PIT_SEASONS = os.path.join(RETRO_DIR, "retrosheet_pitching_seasons.csv")
BAT_STINTS  = os.path.join(RETRO_DIR, "retrosheet_batting_stints.csv")
PIT_STINTS  = os.path.join(RETRO_DIR, "retrosheet_pitching_stints.csv")

# Never touch the live, in-progress season — BDL owns it. Rows at or beyond
# this year are skipped even if a CSV somehow contains them.
CURRENT_YEAR = datetime.date.today().year
_SAVE_BATCH  = 200      # players per DB transaction (combined seasons)
_STINT_BATCH = 1000     # stint rows per DB transaction

log = logging.getLogger(__name__)


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


def _f(v) -> float:
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _effective_pa(raw_pa: int, ab: int, bb: int, hbp: int, sf: int, sh: int) -> int:
    """PA to store: the RAW recorded B_PA when present, else the derived
    identity AB+BB+HBP+SF+SH. Retrosheet doesn't record B_PA pre-modern (~1957
    back), so raw would be 0 there; the fallback keeps PA sensible. Applied
    IDENTICALLY to combined and stint rows so combined PA == sum(stint PA)."""
    return raw_pa if raw_pa > 0 else (ab + bb + hbp + sf + sh)


def _set_state(state, lock, **kw):
    if state is None:
        return
    if lock is not None:
        with lock:
            state.update(kw)
    else:
        state.update(kw)


def _load_league_map(db) -> dict:
    """{(team_id, year): league} from team_seasons — used to fill league on
    NEW rows only (Retrosheet team.key ~ Lahman teamID; best-effort)."""
    out = {}
    for t in db.query(TeamSeason.team_id, TeamSeason.year, TeamSeason.league).all():
        if t.league:
            out[(t.team_id, t.year)] = t.league
    return out


# ---------------------------------------------------------------------------
# PART B — combined-season ingest (OVERWRITE player_seasons / pitcher_seasons)
# ---------------------------------------------------------------------------

def _ingest_batting_seasons(year, league_map, existing, state, lock) -> dict:
    by_pid: dict[int, list[dict]] = defaultdict(list)
    new = over = skipped_year = 0
    with open(BAT_SEASONS, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            yr = _i(r["year"])
            if yr >= CURRENT_YEAR:
                skipped_year += 1
                continue
            if year is not None and yr != year:
                continue
            pid = _i(r["player_id"]); team = r["team"] or None
            ab = _i(r["AB"]); h = _i(r["H"]); doubles = _i(r["doubles"])
            triples = _i(r["triples"]); hr = _i(r["HR"]); bb = _i(r["BB"])
            ibb = _i(r["IBB"]); hbp = _i(r["HBP"]); so = _i(r["SO"])
            sf = _i(r["SF"]); sh = _i(r["SH"])
            derived = lahman_load._batting_derived(
                ab=ab, h=h, doubles=doubles, triples=triples, hr=hr,
                bb=bb, ibb=ibb, hbp=hbp, so=so, sf=sf, sh=sh,
            )
            # Store the EFFECTIVE PA (raw B_PA when recorded, else the derived
            # identity — see _effective_pa) so combined PA == sum of stint PAs
            # AND dead-ball rows aren't 0. Recompute the two PA-denominator rates
            # from it; OBP/SLG/OPS/ISO/BABIP/wOBA don't use PA (they use explicit
            # ab+bb+hbp+sf) so they're untouched.
            pa = _effective_pa(_i(r["PA"]), ab, bb, hbp, sf, sh)
            derived["PA"] = pa
            derived["BB_pct"] = round(bb / pa, 3) if pa > 0 else None
            derived["K_pct"]  = round(so / pa, 3) if pa > 0 else None
            season = {
                "year": yr, "team": team, "source": "retrosheet",
                "G": _i(r["G"]), "AB": ab, "R": _i(r["R"]), "H": h,
                "doubles": doubles, "triples": triples, "HR": hr,
                "RBI": _i(r["RBI"]), "BB": bb, "SO": so, "SB": _i(r["SB"]),
                "CS": _i(r["CS"]), "IBB": ibb, "HBP": hbp, "SH": sh, "SF": sf,
                "GIDP": _i(r["GIDP"]), "TB": _i(r["TB"]),
                **derived,   # PA(raw) + BA/OBP/SLG/OPS/BABIP/ISO/BB_pct/K_pct/wOBA
            }
            if (pid, yr) in existing:
                over += 1
            else:
                new += 1
                lg = league_map.get((team, yr))
                if lg:
                    season["league"] = lg   # NEW rows only; existing keep theirs
            by_pid[pid].append(season)

    pids = list(by_pid.keys())
    saved = 0
    for i in range(0, len(pids), _SAVE_BATCH):
        with connection.get_session() as db:
            for pid in pids[i:i + _SAVE_BATCH]:
                crud.save_player_seasons(db, pid, by_pid[pid])
                saved += len(by_pid[pid])
        _set_state(state, lock, batting_upserted=saved)
    return {"upserted": saved, "new": new, "overwritten": over,
            "skipped_current_year": skipped_year}


def _ingest_pitching_seasons(year, league_map, existing, state, lock) -> dict:
    by_pid: dict[int, list[dict]] = defaultdict(list)
    new = over = skipped_year = 0
    with open(PIT_SEASONS, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            yr = _i(r["year"])
            if yr >= CURRENT_YEAR:
                skipped_year += 1
                continue
            if year is not None and yr != year:
                continue
            pid = _i(r["player_id"]); team = r["team"] or None
            ipouts = round(_f(r["IP"]) * 3)   # recover integer outs from decimal IP
            h = _i(r["H"]); hr = _i(r["HR"]); bb = _i(r["BB"]); hbp = _i(r["HBP"])
            so = _i(r["SO"]); bfp = _i(r["BFP"]); er = _i(r["ER"])
            sh = _i(r["SH"]); sf = _i(r["SF"])
            derived = lahman_load._pitching_derived(
                ipouts=ipouts, h=h, hr=hr, bb=bb, hbp=hbp, so=so, bfp=bfp,
            )
            # Rates above are computed from full-precision ipouts/3; store the
            # RAW decimal IP from the CSV (e.g. 75.333) instead of the helper's
            # 1-decimal round, so combined IP == sum of the player's stint IPs.
            derived["IP"] = _f(r["IP"])
            ip_dec = ipouts / 3 if ipouts > 0 else 0.0
            era = round(er * 9 / ip_dec, 2) if ip_dec > 0 else None
            ab_faced = bfp - bb - hbp - sh - sf
            baopp = round(h / ab_faced, 3) if ab_faced > 0 else None
            season = {
                "year": yr, "team": team, "source": "retrosheet",
                "W": _i(r["W"]), "L": _i(r["L"]), "G": _i(r["G"]),
                "GS": _i(r["GS"]), "SO": so, "BB": bb, "HR": hr,
                "CG": _i(r["CG"]), "SHO": _i(r["SHO"]), "SV": _i(r["SV"]),
                "H": h, "ER": er, "R": _i(r["R"]), "IBB": _i(r["IBB"]),
                "WP": _i(r["WP"]), "HBP": hbp, "BK": _i(r["BK"]), "BFP": bfp,
                "GF": _i(r["GF"]), "SH": sh, "SF": sf, "GIDP": _i(r["GIDP"]),
                "ERA": era, "BAOpp": baopp,
                **derived,   # IP + WHIP/FIP/K_per9/BB_per9/HR_per9/BABIP
            }
            if (pid, yr) in existing:
                over += 1
            else:
                new += 1
                lg = league_map.get((team, yr))
                if lg:
                    season["league"] = lg
            by_pid[pid].append(season)

    pids = list(by_pid.keys())
    saved = 0
    for i in range(0, len(pids), _SAVE_BATCH):
        with connection.get_session() as db:
            for pid in pids[i:i + _SAVE_BATCH]:
                crud.save_pitcher_seasons(db, pid, by_pid[pid])
                saved += len(by_pid[pid])
        _set_state(state, lock, pitching_upserted=saved)
    return {"upserted": saved, "new": new, "overwritten": over,
            "skipped_current_year": skipped_year}


# ---------------------------------------------------------------------------
# PART C — per-team stint ingest (companion tables)
# ---------------------------------------------------------------------------

_BAT_STINT_COLS = ["G", "PA", "AB", "R", "H", "doubles", "triples", "HR",
                   "RBI", "BB", "SO", "SB", "CS", "IBB", "HBP", "SH", "SF",
                   "GIDP", "TB"]
_PIT_STINT_COLS = ["W", "L", "G", "GS", "SO", "BB", "HR", "CG", "SHO", "SV",
                   "H", "ER", "R", "IBB", "WP", "HBP", "BK", "BFP", "GF",
                   "SH", "SF", "GIDP"]


def _ingest_stints(path, cols, ip, save_fn, year, state, lock, key,
                   pa_fallback=False) -> dict:
    rows: list[dict] = []
    skipped_year = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            yr = _i(r["year"])
            if yr >= CURRENT_YEAR:
                skipped_year += 1
                continue
            if year is not None and yr != year:
                continue
            row = {
                "player_id": _i(r["player_id"]), "year": yr, "team": r["team"],
                "stint_order": _i(r["stint_order"]), "source": "retrosheet",
            }
            for c in cols:
                row[c] = _i(r[c])
            if ip:
                row["IP"] = _f(r["IP"])
            if pa_fallback:
                # Same effective-PA rule as the combined rows, per stint, so the
                # summed stint PAs still equal the combined PA on dead-ball rows.
                row["PA"] = _effective_pa(_i(r["PA"]), row["AB"], row["BB"],
                                          row["HBP"], row["SF"], row["SH"])
            rows.append(row)

    saved = 0
    for i in range(0, len(rows), _STINT_BATCH):
        with connection.get_session() as db:
            save_fn(db, rows[i:i + _STINT_BATCH])
        saved += len(rows[i:i + _STINT_BATCH])
        _set_state(state, lock, **{key: saved})
    return {"upserted": len(rows), "skipped_current_year": skipped_year}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(year: Optional[int] = None, state=None, lock=None) -> dict:
    """Ingest the Retrosheet CSVs. year=None ingests all (<CURRENT_YEAR); a
    specific year restricts to that season (used for the dry run)."""
    connection.init_db()   # create_all makes the two new stint tables

    log.info("=" * 52)
    log.info("Retrosheet ingest — %s", f"year={year}" if year else "all years")
    log.info("=" * 52)

    _set_state(state, lock, phase="snapshot")
    with connection.get_session() as db:
        league_map = _load_league_map(db)
        existing_bat = {(r.player_id, r.year)
                        for r in db.query(PlayerSeason.player_id, PlayerSeason.year).all()}
        existing_pit = {(r.player_id, r.year)
                        for r in db.query(PitcherSeason.player_id, PitcherSeason.year).all()}

    _set_state(state, lock, phase="batting_seasons")
    bat = _ingest_batting_seasons(year, league_map, existing_bat, state, lock)
    _set_state(state, lock, phase="pitching_seasons")
    pit = _ingest_pitching_seasons(year, league_map, existing_pit, state, lock)

    _set_state(state, lock, phase="batting_stints")
    bat_stints = _ingest_stints(BAT_STINTS, _BAT_STINT_COLS, False,
                                crud.save_player_season_stints, year, state, lock,
                                "batting_stints_upserted", pa_fallback=True)
    _set_state(state, lock, phase="pitching_stints")
    pit_stints = _ingest_stints(PIT_STINTS, _PIT_STINT_COLS, True,
                                crud.save_pitcher_season_stints, year, state, lock,
                                "pitching_stints_upserted")

    summary = {
        "year": year,
        "batting_seasons": bat,
        "pitching_seasons": pit,
        "batting_stints": bat_stints,
        "pitching_stints": pit_stints,
    }
    _set_state(state, lock, phase="done", summary=summary)
    log.info("Retrosheet ingest done: %s", summary)
    return summary


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    p = argparse.ArgumentParser(description="Ingest aggregated Retrosheet CSVs.")
    p.add_argument("--year", type=int, default=None, help="Ingest only this year (dry run).")
    args = p.parse_args()
    run(year=args.year)
