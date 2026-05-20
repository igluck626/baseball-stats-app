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
from cache import cache as _cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# database imports work after data_service is imported (it adds backend/ to sys.path)
from database import connection, crud                       # noqa: E402
from database.models import (                                # noqa: E402
    BattingGameLog, Pitcher, PitcherSeason, PitchingGameLog,
    Player, PlayerAllstar, PlayerAward, PlayerFielding,
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
# Game-log historical bulk load was removed in the App-Store-
# compliance cleanup — the per-player MLB Stats API path is gone.
# Use `POST /admin/backfill-bdl-gamelogs?start_date=&end_date=`
# instead; it's faster (~15 BDL calls per day vs ~2,400 per-player
# MLB calls) and is the only supported gamelog-history loader now.
# ---------------------------------------------------------------------------


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


def _nightly_phase(
    fetch_bwar_all,
    get_ids,
    save_seasons,
    build_entry,
    parse_bdl_row,
    phase_name: str,
    bio_model,
    current_year: int,
) -> None:
    """One phase of the nightly update (batters or pitchers).

    Pre-fetches BDL season_stats for every BDL-mapped player in a
    single batched HTTP run, then iterates players locally. bwar
    provides the full-history WAR / OPS+ layer. `build_entry` is
    the script-module function which merges the two and falls back
    to MLB Stats API for rows without a `bdl_id`. `bio_model` is
    `Player` / `Pitcher` (used for the bdl_id-map SELECT);
    `parse_bdl_row` is `data_service._parse_bdl_batter_row` /
    `_parse_bdl_pitcher_row` (different normalization per side).
    """
    with _nightly_lock:
        _nightly_state.update(
            phase=phase_name, updated=0, skipped=0, failed=0, total=0
        )

    # Fail fast on missing BDL_KEY so a misconfigured Railway env
    # doesn't drain the rate budget with MLB Stats API fallbacks
    # before anyone notices.
    data_service._get_bdl_key()

    bwar_df      = fetch_bwar_all()
    bwar_current = bwar_df[bwar_df["year_ID"] == current_year]

    with connection.get_session() as db:
        ids: list[int] = get_ids(db)
        # Pre-load bdl_id + mlb_debut in one query — debut gates
        # the unmapped-row warning the builder emits (pre-2002
        # debuts silently skip instead of flooding the log).
        bio_rows = (
            db.query(bio_model.player_id, bio_model.bdl_id, bio_model.mlb_debut)
              .filter(bio_model.player_id.in_(ids))
              .all()
        )
        bdl_id_map: dict[int, int | None] = {r.player_id: r.bdl_id    for r in bio_rows}
        debut_map:  dict[int, int | None] = {r.player_id: r.mlb_debut for r in bio_rows}

    # Bulk BDL fetch — same call shape as the cron-mode nightly's
    # _update_batters / _update_pitchers helpers. Collapses the
    # per-player HTTP loop into ~one request per 50 player_ids.
    bdl_ids_to_fetch = sorted({v for v in bdl_id_map.values() if v is not None})
    raw_batch = data_service._fetch_bdl_batch_stats(bdl_ids_to_fetch, current_year)
    bdl_stats_by_bdl_id: dict[int, dict] = {
        bdl_id: parse_bdl_row(row)
        for bdl_id, row in raw_batch.items()
    }

    with _nightly_lock:
        _nightly_state["total"] = len(ids)

    for player_id in ids:
        try:
            bdl_id = bdl_id_map.get(player_id)
            bdl_stats = bdl_stats_by_bdl_id.get(bdl_id) if bdl_id else None
            entry = build_entry(
                player_id, bdl_id, bwar_current, current_year,
                bdl_stats=bdl_stats,
                mlb_debut=debut_map.get(player_id),
            )
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
        # No per-player BDL sleep — the batch fetch already paced
        # the HTTP. MLB Stats API fallback (only for unmapped rows)
        # is rare and doesn't enforce a strict rate limit.


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
            data_service._bwar_bat_all,
            crud.get_all_player_ids,
            crud.save_player_seasons,
            nightly_update._build_current_batter_entry,
            data_service._parse_bdl_batter_row,
            "batters",
            Player,
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
            data_service._bwar_pitch_all,
            crud.get_all_pitcher_ids,
            crud.save_pitcher_seasons,
            nightly_update._build_current_pitcher_entry,
            data_service._parse_bdl_pitcher_row,
            "pitchers",
            Pitcher,
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
        # Drop every entry in the in-process cache so the next
        # iOS request sees fresh nightly data instead of waiting
        # for individual TTLs to expire. Cheap (clears a dict);
        # only meaningful for the API worker that ran the nightly
        # (other workers — if scaled out — clear via TTL).
        _cache.clear()
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
    # Cache by normalized query — search results don't change
    # within a nightly window, and the user search field can fire
    # a request per keystroke. Lowercase + strip so "Trout" and
    # " trout " collapse to one cache key.
    key = f"search:{name.strip().lower()}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    results = data_service.search_player(name)
    if not results:
        raise HTTPException(status_code=404, detail=f"No players found matching '{name}'")
    payload = {"query": name, "results": results}
    _cache.set(key, payload, ttl_seconds=300)
    return payload


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


@app.get("/players/by-bdl-id/{bdl_id}")
def player_by_bdl_id(bdl_id: int):
    """Direct lookup by BallDontLie player id. BDL ids are the FK on
    every BDL game / stat / play / PA payload, so this is how iOS
    resolves a tapped-in-a-box-score player back to our MLBAM-keyed
    profile. Returns the same `PlayerSearchResult` shape that
    `/players/search` and `/players/by-mlb-id/{id}` produce, so the
    iOS profile navigation works without branching on which id type
    was the entry point."""
    result = data_service.get_player_by_bdl_id(bdl_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No player found with BDL id {bdl_id}",
        )
    return result


@app.get("/players/{player_id}/stats/current")
def current_stats(player_id: int):
    key = f"player_stats:{player_id}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    stats = data_service.get_current_stats(player_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No current season stats found for player_id {player_id}",
        )
    _cache.set(key, stats, ttl_seconds=300)
    return stats


