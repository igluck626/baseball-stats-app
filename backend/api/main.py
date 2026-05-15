"""Baseball stats API."""

import datetime
import logging
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import inspect as _sa_inspect, text as _sa_text

import sys

import data_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# database imports work after data_service is imported (it adds backend/ to sys.path)
from database import connection, crud                       # noqa: E402
from database.models import (                                # noqa: E402
    BattingGameLog, PitcherSeason, PitchingGameLog,
    PlayerAllstar, PlayerAward, PlayerFielding,
    PlayerHof, PlayerPostseasonBatting, PlayerPostseasonPitching,
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
# Game-log historical bulk load
# ---------------------------------------------------------------------------
_gamelog_state: dict = {
    "running":         False,
    "current_season":  None,
    "current_player":  None,
    "players_done":    0,
    "players_total":   0,
    "games_saved":     0,
    "failed_players":  [],
    "error":           None,
    "last_run":        None,
}
_gamelog_lock = threading.Lock()
_GAMELOG_LOAD_SLEEP = 0.1  # pace MLB Stats API to avoid rate limits


def _run_gamelog_load(seasons: list[int], player_ids: list[int] | None) -> None:
    """Background runner: fetch and save batting + pitching gamelogs for the
    given (player_ids × seasons) cross-product. If player_ids is None,
    targets all current-roster players from players + pitchers tables."""
    with _gamelog_lock:
        _gamelog_state.update(
            running=True, current_season=None, current_player=None,
            players_done=0, players_total=0, games_saved=0,
            failed_players=[], error=None,
        )

    try:
        # Resolve target players. We track which IDs are batters vs pitchers
        # so we don't waste calls on the wrong group.
        with connection.get_session() as db:
            if player_ids is None:
                current_year = data_service._current_year()
                bat_ids = {
                    r.player_id for r in db.query(PlayerSeason.player_id)
                    .filter(PlayerSeason.year == current_year).distinct().all()
                }
                pit_ids = {
                    r.player_id for r in db.query(PitcherSeason.player_id)
                    .filter(PitcherSeason.year == current_year).distinct().all()
                }
            else:
                # Caller-provided list — fetch both groups for each (the
                # wrong-group call returns 0 games, no harm).
                bat_ids = set(player_ids)
                pit_ids = set(player_ids)

        all_ids = sorted(bat_ids | pit_ids)
        with _gamelog_lock:
            _gamelog_state["players_total"] = len(all_ids) * len(seasons)

        for season in seasons:
            with _gamelog_lock:
                _gamelog_state["current_season"] = season

            for pid in all_ids:
                with _gamelog_lock:
                    _gamelog_state["current_player"] = pid

                try:
                    if pid in bat_ids:
                        n = data_service.fetch_and_save_batting_gamelogs(pid, season)
                        with _gamelog_lock:
                            _gamelog_state["games_saved"] += n
                    if pid in pit_ids:
                        n = data_service.fetch_and_save_pitching_gamelogs(pid, season)
                        with _gamelog_lock:
                            _gamelog_state["games_saved"] += n
                except Exception as exc:
                    with _gamelog_lock:
                        _gamelog_state["failed_players"].append(
                            {"player_id": pid, "season": season, "error": str(exc)[:200]}
                        )

                with _gamelog_lock:
                    _gamelog_state["players_done"] += 1
                time.sleep(_GAMELOG_LOAD_SLEEP)

    except Exception as exc:
        with _gamelog_lock:
            _gamelog_state["error"] = str(exc)
    finally:
        with _gamelog_lock:
            _gamelog_state["running"]         = False
            _gamelog_state["current_season"]  = None
            _gamelog_state["current_player"]  = None
            _gamelog_state["last_run"]        = datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Nightly-update state (shared between background thread and status endpoint)
# ---------------------------------------------------------------------------
_nightly_state: dict = {
    "running":                  False,
    "phase":                    None,   # "batters" | "pitchers" | "standings" | "gamelogs" | None
    "updated":                  0,
    "skipped":                  0,
    "failed":                   0,
    "total":                    0,
    "standings_updated":        0,
    "standings_failed":         0,
    "gamelog_batters_updated":  0,      # batters whose gamelog fetch+save succeeded
    "gamelog_pitchers_updated": 0,
    "gamelog_failed":           0,      # combined batter + pitcher failures
    "error":                    None,
    "last_run":                 None,   # ISO-8601 UTC timestamp of last completed run
    "last_started":             None,   # ISO-8601 UTC timestamp of when current run began
}
_nightly_lock = threading.Lock()

# Auto-reset the running flag if it's been set this long without
# completion. Threshold is generous — a cold-cache full run including
# pybaseball fetches and 30k+ DB upserts is typically <30 min, so
# anything past 3h is almost certainly a SIGKILL'd thread that never
# reached the finally block.
_NIGHTLY_STALE_AFTER = datetime.timedelta(hours=3)


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
            "IBB":     data_service._safe_col(br, "IBB"),
            "HBP":     data_service._safe_col(br, "HBP"),
            "SH":      data_service._safe_col(br, "SH"),
            "SF":      data_service._safe_col(br, "SF"),
            "GIDP":    data_service._safe_col(br, "GIDP")
                       if "GIDP" in br.index
                       else data_service._safe_col(br, "GDP"),
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
    """Background thread: refresh current-season stats for batters, pitchers, and standings."""
    pid = os.getpid()
    tid = threading.get_ident()
    log.info(f"[nightly] thread entry pid={pid} tid={tid}")

    with _nightly_lock:
        _nightly_state.update(
            running=True, phase=None, updated=0, skipped=0,
            failed=0, total=0,
            standings_updated=0, standings_failed=0,
            gamelog_batters_updated=0, gamelog_pitchers_updated=0,
            gamelog_failed=0,
            error=None,
        )

    try:
        current_year = data_service._current_year()
        log.info(f"[nightly] starting batters phase, year={current_year}")
        _nightly_phase(
            data_service._batting_bref,
            data_service._bwar_bat_all,
            crud.get_all_player_ids,
            crud.save_player_seasons,
            _build_nightly_batter_entry,
            "batters",
            current_year,
        )
        log.info(
            f"[nightly] batters phase done: "
            f"updated={_nightly_state['updated']} "
            f"skipped={_nightly_state['skipped']} "
            f"failed={_nightly_state['failed']} "
            f"total={_nightly_state['total']}"
        )

        log.info("[nightly] starting pitchers phase")
        _nightly_phase(
            data_service._pitching_bref,
            data_service._bwar_pitch_all,
            crud.get_all_pitcher_ids,
            crud.save_pitcher_seasons,
            _build_nightly_pitcher_entry,
            "pitchers",
            current_year,
        )
        log.info(
            f"[nightly] pitchers phase done: "
            f"updated={_nightly_state['updated']} "
            f"skipped={_nightly_state['skipped']} "
            f"failed={_nightly_state['failed']} "
            f"total={_nightly_state['total']}"
        )

        with _nightly_lock:
            _nightly_state["phase"] = "standings"
        log.info("[nightly] starting standings phase")
        s_updated, s_failed = nightly_update._update_standings(current_year)
        with _nightly_lock:
            _nightly_state["standings_updated"] = s_updated
            _nightly_state["standings_failed"]  = s_failed
        log.info(f"[nightly] standings phase done: updated={s_updated} failed={s_failed}")

        with _nightly_lock:
            _nightly_state["phase"] = "gamelogs"
        log.info("[nightly] starting gamelogs phase")
        gl = nightly_update._update_gamelogs(current_year)
        with _nightly_lock:
            _nightly_state["gamelog_batters_updated"]  = gl["batters_processed"]
            _nightly_state["gamelog_pitchers_updated"] = gl["pitchers_processed"]
            _nightly_state["gamelog_failed"] = (
                gl["batters_failed"] + gl["pitchers_failed"]
            )
        log.info(
            f"[nightly] gamelogs phase done: "
            f"batters_processed={gl['batters_processed']} "
            f"pitchers_processed={gl['pitchers_processed']} "
            f"batter_rows={gl['batter_games_saved']} "
            f"pitcher_rows={gl['pitcher_games_saved']} "
            f"batters_failed={gl['batters_failed']} "
            f"pitchers_failed={gl['pitchers_failed']}"
        )
    except Exception as exc:
        # Log the full traceback so silent thread crashes are visible in
        # Railway's log stream. The previous handler stored only str(exc),
        # which loses the stack frame of the actual failure.
        tb = traceback.format_exc()
        log.error(f"[nightly] FAILED pid={pid} tid={tid}: {exc}\n{tb}")
        with _nightly_lock:
            _nightly_state["error"] = f"{exc}\n{tb}"
    finally:
        with _nightly_lock:
            _nightly_state["running"]  = False
            _nightly_state["phase"]    = None
            _nightly_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"
        log.info(f"[nightly] thread exit pid={pid} tid={tid} state={_nightly_state}")


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


@app.get("/players/by-mlb-id/{mlb_id}")
def player_by_mlb_id(mlb_id: int):
    """Direct lookup by MLB Stats API id. Used by the Scores tab —
    the live-feed box score names players by MLBAM id and we need to
    resolve those to our `PlayerSearchResult` shape so navigation
    into the existing player profile works without a search round
    trip. Returns the same payload `/players/search` rows have."""
    result = data_service.get_player_by_id(mlb_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No player found with MLB id {mlb_id}",
        )
    return result


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
    """Enriched awards payload — raw `awards` and `allstar` arrays
    (back-compat with the original shape) plus two derived blocks:

      • `headline_awards`: career counts for MVP / Cy Young / ROY /
        Gold Glove / Silver Slugger / All-Star. Zero-count entries
        are omitted so callers can iterate the dict directly.
      • `career_by_year`: one entry per season the player appeared
        in any awards source — carries that year's award wins,
        All-Star flag, and MVP/CY/ROY voting rank + points when
        present.
      • `award_shares`: raw vote-share rows backing the per-year
        `votes` arrays — useful for clients that want to render
        full voting context without re-deriving from the per-year
        block.
    """
    result = data_service.get_player_awards_full(player_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No awards or All-Star appearances found for player_id {player_id}",
        )
    return result


_AWARD_VOTING_IDS = {"MVP", "CY Young", "ROY"}


@app.get("/awards/voting")
def award_voting(
    award: str = Query(..., description="Award short code: 'MVP', 'CY Young', or 'ROY'"),
    year:  int = Query(..., description="Season year"),
    league: str = Query(..., description="League: 'AL', 'NL', or 'ML' for pre-1969 single-league votes"),
):
    """Full voting leaderboard for a single (award, year, league)
    triple. Each row carries a full PlayerSearchResult-shaped
    `player` block so the iOS row can render the same chrome as the
    leaderboard / search rows and navigation can push straight into
    PlayerProfile."""
    if award not in _AWARD_VOTING_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"award must be one of {sorted(_AWARD_VOTING_IDS)}",
        )
    if league not in ("AL", "NL", "ML"):
        raise HTTPException(
            status_code=400,
            detail="league must be 'AL', 'NL', or 'ML'",
        )
    response = data_service.get_award_voting(award_id=award, year=year, league=league)
    if response is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {award} voting results for {league} {year}",
        )
    return response


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


