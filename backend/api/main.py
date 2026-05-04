"""Baseball stats API."""

import datetime
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import schedule
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("nightly-scheduler")

import sys

import data_service

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# database imports work after data_service is imported (it adds backend/ to sys.path)
from database import connection, crud                       # noqa: E402
from database.models import (                                # noqa: E402
    PitcherSeason, PlayerAllstar, PlayerAward, PlayerFielding,
    PlayerPostseasonBatting, PlayerPostseasonPitching,
    PlayerSeason, TeamSeason,
)

# scripts/ holds the Lahman loader, WAR backfill, and nightly update logic;
# expose them so /admin endpoints can drive the same pipeline as the CLI.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import backfill_war                                         # noqa: E402
import lahman_load                                          # noqa: E402
import nightly_update                                       # noqa: E402
import reset_db                                             # noqa: E402

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ---------------------------------------------------------------------------
# Bulk-load state (shared between background thread and status endpoint)
# ---------------------------------------------------------------------------
# Top-level "phase" tracks where we are in the three-step pipeline:
#   "lahman"          → Phase 0: Lahman archive (every completed season)
#   "war_backfill"    → Phase 1: bwar_bat / bwar_pitch fills WAR / OPS+ / etc.
#   "current_season"  → Phase 2: pybaseball pulls the in-flight season
#   "done"
# The nested "lahman" / "war" dicts get the live progress from the sub-runners.
_bulk_state: dict = {
    "running":  False,
    "phase":    None,
    "lahman":   {},
    "war":      {},
    "current":  {},
    "error":    None,
    "last_run": None,
}
_bulk_lock = threading.Lock()


def _run_bulk_load() -> None:
    """Background thread: Lahman → WAR backfill → current-season fetch."""
    with _bulk_lock:
        _bulk_state.update(
            running=True, phase=None,
            lahman={}, war={}, current={},
            error=None,
        )

    try:
        with _bulk_lock:
            _bulk_state["phase"] = "lahman"
        lahman_load.run(state=_bulk_state["lahman"], lock=_bulk_lock)

        with _bulk_lock:
            _bulk_state["phase"] = "war_backfill"
        backfill_war.run(state=_bulk_state["war"], lock=_bulk_lock)

        with _bulk_lock:
            _bulk_state["phase"] = "current_season"
        current_year = data_service._current_year()
        bat_u, bat_s, bat_f = nightly_update._update_batters(current_year)
        pit_u, pit_s, pit_f = nightly_update._update_pitchers(current_year)
        with _bulk_lock:
            _bulk_state["current"] = {
                "year":              current_year,
                "batters_updated":   bat_u,
                "batters_skipped":   bat_s,
                "batters_failed":    len(bat_f),
                "pitchers_updated":  pit_u,
                "pitchers_skipped":  pit_s,
                "pitchers_failed":   len(pit_f),
            }

        with _bulk_lock:
            _bulk_state["phase"] = "done"
    except Exception as exc:
        with _bulk_lock:
            _bulk_state["error"] = str(exc)
    finally:
        with _bulk_lock:
            _bulk_state["running"]  = False
            _bulk_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Lahman-load state (shared between background thread and status endpoint)
# ---------------------------------------------------------------------------
# Phases: "bridge" → "snapshot" → "batting" → "pitching" → "people" → "done"
_lahman_state: dict = {
    "running":  False,
    "phase":    None,
    "batting_loaded":           0,
    "batting_rows_total":       0,
    "batting_skipped_existing": 0,
    "batting_skipped_no_id":    0,
    "pitching_loaded":          0,
    "pitching_rows_total":      0,
    "pitching_skipped_existing": 0,
    "pitching_skipped_no_id":   0,
    "players_written":  0,
    "pitchers_written": 0,
    "error":    None,
    "last_run": None,        # ISO-8601 UTC timestamp of the last completed run
}
_lahman_lock = threading.Lock()


