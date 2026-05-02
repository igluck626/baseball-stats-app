"""Baseball stats API."""

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
from database import connection          # noqa: E402
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


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