@app.get("/players/{player_id}/gamelogs/batting")
def player_gamelogs_batting(
    player_id: int,
    season: int | None = Query(None, description="Defaults to current year"),
    last_n:  int | None = Query(None, description="If set, return only the most recent N games"),
):
    """Per-game batting log for a player. Returns games in reverse chrono
    order plus a `splits` block with rolling-window aggregates (last 5 / 10
    / 15 / 30 / season). Auto-fetches from the MLB Stats API on cache miss
    for the requested season."""
    response = data_service.get_batting_gamelog_response(
        player_id, season=season, last_n=last_n,
    )
    if not response:
        raise HTTPException(
            status_code=404,
            detail=f"No batting gamelogs found for player_id {player_id}, season {season}",
        )
    return response


@app.get("/players/{player_id}/gamelogs/pitching")
def player_gamelogs_pitching(
    player_id: int,
    season: int | None = Query(None, description="Defaults to current year"),
    last_n:  int | None = Query(None, description="If set, return only the most recent N games"),
):
    response = data_service.get_pitching_gamelog_response(
        player_id, season=season, last_n=last_n,
    )
    if not response:
        raise HTTPException(
            status_code=404,
            detail=f"No pitching gamelogs found for player_id {player_id}, season {season}",
        )
    return response


@app.get("/players/{player_id}/headshot")
def player_headshot(player_id: int):
    """Return MLB Stats API headshot URL plus a generic-silhouette fallback.
    The primary URL automatically falls back server-side if MLB doesn't have
    a portrait for this player_id, so the fallback is rarely needed in
    practice — included for completeness."""
    return {
        "player_id":    player_id,
        "headshot_url": data_service._headshot_url(player_id),
        "fallback_url": data_service._HEADSHOT_FALLBACK_URL,
    }