def _run_lahman_load() -> None:
    """Background thread: run the Lahman loader, reporting progress to _lahman_state."""
    with _lahman_lock:
        _lahman_state.update(
            running=True, phase=None,
            batting_loaded=0, batting_rows_total=0,
            batting_skipped_existing=0, batting_skipped_no_id=0,
            pitching_loaded=0, pitching_rows_total=0,
            pitching_skipped_existing=0, pitching_skipped_no_id=0,
            players_written=0, pitchers_written=0,
            error=None,
        )

    try:
        lahman_load.run(state=_lahman_state, lock=_lahman_lock)
    except Exception as exc:
        with _lahman_lock:
            _lahman_state["error"] = str(exc)
    finally:
        with _lahman_lock:
            _lahman_state["running"]  = False
            _lahman_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# WAR-backfill state (shared between background thread and status endpoint)
# ---------------------------------------------------------------------------
# Phases: "bridge" → "batting_lookup" → "batting" → "pitching_lookup" → "pitching" → "done"
_war_state: dict = {
    "running":  False,
    "phase":    None,
    "batting_total":    0,
    "batting_updated":  0,
    "batting_no_match": 0,
    "pitching_total":    0,
    "pitching_updated":  0,
    "pitching_no_match": 0,
    "error":    None,
    "last_run": None,
}
_war_lock = threading.Lock()


def _run_backfill_war() -> None:
    """Background thread: run the WAR backfill, reporting progress to _war_state."""
    with _war_lock:
        _war_state.update(
            running=True, phase=None,
            batting_total=0, batting_updated=0, batting_no_match=0,
            pitching_total=0, pitching_updated=0, pitching_no_match=0,
            error=None,
        )

    try:
        backfill_war.run(state=_war_state, lock=_war_lock)
    except Exception as exc:
        with _war_lock:
            _war_state["error"] = str(exc)
    finally:
        with _war_lock:
            _war_state["running"]  = False
            _war_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Nightly-update state (shared between background thread and status endpoint)
# ---------------------------------------------------------------------------
_nightly_state: dict = {
    "running":  False,
    "phase":    None,        # "batters" | "pitchers" | None
    "updated":  0,
    "skipped":  0,
    "failed":   0,
    "total":    0,
    "error":    None,
    "last_run": None,        # ISO-8601 UTC timestamp of the last completed run
}
_nightly_lock = threading.Lock()


def _build_nightly_batter_entry(player_id: int, bref_df, bwar_current, current_year: int):
    """Build a player_seasons dict for the current year, or None if no data."""
    player_bref = bref_df[bref_df["mlbID"] == player_id]
    player_war = (
        bwar_current[bwar_current["mlb_ID"] == float(player_id)]
        .sort_values("stint_ID")
    )

    if player_bref.empty and player_war.empty:
        return None

    entry: dict = {"year": current_year, "team": None, "league": None}

    if not player_war.empty:
        group    = player_war
        total_pa = group["PA"].sum()
        ops_plus = (
            float((group["OPS_plus"] * group["PA"]).sum() / total_pa)
            if total_pa > 0 and not group["OPS_plus"].dropna().empty
            else None
        )
        raw_team = str(group.iloc[-1]["team_ID"])
        entry.update({
            "team":           data_service._TEAM_DISPLAY.get(raw_team, raw_team),
            "league":         str(group.iloc[-1]["lg_ID"]),
            "WAR":            round(float(group["WAR"].sum()), 2),
            "WAR_off":        round(float(group["WAR_off"].sum()), 2),
            "WAR_def":        round(float(group["WAR_def"].sum()), 2),
            "WAA":            round(float(group["WAA"].sum()), 2),
            "OPS_plus":       round(ops_plus, 1) if ops_plus is not None else None,
            "runs_above_avg": round(float(group["runs_above_avg"].sum()), 2),
            "runs_above_rep": round(float(group["runs_above_rep"].sum()), 2),
        })

    if not player_bref.empty:
        br = player_bref.iloc[0]
        entry["team"] = str(br["Tm"])
        entry.update({
            "G":       data_service._safe(br["G"]),
            "PA":      data_service._safe(br["PA"]),
            "AB":      data_service._safe(br["AB"]),
            "R":       data_service._safe(br["R"]),
            "H":       data_service._safe(br["H"]),
            "doubles": data_service._safe(br["2B"]),
            "triples": data_service._safe(br["3B"]),
            "HR":      data_service._safe(br["HR"]),
            "RBI":     data_service._safe(br["RBI"]),
            "BB":      data_service._safe(br["BB"]),
            "SO":      data_service._safe(br["SO"]),
            "SB":      data_service._safe(br["SB"]),
            "CS":      data_service._safe(br["CS"]),
            "BA":      data_service._safe(br["BA"]),
            "OBP":     data_service._safe(br["OBP"]),
            "SLG":     data_service._safe(br["SLG"]),
            "OPS":     data_service._safe(br["OPS"]),
            **data_service._batting_derived(br),
        })

    return entry