@app.get("/players/{player_id}/stats/career")
def career_stats(player_id: int):
    key = f"player_career:{player_id}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    stats = data_service.get_career_stats(player_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No career stats found for player_id {player_id}",
        )
    # Career stats barely change inside one game day — the only
    # mover is the nightly which calls `cache.clear()` at end of
    # run. 1-hour TTL guards against memory bloat from a player
    # whose row never changes anyway.
    _cache.set(key, stats, ttl_seconds=3600)
    return stats


@app.get("/players/{player_id}/pitching/current")
def current_pitching(player_id: int):
    key = f"pitcher_stats:{player_id}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    stats = data_service.get_current_pitching_stats(player_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No current season pitching stats found for player_id {player_id}",
        )
    _cache.set(key, stats, ttl_seconds=300)
    return stats


@app.get("/players/{player_id}/pitching/career")
def career_pitching(player_id: int):
    key = f"pitcher_career:{player_id}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    stats = data_service.get_career_pitching_stats(player_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No career pitching stats found for player_id {player_id}",
        )
    _cache.set(key, stats, ttl_seconds=3600)
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
    key = f"standings:{year}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    rows = data_service.get_team_standings(year)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No team data for year {year}")

    # Latest last_updated across the rows. ISO-8601 + Z so it's unambiguous in
    # the response. None if no row carries a timestamp (older Lahman-only data).
    timestamps = [r.get("last_updated") for r in rows if r.get("last_updated") is not None]
    last_updated = max(timestamps).isoformat() + "Z" if timestamps else None

    payload = {"year": year, "last_updated": last_updated, "standings": rows}
    _cache.set(key, payload, ttl_seconds=300)
    return payload


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

    # Cache by the full parameter tuple — leaderboards are
    # expensive ranked-window queries and the iOS leaderboard tab
    # re-fires the same call on every navigation back-and-forth.
    cache_key = (
        f"lb:{stat}:{year}:{mode}:{player_type}:{limit}:"
        f"{league or '_'}:{team or '_'}:{year_from or '_'}:{year_to or '_'}"
    )
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

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
    _cache.set(cache_key, response, ttl_seconds=300)
    return response


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/cache/stats")
def admin_cache_stats():
    """Return the in-process cache health snapshot: total keys,
    keys still within their TTL, and expired (lazily-evicted)
    keys. Multi-worker note: Railway can run more than one worker
    and each holds its own cache; the stats here are one worker's
    view, not the cluster's. Acceptable while single-worker."""
    return _cache.stats()