@app.get("/players/{player_id}/hof")
def player_hof(player_id: int):
    """Hall of Fame summary + full voting history. is_hof is True if any
    ballot row has inducted=True; hof_year is the year of that ballot."""
    result = data_service.get_hof(player_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Hall of Fame ballot history found for player_id {player_id}",
        )
    return result


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

@app.get("/teams/standings")
def team_standings(year: int = Query(..., description="Season year, e.g. 2024")):
    rows = data_service.get_team_standings(year)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No team data for year {year}")

    # Latest last_updated across the rows. ISO-8601 + Z so it's unambiguous in
    # the response. None if no row carries a timestamp (older Lahman-only data).
    timestamps = [r.get("last_updated") for r in rows if r.get("last_updated") is not None]
    last_updated = max(timestamps).isoformat() + "Z" if timestamps else None

    return {"year": year, "last_updated": last_updated, "standings": rows}


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
# Leaderboards
# ---------------------------------------------------------------------------

# Stats accepted by the leaderboard endpoint. Must match the keys in
# data_service._LEADERBOARD_BATTING / _PITCHING. Surfaced here so the
# 400 error message stays in sync with what data_service knows.
_LEADERBOARD_BATTING_STATS  = {
    "HR", "AVG", "RBI", "OPS", "H", "R", "SB", "BB",
    "OBP", "SLG", "WAR", "2B", "3B", "SO", "PA", "AB",
}
_LEADERBOARD_PITCHING_STATS = {
    "ERA", "SO", "W", "WHIP", "SV", "IP",
    "H", "BB", "HR", "WAR", "CG", "SHO",
    "SO/9",
}