def _build_nightly_pitcher_entry(player_id: int, bref_df, bwar_current, current_year: int):
    """Build a pitcher_seasons dict for the current year, or None if no data."""
    player_bref = bref_df[bref_df["mlbID"] == str(player_id)]
    player_war = (
        bwar_current[bwar_current["mlb_ID"] == float(player_id)]
        .sort_values("stint_ID")
        if "stint_ID" in bwar_current.columns
        else bwar_current[bwar_current["mlb_ID"] == float(player_id)]
    )

    if player_bref.empty and player_war.empty:
        return None

    return data_service._build_pitcher_season_entry(
        player_id, current_year, player_war, bref_df,
    )


def _nightly_phase(
    fetch_bref,
    fetch_bwar_all,
    get_ids,
    save_seasons,
    build_entry,
    phase_name: str,
    current_year: int,
) -> None:
    """One phase of the nightly update (batters or pitchers)."""
    with _nightly_lock:
        _nightly_state.update(
            phase=phase_name, updated=0, skipped=0, failed=0, total=0
        )

    bref_df      = fetch_bref(current_year)
    bwar_df      = fetch_bwar_all()
    bwar_current = bwar_df[bwar_df["year_ID"] == current_year]

    with connection.get_session() as db:
        ids: list[int] = get_ids(db)

    with _nightly_lock:
        _nightly_state["total"] = len(ids)

    for player_id in ids:
        try:
            entry = build_entry(player_id, bref_df, bwar_current, current_year)
            if entry is None:
                with _nightly_lock:
                    _nightly_state["skipped"] += 1
                continue
            with connection.get_session() as db:
                save_seasons(db, player_id, [entry])
            with _nightly_lock:
                _nightly_state["updated"] += 1
        except Exception:
            with _nightly_lock:
                _nightly_state["failed"] += 1


def _run_nightly_update() -> None:
    """Background thread: refresh current-season stats for batters and pitchers."""
    with _nightly_lock:
        _nightly_state.update(
            running=True, phase=None, updated=0, skipped=0,
            failed=0, total=0, error=None,
        )

    try:
        current_year = data_service._current_year()
        _nightly_phase(
            data_service._batting_bref,
            data_service._bwar_bat_all,
            crud.get_all_player_ids,
            crud.save_player_seasons,
            _build_nightly_batter_entry,
            "batters",
            current_year,
        )
        _nightly_phase(
            data_service._pitching_bref,
            data_service._bwar_pitch_all,
            crud.get_all_pitcher_ids,
            crud.save_pitcher_seasons,
            _build_nightly_pitcher_entry,
            "pitchers",
            current_year,
        )
    except Exception as exc:
        with _nightly_lock:
            _nightly_state["error"] = str(exc)
    finally:
        with _nightly_lock:
            _nightly_state["running"]  = False
            _nightly_state["phase"]    = None
            _nightly_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