@app.post("/admin/cache/clear")
def admin_cache_clear():
    """Drop every entry in the in-process cache. Called automatically
    at the end of `_run_nightly_update` so fresh nightly data is
    visible immediately; available manually for ad-hoc evictions
    (e.g. after a hot-fix admin endpoint mutates a row)."""
    before = _cache.stats().get("total_keys", 0)
    _cache.clear()
    return {"status": "ok", "cleared": before}


@app.get("/admin/env-check")
def env_check():
    return {"DATABASE_URL_set": bool(os.getenv("DATABASE_URL"))}


@app.post("/admin/sync-player-team/{mlb_id}")
def admin_sync_player_team(mlb_id: int):
    """Ad-hoc fix for a single player whose `team` column is stale
    on the current-year season row. Pulls authoritative team from
    MLB Stats API `/people/{id}.currentTeam.abbreviation` and
    overrides pitcher_seasons.team + player_seasons.team. Used to
    repair offseason-trade cases where bref's `Tm` column hasn't
    yet caught up."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return data_service.sync_player_current_team(mlb_id)


@app.post("/admin/sync-all-player-teams")
def admin_sync_all_player_teams():
    """Bulk team-reconcile pass against MLB Stats API's 30 active
    rosters. Used both as a one-shot repair after the diagnosis
    above and as the post-step the nightly pipeline calls to
    cover everyone whose bref `Tm` is wrong on a given day.
    Now also inserts bio rows for any roster player missing from
    `players` / `pitchers` — see `discover-from-rosters` for the
    discovery-focused alias of the same operation."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year
    return data_service.sync_all_player_teams_from_rosters(current_year)


