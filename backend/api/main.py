"""Baseball stats API."""

import datetime
import os
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

import data_service

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# database imports work after data_service is imported (it adds backend/ to sys.path)
from database import connection, crud    # noqa: E402
from database.models import PlayerSeason # noqa: E402

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ---------------------------------------------------------------------------
# Bulk-load state (shared between the background thread and status endpoint)
# ---------------------------------------------------------------------------
_bulk_state: dict = {
    "running": False,
    "loaded":  0,
    "skipped": 0,
    "failed":  0,
    "total":   0,
    "error":   None,
}
_bulk_lock = threading.Lock()


def _run_bulk_load() -> None:
    """Background thread: populate DB with historical stats for all players."""
    with _bulk_lock:
        _bulk_state.update(
            running=True, loaded=0, skipped=0, failed=0, total=0, error=None
        )

    try:
        bwar = data_service._bwar_bat_all()
        recent = bwar[bwar["year_ID"] >= 1990]
        all_ids: list[int] = sorted(
            int(pid) for pid in recent["mlb_ID"].dropna().unique() if pid > 0
        )

        with connection.get_session() as db:
            rows = db.query(PlayerSeason.player_id).distinct().all()
        already_loaded = {r.player_id for r in rows}

        to_process = [pid for pid in all_ids if pid not in already_loaded]

        with _bulk_lock:
            _bulk_state["total"] = len(to_process)

        current_year = data_service._current_year()

        for player_id in to_process:
            try:
                result = data_service.get_career_stats(player_id)
                with _bulk_lock:
                    if result is not None:
                        _bulk_state["loaded"] += 1
                    else:
                        _bulk_state["skipped"] += 1
            except Exception:
                with _bulk_lock:
                    _bulk_state["failed"] += 1

            time.sleep(2.0)

    except Exception as exc:
        with _bulk_lock:
            _bulk_state["error"] = str(exc)
    finally:
        with _bulk_lock:
            _bulk_state["running"] = False


# ---------------------------------------------------------------------------
# Nightly-update state (shared between background thread and status endpoint)
# ---------------------------------------------------------------------------
_nightly_state: dict = {
    "running":  False,
    "updated":  0,
    "skipped":  0,
    "failed":   0,
    "total":    0,
    "error":    None,
    "last_run": None,   # ISO-8601 UTC timestamp of the last completed run
}
_nightly_lock = threading.Lock()


def _build_nightly_entry(player_id: int, bref_df, bwar_current, current_year: int):
    """Build a player_seasons dict for the current year, or None if no data."""
    player_bref = bref_df[bref_df["mlbID"] == player_id]
    player_war  = (
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


def _run_nightly_update() -> None:
    """Background thread: refresh current-season stats for every player in the DB."""
    with _nightly_lock:
        _nightly_state.update(
            running=True, updated=0, skipped=0, failed=0, total=0, error=None
        )

    try:
        current_year = data_service._current_year()
        bref_df      = data_service._batting_bref(current_year)
        bwar_df      = data_service._bwar_bat_all()
        bwar_current = bwar_df[bwar_df["year_ID"] == current_year]

        with connection.get_session() as db:
            rows = db.query(PlayerSeason.player_id).distinct().all()
        player_ids: list[int] = [r.player_id for r in rows]

        with _nightly_lock:
            _nightly_state["total"] = len(player_ids)

        for player_id in player_ids:
            try:
                entry = _build_nightly_entry(player_id, bref_df, bwar_current, current_year)
                if entry is None:
                    with _nightly_lock:
                        _nightly_state["skipped"] += 1
                    continue
                with connection.get_session() as db:
                    crud.save_player_seasons(db, player_id, [entry])
                with _nightly_lock:
                    _nightly_state["updated"] += 1
            except Exception:
                with _nightly_lock:
                    _nightly_state["failed"] += 1

    except Exception as exc:
        with _nightly_lock:
            _nightly_state["error"] = str(exc)
    finally:
        with _nightly_lock:
            _nightly_state["running"]  = False
            _nightly_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    data_service.init_db()
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
# Admin endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/env-check")
def env_check():
    return {"DATABASE_URL_set": bool(os.getenv("DATABASE_URL"))}


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
    players_in_db = 0
    if connection.db_available():
        try:
            with connection.get_session() as db:
                players_in_db = db.query(PlayerSeason.player_id).distinct().count()
        except Exception:
            pass

    with _bulk_lock:
        state = dict(_bulk_state)

    return {"players_in_db": players_in_db, **state}


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