def _scheduled_nightly_trigger() -> None:
    """schedule callback: kick off nightly update in its own thread.

    The actual work runs in a separate daemon thread so the scheduler loop
    isn't blocked for the duration of the update (which can take several
    minutes). If a previous run is still in flight, this skip-and-logs.
    """
    with _nightly_lock:
        already_running = _nightly_state["running"]

    if already_running:
        log.warning("Scheduled nightly update fired but previous run is still in flight — skipping")
        return

    log.info("Scheduled nightly update firing (4:00 UTC)")
    threading.Thread(target=_nightly_with_completion_log, daemon=True,
                     name="nightly-update").start()


def _nightly_with_completion_log() -> None:
    """Wrap _run_nightly_update to log when it finishes."""
    started = datetime.datetime.utcnow()
    try:
        _run_nightly_update()
    finally:
        elapsed = (datetime.datetime.utcnow() - started).total_seconds()
        with _nightly_lock:
            err = _nightly_state.get("error")
        if err:
            log.error(f"Scheduled nightly update FAILED after {elapsed:.0f}s: {err}")
        else:
            log.info(f"Scheduled nightly update completed in {elapsed:.0f}s")


def _scheduler_loop() -> None:
    """Daemon-thread loop: register the nightly job and tick every 60s.

    The schedule library matches by local clock; we run UTC explicitly via the
    `at(..., "UTC")` argument so the job fires at 04:00 UTC regardless of the
    container's TZ setting.
    """
    schedule.every().day.at("04:00", "UTC").do(_scheduled_nightly_trigger)
    log.info("Nightly update scheduler started — runs daily at 04:00 UTC")
    while True:
        try:
            schedule.run_pending()
        except Exception as exc:
            log.error(f"Scheduler tick raised: {exc}")
        time.sleep(60)


def _start_scheduler() -> None:
    threading.Thread(target=_scheduler_loop, daemon=True,
                     name="nightly-scheduler").start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_service.init_db()
    _start_scheduler()
    yield


app = FastAPI(title="Baseball Stats API", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Player endpoints
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/players/search")
def search_players(name: str = Query(..., min_length=2, description="Player name")):
    results = data_service.search_player(name)
    if not results:
        raise HTTPException(status_code=404, detail=f"No players found matching '{name}'")
    return {"query": name, "results": results}


@app.get("/players/{player_id}/stats/current")
def current_stats(player_id: int):
    stats = data_service.get_current_stats(player_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No current season stats found for player_id {player_id}",
        )
    return stats


@app.get("/players/{player_id}/stats/career")
def career_stats(player_id: int):
    stats = data_service.get_career_stats(player_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No career stats found for player_id {player_id}",
        )
    return stats


@app.get("/players/{player_id}/pitching/current")
def current_pitching(player_id: int):
    stats = data_service.get_current_pitching_stats(player_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No current season pitching stats found for player_id {player_id}",
        )
    return stats


@app.get("/players/{player_id}/pitching/career")
def career_pitching(player_id: int):
    stats = data_service.get_career_pitching_stats(player_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No career pitching stats found for player_id {player_id}",
        )
    return stats


# ---------------------------------------------------------------------------
# Fielding, awards, postseason
# ---------------------------------------------------------------------------

@app.get("/players/{player_id}/fielding")
def player_fielding(player_id: int):
    rows = data_service.get_fielding(player_id)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No fielding data found for player_id {player_id}",
        )
    return {"player_id": player_id, "fielding": rows}


@app.get("/players/{player_id}/awards")
def player_awards(player_id: int):
    awards  = data_service.get_awards(player_id)
    allstar = data_service.get_allstar(player_id)
    if not awards and not allstar:
        raise HTTPException(
            status_code=404,
            detail=f"No awards or All-Star appearances found for player_id {player_id}",
        )
    return {"player_id": player_id, "awards": awards, "allstar": allstar}