_LEADERBOARD_MODES = {"season", "all_time", "career"}


@app.get("/leaderboards")
def leaderboards(
    stat:        str = Query(..., description="Stat key, e.g. HR / AVG / WAR / ERA"),
    year:        int | None = Query(
        None,
        description=(
            "Season year. Required when mode='season'; ignored for "
            "'all_time' and 'career'."
        ),
    ),
    mode:        str = Query(
        "season",
        description=(
            "Leaderboard mode: 'season' (single-year), 'all_time' "
            "(top single seasons across all years), or 'career' "
            "(aggregated career totals)."
        ),
    ),
    player_type: str = Query("batter", description="'batter' or 'pitcher'"),
    limit:       int = Query(25, ge=1, le=100),
    league:      str | None = Query(
        None,
        description="Optional league filter — 'AL' or 'NL'. Omit for both leagues.",
    ),
    team:        str | None = Query(
        None,
        description=(
            "Optional team filter — Lahman team code (e.g. 'NYA' for the "
            "Yankees, 'LAN' for the Dodgers). Omit for all teams."
        ),
    ),
    year_from:   int | None = Query(
        None,
        description=(
            "Optional year-range floor (inclusive). Applies to 'all_time' "
            "(restricts which single seasons are eligible) and 'career' "
            "(restricts which seasons count toward the career aggregate). "
            "Ignored in 'season' mode."
        ),
    ),
    year_to:     int | None = Query(
        None,
        description=(
            "Optional year-range ceiling (inclusive). Paired with year_from "
            "for 'all_time' / 'career' modes. Ignored in 'season' mode."
        ),
    ),
):
    """Top `limit` players for the given (stat, mode). Sort order is
    automatic — ERA / WHIP ascending (lower is better), everything else
    descending. Each row carries a full PlayerSearchResult-shaped
    `player` block so the iOS row can render the same chrome as the
    search results and navigation can push straight into PlayerProfile.

    Three modes:
      • `season`   — single-year leaderboard for `year`. Rate-stat
                     eligibility scales with games played: 502 PA /
                     162 IP for completed seasons, pro-rated for
                     in-progress seasons.
      • `all_time` — top single seasons across every year on record.
                     Flat 502 PA / 162 IP qualifier for rate stats.
      • `career`   — aggregated career totals per player. Counting
                     stats are SUM, rate stats compute from career
                     totals (career AVG = SUM(H)/SUM(AB), career ERA
                     = SUM(ER)*9/SUM(IP), …). Rate stats require at
                     least 1000 PA (batters) or 500 IP (pitchers).

    `league` and `team` are independent filters and may be combined.
    The team value is matched against all known historical Lahman
    variants for that franchise, so e.g. team='MIA' also returns
    "FLO" rows from the Florida Marlins era."""
    if mode not in _LEADERBOARD_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of {sorted(_LEADERBOARD_MODES)}",
        )
    if mode == "season" and year is None:
        raise HTTPException(
            status_code=400,
            detail="year is required when mode='season'",
        )
    if player_type not in ("batter", "pitcher"):
        raise HTTPException(
            status_code=400,
            detail="player_type must be 'batter' or 'pitcher'",
        )
    if league is not None and league not in ("AL", "NL"):
        raise HTTPException(
            status_code=400,
            detail="league must be 'AL' or 'NL' if provided",
        )
    if team is not None and team not in data_service._TEAM_FILTER_VARIANTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"team {team!r} not recognized. Use a Lahman team code: "
                f"{sorted(data_service._TEAM_FILTER_VARIANTS)}"
            ),
        )
    valid_stats = (
        _LEADERBOARD_BATTING_STATS if player_type == "batter"
        else _LEADERBOARD_PITCHING_STATS
    )
    if stat not in valid_stats:
        raise HTTPException(
            status_code=400,
            detail=(
                f"stat {stat!r} not supported for {player_type!r}. "
                f"Try one of: {sorted(valid_stats)}"
            ),
        )

    # Normalize a swapped pair (user dragged the upper handle below
    # the lower one) so the downstream SQL stays predictable. Single
    # equal values are fine — that's just a one-year window.
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from

    response = data_service.get_leaderboard(
        stat=stat, year=year, player_type=player_type, mode=mode,
        limit=limit, league=league, team=team,
        year_from=year_from, year_to=year_to,
    )
    if response is None or not response.get("leaders"):
        suffix = ", ".join(filter(None, [league, team]))
        suffix = f" ({suffix})" if suffix else ""
        scope = (
            f"year {year}" if mode == "season"
            else "all time" if mode == "all_time"
            else "career"
        )
        raise HTTPException(
            status_code=404,
            detail=f"No {stat} leaders found for {scope}{suffix}",
        )
    return response


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