@app.post("/admin/backfill-bdl-gamelogs")
def admin_backfill_bdl_gamelogs(
    start_date: str = Query(..., description="Inclusive start date, yyyy-mm-dd"),
    end_date:   str = Query(..., description="Inclusive end date, yyyy-mm-dd"),
):
    """One-shot history backfill of batting + pitching gamelogs
    via BallDontLie's game-centric `/stats?game_id={id}` endpoint.
    Walks every date in [start_date, end_date], fetches all finals,
    and upserts player game rows.

    Idempotent — the gamelog tables use (player_id, game_id) PKs,
    so re-running on a date range that's already loaded is a
    no-op upsert. Rate-limited at the BDL 5/sec ceiling between
    games. Backfills the entire history we used to fill via per-
    player MLB-Stats-API calls in roughly one BDL call per 25
    player-game rows."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return data_service.backfill_bdl_gamelogs(start_date, end_date)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/admin/discover-from-rosters")
def admin_discover_from_rosters():
    """Walk MLB Stats API's 30 active rosters and insert bio rows
    for any roster player missing from our `players` / `pitchers`
    tables. Used for fresh rookies who debuted recently and
    haven't shown up in bref's batting/pitching stats tables yet
    (the nightly's bref-driven discovery skips them). After this
    runs, the next nightly's normal loop will populate their
    stats via the MLB Stats API override path.
    Same underlying helper as `/admin/sync-all-player-teams` —
    that one also reconciles team codes for known players in the
    same pass."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year
    return data_service.sync_all_player_teams_from_rosters(current_year)


@app.post("/admin/repair-null-stats")
def admin_repair_null_stats():
    """One-shot cleanup for placeholder rows that the Phase 5
    roster sync created but the nightly stat-fill path missed
    (gating bug, since fixed). Finds every current-year row with
    `last_updated IS NULL`, fetches the player's MLB Stats API
    splits, and writes them in. Safe to call repeatedly."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year
    return data_service.repair_null_stats(current_year)


@app.get("/admin/bdl-teams")
def admin_bdl_teams():
    """Walk BallDontLie's `/teams` endpoint and return the full
    franchise list with their BDL ids alongside our Lahman-suggested
    codes. Also stamps `bdl_id` on every `team_seasons` row where the
    Lahman match is unambiguous, so the BDL migration code path can
    resolve via DB lookup before the hand-paste step lands.

    Output is intended for human inspection — the operator pastes
    the (lahman_suggested → bdl_id) pairs into `_BDL_TEAM_ID_MAP` in
    `data_service.py` after spot-checking the abbreviations.

    Requires the `BDL_KEY` env var. Raises 503 with a clear error
    if it's missing."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return data_service.fetch_bdl_teams()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/admin/bdl-mapping-status")
def admin_bdl_mapping_status(
    since_year: int = Query(2002, ge=1871, le=2100,
                            description="Coverage denominator floor: only count "
                                        "players whose mlb_debut >= this. Default 2002."),
):
    """Reporting view of the BDL player-id mapping bootstrap.
    Returns:
      • `coverage` — total / mapped / unmapped per table, with a
        match percentage, for rows with `mlb_debut >= since_year`.
      • `spot_checks` — hand-picked active stars (Trout, Ohtani,
        Freeman, Vlad Jr., Bobby Witt Jr.). Compares stamped BDL
        id vs. expected (where known) and flags `mismatch` /
        `unmapped` / `missing_from_db`.
      • `recent_sample` — 10 deterministic rows from debut >= 2020
        so the operator can eyeball that real names line up with
        sane BDL ids."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return data_service.get_bdl_mapping_status(since_year)


@app.post("/admin/retry-unmapped-bdl-players")
def admin_retry_unmapped_bdl_players(
    limit: int = Query(1000, ge=1, le=10000,
                       description="Max DB rows to process this call. Re-invoke to resume."),
):
    """Re-run the BDL player mapping for rows that previously
    failed. Functionally identical to `build-bdl-player-mapping`
    (the `bdl_id IS NULL` filter already excludes successfully-
    matched rows), but the floor is locked to `mlb_debut >= 2010`
    and the endpoint name documents intent: "go pick up the rows
    the first pass missed, now that the matcher knows about
    suffix variations and accent stripping." Same rate-limit and
    re-invocation semantics."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return data_service.build_bdl_player_mapping(
            since_year=2010, limit=limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/admin/build-bdl-player-mapping")
def admin_build_bdl_player_mapping(
    since_year: int = Query(2010, ge=1871, le=2100,
                            description="Only walk players whose debut year is >= this. "
                                        "Default 2010 — BDL's data starts roughly there, "
                                        "so older players are reliably unmatched."),
    limit: int = Query(1000, ge=1, le=10000,
                       description="Max DB rows to process this call. Re-invoke to resume "
                                   "— the WHERE clause filters out already-stamped rows."),
):
    """One-shot bootstrap of the BDL player-id mapping. Walks every
    `players` and `pitchers` row whose `bdl_id` is NULL and whose
    `mlb_debut >= since_year`, runs a name search against BDL,
    disambiguates by position side (batter vs. pitcher), and stamps
    the matched BDL id onto the row.

    Rate-limited to ≈4.5 req/sec to stay under BDL's 5/sec ceiling.
    A single call sleeps `limit × 0.22s` worth of wall time, so a
    `limit=1000` call takes ~3.7 minutes — under Railway's default
    5-minute HTTP timeout. Re-invoke until `processed = 0` to drain.

    Returns matched / unmatched / ambiguous counts plus a capped
    sample list of each (full unmatched lists balloon on first runs)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return data_service.build_bdl_player_mapping(
            since_year=since_year, limit=limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/admin/backfill-player-history/{player_id}")
def admin_backfill_player_history(
    player_id: int,
    year_from: int = Query(..., ge=1871, le=2100, description="Start year (inclusive)"),
    year_to:   int = Query(..., ge=1871, le=2100, description="End year (inclusive)"),
):
    """Targeted historical backfill for one player. Pulls MLB Stats
    API season splits for each year in [year_from, year_to] and
    writes a `player_seasons` / `pitcher_seasons` row per year,
    stamped with the team they played for that season.

    Motivating case: Riley Greene (682985) — debuted 2022 but his
    `players.bbref_id` is null, so the Lahman bridge never attached
    his 2022–2025 batting rows. This works around that without
    requiring the bref_id to be populated first. Also bootstraps a
    missing bio when the player has never been seen before
    (IL-listed rookies who skip the active-roster discovery)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if year_to < year_from:
        raise HTTPException(status_code=400, detail="year_to must be >= year_from")
    return data_service.backfill_player_seasons(player_id, year_from, year_to)


@app.post("/admin/repair-ip-decimals")
def admin_repair_ip_decimals():
    """One-shot fix for pitcher_seasons IP values stored in
    baseball notation (10.2 = 10 ⅔) instead of true decimal
    (10.667). Caused by an older bref-write path; new writes
    are correct, but existing rows need this pass. Detect: rows
    whose IP tenths digit is 1 or 2 after rounding. Idempotent
    — re-running after a successful pass is a no-op since the
    fixed rows now round to 3 or 7."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year
    return data_service.repair_ip_decimals(current_year)


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