@app.get("/players/{player_id}/postseason/batting")
def player_postseason_batting(player_id: int):
    rows = data_service.get_postseason_batting(player_id)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No postseason batting found for player_id {player_id}",
        )
    return {"player_id": player_id, "postseason": rows}


@app.get("/players/{player_id}/postseason/pitching")
def player_postseason_pitching(player_id: int):
    rows = data_service.get_postseason_pitching(player_id)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No postseason pitching found for player_id {player_id}",
        )
    return {"player_id": player_id, "postseason": rows}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

@app.get("/teams/standings")
def team_standings(year: int = Query(..., description="Season year, e.g. 2024")):
    rows = data_service.get_team_standings(year)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No team data for year {year}")
    return {"year": year, "standings": rows}


@app.get("/teams/{team_id}/history")
def team_history(team_id: str):
    rows = data_service.get_team_history(team_id)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No franchise history found for team_id {team_id!r}",
        )
    return {"team_id": team_id, "history": rows}


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/env-check")
def env_check():
    return {"DATABASE_URL_set": bool(os.getenv("DATABASE_URL"))}


@app.post("/admin/reset-db")
def admin_reset_db():
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    deleted = reset_db.clear_all()
    return {"status": "done", "deleted": deleted}


@app.post("/admin/migrate")
def admin_migrate():
    """Run the schema migration: create missing tables/indexes and add any
    missing bio columns to existing players/pitchers tables. Idempotent."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    summary = connection.init_db()
    return {"status": "done", **summary}


@app.post("/admin/bulk-load")
def start_bulk_load():
    with _bulk_lock:
        if _bulk_state["running"]:
            return {"status": "already_running", **_bulk_state}

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    t = threading.Thread(target=_run_bulk_load, daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/admin/bulk-load/status")
def bulk_load_status():
    counts: dict[str, int] = {}
    if connection.db_available():
        try:
            with connection.get_session() as db:
                counts = {
                    "player_seasons":             db.query(PlayerSeason).count(),
                    "pitcher_seasons":            db.query(PitcherSeason).count(),
                    "player_fielding":            db.query(PlayerFielding).count(),
                    "player_awards":              db.query(PlayerAward).count(),
                    "player_allstar":             db.query(PlayerAllstar).count(),
                    "player_postseason_batting":  db.query(PlayerPostseasonBatting).count(),
                    "player_postseason_pitching": db.query(PlayerPostseasonPitching).count(),
                    "team_seasons":               db.query(TeamSeason).count(),
                    "batters_in_db":              db.query(PlayerSeason.player_id).distinct().count(),
                    "pitchers_in_db":             db.query(PitcherSeason.player_id).distinct().count(),
                }
        except Exception:
            pass

    with _bulk_lock:
        state = dict(_bulk_state)

    return {"counts": counts, **state}


@app.post("/admin/backfill-war")
def start_backfill_war():
    with _war_lock:
        if _war_state["running"]:
            return {"status": "already_running", **_war_state}

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    t = threading.Thread(target=_run_backfill_war, daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/admin/backfill-war/status")
def backfill_war_status():
    with _war_lock:
        return dict(_war_state)


@app.post("/admin/lahman-load")
def start_lahman_load():
    with _lahman_lock:
        if _lahman_state["running"]:
            return {"status": "already_running", **_lahman_state}

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    t = threading.Thread(target=_run_lahman_load, daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/admin/lahman-load/status")
def lahman_load_status():
    with _lahman_lock:
        return dict(_lahman_state)


@app.post("/admin/nightly-update")
def start_nightly_update():
    with _nightly_lock:
        if _nightly_state["running"]:
            return {"status": "already_running", **_nightly_state}

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    t = threading.Thread(target=_run_nightly_update, daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/admin/nightly-update/status")
def nightly_update_status():
    with _nightly_lock:
        state = dict(_nightly_state)
    return state


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