@app.post("/admin/rename-columns")
def admin_rename_columns():
    """One-time fix for the case-folding bug — renames lowercase columns
    (created by the earlier unquoted ALTER TABLE) back to their proper-case
    names (e.g. ibb→IBB, baopp→BAOpp). Idempotent."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    summary = connection.rename_lowercase_columns()
    return {"status": "done", **summary}


_EXPECTED_NEW_COLUMNS_BY_TABLE = {
    "player_seasons":  lambda: [c[0] for c in connection._PLAYER_SEASONS_NEW_COLUMNS],
    "pitcher_seasons": lambda: [c[0] for c in connection._PITCHER_SEASONS_NEW_COLUMNS],
    "team_seasons":    lambda: [c[0] for c in connection._TEAM_SEASONS_NEW_COLUMNS],
    "players":         lambda: [c[0] for c in connection._BIO_COLUMNS],
    "pitchers":        lambda: [c[0] for c in connection._BIO_COLUMNS],
}


def _check_table(table: str, engine) -> dict:
    """Return three views of a table's columns: raw SQL, inspector, and the
    migration's expected-new list. Lets us spot case-folding and other
    metadata-vs-truth mismatches."""
    dialect = engine.dialect.name
    if dialect == "postgresql":
        sql = _sa_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t ORDER BY column_name"
        )
    elif dialect == "sqlite":
        sql = _sa_text(
            "SELECT name AS column_name FROM pragma_table_info(:t) "
            "ORDER BY name"
        )
    else:
        return {"error": f"Unsupported dialect: {dialect}"}

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"t": table}).fetchall()
        raw_columns = [r[0] for r in rows]
    except Exception as exc:
        return {"error": f"raw query failed: {exc}"}

    try:
        inspector_columns = sorted(
            c["name"] for c in _sa_inspect(engine).get_columns(table)
        )
        inspector_error = None
    except Exception as exc:
        inspector_columns = []
        inspector_error = str(exc)

    expected_new = _EXPECTED_NEW_COLUMNS_BY_TABLE.get(table, lambda: [])()
    raw_lower = {x.lower() for x in raw_columns}

    return {
        "raw_columns":           raw_columns,
        "raw_columns_lower":     sorted(raw_lower),
        "raw_count":             len(raw_columns),
        "inspector_columns":     inspector_columns,
        "inspector_error":       inspector_error,
        "expected_new_columns":  expected_new,
        "missing_per_inspector": [c for c in expected_new if c not in inspector_columns],
        "missing_per_raw":       [c for c in expected_new if c not in raw_columns],
        "missing_per_raw_lower": [c for c in expected_new if c.lower() not in raw_lower],
    }


@app.get("/admin/db-check")
def admin_db_check(
    table: str | None = Query(
        None,
        description="Single table to inspect. Default returns both player_seasons and pitcher_seasons.",
    ),
):
    """Diagnostic: dump column lists for player_seasons and pitcher_seasons
    (or one specified table) directly from the live DB. Returns parallel
    raw-SQL, inspector, and expected-new views so case-folding mismatches
    are obvious. Also returns the resolved DB host (no credentials) so we
    can confirm we're connected to the right database."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    db_url = os.getenv("DATABASE_URL", "")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    parsed = urlparse(db_url)
    host_info = (
        f"{parsed.hostname or '?'}:{parsed.port or '?'}"
        f"/{(parsed.path or '').lstrip('/') or '?'}"
    )

    engine = connection._engine
    dialect = engine.dialect.name

    if table is not None:
        return {
            "database_host": host_info,
            "dialect":       dialect,
            "table":         table,
            **_check_table(table, engine),
        }

    return {
        "database_host": host_info,
        "dialect":       dialect,
        "tables": {
            "player_seasons":  _check_table("player_seasons",  engine),
            "pitcher_seasons": _check_table("pitcher_seasons", engine),
        },
    }


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
                    "player_hof":                 db.query(PlayerHof).count(),
                    "team_seasons":               db.query(TeamSeason).count(),
                    "batters_in_db":              db.query(PlayerSeason.player_id).distinct().count(),
                    "pitchers_in_db":             db.query(PitcherSeason.player_id).distinct().count(),
                }
        except Exception:
            pass

    with _bulk_lock:
        state = dict(_bulk_state)

    return {"counts": counts, **state}


@app.post("/admin/gamelog-load")
def start_gamelog_load(payload: dict | None = None):
    """Historical gamelog bulk load. Body:
        {"seasons": [2008, ..., 2025], "player_ids": null}

    `player_ids: null` targets all current-roster players (those with rows in
    player_seasons / pitcher_seasons for the current year). Runs in a
    background thread; poll GET /admin/gamelog-load/status for progress."""
    with _gamelog_lock:
        if _gamelog_state["running"]:
            return {"status": "already_running", **_gamelog_state}

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    body = payload or {}
    seasons    = body.get("seasons")
    player_ids = body.get("player_ids")
    if not isinstance(seasons, list) or not seasons:
        raise HTTPException(status_code=400, detail="`seasons` must be a non-empty list of integers")
    try:
        seasons = [int(s) for s in seasons]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="`seasons` entries must be integers")
    if player_ids is not None:
        if not isinstance(player_ids, list):
            raise HTTPException(status_code=400, detail="`player_ids` must be a list or null")
        try:
            player_ids = [int(p) for p in player_ids]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="`player_ids` entries must be integers")

    t = threading.Thread(
        target=_run_gamelog_load, args=(seasons, player_ids), daemon=True,
        name="gamelog-load",
    )
    t.start()
    return {"status": "started", "seasons": seasons,
            "player_ids": "all_active" if player_ids is None else f"{len(player_ids)} players"}


@app.get("/admin/gamelog-load/status")
def gamelog_load_status():
    with _gamelog_lock:
        return dict(_gamelog_state)


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


@app.post("/admin/load-award-shares")
def admin_load_award_shares():
    """Targeted backfill: load just `AwardsSharePlayers.csv` into
    `player_award_shares`, skipping the full Lahman re-run. Reuses
    `lahman_load._load_award_shares()` so the canonical mapping
    (Lahman awardID → "MVP" / "CY Young" / "ROY") and the per-year
    rank computation stay single-sourced.

    Synchronous — the CSV is small (~7,600 rows) and finishes in a
    few seconds, so there's no need for the background-thread
    pattern the full Lahman load uses. Upserts via crud's ON CONFLICT
    path so re-running the endpoint cleanly overwrites in place.

    Diagnostic mode: returns 200 with `status: "error"` plus the full
    traceback in the response body when the load fails, so a curl
    against this endpoint surfaces the root cause without needing
    server log access. Switch back to a raise once the deployment
    stabilizes.
    """
    import traceback

    if not connection.db_available():
        return {
            "status":  "error",
            "message": "DATABASE_URL is not configured",
        }

    started = time.time()
    try:
        bridge = lahman_load._load_chadwick_bridge()
        rows_loaded = lahman_load._load_award_shares(bridge)
        duration = round(time.time() - started, 2)
        return {
            "status":           "done",
            "rows_loaded":      rows_loaded,
            "duration_seconds": duration,
        }
    except Exception as exc:
        # Log to server stderr so Railway captures it AND echo the
        # traceback back to the caller for direct diagnosis.
        log.exception("load-award-shares failed")
        return {
            "status":     "error",
            "message":    str(exc),
            "error_type": type(exc).__name__,
            "traceback":  traceback.format_exc(),
            "duration_seconds": round(time.time() - started, 2),
        }


@app.get("/admin/lahman-load/status")
def lahman_load_status():
    with _lahman_lock:
        return dict(_lahman_state)


def _is_stale_running() -> tuple[bool, str | None]:
    """If running=True but last_started is older than the stale
    threshold, return (True, last_started). The caller can then auto-
    reset the flag and proceed. Falls open on parse errors — better to
    accept a fresh run than block on garbage state."""
    last = _nightly_state.get("last_started")
    if not last:
        return False, None
    try:
        # last_started is "YYYY-MM-DDTHH:MM:SS.ffffffZ" — strip Z and parse
        started = datetime.datetime.fromisoformat(last.rstrip("Z"))
    except (ValueError, AttributeError):
        return True, last
    age = datetime.datetime.utcnow() - started
    return age > _NIGHTLY_STALE_AFTER, last


@app.post("/admin/nightly-update")
def start_nightly_update():
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    # Atomic claim: check-and-set inside the lock so two simultaneous
    # POSTs can't both pass the running check and spawn duplicate
    # threads. Previously the lock was released between the check and
    # the t.start(), creating a TOCTOU window.
    with _nightly_lock:
        if _nightly_state["running"]:
            stale, last_started = _is_stale_running()
            if stale:
                # Worker likely SIGKILL'd (OOM, redeploy) before its
                # finally block could clear the flag. Auto-reset and
                # proceed — three hours is well past any legitimate run.
                log.warning(
                    f"[nightly] auto-resetting stale lock — "
                    f"last_started={last_started}, threshold={_NIGHTLY_STALE_AFTER}"
                )
                _nightly_state["error"] = (
                    f"auto-reset: previous run claimed at {last_started} "
                    f"never completed (stale)"
                )
                _nightly_state["running"] = False
            else:
                log.info(f"[nightly] POST rejected — already running: {_nightly_state}")
                return {"status": "already_running", **_nightly_state}

        # Claim the run.
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        _nightly_state["running"] = True
        _nightly_state["last_started"] = now_iso
        # Don't blow away an auto-reset error message — it'd be useful
        # in the response when the user POSTs after a stale run. dict.get
        # with a default doesn't help here because the key IS present
        # with a None value; coerce to "" before calling startswith.
        error_val = _nightly_state.get("error") or ""
        if not error_val.startswith("auto-reset:"):
            _nightly_state["error"] = None

    log.info(f"[nightly] POST accepted pid={os.getpid()} started={now_iso} — spawning worker thread")
    t = threading.Thread(target=_run_nightly_update, daemon=True, name="nightly-update")
    t.start()
    return {"status": "started", "last_started": now_iso}


@app.post("/admin/nightly-update/reset")
def reset_nightly_update():
    """Force-clear the running flag regardless of state.

    Use when a previous run was SIGKILL'd and the auto-reset threshold
    (3h) is too long to wait. Doesn't kill any actually-running thread
    — just clears the flag — so calling this while a real run is in
    progress will allow a duplicate thread to start. Use deliberately."""
    with _nightly_lock:
        prior = dict(_nightly_state)
        _nightly_state["running"] = False
        _nightly_state["phase"] = None
        _nightly_state["error"] = "manual reset"
    log.warning(f"[nightly] manual reset, prior state: {prior}")
    return {"status": "reset", "prior_state": prior}


@app.get("/admin/nightly-update/status")
def nightly_update_status():
    with _nightly_lock:
        state = dict(_nightly_state)
    # Log every status call so we can correlate with thread lifecycle
    # in Railway's stream — particularly useful for diagnosing the
    # "last_run stays null" symptom (which indicates the worker died
    # before reaching the finally block, e.g. on OOM).
    log.info(f"[nightly] GET status pid={os.getpid()}: {state}")
    return state


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
