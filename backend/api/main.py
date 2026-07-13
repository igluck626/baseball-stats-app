"""Baseball stats API."""

import csv
import datetime
import json
import logging
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import pandas as pd
import uvicorn
from dotenv import load_dotenv
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from sqlalchemy import (
    func as _sa_func,
    inspect as _sa_inspect,
    or_ as _sa_or,
    text as _sa_text,
)

import sys

import data_service
import live_service
import news_service
from cache import cache as _cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# database imports work after data_service is imported (it adds backend/ to sys.path)
from database import connection, crud                       # noqa: E402
from database.models import (                                # noqa: E402
    AskLog, BattingGameLog, Pitcher, PitcherSeason, PitcherSeasonStint,
    PitchingGameLog, Player, PlayerAllstar, PlayerAward, PlayerFielding,
    PlayerHof, PlayerPostseasonBatting, PlayerPostseasonPitching,
    PlayerSeason, PlayerSeasonStint, SeriesPost,
    StagingBattingGameLog, StagingPitchingGameLog, TeamSeason,
)

# scripts/ holds the Lahman loader, WAR backfill, and nightly update logic;
# expose them so /admin endpoints can drive the same pipeline as the CLI.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import backfill_war                                         # noqa: E402
import lahman_load                                          # noqa: E402
import nightly_update                                       # noqa: E402
import reset_db                                             # noqa: E402
import retrosheet_ingest                                    # noqa: E402
import retrosheet_gamelogs                                  # noqa: E402

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


# ---------------------------------------------------------------------------
# Retrosheet-ingest state (shared between background thread + status endpoint)
# Phases: "snapshot" → "batting_seasons" → "pitching_seasons"
#         → "batting_stints" → "pitching_stints" → "done"
# ---------------------------------------------------------------------------
_retro_state: dict = {
    "running":  False,
    "phase":    None,
    "year":     None,
    "batting_upserted":          0,
    "pitching_upserted":         0,
    "batting_stints_upserted":   0,
    "pitching_stints_upserted":  0,
    "summary":  None,
    "error":    None,
    "last_run": None,
}
_retro_lock = threading.Lock()


def _run_retrosheet_ingest(year: int | None) -> None:
    """Background thread: ingest the committed Retrosheet CSVs (OVERWRITE the
    combined season rows + populate the stint tables)."""
    with _retro_lock:
        _retro_state.update(
            running=True, phase=None, year=year,
            batting_upserted=0, pitching_upserted=0,
            batting_stints_upserted=0, pitching_stints_upserted=0,
            summary=None, error=None,
        )
    try:
        retrosheet_ingest.run(year=year, state=_retro_state, lock=_retro_lock)
    except Exception as exc:
        with _retro_lock:
            _retro_state["error"] = str(exc)
    finally:
        # Drop the in-process cache so post-ingest reads see the rewritten
        # season rows instead of the stale pre-ingest snapshot (player_career:,
        # pitcher_career:, leaders, …) until their TTLs lapse. Same call the
        # nightly makes in its finally; runs after retrosheet_ingest.run() has
        # committed every batch (get_session commits on context exit).
        _cache.clear()
        with _retro_lock:
            _retro_state["running"]  = False
            _retro_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Retrosheet historical GAME-LOG backfill state (1898-1999, runtime download)
# ---------------------------------------------------------------------------
_retro_gl_state: dict = {
    "running":  False,
    "phase":    None,
    "year_from": None,
    "year_to":   None,
    "current_year":     None,
    "years_done":       0,
    "batting_written":  0,
    "pitching_written": 0,
    "unmapped_skipped": 0,
    "summary":  None,
    "error":    None,
    "last_run": None,
}
_retro_gl_lock = threading.Lock()


def _run_retrosheet_gamelogs(year_from: int, year_to: int) -> None:
    """Background thread: download Retrosheet daybyday per year and write
    historical gamelog rows. Clears the cache at the end like the season
    ingest / nightly so post-backfill reads aren't stale."""
    with _retro_gl_lock:
        _retro_gl_state.update(
            running=True, phase=None, year_from=year_from, year_to=year_to,
            current_year=None, years_done=0, batting_written=0,
            pitching_written=0, unmapped_skipped=0, summary=None, error=None,
        )
    try:
        retrosheet_gamelogs.run(year_from=year_from, year_to=year_to,
                                state=_retro_gl_state, lock=_retro_gl_lock)
    except Exception as exc:
        with _retro_gl_lock:
            _retro_gl_state["error"] = str(exc)
    finally:
        _cache.clear()
        with _retro_gl_lock:
            _retro_gl_state["running"]  = False
            _retro_gl_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Retrosheet 2000-2025 game-log STAGING backfill (writes the separate staging
# tables — LIVE batting_gamelogs/pitching_gamelogs are never touched here).
# Uses the B_G>0 APPEARANCE gate so staging is a superset of the live keys.
# ---------------------------------------------------------------------------
_stage_gl_state: dict = {
    "running":  False,
    "phase":    None,
    "year_from": None,
    "year_to":   None,
    "current_year":     None,
    "years_done":       0,
    "batting_written":  0,
    "pitching_written": 0,
    "unmapped_skipped": 0,
    "summary":  None,
    "error":    None,
    "last_run": None,
}
_stage_gl_lock = threading.Lock()


def _run_stage_retrosheet_gamelogs(year_from: int, year_to: int) -> None:
    """Background thread: ingest Retrosheet 2000-2025 game logs into the
    STAGING tables with the appearance gate. Does NOT touch the live tables or
    the cache (staging isn't read by the app)."""
    with _stage_gl_lock:
        _stage_gl_state.update(
            running=True, phase=None, year_from=year_from, year_to=year_to,
            current_year=None, years_done=0, batting_written=0,
            pitching_written=0, unmapped_skipped=0, summary=None, error=None,
        )
    try:
        retrosheet_gamelogs.run(
            year_from=year_from, year_to=year_to,
            state=_stage_gl_state, lock=_stage_gl_lock,
            bat_model=StagingBattingGameLog, pit_model=StagingPitchingGameLog,
            appearance_gate=True,
        )
    except Exception as exc:
        with _stage_gl_lock:
            _stage_gl_state["error"] = str(exc)
    finally:
        with _stage_gl_lock:
            _stage_gl_state["running"]  = False
            _stage_gl_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


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
# For multi-year loads use `POST /admin/backfill-gamelogs-async` so
# the HTTP request doesn't have to stay open for hours.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Async gamelog-backfill state (shared between background thread and status endpoint)
# ---------------------------------------------------------------------------
# Lets a multi-season BDL gamelog backfill (which can run hours for
# 2000→present) kick off from one HTTP call without holding the
# request connection open. POST starts a thread + returns immediately;
# GET reports live progress.
_backfill_state: dict = {
    "running":         False,
    "mode":            None,   # "date" | "season" | None
    "start_season":    None,
    "end_season":      None,
    "start_date":      None,
    "end_date":        None,
    "current_season":  None,   # season currently being worked (season mode)
    "total_games":     0,
    "total_bat_rows":  0,
    "total_pit_rows":  0,
    "error":           None,
    "last_updated":    None,
    "last_run":        None,
}
_backfill_lock = threading.Lock()


def _run_backfill_gamelogs(
    start_season: int | None,
    end_season:   int | None,
    start_date:   str | None,
    end_date:     str | None,
) -> None:
    """Background thread: same pipeline as `/admin/backfill-bdl-gamelogs`
    (dates OR seasons) but reports progress to `_backfill_state` so a
    long multi-year run isn't gated on the HTTP connection.
    """
    def _now_iso() -> str:
        return datetime.datetime.utcnow().isoformat() + "Z"

    has_dates = start_date is not None and end_date is not None
    try:
        if has_dates:
            result = data_service.backfill_bdl_gamelogs(start_date, end_date)
            with _backfill_lock:
                _backfill_state["total_games"]    = int(result.get("total_games")    or 0)
                _backfill_state["total_bat_rows"] = int(result.get("total_bat_rows") or 0)
                _backfill_state["total_pit_rows"] = int(result.get("total_pit_rows") or 0)
                _backfill_state["last_updated"]   = _now_iso()
        else:
            # Season-mode loop: same current-year skip + per-year
            # window as the sync endpoint. Progress is published
            # after each year so the status endpoint can show which
            # season is being worked and the running totals.
            current_year = data_service._current_year()
            for year in range(start_season, end_season + 1):
                if year == current_year:
                    continue
                with _backfill_lock:
                    _backfill_state["current_season"] = year
                    _backfill_state["last_updated"]   = _now_iso()
                r = data_service.backfill_bdl_gamelogs(
                    f"{year}-03-01", f"{year}-11-30",
                )
                with _backfill_lock:
                    _backfill_state["total_games"]    += int(r.get("total_games")    or 0)
                    _backfill_state["total_bat_rows"] += int(r.get("total_bat_rows") or 0)
                    _backfill_state["total_pit_rows"] += int(r.get("total_pit_rows") or 0)
                    _backfill_state["last_updated"]   = _now_iso()
        # Auto-dedup tail — same gate as the sync endpoint. Non-fatal:
        # backfill rows have already committed, so log + continue.
        try:
            connection.dedupe_gamelog_duplicates(
                "batting_gamelogs",
                connection._BATTING_GAMELOGS_QUALITY_COLUMNS,
            )
            connection.dedupe_gamelog_duplicates(
                "pitching_gamelogs",
                connection._PITCHING_GAMELOGS_QUALITY_COLUMNS,
            )
        except Exception as exc:
            log.error(f"async backfill auto-dedup FAILED (non-fatal): {exc}")
    except Exception as exc:
        tb = traceback.format_exc()
        with _backfill_lock:
            _backfill_state["error"] = f"{exc}\n{tb}"
        log.error(f"async backfill FAILED: {exc}", exc_info=True)
    finally:
        with _backfill_lock:
            _backfill_state["running"]        = False
            _backfill_state["current_season"] = None
            _backfill_state["last_run"]       = _now_iso()
            _backfill_state["last_updated"]   = _backfill_state["last_run"]


# ---------------------------------------------------------------------------
# MLB Stats API async gamelog-backfill state (separate from the BDL
# async backfill above so a long-running historical MLB load doesn't
# block the BDL date/season backfill, and vice versa).
# ---------------------------------------------------------------------------
# Historical loader fed by statsapi.mlb.com — used to fill pre-2010
# seasons where BDL `/stats?game_id=` has no coverage. Rate-limited
# deliberately (1s/game, 2s/date) so multi-year runs stay well within
# MLB Stats API's tolerance.
_mlb_backfill_state: dict = {
    "running":         False,
    "start_season":    None,
    "end_season":      None,
    "current_season":  None,
    "current_date":    None,
    "total_games":     0,
    "total_bat_rows":  0,
    "total_pit_rows":  0,
    "error":           None,
    "last_updated":    None,
    "last_run":        None,
}
_mlb_backfill_lock = threading.Lock()


def _run_mlb_backfill_gamelogs(start_season: int, end_season: int) -> None:
    """Background thread: walk every regular-season date from
    `{year}-03-25` through `{year}-11-01` for each year in
    `[start_season, end_season]`, hitting MLB Stats API for schedule
    + boxscores. Sleep 1s after each boxscore and 2s after each
    date's schedule (the inter-date pause sits inside this loop;
    `mlb_save_gamelogs_for_date` only paces between boxscores)."""
    def _now_iso() -> str:
        return datetime.datetime.utcnow().isoformat() + "Z"
    try:
        known_pids = data_service.get_known_player_ids()
        log.info(f"[mlb-backfill] loaded {len(known_pids)} known player_ids")
        for year in range(start_season, end_season + 1):
            with _mlb_backfill_lock:
                _mlb_backfill_state["current_season"] = year
                _mlb_backfill_state["last_updated"]   = _now_iso()
            d   = datetime.date(year, 3, 25)
            end = datetime.date(year, 11, 1)
            while d <= end:
                date_iso = d.isoformat()
                with _mlb_backfill_lock:
                    _mlb_backfill_state["current_date"] = date_iso
                    _mlb_backfill_state["last_updated"] = _now_iso()
                try:
                    r = data_service.mlb_save_gamelogs_for_date(
                        date_iso, known_pids, sleep_between_games=1.0,
                    )
                    with _mlb_backfill_lock:
                        _mlb_backfill_state["total_games"]    += int(r.get("games")    or 0)
                        _mlb_backfill_state["total_bat_rows"] += int(r.get("bat_rows") or 0)
                        _mlb_backfill_state["total_pit_rows"] += int(r.get("pit_rows") or 0)
                except Exception as exc:
                    log.error(
                        f"[mlb-backfill] {date_iso} failed (skipping): {exc}",
                        exc_info=True,
                    )
                d += datetime.timedelta(days=1)
                # Inter-date pause sits outside `mlb_save_gamelogs_for_date`
                # so it applies whether the date had games or not.
                time.sleep(2.0)
    except Exception as exc:
        tb = traceback.format_exc()
        with _mlb_backfill_lock:
            _mlb_backfill_state["error"] = f"{exc}\n{tb}"
        log.error(f"[mlb-backfill] FAILED: {exc}", exc_info=True)
    finally:
        with _mlb_backfill_lock:
            _mlb_backfill_state["running"]        = False
            _mlb_backfill_state["current_season"] = None
            _mlb_backfill_state["current_date"]   = None
            _mlb_backfill_state["last_run"]       = datetime.datetime.utcnow().isoformat() + "Z"
            _mlb_backfill_state["last_updated"]   = _mlb_backfill_state["last_run"]


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


# Catch-up update — independent of `_nightly_state` so a running
# nightly doesn't block a mid-day catch-up from reporting its own
# progress, and vice versa.
_catchup_state: dict = {
    "running":          False,
    "phase":            None,   # "scanning" | "updating" | None
    "result":           None,   # counts dict from run_catchup_update on completion
    "error":            None,
    "last_run":         None,   # ISO-8601 UTC of last completed run
    "last_started":     None,   # ISO-8601 UTC of current run's start
}
_catchup_lock = threading.Lock()
# Catch-up's stale-flag threshold. The full pass is at most ~30
# game-stats fetches at one BDL request each — should finish in
# <2min. 30min is generous enough for transient slowness without
# letting a SIGKILL'd worker block subsequent runs.
_CATCHUP_STALE_AFTER = datetime.timedelta(minutes=30)


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

    # bref WAR is non-critical for the nightly — BDL ships the
    # current-season counting stats, and WAR / OPS+ / ERA+ only
    # come from `pybaseball.bwar_*`. If baseball-reference.com is
    # down (its read endpoint times out intermittently),
    # `fetch_bwar_all` will have already retried 3× and tried to
    # serve the previous run's cached value. If even that fails —
    # first nightly after a fresh deploy with bref still down —
    # log a warning and degrade to an empty DataFrame so the rest
    # of the phase (BDL stat ingestion) still runs.
    try:
        bwar_df = fetch_bwar_all()
    except Exception as exc:
        log.warning(
            "bref WAR download failed after 3 attempts — continuing with empty "
            "WAR data: %s", exc,
        )
        bwar_df = pd.DataFrame()
    if bwar_df.empty or "year_ID" not in bwar_df.columns:
        # Same empty-frame fall-through downstream — `build_entry`
        # handles a no-WAR match per-player.
        bwar_current = bwar_df
    else:
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

        # Auto-dedup. The gamelogs phase often surfaces cross-format
        # duplicates — same logical game written once under an MLB
        # Stats API id and once under a BDL id. The dedup helper's
        # `min_game_id < 1_000_000` gate is what spares legitimate
        # BDL-only doubleheaders. Running this nightly removes the
        # need for manual `/admin/dedupe-gamelogs` after most ingests.
        log.info("[nightly] starting dedup phase")
        try:
            bat_removed = connection.dedupe_gamelog_duplicates(
                "batting_gamelogs",
                connection._BATTING_GAMELOGS_QUALITY_COLUMNS,
            )
            pit_removed = connection.dedupe_gamelog_duplicates(
                "pitching_gamelogs",
                connection._PITCHING_GAMELOGS_QUALITY_COLUMNS,
            )
            log.info(
                f"[nightly] dedup phase done: "
                f"batting_removed={bat_removed} "
                f"pitching_removed={pit_removed}"
            )
        except Exception as exc:
            # Non-fatal — the nightly's stat-write phases already
            # committed, so a dedup failure is recoverable on the
            # next run / via the manual endpoint.
            log.error(f"[nightly] dedup phase FAILED (non-fatal): {exc}")

        # Phase 4b — batting counting aggregation. Runs after game logs
        # AND dedup so it sums the freshest, duplicate-free per-game rows.
        # Overwrites the current-season counting fields the BDL batter
        # phase doesn't write (PA/H/HBP/SF/CS/IBB/GIDP/SH) and refreshes
        # stale bref-seed values like GIDP. Non-fatal.
        log.info("[nightly] starting batting-counting aggregation phase")
        try:
            with connection.get_session() as db:
                bc_updated = data_service.recalculate_batting_counting(db, current_year)
                db.commit()
            log.info(f"[nightly] batting-counting aggregation done: rows updated={bc_updated}")
        except Exception as exc:
            log.error(f"[nightly] batting-counting aggregation FAILED (non-fatal): {exc}")

        # Pitcher counterpart — overwrites the current-season R/HBP the BDL
        # pitcher phase doesn't write, summed from pitching_gamelogs. Non-fatal.
        log.info("[nightly] starting pitching-counting aggregation phase")
        try:
            with connection.get_session() as db:
                pc_updated = data_service.recalculate_pitching_counting(db, current_year)
                db.commit()
            log.info(f"[nightly] pitching-counting aggregation done: rows updated={pc_updated}")
        except Exception as exc:
            log.error(f"[nightly] pitching-counting aggregation FAILED (non-fatal): {exc}")

        # Phase 4c — advanced batting rates (wOBA / K% / BB% / ISO). Runs
        # AFTER 4b so the components it derives from are correct. BDL omits
        # these, so without this pass current-season rows stay NULL. Non-fatal.
        log.info("[nightly] starting batting-rates phase")
        try:
            with connection.get_session() as db:
                br_updated = data_service.recalculate_batting_rates(db, current_year)
                db.commit()
            log.info(f"[nightly] batting-rates phase done: rows updated={br_updated}")
        except Exception as exc:
            log.error(f"[nightly] batting-rates phase FAILED (non-fatal): {exc}")

        # Phase 5 — reconcile current-year season teams from BDL's
        # active rosters. Belt-and-suspenders for offseason-trade /
        # FA-signing cases where bref's `Tm` column lags the move.
        # Mirrors `scripts/nightly_update.py::main()`; each post-stat
        # phase is independently try/except'd so one failure doesn't
        # abort the others.
        log.info("[nightly] starting team-reconcile phase")
        try:
            team_sync = data_service.sync_all_player_teams_from_rosters(current_year)
            log.info(
                f"[nightly] teams reconciled — rows updated: {team_sync.get('updated', 0)}, "
                f"failed teams: {team_sync.get('failed_teams', [])}"
            )
        except Exception as exc:
            log.error(f"[nightly] team reconcile FAILED (non-fatal): {exc}")

        # Phase 5b — active-status sync via BDL `/players?player_ids[]=`.
        # Reconciles `mlb_last_season` against BDL's `active` flag so
        # returning players (comebacks) get un-retired and players BDL
        # flags inactive get stamped. The roster walk above can't see
        # players who left the league entirely, so this id-keyed pass
        # is what catches retirements and comebacks.
        log.info("[nightly] starting active-status phase")
        try:
            active_sync = data_service.sync_player_active_status_from_bdl(current_year)
            ac = active_sync.get("counts") or {}
            log.info(
                f"[nightly] active-status reconciled — activated: {ac.get('activated', 0)}, "
                f"retired: {ac.get('retired', 0)}, "
                f"team_updated: {ac.get('team_updated', 0)}, "
                f"no_data: {ac.get('no_data', 0)}, "
                f"failed: {ac.get('failed', 0)}"
            )
        except Exception as exc:
            log.error(f"[nightly] active-status reconcile FAILED (non-fatal): {exc}")

        # Phase 5c — call-up discovery via BDL's active-roster walk.
        # Any BDL roster player without a matching MLBAM-keyed row in
        # `players` / `pitchers` gets resolved via MLB Stats API name
        # search and inserted on the spot, so iOS sees a real bio +
        # headshot the morning after a debut.
        log.info("[nightly] starting discover phase")
        dc: dict = {}
        try:
            discover_result = data_service.discover_new_players(current_year)
            dc = discover_result.get("counts") or {}
            log.info(
                "[nightly] discover phase: %d new batters, "
                "%d new pitchers, %d failed",
                dc.get("new_players_created", 0),
                dc.get("new_pitchers_created", 0),
                dc.get("new_players_failed", 0),
            )
        except Exception as exc:
            log.error(f"[nightly] discover phase FAILED (non-fatal): {exc}")

        # Phase 5d — same-season gamelog backfill for newly-discovered
        # players. Only fires when Phase 5c actually inserted at least
        # one new bio — the operation walks every BDL final from
        # March 25 through today, too expensive to run on quiet nights.
        # Idempotent (PK upsert), with an auto-dedup tail inside
        # `backfill_bdl_gamelogs`.
        new_bios = (
            dc.get("new_players_created",  0)
            + dc.get("new_pitchers_created", 0)
        )
        if new_bios > 0:
            try:
                today_et = datetime.datetime.now(
                    data_service._MLB_LOCAL_TZ,
                ).date().isoformat()
                start_date = f"{current_year}-03-25"
                log.info(
                    "[nightly] new player gamelog backfill: "
                    "%d new bios → walking %s..%s",
                    new_bios, start_date, today_et,
                )
                bf = data_service.backfill_bdl_gamelogs(start_date, today_et)
                log.info(
                    "[nightly] new player game log backfill: "
                    "%d games, %d bat_rows, %d pit_rows",
                    bf.get("total_games",    0),
                    bf.get("total_bat_rows", 0),
                    bf.get("total_pit_rows", 0),
                )
            except Exception as exc:
                log.error(
                    f"[nightly] new-player gamelog backfill FAILED (non-fatal): {exc}"
                )

        # Phase 6 — hot/cold heat. Reads fully-ingested, deduped gamelogs
        # vs season baselines and stamps heat_score / heat_tier. Non-fatal.
        log.info("[nightly] starting heat phase")
        try:
            with connection.get_session() as db:
                heat = data_service.compute_all_player_heat(db, current_year)
            log.info(
                f"[nightly] heat computed — scored: {heat.get('scored', 0)}, "
                f"hot: {heat.get('hot', 0)}, cold: {heat.get('cold', 0)}, "
                f"neutral: {heat.get('neutral', 0)}, skipped: {heat.get('skipped', 0)}"
            )
        except Exception as exc:
            log.error(f"[nightly] heat phase FAILED (non-fatal): {exc}")
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


def _run_catchup_update() -> None:
    """Background thread: drive `nightly_update.run_catchup_update`
    and stash the result on `_catchup_state` so the status endpoint
    can report it. Mirrors `_run_nightly_update`'s lock/finally
    pattern but skips the cache-clear (the catch-up's touched
    rows are too few to warrant blowing the whole TTL window)."""
    pid = os.getpid()
    tid = threading.get_ident()
    log.info(f"[catchup] thread entry pid={pid} tid={tid}")
    with _catchup_lock:
        _catchup_state.update(
            running=True, phase="scanning", result=None, error=None,
        )
    try:
        result = nightly_update.run_catchup_update()
        with _catchup_lock:
            _catchup_state["result"] = result
        log.info(f"[catchup] thread complete: {result}")
    except Exception as exc:
        tb = traceback.format_exc()
        log.error(f"[catchup] FAILED pid={pid} tid={tid}: {exc}\n{tb}")
        with _catchup_lock:
            _catchup_state["error"] = f"{exc}\n{tb}"
    finally:
        with _catchup_lock:
            _catchup_state["running"] = False
            _catchup_state["phase"]   = None
            _catchup_state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"
        log.info(f"[catchup] thread exit pid={pid} tid={tid} state={_catchup_state}")


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    data_service.init_db()
    # Start the in-process live-game proxy loop (single-worker assumption —
    # see live_service.py). Guarded so it starts at most once per process.
    live_service.start_live_loop()
    # Open + warm the play-by-play store in a daemon thread so boot isn't
    # blocked by the ~900ms cold open + full-scan warmup over the slow volume;
    # every other endpoint serves immediately while it warms.
    threading.Thread(target=_plays_warmup_worker, name="plays-warmup",
                     daemon=True).start()
    yield
    live_service.stop_live_loop()


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


@app.get("/players/heat")
def players_heat(
    tier: str | None = Query(
        None,
        description="'hot' (red_hot + hot) or 'cold' (ice_cold + cold). "
                    "Omit to return both halves.",
    ),
    limit: int = Query(10, ge=1, le=50, description="Max players per list."),
):
    """League-wide hottest / coldest qualified players for the Search tab,
    split six ways: `hot_hitters` / `hot_starters` / `hot_relievers` and the
    cold trio (`cold_hitters` / `cold_starters` / `cold_relievers`). With
    `tier=hot` returns just the hot trio, `tier=cold` just the cold trio,
    omitted returns all six. Starters vs relievers come from each pitcher's
    stored `heat_role`. Only ratings refreshed in the last couple of days are
    included, so stale heat never surfaces."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if tier is not None and tier not in ("hot", "cold"):
        raise HTTPException(status_code=400, detail="tier must be 'hot' or 'cold'")
    return data_service.get_heat_leaders(tier=tier, limit=limit)


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


@app.get("/awards/available")
def awards_available():
    """Which (award, year, league) voting combinations actually have data,
    so a picker can offer only valid choices. Grouped per award: overall
    league set, min/max year, and a newest-first per-year list of which
    leagues have data that year (this captures the ML→AL/NL split — early
    Cy Young / Rookie of the Year years return ['ML'], later years
    ['AL','NL']). Restricted to `_AWARD_VOTING_IDS` so it never advertises an
    award the /awards/voting breakdown can't render. Read-only metadata."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return data_service.get_awards_available(_AWARD_VOTING_IDS)


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


@app.get("/players/{player_id}/pitcher-record-at-date")
def player_pitcher_record_at_date(
    player_id: int,
    game_date: datetime.date = Query(
        ...,
        description="Cumulative through this date (inclusive). yyyy-mm-dd.",
    ),
):
    """Pitcher's cumulative W-L-SV through `game_date` (inclusive),
    counted from this player's `pitching_gamelogs` rows for the
    season the date falls in. Used by the box-score and score-card
    expanded views to render a pitcher's record AS OF the game
    they're displaying — independent of any later games BDL has
    since absorbed into season stats.

    `pitching_gamelogs` doesn't have boolean win/loss/save columns;
    the decision is stored under the `result: String` column with
    values `"W" / "L" / "S" / "H" / "ND"`. We aggregate by string
    match so the totals always reflect what the per-game pipeline
    actually wrote.

    `includes_today` reports whether any row in the result set
    falls on the queried `game_date`. Callers (iOS) use this to
    decide whether to add a `+1` for the displayed game's just-
    happened decision: when `false` the gamelog ingest hasn't
    absorbed this game yet (mid-game or before the next catch-up
    run), and the displayed record needs the bump. When `true`
    the nightly or catch-up already absorbed it and the record
    already counts it — no bump.

    Returns `{player_id, game_date, wins, losses, saves,
    includes_today}`. Zeros + `false` for a player with no
    qualifying gamelog rows (rookie debuting on `game_date` with
    no prior games will read 0-0-0 / false)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    season = game_date.year
    with connection.get_session() as db:
        rows = (
            db.query(PitchingGameLog.result, PitchingGameLog.game_date)
              .filter(PitchingGameLog.player_id == player_id)
              .filter(PitchingGameLog.season    == season)
              .filter(PitchingGameLog.game_date <= game_date)
              .all()
        )
    wins   = sum(1 for r in rows if r.result == "W")
    losses = sum(1 for r in rows if r.result == "L")
    saves  = sum(1 for r in rows if r.result == "S")
    includes_today = any(r.game_date == game_date for r in rows)
    return {
        "player_id":      player_id,
        "game_date":      game_date.isoformat(),
        "wins":           wins,
        "losses":         losses,
        "saves":          saves,
        "includes_today": includes_today,
    }


@app.get("/players/{player_id}/batter-stats-at-date")
def player_batter_stats_at_date(
    player_id: int,
    game_date: datetime.date = Query(
        ...,
        description="Cumulative through this date (inclusive). yyyy-mm-dd.",
    ),
):
    """Batter's cumulative HR / 2B / 3B through `game_date`
    (inclusive), counted from this player's `batting_gamelogs`
    rows for the season the date falls in. Sister endpoint to
    `/pitcher-record-at-date`; used by the box-score and score-
    card notable-plays lines to render an accurate "(N)" total
    AS OF the game being displayed, independent of any later
    games BDL has since absorbed into season stats.

    `includes_today` reports whether any row in the result set
    falls on the queried `game_date`. Callers (iOS) use this to
    decide whether to fold the displayed game's just-hit HR/2B/3B
    into the displayed total: when `false` the gamelog ingest
    hasn't absorbed this game yet (mid-game or before the next
    catch-up run) and the per-game count needs to be added. When
    `true` the nightly already absorbed it and the total already
    includes it.

    Returns `{player_id, game_date, home_runs, doubles, triples,
    includes_today}`. Zeros + `false` for a player with no
    qualifying gamelog rows."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    season = game_date.year
    with connection.get_session() as db:
        rows = (
            db.query(
                BattingGameLog.HR,
                BattingGameLog.doubles,
                BattingGameLog.triples,
                BattingGameLog.game_date,
            )
              .filter(BattingGameLog.player_id == player_id)
              .filter(BattingGameLog.season    == season)
              .filter(BattingGameLog.game_date <= game_date)
              .all()
        )
    home_runs = sum((r.HR      or 0) for r in rows)
    doubles   = sum((r.doubles or 0) for r in rows)
    triples   = sum((r.triples or 0) for r in rows)
    includes_today = any(r.game_date == game_date for r in rows)
    return {
        "player_id":      player_id,
        "game_date":      game_date.isoformat(),
        "home_runs":      home_runs,
        "doubles":        doubles,
        "triples":        triples,
        "includes_today": includes_today,
    }


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


@app.get("/teams/{team_id}/awards")
def team_awards(team_id: str):
    """Major-award winners (MVP, Cy Young, Rookie of the Year, Gold
    Glove, Silver Slugger) across the franchise's entire history,
    grouped by award type and sorted year-desc within each group.

    `team_id` accepts either the Lahman teamID (e.g. "LAN") or the
    franchID (e.g. "LAD") — same resolution path as `/history`, so a
    relocation history (MON → WSN) collapses into one franchise.
    A winner is attributed to the franchise when they logged a
    batting or pitching season for one of the franchise's team codes
    in the award year."""
    result = data_service.get_team_awards(team_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No franchise found for team_id {team_id!r}",
        )
    return result


# Lahman round-code → display name. Doubled-up rounds in the wild-
# card era (ALDS1/ALDS2, NLWC1/NLWC2, etc.) collapse to the bare
# round name so the iOS card doesn't read "ALDS1" vs "ALDS2" as
# two different things. Anything unmapped passes through unchanged.
_POSTSEASON_ROUND_DISPLAY: dict[str, str] = {
    "WS":    "World Series",
    "ALCS":  "ALCS",
    "NLCS":  "NLCS",
    "ALDS":  "ALDS",  "ALDS1": "ALDS",  "ALDS2": "ALDS",
    "NLDS":  "NLDS",  "NLDS1": "NLDS",  "NLDS2": "NLDS",
    "ALWC":  "AL Wild Card", "ALWC1": "AL Wild Card", "ALWC2": "AL Wild Card",
    "NLWC":  "NL Wild Card", "NLWC1": "NL Wild Card", "NLWC2": "NL Wild Card",
}


@app.get("/teams/{team_id}/postseason")
def team_postseason(team_id: str):
    """All postseason series this franchise has played in (winner or
    loser side), sorted year desc / round. `team_id` accepts either
    the Lahman teamID (e.g. "LAN") or the franchID (e.g. "LAD") —
    same resolution path the `/history` endpoint uses, so a
    relocation history (MON → WSN) collapses to a single franchise
    response.

    Each entry carries `won: bool` (this franchise's side) and the
    opposing Lahman teamID, so the iOS card can render "Beat TBA 4-2
    in the 2020 World Series" without further joins."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    with connection.get_session() as db:
        franch_id = crud.get_team_franchise(db, team_id)
        if franch_id is None:
            raise HTTPException(
                status_code=404,
                detail=f"No franchise found for team_id {team_id!r}",
            )
        # All Lahman team_ids that share this franch_id — covers
        # relocations and rebrands so a series won under the old
        # code still shows up.
        franchise_team_ids = {
            r.team_id for r in
            db.query(TeamSeason.team_id)
              .filter(TeamSeason.franch_id == franch_id)
              .distinct()
              .all()
        }
        rows = crud.get_series_post_by_team(db, franch_id)
        postseason = []
        for r in rows:
            won = r.team_id_winner in franchise_team_ids
            opponent = r.team_id_loser if won else r.team_id_winner
            postseason.append({
                "year":     r.year,
                "round":    _POSTSEASON_ROUND_DISPLAY.get(r.round, r.round),
                "won":      won,
                "opponent": opponent,
                "wins":     r.wins,
                "losses":   r.losses,
            })

    return {
        "team_id":    team_id,
        "postseason": postseason,
    }


@app.get("/live/games")
def live_games():
    """All currently-live MLB games as compact summary cards (teams, score,
    inning/half/outs, live flag), served straight from the live-proxy cache —
    never calls balldontlie. Empty list when nothing is live. Each card (and the
    envelope) carries the snapshot's `fetched_at` timestamp."""
    return live_service.get_live_summary()


@app.get("/live/games/{game_id}")
def live_game(game_id: int):
    """The full unified live snapshot for one game (score, linescore grid,
    inning/count/base state, current batter/pitcher, recent + scoring plays, and
    per-player batting/pitching lines) — all from ONE cached snapshot, so every
    element is mutually consistent. 404 when the game isn't currently live /
    not in cache. Reads cache only — never calls balldontlie."""
    snapshot = live_service.get_live_game(game_id)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"Game {game_id} is not currently live (no cached snapshot)",
        )
    return snapshot


@app.get("/postseason/available")
def postseason_available():
    """Years that have postseason series, newest first — drives the Playoff
    History year picker so it only offers real years (gaps like 1904 and 1994,
    which had no postseason, are simply absent). Read-only metadata."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    with connection.get_session() as db:
        years = crud.get_series_post_years(db)
    return {"years": years}


@app.get("/postseason/champions")
def postseason_champions():
    """Every World Series result, newest year first, in one call — drives the
    Playoff History champions list. Each row carries explicit winner/loser
    (raw Lahman codes) + leagues + series W-L (winner perspective). Read-only."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    with connection.get_session() as db:
        rows = crud.get_world_series_results(db)
        # Shape INSIDE the session block (the session commits + expires ORM
        # instances on exit, so attribute reads afterward would raise
        # DetachedInstanceError — same fix as /postseason?year=).
        champions = [
            {
                "year":          r.year,
                "winner":        r.team_id_winner,
                "loser":         r.team_id_loser,
                "winner_league": r.league_id_winner,
                "loser_league":  r.league_id_loser,
                "wins":          r.wins,
                "losses":        r.losses,
                "ties":          r.ties,
            }
            for r in rows
        ]
    return {"champions": champions}


@app.get("/postseason")
def postseason(year: int = Query(..., description="Postseason year")):
    """All postseason series for one year, across both leagues, shaped for
    bracket rendering. Each series returns the winner and loser explicitly (a
    bracket needs to know who advanced), BOTH the raw Lahman `round_code` AND a
    normalized display `round` label, and the winner-perspective W-L (`wins`/
    `losses`/`ties`).

    Unrecognized / legacy round codes (1981 AEDIV/AWDIV/NEDIV/NWDIV, pre-1900
    Temple Cup codes, etc.) pass through with their raw code as both
    `round_code` and `round` (the display map falls back to the raw code), so
    the client can choose to bracket them or fall back to a flat list — nothing
    is filtered out.

    Team codes stay raw Lahman codes; the iOS side maps them to abbreviation /
    color via its shared helpers. An empty `series` (no rows for the year) is a
    valid response — matches `/teams/{id}/postseason`, which never 404s on an
    empty result."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    with connection.get_session() as db:
        rows = crud.get_series_post_by_year(db, year)
        # Shape INSIDE the session block: get_session() commits on exit, which
        # expires the ORM instances (expire_on_commit defaults True), so reading
        # their attributes after the block raises DetachedInstanceError. Same
        # pattern as /teams/{id}/postseason, which builds its list in-block.
        series = [
            {
                "round_code":    r.round,
                "round":         _POSTSEASON_ROUND_DISPLAY.get(r.round, r.round),
                "winner":        r.team_id_winner,
                "loser":         r.team_id_loser,
                "winner_league": r.league_id_winner,
                "loser_league":  r.league_id_loser,
                "wins":          r.wins,
                "losses":        r.losses,
                "ties":          r.ties,
            }
            for r in rows
        ]
    return {"year": year, "series": series}


# ---------------------------------------------------------------------------
# Leaderboards
# ---------------------------------------------------------------------------

# Stats accepted by the leaderboard endpoint. Derived directly from the
# data_service catalogs so validation + the 400 "Try one of" message always
# track exactly what the catalogs support — adding a stat there makes it valid
# here automatically (no manual sync, no drift). `set(dict)` yields the keys.
_LEADERBOARD_BATTING_STATS  = set(data_service._LEADERBOARD_BATTING)
_LEADERBOARD_PITCHING_STATS = set(data_service._LEADERBOARD_PITCHING)


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
    # Resolve relocation aliases (e.g. "ATH" → "OAK") before validating
    # so the canonical franchise code is both accepted and forwarded.
    if team is not None:
        team = data_service._resolve_team_alias(team)
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


@app.post("/admin/reset-player-season/{player_id}")
def admin_reset_player_season(
    player_id: int,
    season:    int = Query(..., description="Season year to reset, e.g. 2026."),
    team_code: str = Query(..., description="Team code to re-pin to (e.g. 'ATH')."),
):
    """Reset a single player's `player_seasons` row for `season` and
    re-pin them to `team_code` so the API-derived bio fields
    (`current_team` / `team_code`) resolve to the new team.

    Schema note: `current_team` and `team_code` are NOT stored on
    the `players` bio table — they're derived at response time from
    the latest `player_seasons.team` (see `_latest_team_info` in
    `data_service.py`). "Update the bio" therefore means rewriting
    the season row whose `team` the bio derivation reads from.

    Steps:
      1. DELETE FROM player_seasons WHERE player_id=? AND year=?
      2. INSERT a stub player_seasons row with `team = team_code`;
         every stat column is left NULL. The next nightly run will
         repopulate the stats from BDL under the corrected team.

    Use case: BDL ingested a player under the wrong team (mid-
    season trade BDL didn't catch). Resetting wipes the bad stats
    and pins the bio to the correct team while we wait for the
    nightly to re-fill the row."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    with connection.get_session() as db:
        deleted = (
            db.query(PlayerSeason)
              .filter(PlayerSeason.player_id == player_id)
              .filter(PlayerSeason.year      == season)
              .delete(synchronize_session=False)
        )
        db.add(PlayerSeason(
            player_id = player_id,
            year      = season,
            team      = team_code,
        ))
        db.commit()

    return {
        "status":       "ok",
        "player_id":    player_id,
        "season":       season,
        "deleted_rows": int(deleted),
        "inserted_row": {
            "player_id": player_id,
            "year":      season,
            "team":      team_code,
        },
    }


@app.post("/admin/recalculate-pa")
def admin_recalculate_pa(
    season: int | None = Query(
        None,
        description="Season year to backfill PA for. Defaults to the current season.",
    ),
):
    """Backfill plate appearances (PA) for `season`.

    PA is absent from BDL's season + per-game payloads, so the nightly
    never populated it — which silently excluded every active player
    from the rate-stat leaderboards (the qualifier filters on
    `PA >= min_pa`). This recomputes PA from the counting components:

      1. `batting_gamelogs.PA` — set to AB + BB + HBP + SF for rows
         that don't already carry a stored PA. (The BDL gamelog parser
         now stores BDL's `plate_appearances` directly going forward;
         this fills in the pre-existing NULL rows.)
      2. `player_seasons.PA` — set to the sum of those per-game
         components across the season.

    Idempotent; safe to re-run. The component sum omits SH, so it's a
    slight undercount vs true PA — but far better than the NULL it
    replaces.

    Column identifiers are double-quoted because the schema stores
    them in their original mixed/upper case (Postgres folds unquoted
    identifiers to lowercase — see `connection.py`)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    if season is None:
        season = data_service._current_year()

    gamelog_sql = _sa_text(
        """
        UPDATE batting_gamelogs
        SET "PA" = COALESCE("AB", 0) + COALESCE("BB", 0)
                 + COALESCE("HBP", 0) + COALESCE("SF", 0)
        WHERE season = :season AND "PA" IS NULL
        """
    )
    season_sql = _sa_text(
        """
        UPDATE player_seasons
        SET "PA" = (
            SELECT COALESCE(SUM(
                COALESCE("AB", 0) + COALESCE("BB", 0)
                + COALESCE("HBP", 0) + COALESCE("SF", 0)
            ), 0)
            FROM batting_gamelogs
            WHERE batting_gamelogs.player_id = player_seasons.player_id
              AND batting_gamelogs.season    = player_seasons.year
        )
        WHERE year = :season
        """
    )

    with connection.get_session() as db:
        gl_result = db.execute(gamelog_sql, {"season": season})
        ps_result = db.execute(season_sql, {"season": season})
        db.commit()
        gamelogs_updated = gl_result.rowcount
        seasons_updated  = ps_result.rowcount

    return {
        "status":           "ok",
        "season":           season,
        "gamelogs_updated": int(gamelogs_updated) if gamelogs_updated is not None else None,
        "seasons_updated":  int(seasons_updated) if seasons_updated is not None else None,
    }


@app.post("/admin/recalculate-batting-counting")
def admin_recalculate_batting_counting(
    season: int | None = Query(
        None,
        description="Season year to aggregate. Defaults to the current season.",
    ),
    player_id: int | None = Query(
        None,
        description="Optional — target one player. Omit for a league-wide pass.",
    ),
):
    """Fill the batting counting stats that BDL's season-stats payload
    omits — PA, H, HBP, SF, CS, IBB, GIDP, SH — by summing them from
    `batting_gamelogs` into the matching `player_seasons` row.

    Motivating cases: (a) a batter whose current-season row was created
    fresh from BDL (`/admin/backfill-player-history` / swap-repair) lands
    with these columns NULL, because the nightly batter phase never writes
    them; (b) a stale bref-seed value (e.g. Aaron Judge's GIDP frozen below
    the live total) that needs refreshing to the game-log truth.

    Delegates to `data_service.recalculate_batting_counting`, which
    OVERWRITES from the game-log sum for the CURRENT season (the logs are
    the freshest source) but is COALESCE-only (fill-NULLs) for PAST seasons
    so historical Lahman/bref totals are never clobbered. The `EXISTS`
    guard means only rows that actually have game logs for the year are
    touched. Idempotent.

    Notes:
      • GIDP / SH come from `batting_gamelogs` (`gidp` / `sac_bunts`), so
        older logs sum to 0 until re-pulled via `/admin/backfill-bdl-gamelogs`.
      • IBB: BDL `/stats` omits it per game, so it usually sums to 0
        (MLB-Stats-API-sourced logs do carry it).
      • 2B/3B left alone — they render as 0 acceptably."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    if season is None:
        season = data_service._current_year()

    with connection.get_session() as db:
        seasons_updated = data_service.recalculate_batting_counting(db, season, player_id)
        db.commit()

    return {
        "status":          "ok",
        "season":          season,
        "player_id":       player_id,
        "mode":            "overwrite" if season >= data_service._current_year() else "fill-nulls",
        "fields":          list(data_service._BATTING_COUNTING_FIELDS),
        "seasons_updated": int(seasons_updated),
    }


@app.post("/admin/recalculate-pitching-counting")
def admin_recalculate_pitching_counting(
    season: int | None = Query(
        None,
        description="Season year to aggregate. Defaults to the current season.",
    ),
    player_id: int | None = Query(
        None,
        description="Optional — target one player. Omit for a league-wide pass.",
    ),
):
    """Pitcher analog of `/admin/recalculate-batting-counting`. Fill the pitcher
    counting stats BDL's season-stats payload omits — R (runs allowed) and HBP —
    by summing them from `pitching_gamelogs` into the matching `pitcher_seasons`
    row. The per-game data already exists (the parser reads `p_runs` / `pitching_hbp`),
    so no game-log backfill is needed — only this rollup, which the pipeline was
    missing.

    Motivating case: a pitcher whose current-season row was built fresh from BDL
    (`/admin/backfill-player-history` / seed-then-nightly, e.g. Cade Smith) lands
    with R/HBP NULL because the BDL pitcher phase never writes them.

    Delegates to `data_service.recalculate_pitching_counting`, which OVERWRITES
    from the game-log sum for the CURRENT season (freshest source) but is
    COALESCE-only (fill-NULLs) for PAST seasons so historical Lahman/bref totals
    are never clobbered. The EXISTS guard means only rows that actually have game
    logs for the year are touched. Idempotent.

    BFP is intentionally excluded — `batters_faced` isn't captured on
    `pitching_gamelogs` yet (needs a schema column + parser change + gamelog
    backfill; separate task)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    if season is None:
        season = data_service._current_year()

    with connection.get_session() as db:
        seasons_updated = data_service.recalculate_pitching_counting(db, season, player_id)
        db.commit()

    return {
        "status":          "ok",
        "season":          season,
        "player_id":       player_id,
        "mode":            "overwrite" if season >= data_service._current_year() else "fill-nulls",
        "fields":          list(data_service._PITCHING_COUNTING_FIELDS),
        "seasons_updated": int(seasons_updated),
    }


@app.post("/admin/backfill-season-source")
def admin_backfill_season_source():
    """One-time, idempotent labeling of PRE-INGEST row provenance on
    `player_seasons` / `pitcher_seasons`.

    Uses the documented Lahman-vs-BDL boundary (see `lahman_load`): rows before
    2008 were loaded from the Lahman bulk archive; 2008+ come from the BDL /
    current pipeline. WAR/OPS+ are a separate BRef column-overlay, not a row
    source, so the row `source` is only ever 'lahman' or 'bdl' at this stage.

    `WHERE source IS NULL` makes it idempotent and safe to re-run — it never
    relabels a row the Retrosheet historical ingest has since flipped to
    'retrosheet'. Returns rows labeled per table + source."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    labeled: dict = {}
    with connection.get_session() as db:
        for table in ("player_seasons", "pitcher_seasons"):
            # Table names are hardcoded literals (not user input) — safe.
            lahman = db.execute(_sa_text(
                f"UPDATE {table} SET source = 'lahman' "
                "WHERE year < 2008 AND source IS NULL"
            )).rowcount or 0
            bdl = db.execute(_sa_text(
                f"UPDATE {table} SET source = 'bdl' "
                "WHERE year >= 2008 AND source IS NULL"
            )).rowcount or 0
            labeled[table] = {"lahman": int(lahman), "bdl": int(bdl)}
        db.commit()

    return {"status": "ok", "labeled": labeled}


@app.post("/admin/populate-retro-id")
def admin_populate_retro_id():
    """Stamp `retro_id` on `players` + `pitchers` bio rows from the Chadwick
    key_mlbam → key_retro bridge (`backend/data/retrosheet/chadwick_retro_bridge.csv`).

    Matches the register's `key_mlbam` to our MLBAM primary key and sets
    `retro_id = key_retro`. OVERWRITE (not fill-null): the register is
    authoritative and retro ids are stable, so re-running re-applies the same
    value (and would pick up any register correction). A player with a row in
    BOTH tables (two-way / dual-bio) gets the same retro_id on each, since both
    are updated by `player_id`.

    Returns the map size applied plus the resulting count of bio rows that now
    carry a retro_id (the useful verification figure — non-null after the run)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    mp = data_service._load_chadwick_mlbam_to_retro()
    if not mp:
        return {"status": "ok", "distinct_retro_applied": 0,
                "players_updated": 0, "pitchers_updated": 0,
                "note": "retro bridge CSV empty or missing — no-op"}

    params = [{"mlbam": m, "retro": r} for m, r in mp.items()]
    with connection.get_session() as db:
        for table in ("players", "pitchers"):
            # Table names are hardcoded literals (not user input) — safe.
            db.execute(
                _sa_text(f"UPDATE {table} SET retro_id = :retro "
                         "WHERE player_id = :mlbam"),
                params,
            )
        db.commit()
        # executemany rowcount isn't reliable across drivers; count the result
        # directly — rows now carrying a retro_id (== rows populated this run
        # on a fresh column; stable on re-run since it's an overwrite).
        players_pop = db.execute(_sa_text(
            "SELECT COUNT(*) FROM players WHERE retro_id IS NOT NULL")).scalar() or 0
        pitchers_pop = db.execute(_sa_text(
            "SELECT COUNT(*) FROM pitchers WHERE retro_id IS NOT NULL")).scalar() or 0

    return {
        "status":                "ok",
        "distinct_retro_applied": len(mp),
        "players_updated":        int(players_pop),
        "pitchers_updated":       int(pitchers_pop),
    }


@app.post("/admin/recalculate-batting-rates")
def admin_recalculate_batting_rates(
    season: int | None = Query(
        None,
        description="Season year to recompute. Defaults to the current season.",
    ),
    player_id: int | None = Query(
        None,
        description="Optional — target one player. Omit for a league-wide pass.",
    ),
):
    """Compute the advanced batting RATE columns — wOBA, K_pct, BB_pct, ISO —
    on `player_seasons` from the stored components. BDL's season_stats omits
    these, so current-season rows land NULL (blank career rates + empty
    wOBA/K%/BB%/ISO leaderboards for the year). Run AFTER
    `/admin/recalculate-batting-counting` so the components are correct first.

    Standard wOBA linear weights (uBB = BB - IBB, singles), matching the bref
    builder. Overwrites the season's rows (live components are authoritative)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    if season is None:
        season = data_service._current_year()

    with connection.get_session() as db:
        seasons_updated = data_service.recalculate_batting_rates(db, season, player_id)
        db.commit()

    return {
        "status":          "ok",
        "season":          season,
        "player_id":       player_id,
        "fields":          ["wOBA", "K_pct", "BB_pct", "ISO"],
        "seasons_updated": int(seasons_updated),
    }


@app.post("/admin/recalculate-leaders")
def admin_recalculate_leaders(
    season: int | None = Query(
        None,
        description="Season year to recompute leader badges for. Defaults to the current season.",
    ),
):
    """Re-run the leader-badge calculation (the per-stat league/majors
    #1 check) for every current-season player row, then drop the
    response cache so the recomputed badges are what live requests
    return.

    Leader badges aren't stored — they're derived at response time by
    `_season_leaders` from the current `player_seasons` data and the
    PA/IP qualifier. After the PA backfill changed who qualifies for
    rate stats, any career responses cached beforehand still carry the
    old badges (e.g. a now-qualifying leader missing his bold marker).
    This recomputes across the whole season in one pass (returning
    per-stat leader counts so the change is verifiable) and clears the
    in-process cache so `/players/{id}/stats/{career,current}` and the
    leaderboards recompute on next read."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    result = data_service.recalculate_current_season_leaders(season)

    # Badges live inside the cached career / current-stats / leaderboard
    # payloads; drop them so the recomputed values take effect now.
    cleared = _cache.stats().get("total_keys", 0)
    _cache.clear()

    return {
        "status":        "ok",
        **result,
        "cache_cleared": cleared,
    }


@app.post("/admin/backfill-pitching-hbp")
def admin_backfill_pitching_hbp(
    season: int | None = Query(
        None,
        description="Season to backfill HBP for. Defaults to the current season.",
    ),
    max_games: int | None = Query(
        None,
        description=(
            "Cap on games processed this call (chunking for long runs). "
            "Omit to process every affected game."
        ),
    ),
):
    """One-time backfill of pitcher hit-by-pitch on `season` game logs.

    HBP was never parsed from BDL's per-game `/stats`, so existing
    `pitching_gamelogs` rows have `HBP IS NULL`. This re-fetches `/stats`
    for each distinct BDL game id that still has a null-HBP pitching row,
    reads HBP off the raw stat line, and fills only the NULL cells (no
    other column is touched). Idempotent and resumable.

    Runs synchronously at the BDL rate limit (~0.22s/game), so a full
    in-season run over hundreds of games can take several minutes — pass
    `max_games` to chunk it under the request timeout and re-invoke until
    `remaining` reaches 0."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if season is None:
        season = data_service._current_year()
    result = data_service.backfill_pitching_hbp(season, max_games=max_games)
    if result.get("status") == "no_db":
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return result


@app.post("/admin/backfill-batting-ibb")
def admin_backfill_batting_ibb(
    max_games: int | None = Query(
        None,
        description=(
            "Cap on games processed this call (chunking for long runs). "
            "Omit to process every affected game."
        ),
    ),
):
    """One-time backfill of batter intentional-walks (IBB) on the CURRENT
    season's game logs.

    The gamelog parser hardcoded `IBB: None` (stale comment) though BDL's
    per-game `/stats` carries `intentional_walks`, so existing
    `batting_gamelogs` rows have `IBB IS NULL`. Because the season-counting
    aggregator OVERWRITES the current season from game-log sums, that zeroed
    the current-season `player_seasons.IBB` while past seasons keep their
    authoritative legacy IBB (fill-null-only, untouched).

    CURRENT SEASON ONLY — there is intentionally NO `season` parameter: the
    endpoint always targets `_current_year()`, so it can never re-parse
    past-season game logs (which would risk clobbering good legacy data). It
    re-fetches `/stats` per affected game, fills only the NULL `IBB` cells
    from `intentional_walks`, then (on the final chunk) runs
    `recalculate_batting_counting` so the season rollup updates immediately.

    Runs synchronously at the BDL rate limit (~0.22s/game); pass `max_games`
    to chunk long runs and re-invoke until `remaining` reaches 0."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    season = data_service._current_year()
    result = data_service.backfill_batting_ibb(season, max_games=max_games)
    if result.get("status") == "no_db":
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return result


@app.post("/admin/add-historical-player")
def admin_add_historical_player(
    mlbam_id:    int  = Query(..., description="MLBAM player id (our primary key)."),
    bbref_id:    str  = Query(..., description="Baseball Reference id, e.g. 'dickera01'."),
    full_name:   str  = Query(..., description="Player's full name."),
    position:    str  = Query(..., description="Position abbrev (e.g. 'SP', '1B')."),
    bio_type:    str  = Query(..., description="'batter' or 'pitcher' — picks the bio table."),
    debut_year:  int  = Query(..., description="MLB debut year — written to `mlb_debut`."),
    last_season: int  = Query(..., description="Last MLB season year — written to `mlb_last_season`."),
    birth_year:  int | None = Query(None),
    birth_month: int | None = Query(None),
    birth_day:   int | None = Query(None),
    throws:      str | None = Query(None, description="'R' / 'L'."),
    bats:        str | None = Query(None, description="'R' / 'L' / 'B'."),
):
    """Insert a bio row for a historical player Lahman doesn't ship
    (e.g. R.A. Dickey, missing 19th-century players, etc.) so the
    awards / stats / search surfaces have a record to point at.

    `headshot_url` is NOT a stored column — every API response derives
    it from the row's `player_id` via
    `data_service._headshot_url(...)`, so the headshot lights up
    automatically once the bio is inserted. The derived URL is
    returned in the response body for the caller's reference.

    After running this, call `/admin/backfill-player-history`
    (career stats) or `/admin/load-award-shares` (just vote rows)
    to populate the player's downstream tables.

    Returns 409 if a bio row for this `mlbam_id` already exists in
    the chosen table — re-running is intentionally not idempotent
    so the caller can spot stale data.
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if bio_type not in ("batter", "pitcher"):
        raise HTTPException(
            status_code=400,
            detail="bio_type must be 'batter' or 'pitcher'",
        )

    model = Pitcher if bio_type == "pitcher" else Player
    with connection.get_session() as db:
        existing = db.query(model).filter(model.player_id == mlbam_id).first()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{bio_type} row already exists for mlbam_id {mlbam_id}",
            )
        row = model(
            player_id       = mlbam_id,
            name            = full_name,
            bbref_id        = bbref_id,
            mlb_debut       = debut_year,
            mlb_last_season = last_season,
            position        = position,
            bats            = bats,
            throws          = throws,
            birth_year      = birth_year,
            birth_month     = birth_month,
            birth_day       = birth_day,
        )
        db.add(row)
        db.commit()

    return {
        "status":          "ok",
        "bio_type":        bio_type,
        "player_id":       mlbam_id,
        "bbref_id":        bbref_id,
        "name":            full_name,
        "position":        position,
        "mlb_debut":       debut_year,
        "mlb_last_season": last_season,
        "birth_year":      birth_year,
        "birth_month":     birth_month,
        "birth_day":       birth_day,
        "throws":          throws,
        "bats":            bats,
        "headshot_url":    data_service._headshot_url(mlbam_id),
    }


@app.post("/admin/reload-player-lahman/{player_id}")
def admin_reload_player_lahman(player_id: int):
    """Load (or refresh) Lahman career stats for a single player by
    `bbref_id` filter against Batting.csv / Pitching.csv. Designed as
    the follow-up to `/admin/add-historical-player` — the bio row
    lands first via that endpoint, then this one pulls the player's
    actual seasons in.

    Auto-detects the side to load: if a `players` row exists, scans
    Batting.csv; if a `pitchers` row exists, scans Pitching.csv;
    two-way bio rows on both sides get both passes (rare, but the
    schema supports it). Returns 404 when neither bio row is found.

    Stints within a year are aggregated into a single season row
    (matches the existing `_read_batting_aggregated` /
    `_read_pitching_aggregated` semantics). Unlike the full Lahman
    loader, this endpoint deliberately does NOT apply CUTOFF_YEAR —
    a manually-added historical player typically has zero bref-
    sourced rows to collide with, and we want every Lahman season
    they have."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    # Pull the bbref_id + side flags out of the ORM rows BEFORE the
    # session closes — touching `player_row.bbref_id` after the
    # `with` block exits would raise `DetachedInstanceError` because
    # SQLAlchemy expires the row's attributes on session close.
    with connection.get_session() as db:
        player_row  = db.query(Player).filter(Player.player_id == player_id).first()
        pitcher_row = db.query(Pitcher).filter(Pitcher.player_id == player_id).first()
        if player_row is None and pitcher_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No bio row in players or pitchers for player_id {player_id}",
            )
        # `bbref_id` is the join key into Lahman's playerID column.
        # Both bio sides carry the same one when both exist.
        bbref_id    = (player_row.bbref_id if player_row else pitcher_row.bbref_id)
        has_batter  = player_row  is not None
        has_pitcher = pitcher_row is not None

    if not bbref_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"player_id {player_id} has no bbref_id — Lahman CSVs "
                "are keyed on bbref/playerID, so we can't locate rows "
                "without it."
            ),
        )

    batting_saved  = 0
    pitching_saved = 0
    years_batting:  list[int] = []
    years_pitching: list[int] = []

    if has_batter:
        seasons = _lahman_batting_seasons_for_bbref(bbref_id)
        if seasons:
            with connection.get_session() as db:
                crud.save_player_seasons(db, player_id, seasons)
            batting_saved = len(seasons)
            years_batting = sorted(s["year"] for s in seasons)

    if has_pitcher:
        seasons = _lahman_pitching_seasons_for_bbref(bbref_id)
        if seasons:
            with connection.get_session() as db:
                crud.save_pitcher_seasons(db, player_id, seasons)
            pitching_saved = len(seasons)
            years_pitching = sorted(s["year"] for s in seasons)

    return {
        "status":          "ok",
        "player_id":       player_id,
        "bbref_id":        bbref_id,
        "batting_saved":   batting_saved,
        "pitching_saved":  pitching_saved,
        "years_batting":   years_batting,
        "years_pitching":  years_pitching,
    }


# Stint aggregation + derived-stat layout that follows
# `lahman_load._read_batting_aggregated` and the per-row `_load_batting`
# builder, but scoped to a single bbref id and without the CUTOFF_YEAR
# gate. Stored here instead of as a generic helper in `lahman_load.py`
# because the rest of that module is full-table-scan oriented; this
# is the only single-player variant the codebase needs today.
def _lahman_batting_seasons_for_bbref(bbref_id: str) -> list[dict]:
    by_year: dict[int, dict] = {}
    with open(lahman_load.BATTING_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if row.get("playerID") != bbref_id:
                continue
            year  = int(row["yearID"])
            stint = int(row["stint"])
            agg = by_year.setdefault(year, {
                "stint":   0, "teamID": "", "lgID": "",
                "G": 0, "AB": 0, "R": 0, "H": 0, "2B": 0, "3B": 0, "HR": 0,
                "RBI": 0.0, "SB": 0.0, "CS": 0.0, "BB": 0, "SO": 0.0,
                "IBB": 0.0, "HBP": 0.0, "SH": 0.0, "SF": 0.0, "GIDP": 0.0,
            })
            agg["G"]    += lahman_load._i(row["G"])
            agg["AB"]   += lahman_load._i(row["AB"])
            agg["R"]    += lahman_load._i(row["R"])
            agg["H"]    += lahman_load._i(row["H"])
            agg["2B"]   += lahman_load._i(row["2B"])
            agg["3B"]   += lahman_load._i(row["3B"])
            agg["HR"]   += lahman_load._i(row["HR"])
            agg["RBI"]  += lahman_load._f(row["RBI"])
            agg["SB"]   += lahman_load._f(row["SB"])
            agg["CS"]   += lahman_load._f(row["CS"])
            agg["BB"]   += lahman_load._i(row["BB"])
            agg["SO"]   += lahman_load._f(row["SO"])
            agg["IBB"]  += lahman_load._f(row["IBB"])
            agg["HBP"]  += lahman_load._f(row["HBP"])
            agg["SH"]   += lahman_load._f(row["SH"])
            agg["SF"]   += lahman_load._f(row["SF"])
            agg["GIDP"] += lahman_load._f(row.get("GIDP"))
            if stint >= agg["stint"]:
                agg["stint"]  = stint
                agg["teamID"] = row["teamID"]
                agg["lgID"]   = row["lgID"]

    seasons: list[dict] = []
    for year, agg in by_year.items():
        derived = lahman_load._batting_derived(
            ab=agg["AB"], h=agg["H"],
            doubles=agg["2B"], triples=agg["3B"], hr=agg["HR"],
            bb=agg["BB"], ibb=agg["IBB"], hbp=agg["HBP"],
            so=agg["SO"], sf=agg["SF"], sh=agg["SH"],
        )
        seasons.append({
            "year":    year,
            "team":    agg["teamID"] or None,
            "league":  agg["lgID"] or None,
            "G":       agg["G"],
            "AB":      agg["AB"],
            "R":       agg["R"],
            "H":       agg["H"],
            "doubles": agg["2B"],
            "triples": agg["3B"],
            "HR":      agg["HR"],
            "RBI":     int(agg["RBI"]),
            "SB":      int(agg["SB"]),
            "CS":      int(agg["CS"]),
            "BB":      agg["BB"],
            "SO":      int(agg["SO"]),
            "IBB":     int(agg["IBB"]),
            "HBP":     int(agg["HBP"]),
            "SH":      int(agg["SH"]),
            "SF":      int(agg["SF"]),
            "GIDP":    int(agg["GIDP"]),
            "TB":      (int(agg["H"]) + int(agg["2B"])
                        + 2 * int(agg["3B"]) + 3 * int(agg["HR"])),
            **derived,
        })
    return seasons


def _lahman_pitching_seasons_for_bbref(bbref_id: str) -> list[dict]:
    by_year: dict[int, dict] = {}
    with open(lahman_load.PITCHING_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if row.get("playerID") != bbref_id:
                continue
            year  = int(row["yearID"])
            stint = int(row["stint"])
            agg = by_year.setdefault(year, {
                "stint":  0, "teamID": "", "lgID": "",
                "W": 0, "L": 0, "G": 0, "GS": 0, "CG": 0, "SHO": 0, "SV": 0,
                "IPouts": 0, "H": 0, "ER": 0, "R": 0, "HR": 0, "BB": 0, "SO": 0,
                "IBB": 0.0, "WP": 0, "BFP": 0.0, "HBP": 0.0, "BK": 0, "GF": 0,
                "SH": 0.0, "SF": 0.0, "GIDP": 0.0,
            })
            agg["W"]      += lahman_load._i(row["W"])
            agg["L"]      += lahman_load._i(row["L"])
            agg["G"]      += lahman_load._i(row["G"])
            agg["GS"]     += lahman_load._i(row["GS"])
            agg["CG"]     += lahman_load._i(row.get("CG"))
            agg["SHO"]    += lahman_load._i(row.get("SHO"))
            agg["SV"]     += lahman_load._i(row.get("SV"))
            agg["IPouts"] += lahman_load._i(row["IPouts"])
            agg["H"]      += lahman_load._i(row["H"])
            agg["ER"]     += lahman_load._i(row["ER"])
            agg["R"]      += lahman_load._i(row.get("R"))
            agg["HR"]     += lahman_load._i(row["HR"])
            agg["BB"]     += lahman_load._i(row["BB"])
            agg["SO"]     += lahman_load._i(row["SO"])
            agg["IBB"]    += lahman_load._f(row.get("IBB"))
            agg["WP"]     += lahman_load._i(row.get("WP"))
            agg["BFP"]    += lahman_load._f(row["BFP"])
            agg["HBP"]    += lahman_load._f(row["HBP"])
            agg["BK"]     += lahman_load._i(row.get("BK"))
            agg["GF"]     += lahman_load._i(row.get("GF"))
            agg["SH"]     += lahman_load._f(row.get("SH"))
            agg["SF"]     += lahman_load._f(row.get("SF"))
            agg["GIDP"]   += lahman_load._f(row.get("GIDP"))
            if stint >= agg["stint"]:
                agg["stint"]  = stint
                agg["teamID"] = row["teamID"]
                agg["lgID"]   = row["lgID"]

    seasons: list[dict] = []
    for year, agg in by_year.items():
        derived = lahman_load._pitching_derived(
            ipouts=agg["IPouts"], h=agg["H"], hr=agg["HR"],
            bb=agg["BB"], hbp=agg["HBP"], so=agg["SO"], bfp=agg["BFP"],
        )
        ip_dec = agg["IPouts"] / 3 if agg["IPouts"] > 0 else 0.0
        era = round(agg["ER"] * 9 / ip_dec, 2) if ip_dec > 0 else None
        ab_faced = agg["BFP"] - agg["BB"] - agg["HBP"] - agg["SH"] - agg["SF"]
        baopp = round(agg["H"] / ab_faced, 3) if ab_faced > 0 else None
        seasons.append({
            "year":    year,
            "team":    agg["teamID"] or None,
            "league":  agg["lgID"] or None,
            "W":       agg["W"],
            "L":       agg["L"],
            "G":       agg["G"],
            "GS":      agg["GS"],
            "CG":      agg["CG"],
            "SHO":     agg["SHO"],
            "SV":      agg["SV"],
            "GF":      agg["GF"],
            "H":       agg["H"],
            "ER":      agg["ER"],
            "R":       agg["R"],
            "HR":      agg["HR"],
            "BB":      agg["BB"],
            "IBB":     int(agg["IBB"]),
            "SO":      agg["SO"],
            "HBP":     int(agg["HBP"]),
            "WP":      agg["WP"],
            "BK":      agg["BK"],
            "BFP":     int(agg["BFP"]),
            "SH":      int(agg["SH"]),
            "SF":      int(agg["SF"]),
            "GIDP":    int(agg["GIDP"]),
            "ERA":     era,
            "BAOpp":   baopp,
            **derived,
        })
    return seasons


@app.post("/admin/update-player-bio/{player_id}")
def admin_update_player_bio(
    player_id:     int,
    bio_type:      str  = Query(..., description="'batter' or 'pitcher' — picks which bio table to update."),
    height:        int | None = Query(None, description="Height in inches."),
    weight:        int | None = Query(None, description="Weight in pounds."),
    birth_city:    str | None = Query(None),
    birth_state:   str | None = Query(None),
    birth_country: str | None = Query(None),
    debut:         str | None = Query(None, description="ISO date string, e.g. '2001-04-22'."),
    team_code:     str | None = Query(None, description="Lahman team code to set on the player's most-recent season row (since team_code is NOT a column on the bio — it's derived from the latest season's team)."),
):
    """Partial update of an existing player/pitcher bio row. Every
    bio param is optional — only fields supplied (non-None) are
    written.

    `team_code` is a special case: the bio tables don't carry a
    team column (the API derives `current_team` / `team_code` at
    response time from the latest `player_seasons`/`pitcher_seasons`
    row's `team`). When `team_code` is provided, we update the most
    recent season row's `team` for the chosen side. If the player
    has no season rows, `team_code` is silently ignored — there's
    nothing to write through to, and the caller can run
    `/admin/reload-player-lahman/{player_id}` (or insert a season
    via `/admin/reset-player-season/{player_id}`) first.

    Returns 404 if no bio row exists for `(bio_type, player_id)`.
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if bio_type not in ("batter", "pitcher"):
        raise HTTPException(
            status_code=400,
            detail="bio_type must be 'batter' or 'pitcher'",
        )

    bio_model    = Pitcher if bio_type == "pitcher" else Player
    season_model = PitcherSeason if bio_type == "pitcher" else PlayerSeason

    # Build the {column → value} update map from the non-None params.
    # Stored separately so the response can echo exactly what changed.
    bio_updates: dict[str, object] = {}
    if height        is not None: bio_updates["height"]        = height
    if weight        is not None: bio_updates["weight"]        = weight
    if birth_city    is not None: bio_updates["birth_city"]    = birth_city
    if birth_state   is not None: bio_updates["birth_state"]   = birth_state
    if birth_country is not None: bio_updates["birth_country"] = birth_country
    if debut         is not None: bio_updates["debut"]         = debut

    team_code_applied_to_year: int | None = None
    bio_snapshot: dict = {}

    with connection.get_session() as db:
        bio_row = (
            db.query(bio_model)
              .filter(bio_model.player_id == player_id)
              .first()
        )
        if bio_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No {bio_type} bio row for player_id {player_id}",
            )

        for col, value in bio_updates.items():
            setattr(bio_row, col, value)

        if team_code is not None:
            latest = (
                db.query(season_model)
                  .filter(season_model.player_id == player_id)
                  .order_by(season_model.year.desc())
                  .first()
            )
            if latest is not None:
                latest.team = team_code
                team_code_applied_to_year = latest.year

        db.commit()
        # Re-read while the session is still open so the returned
        # snapshot reflects what's actually persisted (and avoid
        # DetachedInstanceError on the response build).
        db.refresh(bio_row)
        bio_snapshot = {
            "player_id":     bio_row.player_id,
            "name":          bio_row.name,
            "bbref_id":      bio_row.bbref_id,
            "position":      bio_row.position,
            "bats":          bio_row.bats,
            "throws":        bio_row.throws,
            "height":        bio_row.height,
            "weight":        bio_row.weight,
            "birth_year":    bio_row.birth_year,
            "birth_month":   bio_row.birth_month,
            "birth_day":     bio_row.birth_day,
            "birth_city":    bio_row.birth_city,
            "birth_state":   bio_row.birth_state,
            "birth_country": bio_row.birth_country,
            "debut":         bio_row.debut,
        }

    return {
        "status":                     "ok",
        "bio_type":                   bio_type,
        "updated_columns":            list(bio_updates.keys()),
        "team_code_applied":          team_code if team_code_applied_to_year is not None else None,
        "team_code_applied_to_year":  team_code_applied_to_year,
        "bio":                        bio_snapshot,
    }


@app.post("/admin/repair-swapped-player")
def admin_repair_swapped_player(
    real_mlbam:    int = Query(..., description="The real modern player's MLBAM id (keeps its game logs + bdl_id)."),
    correct_bbref: str = Query(..., description="The bbref_id that actually belongs to this modern player, e.g. 'brownbe01'."),
    bogus_mlbam:   int | None = Query(None, description="Synthetic Negro-Leagues MLBAM id currently holding the real career (deleted if safe). OMIT for a straightforwardly-misclassified modern player whose stats already live on real_mlbam (no split) — phases A/F are then skipped."),
    position_type: str = Query(..., description="'pitcher' or 'batter' — the modern player's real side."),
    position:      str | None = Query(None, description="Position to stamp on a NEWLY-created bio row (defaults 'P' for pitcher, else None). Ignored when the correct-side row already exists."),
    skip_lahman_reload: bool = Query(False, description="When true, run phases C (create correct-side bio) + D (delete wrong-side bio + all season rows), then RETURN before phase E (the Lahman reload) and phase F. Use for a modern (post-Lahman-debut) player whose real season stats come from the nightly BDL ingest, not Lahman, and whose correct_bbref may be conflated (avoids re-injecting a bogus historical season)."),
):
    """Repair ONE swapped-Chadwick split player (see the Ben Brown
    investigation). A 2024 Negro-Leagues Chadwick merge cross-assigned
    synthetic MLBAM ids sharing a surname stem, so a modern player's real
    MLBAM id (`real_mlbam`) got linked to a historical player's bbref —
    loading the wrong/empty career — while a synthetic id (`bogus_mlbam`)
    soaked up the modern player's real Lahman career.

    Per-player sequence (each phase in its own session; safe to re-run —
    season writes upsert and the bogus delete is idempotent):
      A. Guard `bogus_mlbam`: confirm it has NO bdl_id and NO game logs.
         If it has either, the bogus delete (phase F) is SKIPPED and the
         response is flagged `ok_with_warnings` — we never destroy a row
         that carries live keys.
      B. Capture the modern player's bio off `real_mlbam` (prefer the
         bdl-stamped row — that's the BDL-synced modern identity).
      C. Ensure the correct-side bio row (`position_type`) exists with
         `correct_bbref`; create it (carrying bdl_id/birth/etc.) if the
         current row is on the wrong side (e.g. Ben Brown has only a
         batter/SS row but is a pitcher).
      D. Delete the wrong-side bio row and ALL season rows on
         `real_mlbam` (the historical junk — it can sit on either side).
         Game logs are keyed on player_id and are NOT touched.
      E. Reload the real career from Lahman via `correct_bbref` onto
         `real_mlbam` (`_lahman_pitching/batting_seasons_for_bbref`).
      F. Delete the bogus duplicate record (bio + seasons), guarded by A.

    Modern-player mode: pass `skip_lahman_reload=true` (and omit `bogus_mlbam`)
    for a straightforwardly-misclassified modern player — runs C+D then RETURNS
    before the Lahman reload (E) and the bogus delete (F). Used to reclassify a
    player whose real season stats come from the nightly BDL ingest, not Lahman
    (pair with `seed-pitcher-season` to seed the stub the nightly then fills).

    Returns a per-phase summary instead of raising, so a partial failure
    is visible without a 500. Scope: the 10 CLEAN swaps only — NOT the
    harriho/663687 or rodrijo/679563 multi-way tangles."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if position_type not in ("pitcher", "batter"):
        raise HTTPException(status_code=400, detail="position_type must be 'pitcher' or 'batter'")

    correct_is_pitcher = position_type == "pitcher"
    CorrectBio = Pitcher if correct_is_pitcher else Player
    WrongBio   = Player  if correct_is_pitcher else Pitcher

    summary: dict = {
        "status":        "ok",
        "real_mlbam":    real_mlbam,
        "correct_bbref": correct_bbref,
        "bogus_mlbam":   bogus_mlbam,
        "position_type": position_type,
        "steps":         [],
    }
    step = summary["steps"].append

    try:
        # --- A. Guard the bogus record (must carry NO bdl_id / NO game logs) ---
        # When `bogus_mlbam` is omitted (a straightforwardly-misclassified modern
        # player with no split), there is no bogus record to guard or delete —
        # skip A entirely and treat it as "already absent" for phase F.
        if bogus_mlbam is None:
            bogus_exists   = False
            bogus_bdl      = None
            bogus_bat_logs = 0
            bogus_pit_logs = 0
            bogus_safe     = False
            step("phase A skipped — no bogus_mlbam supplied (no split to repair)")
        else:
            with connection.get_session() as db:
                bp  = db.get(Player, bogus_mlbam)
                bpi = db.get(Pitcher, bogus_mlbam)
                bogus_exists = (bp is not None) or (bpi is not None)
                bogus_bdl = next(
                    (x.bdl_id for x in (bp, bpi) if x is not None and x.bdl_id is not None),
                    None,
                )
                bogus_bat_logs = (
                    db.query(BattingGameLog)
                      .filter(BattingGameLog.player_id == bogus_mlbam).count()
                )
                bogus_pit_logs = (
                    db.query(PitchingGameLog)
                      .filter(PitchingGameLog.player_id == bogus_mlbam).count()
                )
            bogus_safe = (
                bogus_bdl is None and bogus_bat_logs == 0 and bogus_pit_logs == 0
            )
        summary["bogus_record"] = {
            "exists":            bogus_exists,
            "bdl_id":            bogus_bdl,
            "batting_gamelogs":  bogus_bat_logs,
            "pitching_gamelogs": bogus_pit_logs,
            "safe_to_delete":    bogus_safe,
        }

        # --- B. Capture preserved bio off real_mlbam (prefer the bdl-stamped row) ---
        with connection.get_session() as db:
            rp  = db.get(Player, real_mlbam)
            rpi = db.get(Pitcher, real_mlbam)
            if rp is None and rpi is None:
                return {
                    "status": "error",
                    "detail": f"real_mlbam {real_mlbam} has no bio row in players or pitchers",
                    "steps":  summary["steps"],
                }
            src = next(
                (x for x in (rp, rpi) if x is not None and x.bdl_id is not None),
                None,
            ) or rp or rpi
            preserved = {
                "name":          src.name,
                "bdl_id":        src.bdl_id,
                "mlb_debut":     src.mlb_debut,
                "bats":          src.bats,
                "throws":        src.throws,
                "height":        src.height,
                "weight":        src.weight,
                "birth_year":    src.birth_year,
                "birth_month":   src.birth_month,
                "birth_day":     src.birth_day,
                "birth_city":    src.birth_city,
                "birth_state":   src.birth_state,
                "birth_country": src.birth_country,
                "debut":         src.debut,
            }
        # Current team comes from the bogus record's latest season (that's
        # where the REAL career lives right now) — report-only; the Lahman
        # reload + nightly roster sync re-establish team on real_mlbam.
        with connection.get_session() as db:
            CorrectSeason = PitcherSeason if correct_is_pitcher else PlayerSeason
            latest = (
                db.query(CorrectSeason)
                  .filter(CorrectSeason.player_id == bogus_mlbam)
                  .order_by(CorrectSeason.year.desc())
                  .first()
            )
            preserved_team = latest.team if latest is not None else None
        summary["preserved"] = {
            "name":                  preserved["name"],
            "bdl_id":                preserved["bdl_id"],
            "team_latest_on_bogus":  preserved_team,
        }

        # --- C. Ensure the correct-side bio exists with the correct bbref ---
        with connection.get_session() as db:
            row = db.get(CorrectBio, real_mlbam)
            if row is None:
                row = CorrectBio(
                    player_id       = real_mlbam,
                    name            = preserved["name"],
                    bbref_id        = correct_bbref,
                    bdl_id          = preserved["bdl_id"],
                    mlb_debut       = preserved["mlb_debut"],
                    mlb_last_season = None,   # active
                    position        = position or ("P" if correct_is_pitcher else None),
                    bats            = preserved["bats"],
                    throws          = preserved["throws"],
                    height          = preserved["height"],
                    weight          = preserved["weight"],
                    birth_year      = preserved["birth_year"],
                    birth_month     = preserved["birth_month"],
                    birth_day       = preserved["birth_day"],
                    birth_city      = preserved["birth_city"],
                    birth_state     = preserved["birth_state"],
                    birth_country   = preserved["birth_country"],
                    debut           = preserved["debut"],
                )
                db.add(row)
                step(f"created {position_type} bio for {real_mlbam} with bbref {correct_bbref}")
                created_bio = True
            else:
                row.bbref_id = correct_bbref
                if row.bdl_id is None and preserved["bdl_id"] is not None:
                    row.bdl_id = preserved["bdl_id"]
                step(f"updated existing {position_type} bio bbref -> {correct_bbref}")
                created_bio = False
            db.commit()

        # --- D. Delete wrong-side bio + ALL historical season rows on real_mlbam ---
        with connection.get_session() as db:
            wrong = db.get(WrongBio, real_mlbam)
            wrong_deleted = False
            if wrong is not None:
                db.delete(wrong)
                wrong_deleted = True
            pl_seasons = (
                db.query(PlayerSeason)
                  .filter(PlayerSeason.player_id == real_mlbam)
                  .delete(synchronize_session=False)
            )
            pi_seasons = (
                db.query(PitcherSeason)
                  .filter(PitcherSeason.player_id == real_mlbam)
                  .delete(synchronize_session=False)
            )
            db.commit()
        step(
            f"deleted wrong-side bio={wrong_deleted}; cleared seasons "
            f"player={pl_seasons} pitcher={pi_seasons}"
        )

        # --- (modern-player mode) skip E + F and return after the reclassify ---
        # Post-Lahman-debut players have no Lahman rows (and `correct_bbref` may be
        # conflated), so a reload would write nothing useful and risks re-injecting
        # a bogus historical season. Their current-season stats come from the
        # nightly BDL ingest — seed a stub PitcherSeason (see /admin/seed-pitcher-
        # season) so the nightly picks them up.
        if skip_lahman_reload:
            step("phase E skipped — skip_lahman_reload=true (no Lahman reload); "
                 "phase F skipped (no bogus delete)")
            summary["seasons_written"] = 0
            summary["season_years"]    = []
            summary["created_bio"]     = created_bio
            with connection.get_session() as db:
                rb = (db.query(BattingGameLog)
                        .filter(BattingGameLog.player_id == real_mlbam).count())
                rpg = (db.query(PitchingGameLog)
                         .filter(PitchingGameLog.player_id == real_mlbam).count())
            summary["real_gamelogs_preserved"] = {"batting": rb, "pitching": rpg}
            return summary

        # --- E. Reload the real career from Lahman onto real_mlbam ---
        if correct_is_pitcher:
            seasons = _lahman_pitching_seasons_for_bbref(correct_bbref)
            if seasons:
                with connection.get_session() as db:
                    crud.save_pitcher_seasons(db, real_mlbam, seasons)
        else:
            seasons = _lahman_batting_seasons_for_bbref(correct_bbref)
            if seasons:
                with connection.get_session() as db:
                    crud.save_player_seasons(db, real_mlbam, seasons)
        years = sorted(s["year"] for s in seasons)
        step(f"reloaded {len(seasons)} {position_type} seasons from {correct_bbref}: {years}")
        summary["seasons_written"] = len(seasons)
        summary["season_years"]    = years
        if not seasons:
            summary["status"] = "ok_with_warnings"
            step(f"WARNING: Lahman has no {position_type} rows for {correct_bbref} "
                 "(career may post-date the Lahman release — the nightly stat "
                 "ingest will fill current-season rows)")

        # --- F. Delete the bogus duplicate record (guarded by phase A) ---
        if not bogus_exists:
            step("bogus record already absent — nothing to delete")
            summary["bogus_deleted"] = False
        elif bogus_safe:
            with connection.get_session() as db:
                bs_pl = (
                    db.query(PlayerSeason)
                      .filter(PlayerSeason.player_id == bogus_mlbam)
                      .delete(synchronize_session=False)
                )
                bs_pi = (
                    db.query(PitcherSeason)
                      .filter(PitcherSeason.player_id == bogus_mlbam)
                      .delete(synchronize_session=False)
                )
                b_pl = db.get(Player, bogus_mlbam)
                b_pi = db.get(Pitcher, bogus_mlbam)
                if b_pl is not None:
                    db.delete(b_pl)
                if b_pi is not None:
                    db.delete(b_pi)
                db.commit()
            step(f"deleted bogus {bogus_mlbam} (seasons player={bs_pl} pitcher={bs_pi})")
            summary["bogus_deleted"] = True
        else:
            step(
                f"SKIPPED deleting bogus {bogus_mlbam} — it has bdl_id={bogus_bdl} "
                f"or game logs (bat={bogus_bat_logs}, pit={bogus_pit_logs}); not safe"
            )
            summary["bogus_deleted"] = False
            summary["status"] = "ok_with_warnings"

        # --- Confirm real_mlbam's game logs are intact (never touched) ---
        with connection.get_session() as db:
            rb = (
                db.query(BattingGameLog)
                  .filter(BattingGameLog.player_id == real_mlbam).count()
            )
            rpg = (
                db.query(PitchingGameLog)
                  .filter(PitchingGameLog.player_id == real_mlbam).count()
            )
        summary["created_bio"] = created_bio
        summary["real_gamelogs_preserved"] = {"batting": rb, "pitching": rpg}
        return summary

    except Exception as exc:   # never 500 — surface the partial state
        return {
            "status": "error",
            "detail": f"{type(exc).__name__}: {exc}",
            "steps":  summary["steps"],
        }


@app.post("/admin/seed-pitcher-season/{player_id}")
def admin_seed_pitcher_season(
    player_id: int,
    year:      int | None = Query(None, description="Season year to seed (defaults to the current UTC year, e.g. 2026)."),
    team_code: str = Query(..., description="Team code to pin the seed row to (e.g. 'CLE')."),
):
    """Seed a STUB `pitcher_seasons` row for `(player_id, year)` so the nightly
    pitcher aggregation picks the player up and fills the real stats from BDL.

    Why this exists: `nightly_update._update_pitchers` iterates only players
    already in `pitcher_seasons` (`crud.get_all_pitcher_ids` =
    `SELECT DISTINCT player_id FROM pitcher_seasons`). A real pitcher who has
    pitching game logs but no `pitcher_seasons` row — e.g. one misclassified as a
    position player — is therefore never seeded and never aggregated. Inserting a
    stub row (after reclassifying his bio to pitcher) makes the next nightly fill
    his line from BDL (keyed by `bdl_id`), so he self-heals.

    INSERT-IF-ABSENT — the critical semantic: if a `pitcher_seasons` row already
    exists for `(player_id, year)`, this is a NO-OP and the existing row (which may
    already hold nightly-filled stats) is left UNTOUCHED. So it's idempotent and
    non-destructive — unlike `reset-player-season`, which is a destructive
    delete-then-insert for the team-repin use case. Here we only ever CREATE a
    missing seed; we never overwrite real stats. The stub carries `team` only;
    every stat column is left NULL until the nightly fills it."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    season = year if year is not None else datetime.datetime.utcnow().year

    with connection.get_session() as db:
        existing = (
            db.query(PitcherSeason)
              .filter(PitcherSeason.player_id == player_id)
              .filter(PitcherSeason.year      == season)
              .first()
        )
        if existing is not None:
            # A row already exists — DO NOT overwrite (it may hold real stats).
            return {
                "status":    "already_exists",
                "player_id": player_id,
                "year":      season,
                "team":      existing.team,
                "note":      "pitcher_seasons row already present — left untouched (no overwrite)",
            }
        db.add(PitcherSeason(
            player_id = player_id,
            year      = season,
            team      = team_code,
        ))
        db.commit()

    return {
        "status":    "seeded",
        "player_id": player_id,
        "year":      season,
        "team":      team_code,
        "note":      "stub pitcher_seasons row inserted (stats NULL); next nightly fills from BDL",
    }


@app.get("/admin/misclassified-pitchers")
def admin_misclassified_pitchers(
    season:    int = Query(2026, description="Season to inspect (default 2026)."),
    bdl_limit: int = Query(60,   description="Max DISTINCT candidates to run the rate-limited BDL classification for. Beyond this, bdl_says_pitcher is left null and the response is flagged `bdl_truncated`. 0 disables BDL calls entirely."),
):
    """READ-ONLY diagnostic — no writes, no seeding, no repair.

    Finds the 'Cade Smith pattern' (real pitchers invisible to the nightly
    pitcher aggregation) and the reverse (pitchers misfiled as batters), and tags
    each candidate with BDL's authoritative pitcher-vs-position call — the signal
    Part B's self-heal guard uses to tell a misclassified pitcher (should
    self-heal) from a position player who threw a mop-up inning (must NOT be
    seeded).

    Q1 CANDIDATES: player_ids with `pitching_gamelogs` for `season` but NO
        `pitcher_seasons` row (any year) — the set the nightly never seeds
        (`get_all_pitcher_ids` = DISTINCT player_id FROM pitcher_seasons).
    Q2 bdl_says_pitcher: for each candidate/reverse row, fetch its BDL bio by
        bdl_id and classify via `_bdl_is_pitcher`. null on missing bdl_id or any
        BDL failure (never crashes). Paced at `_BDL_RATE_LIMIT_SLEEP` between
        calls; capped at `bdl_limit` DISTINCT ids (response flags `bdl_truncated`).
    Q3 REVERSE: player_ids with a `player_seasons` row for `season` that ALSO
        have `pitching_gamelogs` for `season` — pitchers producing empty batting
        lines (Cade Smith was one pre-repair).
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    # ---- Phase 1: pure-DB reads. No network I/O while the session is open, and
    #      every ORM object is flattened to plain scalars before the session
    #      closes (so Phase 2 touches no detached instances). READ-ONLY. ----
    with connection.get_session() as db:
        # player_ids that already have ANY pitcher_seasons row are visible to the
        # nightly, so they are NOT candidates.
        seeded_pitcher_ids = {
            pid for (pid,) in db.query(PitcherSeason.player_id).distinct().all()
        }

        # Q1: per-player pitching-log aggregates for the season.
        q1_agg = (
            db.query(
                PitchingGameLog.player_id,
                _sa_func.count().label("games"),
                _sa_func.sum(PitchingGameLog.IP).label("ip"),
            )
            .filter(PitchingGameLog.season == season)
            .group_by(PitchingGameLog.player_id)
            .all()
        )

        candidates = []
        for pid, games, ip in q1_agg:
            if pid in seeded_pitcher_ids:
                continue
            batter_bio  = db.get(Player, pid)
            pitcher_bio = db.get(Pitcher, pid)
            bdl_id = (getattr(batter_bio, "bdl_id", None)
                      or getattr(pitcher_bio, "bdl_id", None))
            name = (getattr(batter_bio, "name", None)
                    or getattr(pitcher_bio, "name", None))
            # Bio tables carry no team; derive from the latest season row. For
            # this pattern the batter-season row usually has it (Cade -> CLE).
            latest_ps = (
                db.query(PlayerSeason)
                  .filter(PlayerSeason.player_id == pid)
                  .order_by(PlayerSeason.year.desc())
                  .first()
            )
            candidates.append({
                "player_id":       pid,
                "name":            name,
                "team":            latest_ps.team if latest_ps else None,
                "games":           int(games),
                "ip":              round(float(ip), 1) if ip is not None else 0.0,
                "in_batter_bio":   batter_bio is not None,
                "in_pitcher_bio":  pitcher_bio is not None,
                "batter_position": getattr(batter_bio, "position", None),
                "bdl_id":          bdl_id,
            })
        candidates.sort(key=lambda c: c["ip"], reverse=True)

        # Q3 reverse: player_seasons (batter) for the season who ALSO pitched it.
        batter_season_ids = {
            pid for (pid,) in db.query(PlayerSeason.player_id)
                              .filter(PlayerSeason.year == season).all()
        }
        pitched_ids = {
            pid for (pid,) in db.query(PitchingGameLog.player_id)
                              .filter(PitchingGameLog.season == season).distinct().all()
        }
        reverse = []
        for pid in (batter_season_ids & pitched_ids):
            ps = (db.query(PlayerSeason)
                    .filter(PlayerSeason.player_id == pid,
                            PlayerSeason.year == season)
                    .first())
            batter_bio = db.get(Player, pid)
            pitching_games = (db.query(PitchingGameLog)
                                .filter(PitchingGameLog.player_id == pid,
                                        PitchingGameLog.season == season).count())
            batting_games = (db.query(BattingGameLog)
                               .filter(BattingGameLog.player_id == pid,
                                       BattingGameLog.season == season).count())
            reverse.append({
                "player_id":      pid,
                "name":           getattr(batter_bio, "name", None),
                "team":           ps.team if ps else None,
                "batter_PA":      getattr(ps, "PA", None),
                "batter_G":       getattr(ps, "G", None),
                "pitching_games": int(pitching_games),
                "batting_games":  int(batting_games),
                "bdl_id":         getattr(batter_bio, "bdl_id", None),
            })
        reverse.sort(key=lambda r: r["pitching_games"], reverse=True)

    # ---- Phase 2: BDL classification (network I/O, session already closed).
    #      Memoized so an id in both lists is fetched once; paced + capped for
    #      the BDL rate limit; every failure degrades to null, never raises. ----
    bdl_cache: dict = {}          # bdl_id -> (bool | None, note | None)
    stats = {"calls": 0, "truncated": False}

    def classify(bdl_id):
        if bdl_id is None:
            return None, "no bdl_id"
        if bdl_id in bdl_cache:
            return bdl_cache[bdl_id]
        if stats["calls"] >= bdl_limit:
            stats["truncated"] = True
            return None, "bdl_limit reached — not classified"
        if stats["calls"] > 0:                       # pace between calls only
            time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
        stats["calls"] += 1
        try:
            bio = data_service.fetch_bdl_player_bio(bdl_id)
            result = ((data_service._bdl_is_pitcher(bio), None) if bio is not None
                      else (None, "BDL returned no bio"))
        except Exception as exc:
            result = (None, f"BDL error: {type(exc).__name__}")
        bdl_cache[bdl_id] = result
        return result

    # Classify Q1 first (most likely real pitchers, sorted by IP desc), then Q3.
    for row in candidates + reverse:
        says, note = classify(row["bdl_id"])
        row["bdl_says_pitcher"] = says
        if note:
            row["bdl_note"] = note

    return {
        "season":             season,
        "read_only":          True,
        "q1_candidate_count": len(candidates),
        "q3_reverse_count":   len(reverse),
        "bdl_calls_made":     stats["calls"],
        "bdl_limit":          bdl_limit,
        "bdl_truncated":      stats["truncated"],
        "candidates":         candidates,   # Q1: pitching logs, no pitcher_seasons row
        "reverse":            reverse,       # Q3: batter season + pitched this year
    }


@app.get("/admin/dob-coverage")
def admin_dob_coverage():
    """Diagnostic: report birth-date completeness across both bio
    tables. Useful for sizing the gap between BDL roster discovery
    and what we actually have stored — recent call-ups often land
    in `players` / `pitchers` with NULL DOB because BDL's
    `/players/{id}` payload is sparse for them, while established
    veterans should all have a full DOB from the Lahman import.

    For each side (`players`, `pitchers`):
      • `active_total`   — `mlb_last_season IS NULL`
      • `with_full_dob`  — also has `birth_year/month/day` all set
      • `missing_dob`    — active_total - with_full_dob
      • `sample_missing` — up to 5 example rows with name / team /
                           position so the operator can eyeball
                           whether the gap is real call-ups or a
                           data-write bug worth chasing.

    Team is read from the current-year `player_seasons` /
    `pitcher_seasons` row (`team` column). nil when the player
    isn't on a current-year roster yet."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year

    def _side_summary(bio_model, season_model) -> dict:
        with connection.get_session() as db:
            active_total = (
                db.query(bio_model.player_id)
                  .filter(bio_model.mlb_last_season.is_(None))
                  .count()
            )
            with_full_dob = (
                db.query(bio_model.player_id)
                  .filter(bio_model.mlb_last_season.is_(None))
                  .filter(bio_model.birth_year.isnot(None))
                  .filter(bio_model.birth_month.isnot(None))
                  .filter(bio_model.birth_day.isnot(None))
                  .count()
            )
            sample_rows = (
                db.query(
                    bio_model.player_id,
                    bio_model.name,
                    bio_model.position,
                )
                  .filter(bio_model.mlb_last_season.is_(None))
                  .filter(_sa_or(
                      bio_model.birth_year.is_(None),
                      bio_model.birth_month.is_(None),
                      bio_model.birth_day.is_(None),
                  ))
                  .limit(5)
                  .all()
            )
            sample: list[dict] = []
            for r in sample_rows:
                team_row = (
                    db.query(season_model.team)
                      .filter(season_model.player_id == r.player_id)
                      .filter(season_model.year      == current_year)
                      .one_or_none()
                )
                sample.append({
                    "player_id": r.player_id,
                    "name":      r.name,
                    "team":      team_row.team if team_row else None,
                    "position":  r.position,
                })
        return {
            "active_total":   active_total,
            "with_full_dob":  with_full_dob,
            "missing_dob":    active_total - with_full_dob,
            "sample_missing": sample,
        }

    return {
        "current_year": current_year,
        "players":      _side_summary(Player,  PlayerSeason),
        "pitchers":     _side_summary(Pitcher, PitcherSeason),
    }


@app.get("/admin/duplicate-bdl-ids")
def admin_duplicate_bdl_ids():
    """Diagnostic: surface every case where the same `bdl_id` is
    stamped on more than one bio row. These are signs of either:

      • Old name-only mapping bug putting the wrong bdl_id on a
        retired veteran (Jose Ramirez SP took the active 3B's id
        before the scoring rubric landed).
      • Two BDL ids collapsing onto the same MLBAM — rare; usually
        a BDL-side duplicate-entry that needs hand-curation.
      • Cross-table mismatch: same bdl_id on a `players` row for
        MLBAM X and a `pitchers` row for MLBAM Y. The two-way
        path expects the SAME player_id on both sides; different
        player_ids means one of the stamps is wrong.

    Returns three buckets plus a summary count. Two-way players
    (Ohtani: same bdl_id on both tables for the same player_id)
    are NOT flagged — that's the correct shape.

    Each duplicate group: `{bdl_id, players: [{player_id, name,
    dob, position, active, team, side?}]}`. `side` is added only
    on cross-table entries so the operator can tell which row was
    in which table.
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year

    def _dob_str(by, bm, bd):
        if by is None or bm is None or bd is None:
            return None
        return f"{by:04d}-{bm:02d}-{bd:02d}"

    def _enrich(rows, season_model, db) -> list[dict]:
        out: list[dict] = []
        for r in rows:
            team_row = (
                db.query(season_model.team)
                  .filter(season_model.player_id == r.player_id)
                  .filter(season_model.year      == current_year)
                  .one_or_none()
            )
            out.append({
                "player_id": r.player_id,
                "name":      r.name,
                "dob":       _dob_str(r.birth_year, r.birth_month, r.birth_day),
                "position":  r.position,
                "active":    r.mlb_last_season is None,
                "team":      team_row.team if team_row else None,
            })
        return out

    def _same_table_dupes(bio_model, season_model, db) -> list[dict]:
        # bdl_ids appearing on more than one row in this bio table.
        bdl_ids = [
            r[0] for r in
            db.query(bio_model.bdl_id)
              .filter(bio_model.bdl_id.isnot(None))
              .group_by(bio_model.bdl_id)
              .having(_sa_func.count(bio_model.player_id) > 1)
              .all()
        ]
        groups: list[dict] = []
        for bdl_id in bdl_ids:
            rows = (
                db.query(
                    bio_model.player_id, bio_model.name,
                    bio_model.birth_year, bio_model.birth_month, bio_model.birth_day,
                    bio_model.position, bio_model.mlb_last_season,
                )
                .filter(bio_model.bdl_id == bdl_id)
                .all()
            )
            groups.append({
                "bdl_id":  int(bdl_id),
                "players": _enrich(rows, season_model, db),
            })
        return groups

    with connection.get_session() as db:
        bat_dupes = _same_table_dupes(Player,  PlayerSeason,  db)
        pit_dupes = _same_table_dupes(Pitcher, PitcherSeason, db)

        # Cross-table: same bdl_id appears in BOTH tables with
        # different player_ids. Build {bdl_id → player_id} for each
        # side first so the diff is a simple intersection check.
        # Skip nulls; multi-stamped bdl_ids inside one table are
        # already surfaced above and would key inconsistently here.
        bat_map: dict[int, int] = {}
        for r in (
            db.query(Player.player_id, Player.bdl_id)
              .filter(Player.bdl_id.isnot(None))
              .all()
        ):
            bat_map.setdefault(int(r.bdl_id), int(r.player_id))
        pit_map: dict[int, int] = {}
        for r in (
            db.query(Pitcher.player_id, Pitcher.bdl_id)
              .filter(Pitcher.bdl_id.isnot(None))
              .all()
        ):
            pit_map.setdefault(int(r.bdl_id), int(r.player_id))

        cross_dupes: list[dict] = []
        for bdl_id, bat_pid in bat_map.items():
            if bdl_id not in pit_map:
                continue
            if bat_pid == pit_map[bdl_id]:
                # Same player_id on both sides — two-way player
                # (Ohtani-style), the correct shape. Skip.
                continue
            bat_rows = (
                db.query(
                    Player.player_id, Player.name,
                    Player.birth_year, Player.birth_month, Player.birth_day,
                    Player.position, Player.mlb_last_season,
                )
                .filter(Player.bdl_id == bdl_id)
                .all()
            )
            pit_rows = (
                db.query(
                    Pitcher.player_id, Pitcher.name,
                    Pitcher.birth_year, Pitcher.birth_month, Pitcher.birth_day,
                    Pitcher.position, Pitcher.mlb_last_season,
                )
                .filter(Pitcher.bdl_id == bdl_id)
                .all()
            )
            bat_enriched = _enrich(bat_rows, PlayerSeason,  db)
            pit_enriched = _enrich(pit_rows, PitcherSeason, db)
            for p in bat_enriched:
                p["side"] = "batter"
            for p in pit_enriched:
                p["side"] = "pitcher"
            cross_dupes.append({
                "bdl_id":  bdl_id,
                "players": bat_enriched + pit_enriched,
            })

    total = len(bat_dupes) + len(pit_dupes) + len(cross_dupes)
    return {
        "current_year":              current_year,
        "player_table_duplicates":   bat_dupes,
        "pitcher_table_duplicates":  pit_dupes,
        "cross_table_duplicates":    cross_dupes,
        "total_issues":              total,
    }


@app.get("/admin/test-bdl-mapping")
def admin_test_bdl_mapping():
    """READ-ONLY diagnostic. Walks every BDL active roster and scores
    each player against our `players` / `pitchers` bio tables using
    the five-signal rubric (DOB / last / first / role / active).
    Reports auto-match candidates, ties needing review, and low-
    confidence cases. No DB writes — the operator inspects the
    results before deciding whether to wire the rubric into the
    actual mapping path.

    Returns a summary plus per-bucket detail lists. Each detail
    entry carries `{bdl_id, bdl_name, bdl_dob, bdl_team,
    bdl_position, matched_player_id, matched_name, matched_side,
    score, runner_up_score}` so the operator can spot the
    classification reason at a glance."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year
    return data_service.test_bdl_mapping(current_year)


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


@app.post("/admin/discover-new-players")
def admin_discover_new_players():
    """On-demand call-up discovery. Walks BDL's 30 active rosters,
    surfaces players we don't have in `players` / `pitchers` yet,
    looks each one up by name on the MLB Stats API to resolve
    their MLBAM id, fetches the BDL bio, and inserts a new row.
    Same-year stats backfill fires automatically after each insert.

    Useful right after a notable rookie call-up so the new bio +
    headshot land in iOS without waiting for the next nightly.
    Idempotent — players already in the DB are a no-op (their
    `bdl_id` and current-year team get reconciled along the way).
    Returns the same envelope as `/admin/sync-all-player-teams`,
    so the operator can also see how many new bios were created
    (`new_players_created` / `new_pitchers_created`) and how many
    name-search misses landed in `unresolved` for hand-curation."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year
    return data_service.discover_new_players(current_year)


@app.post("/admin/sync-player-active-status")
def admin_sync_player_active_status():
    """Reconcile every mapped player's retired / active state against
    BDL. Clears stale `mlb_last_season` on returning players
    (Pomeranz) and stamps `current_year - 1` on players BDL flags
    as inactive (Kershaw). Updates the current-year season `team`
    in the same pass for any active player whose BDL team resolves
    to a Lahman code.

    Read-mostly — only writes when BDL disagrees with our row, so
    safe to re-run any time. Returns
    `{status, counts: {total_checked, activated, retired,
    team_updated, no_data, failed}}`."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year
    return data_service.sync_player_active_status_from_bdl(current_year)


@app.post("/admin/backfill-bdl-gamelogs")
def admin_backfill_bdl_gamelogs(
    start_date:   str | None = Query(None, description="Inclusive start date, yyyy-mm-dd. Mutually exclusive with start_season."),
    end_date:     str | None = Query(None, description="Inclusive end date, yyyy-mm-dd. Mutually exclusive with end_season."),
    start_season: int | None = Query(None, description="Inclusive start season year. Pairs with end_season for multi-year backfills."),
    end_season:   int | None = Query(None, description="Inclusive end season year. Pairs with start_season."),
):
    """One-shot history backfill of batting + pitching gamelogs
    via BallDontLie's game-centric `/stats?game_id={id}` endpoint.

    Two input modes (exactly one must be supplied):
    - **Date range** (`start_date` + `end_date`) — walks every date
      in [start_date, end_date], fetches all finals, upserts.
    - **Season range** (`start_season` + `end_season`) — iterates
      each year in [start_season, end_season] and backfills
      `{year}-03-01` through `{year}-11-30` (covers spring openers
      through the regular-season finale). The current season is
      skipped because the nightly already covers it and re-running
      would be wasted work.

    Idempotent — the gamelog tables use (player_id, game_id) PKs,
    so re-running on a date range that's already loaded is a
    no-op upsert. Rate-limited at the BDL 5/sec ceiling between
    games. Backfills the entire history we used to fill via per-
    player MLB-Stats-API calls in roughly one BDL call per 25
    player-game rows.

    Auto-dedups at the tail (same gate as `/admin/dedupe-gamelogs`)
    so cross-format MLB-Stats-API × BDL duplicates from past
    ingests are collapsed in the same pass."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    has_dates   = start_date   is not None and end_date   is not None
    has_seasons = start_season is not None and end_season is not None
    if has_dates == has_seasons:
        # Both pairs or neither — ambiguous request.
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide exactly one input mode: either "
                "start_date+end_date or start_season+end_season."
            ),
        )

    if has_dates:
        try:
            result = data_service.backfill_bdl_gamelogs(start_date, end_date)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
    else:
        # Season mode. Current-year skip uses `_current_year()` so
        # the behavior tracks the calendar instead of hardcoding a
        # year that would silently start re-fetching in 2027.
        if end_season < start_season:
            raise HTTPException(
                status_code=400,
                detail="end_season must be >= start_season",
            )
        current_year = data_service._current_year()
        per_season: list[dict] = []
        skipped: list[int] = []
        total_games = 0
        total_bat   = 0
        total_pit   = 0
        for year in range(start_season, end_season + 1):
            if year == current_year:
                skipped.append(year)
                continue
            try:
                r = data_service.backfill_bdl_gamelogs(
                    f"{year}-03-01", f"{year}-11-30",
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc))
            games_n = int(r.get("total_games")    or 0)
            bat_n   = int(r.get("total_bat_rows") or 0)
            pit_n   = int(r.get("total_pit_rows") or 0)
            per_season.append({
                "season":   year,
                "games":    games_n,
                "bat_rows": bat_n,
                "pit_rows": pit_n,
            })
            total_games += games_n
            total_bat   += bat_n
            total_pit   += pit_n
        result = {
            "status":         "ok",
            "mode":           "season",
            "start_season":   start_season,
            "end_season":     end_season,
            "skipped_seasons": skipped,
            "total_games":    total_games,
            "total_bat_rows": total_bat,
            "total_pit_rows": total_pit,
            "per_season":     per_season,
        }

    # Auto-dedup tail. Runs once after the full backfill (date or
    # multi-season) finishes — calling per-iteration in season mode
    # would just repeat O(table) work without finding new dupes.
    # Doubleheaders (two BDL ids on the same date) are preserved by
    # the helper's `min_game_id < 1_000_000` gate.
    try:
        bat_removed = connection.dedupe_gamelog_duplicates(
            "batting_gamelogs",
            connection._BATTING_GAMELOGS_QUALITY_COLUMNS,
        )
        pit_removed = connection.dedupe_gamelog_duplicates(
            "pitching_gamelogs",
            connection._PITCHING_GAMELOGS_QUALITY_COLUMNS,
        )
        result["batting_dedup_removed"]  = bat_removed
        result["pitching_dedup_removed"] = pit_removed
    except Exception as exc:
        # Non-fatal — the backfill rows already committed. Log and
        # surface so the caller knows the dedup tail didn't run.
        log.error(f"backfill auto-dedup FAILED (non-fatal): {exc}")
        result["dedup_error"] = str(exc)
    return result


@app.post("/admin/backfill-gamelogs-async")
def admin_backfill_gamelogs_async(
    start_date:   str | None = Query(None, description="Inclusive start date, yyyy-mm-dd. Mutually exclusive with start_season."),
    end_date:     str | None = Query(None, description="Inclusive end date, yyyy-mm-dd. Mutually exclusive with end_season."),
    start_season: int | None = Query(None, description="Inclusive start season year. Pairs with end_season."),
    end_season:   int | None = Query(None, description="Inclusive end season year. Pairs with start_season."),
):
    """Same pipeline as `/admin/backfill-bdl-gamelogs` but kicked off
    in a background thread so the HTTP request returns immediately.
    Poll `/admin/backfill-gamelogs-status` for live progress.

    Designed for multi-year history loads (e.g. 2000-2025) that run
    for hours — the sync endpoint would otherwise require holding
    the curl connection open the whole time."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    has_dates   = start_date   is not None and end_date   is not None
    has_seasons = start_season is not None and end_season is not None
    if has_dates == has_seasons:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide exactly one input mode: either "
                "start_date+end_date or start_season+end_season."
            ),
        )
    if has_seasons and end_season < start_season:
        raise HTTPException(
            status_code=400,
            detail="end_season must be >= start_season",
        )

    with _backfill_lock:
        if _backfill_state["running"]:
            return {
                "status":  "already_running",
                "task_id": "backfill_gamelogs",
                **_backfill_state,
            }
        _backfill_state.update(
            running=True,
            mode=("date" if has_dates else "season"),
            start_season=start_season,
            end_season=end_season,
            start_date=start_date,
            end_date=end_date,
            current_season=None,
            total_games=0,
            total_bat_rows=0,
            total_pit_rows=0,
            error=None,
            last_updated=datetime.datetime.utcnow().isoformat() + "Z",
        )

    t = threading.Thread(
        target=_run_backfill_gamelogs,
        kwargs={
            "start_season": start_season,
            "end_season":   end_season,
            "start_date":   start_date,
            "end_date":     end_date,
        },
        daemon=True,
        name="backfill-gamelogs-async",
    )
    t.start()
    return {
        "status":  "started",
        "task_id": "backfill_gamelogs",
        "message": "Backfill running in background",
    }


@app.get("/admin/backfill-gamelogs-status")
def admin_backfill_gamelogs_status():
    """Live progress for the async gamelog backfill. Same shape on
    every call — fields stay populated after completion so the
    last-run summary is still readable until the next run starts."""
    with _backfill_lock:
        return dict(_backfill_state)


@app.post("/admin/backfill-mlb-gamelogs-async")
def admin_backfill_mlb_gamelogs_async(
    start_season: int = Query(..., description="Inclusive start season year."),
    end_season:   int = Query(..., description="Inclusive end season year."),
):
    """Historical gamelog backfill from MLB Stats API, run in a
    background thread. Walks every date from March 25 → November 1
    in each requested season, fetching schedule + boxscore from
    `statsapi.mlb.com` (rate-limited: 1s after each boxscore, 2s
    after each date's schedule).

    Used to fill seasons BDL `/stats?game_id=` doesn't cover
    (pre-2010). Idempotent — re-runs are no-op upserts. Poll
    `/admin/backfill-mlb-gamelogs-status` for progress."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if end_season < start_season:
        raise HTTPException(
            status_code=400,
            detail="end_season must be >= start_season",
        )

    with _mlb_backfill_lock:
        if _mlb_backfill_state["running"]:
            return {
                "status":  "already_running",
                "task_id": "backfill_mlb_gamelogs",
                **_mlb_backfill_state,
            }
        _mlb_backfill_state.update(
            running=True,
            start_season=start_season,
            end_season=end_season,
            current_season=None,
            current_date=None,
            total_games=0,
            total_bat_rows=0,
            total_pit_rows=0,
            error=None,
            last_updated=datetime.datetime.utcnow().isoformat() + "Z",
        )

    t = threading.Thread(
        target=_run_mlb_backfill_gamelogs,
        kwargs={"start_season": start_season, "end_season": end_season},
        daemon=True,
        name="backfill-mlb-gamelogs-async",
    )
    t.start()
    return {
        "status":  "started",
        "task_id": "backfill_mlb_gamelogs",
        "message": "MLB Stats API backfill running in background",
    }


@app.get("/admin/backfill-mlb-gamelogs-status")
def admin_backfill_mlb_gamelogs_status():
    """Live progress for the async MLB Stats API gamelog backfill."""
    with _mlb_backfill_lock:
        return dict(_mlb_backfill_state)


@app.post("/admin/delete-historical-gamelogs")
def admin_delete_historical_gamelogs(
    confirm: bool = Query(False, description="Must be true to proceed."),
):
    """DELETE every row from `batting_gamelogs` and `pitching_gamelogs`
    where `season < 2026`. Used to wipe a partial BDL backfill before
    running the full MLB Stats API historical re-load. Requires
    `?confirm=true` to prevent accidental fire."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Add ?confirm=true to confirm destructive deletion.",
        )
    with connection.get_session() as db:
        bat_removed = db.execute(
            _sa_text("DELETE FROM batting_gamelogs WHERE season < 2026")
        ).rowcount or 0
        pit_removed = db.execute(
            _sa_text("DELETE FROM pitching_gamelogs WHERE season < 2026")
        ).rowcount or 0
        db.commit()
    return {
        "status":           "ok",
        "batting_removed":  int(bat_removed),
        "pitching_removed": int(pit_removed),
    }


@app.get("/admin/gamelog-year-coverage")
def admin_gamelog_year_coverage():
    """Temporary diagnostic: row count in `batting_gamelogs` and
    `pitching_gamelogs` grouped by season year. Used to verify a
    multi-year backfill actually landed data for every year in the
    requested range. Returned in descending season order.

    Also returns up to 3 sample rows from `batting_gamelogs` for
    season=2006 so we can eyeball game_id shape + opponent values
    that landed during the historical BDL backfill."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    with connection.get_session() as db:
        bat_rows = (
            db.query(BattingGameLog.season, _sa_func.count())
              .group_by(BattingGameLog.season)
              .order_by(BattingGameLog.season.desc())
              .all()
        )
        pit_rows = (
            db.query(PitchingGameLog.season, _sa_func.count())
              .group_by(PitchingGameLog.season)
              .order_by(PitchingGameLog.season.desc())
              .all()
        )
        sample_2006 = (
            db.query(
                BattingGameLog.player_id,
                BattingGameLog.game_id,
                BattingGameLog.game_date,
                BattingGameLog.opponent,
            )
              .filter(BattingGameLog.season == 2006)
              .limit(3)
              .all()
        )
    return {
        "batting":  [{"season": s, "rows": int(c)} for s, c in bat_rows],
        "pitching": [{"season": s, "rows": int(c)} for s, c in pit_rows],
        "sample_2006_batting": [
            {
                "player_id": r.player_id,
                "game_id":   r.game_id,
                "game_date": r.game_date.isoformat() if r.game_date else None,
                "opponent":  r.opponent,
            }
            for r in sample_2006
        ],
    }


@app.get("/admin/find-missing-doubleheaders")
def admin_find_missing_doubleheaders(
    season: int = Query(
        ..., description="Season year to scan, e.g. 2026",
    ),
):
    """Read-only diagnostic. Walks BDL's `/games` for `season`,
    groups by `(date, away_team, home_team)`, flags any matchup
    BDL ships twice for the same date (= doubleheader), and
    reports which of those games are missing from our
    `batting_gamelogs` table.

    Designed to drive a targeted re-backfill after the doubleheader
    dedup fix: pass any returned `missing_dates` to
    `POST /admin/backfill-bdl-gamelogs?start_date=&end_date=` to
    re-ingest just those dates. No DB writes here."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return data_service.find_missing_doubleheaders(season)
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


@app.get("/admin/bref-war-status")
def admin_bref_war_status():
    """Snapshot of the bref WAR CSV state per side. Surfaces the
    download timestamp, row count, content hash, and a sample of
    the most-recent year's data so an operator can confirm whether
    bref has updated since the morning nightly. The
    `updated_since_last_seen` flag compares the just-fetched hash
    against the module-level `_last_war_hashes` snapshot taken
    BEFORE this call — useful to verify that the next catch-up
    will actually refresh WAR rather than skip it.
    """
    def _side_status(side: str, fetch_meta) -> dict:
        prior_hash = data_service.get_last_war_hash(side)
        try:
            meta = fetch_meta()
        except Exception as exc:
            return {"status": "error", "error": str(exc), "prior_hash": prior_hash}
        df = meta["df"]
        recent_entry = None
        recent_year_rows = None
        if "year_ID" in df.columns and not df.empty:
            try:
                recent_year = int(df["year_ID"].max())
                recent_slice = df[df["year_ID"] == recent_year]
                recent_year_rows = int(len(recent_slice))
                if not recent_slice.empty:
                    r = recent_slice.iloc[0]
                    recent_entry = {
                        "year_ID":     recent_year,
                        "name_common": str(r["name_common"]) if "name_common" in df.columns else None,
                        "team_ID":     str(r["team_ID"]) if "team_ID" in df.columns else None,
                        "PA":          (int(r["PA"]) if "PA" in df.columns and pd.notna(r["PA"]) else None),
                        "WAR":         (float(r["WAR"]) if "WAR" in df.columns and pd.notna(r["WAR"]) else None),
                    }
            except (ValueError, TypeError) as exc:
                # Defensive — surface the failure rather than 500
                # the whole endpoint if a column has unexpected dtype.
                recent_entry = {"error": f"recent-entry probe failed: {exc}"}
        return {
            "status":                 "ok",
            "downloaded_at":          meta.get("downloaded_at"),
            "hash":                   meta.get("hash"),
            "rows":                   meta.get("rows"),
            "recent_entry":           recent_entry,
            "recent_year_rows":       recent_year_rows,
            "prior_hash":             prior_hash,
            "updated_since_last_seen": (prior_hash is None) or (prior_hash != meta.get("hash")),
        }

    return {
        "batting":  _side_status("bat",   data_service._bwar_bat_all_meta),
        "pitching": _side_status("pitch", data_service._bwar_pitch_all_meta),
    }


@app.get("/admin/check-gamelog-duplicates")
def admin_check_gamelog_duplicates():
    """Diagnostic: summarize duplicate patterns in the gamelog
    tables so we can tell what kind of duplicates we're dealing
    with before/after the dedupe pass runs. Returns per-table:
      • total row count
      • distinct (player_id, game_id) pairs — should equal total
        once the PK is in place.
      • distinct (player_id, game_date) pairs — when this is
        *less than* the (player_id, game_id) count, the same
        logical game is stored under multiple game_id strings
        (e.g. MLB Stats API gamePks vs. BDL game ids). The
        (player_id, game_id) PK alone can't fix those — they
        need a date-keyed reconciliation.
      • sample 5 Ohtani rows so we can eyeball the game_id shape.

    Postgres-only — runs raw SQL against the engine.
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    def _summarize(table: str) -> dict:
        with connection.get_session() as db:
            total = db.execute(
                _sa_text(f"SELECT COUNT(*) FROM {table}")
            ).scalar() or 0
            by_game_id = db.execute(
                _sa_text(
                    f"SELECT COUNT(*) FROM "
                    f"(SELECT DISTINCT player_id, game_id FROM {table}) s"
                )
            ).scalar() or 0
            by_game_date = db.execute(
                _sa_text(
                    f"SELECT COUNT(*) FROM "
                    f"(SELECT DISTINCT player_id, game_date FROM {table}) s"
                )
            ).scalar() or 0
            ohtani_rows = db.execute(_sa_text(
                f"SELECT player_id, game_id, game_date, opponent "
                f"FROM {table} WHERE player_id = 660271 "
                f"ORDER BY game_date DESC LIMIT 5"
            )).fetchall()
            return {
                "total_rows":                       int(total),
                "distinct_player_game_id_pairs":    int(by_game_id),
                "distinct_player_game_date_pairs":  int(by_game_date),
                "duplicate_rows_over_game_id":      int(total) - int(by_game_id),
                "ohtani_sample": [
                    {
                        "player_id": r[0],
                        "game_id":   r[1],
                        "game_date": str(r[2]) if r[2] is not None else None,
                        "opponent":  r[3],
                    }
                    for r in ohtani_rows
                ],
            }

    return {
        "batting_gamelogs":  _summarize("batting_gamelogs"),
        "pitching_gamelogs": _summarize("pitching_gamelogs"),
    }


@app.post("/admin/dedup-gamelogs")
def admin_dedup_gamelogs():
    """One-shot cleanup: remove duplicate `(player_id, game_id)`
    rows from `batting_gamelogs` and `pitching_gamelogs`, keeping
    the row with the most non-NULL stat columns. Used when an
    older deploy created the gamelog tables without the composite
    PK and duplicate rows accumulated under different ingest
    paths (MLB Stats API gamePks vs. BDL game ids for the same
    logical game).

    Mirrors what `init_db()` already runs at startup, so calling
    this is only useful when you don't want to wait for the next
    deploy. Idempotent — re-runs are no-ops once each table is
    clean. After deduping, run `POST /admin/backfill-bdl-gamelogs`
    to fill any gaps the duplicate cleanup left behind.

    Postgres-only — the helper bails (returns 0) on SQLite where
    the dev DB rarely accumulates duplicates anyway.
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    batting_removed = connection.dedupe_gamelog_duplicates(
        "batting_gamelogs",
        connection._BATTING_GAMELOGS_QUALITY_COLUMNS,
    )
    pitching_removed = connection.dedupe_gamelog_duplicates(
        "pitching_gamelogs",
        connection._PITCHING_GAMELOGS_QUALITY_COLUMNS,
    )
    return {
        "status":            "ok",
        "batting_removed":   batting_removed,
        "pitching_removed":  pitching_removed,
    }


@app.post("/admin/remove-spring-training-gamelogs")
def admin_remove_spring_training_gamelogs():
    """One-shot cleanup: delete `batting_gamelogs` and
    `pitching_gamelogs` rows whose `game_date` is before the
    current season's Opening Night (2026-03-25). Spring training
    and exhibition games leaked into the gamelog tables before
    `fetch_bdl_games_for_date` started filtering on
    `season_type == "regular"`; this is the cleanup pass for
    those orphan rows. Idempotent — re-runs are no-ops once the
    tables are clean.
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    cutoff = "2026-03-25"
    with connection.get_session() as db:
        batting = db.execute(
            _sa_text("DELETE FROM batting_gamelogs WHERE game_date < :cutoff"),
            {"cutoff": cutoff},
        )
        pitching = db.execute(
            _sa_text("DELETE FROM pitching_gamelogs WHERE game_date < :cutoff"),
            {"cutoff": cutoff},
        )
        db.commit()
        return {
            "status":           "ok",
            "cutoff":           cutoff,
            "batting_removed":  batting.rowcount or 0,
            "pitching_removed": pitching.rowcount or 0,
        }


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


@app.post("/admin/set-bdl-id/{player_id}")
def admin_set_bdl_id(
    player_id: int,
    bdl_id: str = Query(...,
                        description="BDL player id (integer) to stamp onto the row, "
                                    "or the literal string 'null' to clear an existing "
                                    "mapping."),
    bio_type: str = Query(..., pattern="^(batter|pitcher)$",
                          description="Which bio table to update — 'batter' (`players`) "
                                      "or 'pitcher' (`pitchers`). Two-way players will need "
                                      "one call per side."),
):
    """One-off override of a player's `bdl_id` mapping. Used to repair
    rows where the bootstrap matcher picked the wrong BDL row (e.g. a
    name-twin's dad / son pair where the DB row was stamped before
    the debut-year tiebreaker landed in the matcher).

    The bootstrap endpoints (`build-bdl-player-mapping`,
    `retry-unmapped-bdl-players`) only act on rows where `bdl_id IS
    NULL`, so they can't repair an already-mismapped row. This
    endpoint is the targeted fix. Provide the BDL id you've verified
    via `GET https://api.balldontlie.io/mlb/v1/players/{id}`.

    Passing `bdl_id=null` clears the existing mapping back to NULL.
    Useful when the wrong row was stamped and you want the next
    `retry-unmapped-bdl-players` pass to re-attempt the match.
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    # Parse the string-typed `bdl_id`. We accept str (not int) on
    # the query param so the literal "null" form is reachable —
    # FastAPI would 422 on a non-int otherwise.
    new_bdl_id: int | None
    if bdl_id.lower() == "null":
        new_bdl_id = None
    else:
        try:
            new_bdl_id = int(bdl_id)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"bdl_id must be an integer or 'null', got {bdl_id!r}",
            )
    bio_model = Player if bio_type == "batter" else Pitcher
    with connection.get_session() as db:
        row = db.query(bio_model).filter(bio_model.player_id == player_id).one_or_none()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No {bio_type} found with player_id {player_id}",
            )
        previous = row.bdl_id
        row.bdl_id = new_bdl_id
        db.commit()
        return {
            "status":      "ok",
            "player_id":   player_id,
            "name":        row.name,
            "bio_type":    bio_type,
            "previous_bdl_id": previous,
            "new_bdl_id":  new_bdl_id,
        }


@app.post("/admin/set-player-active/{player_id}")
def admin_set_player_active(
    player_id: int,
    bio_type: str = Query(..., pattern="^(batter|pitcher)$",
                          description="Which bio table to update — 'batter' "
                                      "(`players`) or 'pitcher' (`pitchers`). "
                                      "Two-way players need one call per side."),
):
    """One-off override that clears `mlb_last_season` on a bio row,
    flipping the player to "active" for the iOS retired-check
    (`PlayerViewModel.isRetired` reads this field directly).

    Used when the BDL active-status sync (or the original Lahman
    `finalGame` import) incorrectly stamped a returning / mid-
    season vet as retired. The dedicated GP guard in
    `sync_player_active_status_from_bdl` should catch most of
    these on the next nightly, but this endpoint is the manual
    override when an operator already knows the answer.

    Returns the previous and new `mlb_last_season` values so the
    caller can confirm the row was actually stamped before the
    clear (vs. a no-op call against an already-active row)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    bio_model = Player if bio_type == "batter" else Pitcher
    with connection.get_session() as db:
        row = (
            db.query(bio_model)
            .filter(bio_model.player_id == player_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No {bio_type} found with player_id {player_id}",
            )
        previous = row.mlb_last_season
        row.mlb_last_season = None
        db.commit()
        return {
            "status":                  "ok",
            "player_id":               player_id,
            "name":                    row.name,
            "bio_type":                bio_type,
            "previous_mlb_last_season": previous,
            "new_mlb_last_season":      None,
        }


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


@app.post("/admin/ingest-retrosheet")
def start_ingest_retrosheet(year: int | None = Query(default=None)):
    """Ingest the committed Retrosheet season/stint CSVs (background thread).
    `?year=YYYY` restricts to one season (dry run); omit for all years."""
    with _retro_lock:
        if _retro_state["running"]:
            return {"status": "already_running", **_retro_state}

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    t = threading.Thread(target=_run_retrosheet_ingest, args=(year,), daemon=True)
    t.start()
    return {"status": "started", "year": year}


@app.get("/admin/ingest-retrosheet/status")
def ingest_retrosheet_status():
    counts: dict[str, int] = {}
    if connection.db_available():
        try:
            with connection.get_session() as db:
                counts = {
                    "player_seasons_retrosheet":
                        db.query(PlayerSeason).filter(PlayerSeason.source == "retrosheet").count(),
                    "pitcher_seasons_retrosheet":
                        db.query(PitcherSeason).filter(PitcherSeason.source == "retrosheet").count(),
                    "player_season_stints":  db.query(PlayerSeasonStint).count(),
                    "pitcher_season_stints": db.query(PitcherSeasonStint).count(),
                }
        except Exception:
            pass

    with _retro_lock:
        state = dict(_retro_state)
    return {"counts": counts, **state}


@app.post("/admin/ingest-retrosheet-gamelogs")
def start_ingest_retrosheet_gamelogs(
    year_from: int = Query(default=1898),
    year_to: int = Query(default=1999),
):
    """Backfill historical game logs from Retrosheet daybyday (runtime download).
    Defaults to the full 1898-1999 range; narrow with ?year_from=&year_to= for
    a single-year/decade dry run."""
    with _retro_gl_lock:
        if _retro_gl_state["running"]:
            return {"status": "already_running", **_retro_gl_state}

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    t = threading.Thread(
        target=_run_retrosheet_gamelogs, args=(year_from, year_to), daemon=True,
    )
    t.start()
    return {"status": "started", "year_from": year_from, "year_to": year_to}


@app.get("/admin/ingest-retrosheet-gamelogs/status")
def ingest_retrosheet_gamelogs_status():
    counts: dict[str, int] = {}
    if connection.db_available():
        try:
            with connection.get_session() as db:
                counts = {
                    "batting_gamelogs_pre2000":
                        db.query(BattingGameLog).filter(BattingGameLog.season < 2000).count(),
                    "pitching_gamelogs_pre2000":
                        db.query(PitchingGameLog).filter(PitchingGameLog.season < 2000).count(),
                }
        except Exception:
            pass

    with _retro_gl_lock:
        state = dict(_retro_gl_state)
    return {"counts": counts, **state}


@app.post("/admin/stage-retrosheet-gamelogs")
def start_stage_retrosheet_gamelogs(
    year_from: int = Query(default=2000),
    year_to: int = Query(default=2025),
):
    """Ingest Retrosheet 2000-2025 game logs into the SEPARATE staging tables
    (appearance gate). Live game logs are NOT touched — this stages a candidate
    replacement for later comparison/swap. Narrow with ?year_from=&year_to= for
    a single-year dry run."""
    with _stage_gl_lock:
        if _stage_gl_state["running"]:
            return {"status": "already_running", **_stage_gl_state}

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    t = threading.Thread(
        target=_run_stage_retrosheet_gamelogs, args=(year_from, year_to), daemon=True,
    )
    t.start()
    return {"status": "started", "year_from": year_from, "year_to": year_to}


@app.get("/admin/stage-retrosheet-gamelogs/status")
def stage_retrosheet_gamelogs_status():
    counts: dict[str, int] = {}
    if connection.db_available():
        try:
            with connection.get_session() as db:
                counts = {
                    "staging_batting_gamelogs":  db.query(StagingBattingGameLog).count(),
                    "staging_pitching_gamelogs": db.query(StagingPitchingGameLog).count(),
                }
        except Exception:
            pass

    with _stage_gl_lock:
        state = dict(_stage_gl_state)
    return {"counts": counts, **state}


# Every batting stat — an empty pitcher-appearance row has all of these 0/null.
_STAGING_BAT_ZERO_COLS = ["PA", "AB", "R", "H", "doubles", "triples", "HR",
                          "RBI", "BB", "IBB", "SO", "SB", "CS", "HBP", "SF",
                          "GIDP", "SH"]


@app.post("/admin/cleanup-staging-empty-pitcher-rows")
def cleanup_staging_empty_pitcher_rows(apply: bool = Query(default=False)):
    """Remove empty pitcher-appearance batting rows from STAGING — rows where
    the player pitched that game (a staging pitching row exists for the same
    game_id) AND has NO batting contribution (every batting stat 0/null). These
    are the DH-era B_G>0 appearance rows for non-batting pitchers that the live
    data (correctly) omits. Dry-run by default (apply=false → counts only);
    apply=true performs the DELETE. Touches STAGING only, never live. The
    predicate requires every batting stat = 0, so it can never remove a row
    where the player actually batted — the safety count must be 0."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    zero = " AND ".join(f'COALESCE(b."{c}", 0) = 0' for c in _STAGING_BAT_ZERO_COLS)
    exists_pit = ("EXISTS (SELECT 1 FROM staging_pitching_gamelogs p "
                  "WHERE p.player_id = b.player_id AND p.game_id = b.game_id)")
    # Only 2022+: pre-2022 (no universal DH) live ALSO carries empty reliever
    # batting rows, so dropping them from staging would push staging below live.
    where = f"b.season >= 2022 AND {zero} AND {exists_pit}"
    # Any row that matched the delete but nonetheless has real batting — must be
    # 0 by construction (the predicate already forces every batting stat to 0).
    unsafe_where = (f"{where} AND (COALESCE(b.\"AB\", 0) > 0 OR COALESCE(b.\"H\", 0) > 0 "
                    f"OR COALESCE(b.\"PA\", 0) > 0 OR COALESCE(b.\"BB\", 0) > 0 "
                    f"OR COALESCE(b.\"R\", 0) > 0)")

    with connection.get_session() as db:
        matched = db.execute(_sa_text(
            f"SELECT COUNT(*) FROM staging_batting_gamelogs b WHERE {where}")).scalar() or 0
        unsafe = db.execute(_sa_text(
            f"SELECT COUNT(*) FROM staging_batting_gamelogs b WHERE {unsafe_where}")).scalar() or 0
        by_season = {int(s): int(n) for s, n in db.execute(_sa_text(
            f"SELECT season, COUNT(*) FROM staging_batting_gamelogs b WHERE {where} "
            "GROUP BY season ORDER BY season")).all()}
        deleted = 0
        if apply and unsafe == 0:
            deleted = db.execute(_sa_text(
                f"DELETE FROM staging_batting_gamelogs b WHERE {where}")).rowcount or 0
            db.commit()

    return {
        "applied":              bool(apply),
        "matched":              int(matched),
        "unsafe_rows_in_match": int(unsafe),   # MUST be 0 — never deletes a real batting row
        "deleted":              int(deleted),
        "matched_by_season":    by_season,
    }


# ---------------------------------------------------------------------------
# Stage 2 — staging-vs-live game-log comparison (READ-ONLY go/no-go gate).
# Compares per-player COUNTS + season TOTALS, never a (player, date) join, so
# the recon's ±1-day date-attribution cases don't throw false discrepancies.
# ---------------------------------------------------------------------------
# PA-derived batting stats + core pitching stats are equality-checked (the ±1
# tolerance absorbs the rare Retrosheet variance). Baserunning (R/SB/CS) is
# superset-tolerant — the appearance gate legitimately adds pinch-run rows — so
# it's reported (stage < live only) but never gates the verdict.
_CMP_BAT_EQ = {"AB": 1, "H": 1, "doubles": 1, "triples": 1, "HR": 1,
               "RBI": 1, "BB": 1, "SO": 1}
_CMP_BAT_BASERUN = ["R", "SB", "CS"]
_CMP_PIT_EQ = {"IP": 0.5, "H": 1, "ER": 1, "SO": 1, "BB": 1, "HR": 1}
_CMP_LIST_CAP = 500   # cap discrepancy lists so a broken season can't blow up the payload


def _gl_season_agg(db, model, year: int, stat_cols: list) -> dict:
    """Per-player aggregates for one season — {player_id: {"g": count, col: sum}}.
    Pure SELECT + GROUP BY (no writes)."""
    cols = [model.player_id, _sa_func.count().label("g")]
    cols += [_sa_func.sum(getattr(model, c)).label(c) for c in stat_cols]
    out: dict = {}
    for row in (db.query(*cols)
                  .filter(model.season == year)
                  .group_by(model.player_id).all()):
        out[row.player_id] = {"g": row.g, **{c: getattr(row, c) for c in stat_cols}}
    return out


def _compare_gl_season(db, year, live_model, stage_model, eq_tol, baserun_cols, is_batting):
    """Compare one side (batting or pitching) for one season. Returns
    (summary_block, detail_block). Read-only.

    Source-trust model: Retrosheet (staging) is the authoritative source, so
    stat disagreements do NOT block — staging>live are ENRICHMENTS (staging
    more complete, e.g. the international series BDL missed) and staging<live
    are reported but accepted as ±1-2 attribution variance. The ONLY blocker is
    a GENUINE coverage regression: a player with fewer staging games AND lower
    totals (a real missing game). A fewer-games player whose season totals still
    match is a ±1-day/doubleheader/empty-row artifact — the game exists — and
    does NOT block."""
    stat_cols = list(eq_tol.keys()) + list(baserun_cols)
    live = _gl_season_agg(db, live_model, year, stat_cols)
    stage = _gl_season_agg(db, stage_model, year, stat_cols)

    def _n(v):
        return float(v) if v is not None else 0.0

    enrichments, staging_below, baserun_below = [], [], []
    below_players = set()          # players whose staging totals are LOWER on a REAL stat
    for pid, lv in live.items():
        # {} when the player is absent from staging entirely — his staging stats
        # then read as 0, so the comparison still runs (a reliever whose only
        # empty rows were dropped must be judged by whether he had REAL stats,
        # not by his mere absence).
        svd = stage.get(pid) or {}
        for c, tol in eq_tol.items():
            a, b = _n(lv.get(c)), _n(svd.get(c))
            if abs(a - b) > tol:
                rec = {"player_id": pid, "stat": c, "live": round(a, 3),
                       "stage": round(b, 3), "delta": round(b - a, 3)}
                if b > a:
                    enrichments.append(rec)                 # staging MORE complete
                else:
                    staging_below.append(rec)               # staging LESS on a REAL stat
                    below_players.add(pid)
        for c in baserun_cols:
            if _n(svd.get(c)) < _n(lv.get(c)):
                baserun_below.append({"player_id": pid, "stat": c,
                                      "live": _n(lv.get(c)), "stage": _n(svd.get(c))})

    # Coverage — flag players with fewer staging games. A GENUINE regression is
    # one where staging is ALSO short on a REAL equality-checked stat (staging
    # missing actual batting → `below_players`). A fewer-games player whose real
    # totals still match — INCLUDING a reliever absent from staging whose live
    # rows are all empty (0 == 0) — is a benign artifact and does NOT block.
    coverage, genuine = [], 0
    for pid, lv in live.items():
        sv = stage.get(pid)
        sg = sv["g"] if sv else 0
        if sg < lv["g"]:
            totals_match = pid not in below_players
            coverage.append({"player_id": pid, "live_g": lv["g"], "stage_g": sg,
                             "totals_match": totals_match})
            if not totals_match:
                genuine += 1

    live_rows = sum(v["g"] for v in live.values())
    stage_rows = sum(v["g"] for v in stage.values())
    summary = {
        "live_rows":                         live_rows,
        "staging_rows":                      stage_rows,
        "net_new":                           stage_rows - live_rows,
        "players_compared":                  len(live),
        "coverage_regression_count":         len(coverage),
        "genuine_coverage_regression_count": genuine,        # <- the ONLY blocker
        "enrichment_count":                  len(enrichments),
        "staging_below_count":               len(staging_below),
    }
    if is_batting:
        lt = db.query(_sa_func.count()).filter(live_model.season == year).scalar() or 0
        ln = db.query(_sa_func.count()).filter(live_model.season == year,
                                               live_model.PA.is_(None)).scalar() or 0
        st = db.query(_sa_func.count()).filter(stage_model.season == year).scalar() or 0
        sn = db.query(_sa_func.count()).filter(stage_model.season == year,
                                               stage_model.PA.is_(None)).scalar() or 0
        summary["null_pa_live_pct"] = round(100 * ln / lt, 1) if lt else None
        summary["null_pa_staging_pct"] = round(100 * sn / st, 1) if st else None

    detail = {
        "coverage_regressions":         coverage[:_CMP_LIST_CAP],
        "enrichments":                  enrichments[:_CMP_LIST_CAP],
        "staging_below":                staging_below[:_CMP_LIST_CAP],
        "baserunning_stage_below_live": baserun_below[:_CMP_LIST_CAP],
    }
    return summary, detail


@app.get("/admin/compare-staging-gamelogs")
def compare_staging_gamelogs(season: int | None = Query(default=None)):
    """READ-ONLY go/no-go gate for the Stage 3 replacement. Source-trust model:
    Retrosheet (staging) is authoritative, so stat disagreements never block —
    only a GENUINE coverage regression does (a player with fewer staging games
    AND lower totals, i.e. a real missing game). Compares per-player game counts
    + season stat totals (never a date join). Omit ?season= to scan 2000-2025.
    No writes."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    seasons = [season] if season is not None else list(range(2000, 2026))
    out = []
    with connection.get_session() as db:
        for y in seasons:
            b_sum, b_det = _compare_gl_season(
                db, y, BattingGameLog, StagingBattingGameLog,
                _CMP_BAT_EQ, _CMP_BAT_BASERUN, is_batting=True)
            p_sum, p_det = _compare_gl_season(
                db, y, PitchingGameLog, StagingPitchingGameLog,
                _CMP_PIT_EQ, [], is_batting=False)
            # GO iff no GENUINE coverage regression on either side. Enrichments
            # (staging>live) and benign count artifacts (totals match) never block.
            go = (b_sum["genuine_coverage_regression_count"] == 0
                  and p_sum["genuine_coverage_regression_count"] == 0)
            out.append({
                "season":  y,
                "verdict": "GO" if go else "NO-GO",
                "summary": {"batting": b_sum, "pitching": p_sum},
                "batting":  b_det,
                "pitching": p_det,
            })

    overall = "GO" if out and all(s["verdict"] == "GO" for s in out) else "NO-GO"
    return {"overall_verdict": overall, "seasons": out}


# ---------------------------------------------------------------------------
# Stage 3 — promote staging → live game logs (THE destructive swap).
# Per season, atomic: (a) already-promoted? skip → (b) inline gate re-check
# (GO iff genuine==0 both sides) → (c) dry-run report OR wholesale
# delete-live-then-insert-staging in ONE transaction with pre-commit assertions.
# confirm=false is a no-write dry run; confirm=true executes. Never auto-run.
# ---------------------------------------------------------------------------
_promote_lock = threading.Lock()

# Last-run outcome, captured server-side so a swap_failed error survives beyond
# the HTTP response / log stream. Written only after a run (no swap behavior).
_promote_state: dict = {
    "running":          False,
    "last_run":         None,
    "confirm":          None,
    "swapped":          [],
    "already_promoted": [],
    "skipped_no_go":    [],
    "would_swap":       [],
    "swap_failed":      [],
    "errors":           {},   # {season: {"error": str, "traceback": str}}
}

_BAT_GL_COLS = ["player_id", "game_id", "game_date", "season", "opponent",
                "home_away", "result", "team_score", "opp_score", "PA", "AB",
                "R", "H", "doubles", "triples", "HR", "RBI", "BB", "IBB", "SO",
                "SB", "CS", "HBP", "SF", "GIDP", "SH", "LOB"]
_PIT_GL_COLS = ["player_id", "game_id", "game_date", "season", "opponent",
                "home_away", "result", "IP", "H", "R", "ER", "BB", "SO", "HR",
                "HBP", "WP", "pitches", "strikes"]


def _promote_insert_sql(live_table: str, stage_table: str, cols: list) -> str:
    """INSERT INTO <live> (<cols>) SELECT <cols> FROM <staging> WHERE season=:y.
    Explicit column list = the exact table columns (the gamelog tables have NO
    last_updated column, so we don't stamp one — provenance is the retro- game_id
    prefix). `cols` must be every column of both the live and staging table."""
    q = ", ".join(f'"{c}"' for c in cols)
    return (f'INSERT INTO {live_table} ({q}) '
            f'SELECT {q} FROM {stage_table} WHERE season = :y')


_BAT_PROMOTE_SQL = _promote_insert_sql("batting_gamelogs", "staging_batting_gamelogs", _BAT_GL_COLS)
_PIT_PROMOTE_SQL = _promote_insert_sql("pitching_gamelogs", "staging_pitching_gamelogs", _PIT_GL_COLS)


def _promote_one_season(year: int, confirm: bool) -> dict:
    """One season, one transaction. Ordering: already-promoted → gate → swap.
    Returns an audit record; the get_session commits on clean exit and rolls
    back the whole season if a post-swap assertion raises."""
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        with connection.get_session() as db:
            def cnt(sql: str) -> int:
                return int(db.execute(_sa_text(sql), {"y": year}).scalar() or 0)

            live_bat = cnt("SELECT COUNT(*) FROM batting_gamelogs WHERE season = :y")
            live_pit = cnt("SELECT COUNT(*) FROM pitching_gamelogs WHERE season = :y")
            nonretro_bat = cnt("SELECT COUNT(*) FROM batting_gamelogs "
                               "WHERE season = :y AND game_id NOT LIKE 'retro-%'")
            nonretro_pit = cnt("SELECT COUNT(*) FROM pitching_gamelogs "
                               "WHERE season = :y AND game_id NOT LIKE 'retro-%'")
            stg_bat = cnt("SELECT COUNT(*) FROM staging_batting_gamelogs WHERE season = :y")
            stg_pit = cnt("SELECT COUNT(*) FROM staging_pitching_gamelogs WHERE season = :y")

            # (a) Already promoted — live is entirely retro- on both tables. Skip
            # cleanly; do NOT re-churn correct production data (loop re-runnable).
            if nonretro_bat == 0 and nonretro_pit == 0:
                log.info("[promote] %d already_promoted (batting=%d pitching=%d retro-)",
                         year, live_bat, live_pit)
                return {"season": year, "action": "already_promoted",
                        "live_batting": live_bat, "live_pitching": live_pit,
                        "staging_batting": stg_bat, "staging_pitching": stg_pit}

            # (b) Inline gate re-check — same snapshot as the swap.
            b_sum, _ = _compare_gl_season(db, year, BattingGameLog, StagingBattingGameLog,
                                          _CMP_BAT_EQ, _CMP_BAT_BASERUN, is_batting=True)
            p_sum, _ = _compare_gl_season(db, year, PitchingGameLog, StagingPitchingGameLog,
                                          _CMP_PIT_EQ, [], is_batting=False)
            gb = b_sum["genuine_coverage_regression_count"]
            gp = p_sum["genuine_coverage_regression_count"]
            if gb != 0 or gp != 0:
                log.info("[promote] %d skipped_no_go (genuine batting=%d pitching=%d)", year, gb, gp)
                return {"season": year, "action": "skipped_no_go",
                        "genuine_batting": gb, "genuine_pitching": gp,
                        "live_batting": live_bat, "staging_batting": stg_bat,
                        "live_pitching": live_pit, "staging_pitching": stg_pit}

            # (c) Dry run — report the plan, write nothing.
            if not confirm:
                return {"season": year, "action": "would_swap", "verdict": "GO",
                        "live_batting": live_bat, "staging_batting": stg_bat,
                        "net_batting": stg_bat - live_bat,
                        "live_pitching": live_pit, "staging_pitching": stg_pit,
                        "net_pitching": stg_pit - live_pit}

            # (c) Swap — wholesale delete live + insert staging, both tables, one txn.
            db.execute(_sa_text("DELETE FROM batting_gamelogs WHERE season = :y"), {"y": year})
            db.execute(_sa_text(_BAT_PROMOTE_SQL), {"y": year})
            db.execute(_sa_text("DELETE FROM pitching_gamelogs WHERE season = :y"), {"y": year})
            db.execute(_sa_text(_PIT_PROMOTE_SQL), {"y": year})

            # (d) Pre-commit assertions — raise → get_session rolls back the season.
            post_bat = cnt("SELECT COUNT(*) FROM batting_gamelogs WHERE season = :y")
            post_pit = cnt("SELECT COUNT(*) FROM pitching_gamelogs WHERE season = :y")
            left_bat = cnt("SELECT COUNT(*) FROM batting_gamelogs "
                           "WHERE season = :y AND game_id NOT LIKE 'retro-%'")
            left_pit = cnt("SELECT COUNT(*) FROM pitching_gamelogs "
                           "WHERE season = :y AND game_id NOT LIKE 'retro-%'")
            if post_bat != stg_bat or post_pit != stg_pit or left_bat != 0 or left_pit != 0:
                raise RuntimeError(
                    f"post-swap assertion failed for {year}: "
                    f"batting {post_bat}/{stg_bat} (leftover {left_bat}), "
                    f"pitching {post_pit}/{stg_pit} (leftover {left_pit})")

            log.info("[promote] %d SWAPPED batting %d->%d, pitching %d->%d at %s",
                     year, live_bat, post_bat, live_pit, post_pit, ts)
            return {"season": year, "action": "swapped",
                    "pre_live_batting": live_bat, "post_live_batting": post_bat, "staging_batting": stg_bat,
                    "pre_live_pitching": live_pit, "post_live_pitching": post_pit, "staging_pitching": stg_pit,
                    "timestamp": ts}
            # <- get_session commits here on clean exit
    except Exception as exc:  # noqa: BLE001 - assertion/SQL failure rolled the season back
        tb = traceback.format_exc()
        log.error("[promote] %d swap_failed (rolled back): %s\n%s", year, exc, tb)
        return {"season": year, "action": "swap_failed", "error": str(exc), "traceback": tb}


@app.post("/admin/promote-staging-gamelogs")
def promote_staging_gamelogs(
    season: int | None = Query(default=None),
    confirm: bool = Query(default=False),
):
    """DESTRUCTIVE (confirm=true only). Replace live game logs with the staged
    Retrosheet rows, per season, atomically. Per season: skip if already
    promoted; else re-check the gate and swap ONLY if GO. confirm=false is a
    no-write dry run reporting exactly what would happen. Omit ?season= to loop
    2000-2025 (re-runnable — already-promoted seasons are skipped)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    if not _promote_lock.acquire(blocking=False):
        return {"status": "already_running", **_promote_state}
    _promote_state["running"] = True
    try:
        seasons = [season] if season is not None else list(range(2000, 2026))
        records = [_promote_one_season(y, confirm) for y in seasons]

        def _of(action):
            return [r["season"] for r in records if r["action"] == action]
        swapped = _of("swapped")
        if confirm and swapped:
            _cache.clear()

        result = {
            "confirm":          confirm,
            "season":           season,
            "seasons":          records,
            "swapped":          swapped,
            "already_promoted": _of("already_promoted"),
            "skipped_no_go":    _of("skipped_no_go"),
            "would_swap":       _of("would_swap"),
            "swap_failed":      _of("swap_failed"),
        }
        # Capture the outcome server-side (survives the HTTP response / log
        # rotation). Only records what happened — no swap behavior here.
        _promote_state.update({
            "last_run":         datetime.datetime.utcnow().isoformat() + "Z",
            "confirm":          confirm,
            "swapped":          swapped,
            "already_promoted": result["already_promoted"],
            "skipped_no_go":    result["skipped_no_go"],
            "would_swap":       result["would_swap"],
            "swap_failed":      result["swap_failed"],
            "errors":           {r["season"]: {"error": r.get("error"),
                                               "traceback": r.get("traceback")}
                                 for r in records if r["action"] == "swap_failed"},
        })
        return result
    finally:
        _promote_state["running"] = False
        _promote_lock.release()


@app.get("/admin/promote-staging-gamelogs/status")
def promote_staging_gamelogs_status():
    """Last promote-run outcome captured server-side — swapped / already_promoted
    / skipped_no_go / would_swap / swap_failed lists, plus per-season error +
    traceback for any swap_failed. Read-only."""
    return dict(_promote_state)


@app.get("/admin/volume-check")
def volume_check(path: str = Query(default="/data")):
    """Verify the mounted volume is present, writable, and readable from INSIDE
    the running backend, and report free space — the gate before uploading the
    plays.duckdb. Writes ONE tiny test file, reads it back, then deletes it (the
    only write). A permission error here means the container runs non-root and
    can't write the volume → set RAILWAY_RUN_UID=0 (runtime_uid shows the uid)."""
    import shutil
    r = {
        "checked_path":    path,
        "mount_path_env":  os.getenv("RAILWAY_VOLUME_MOUNT_PATH"),
        "volume_name_env": os.getenv("RAILWAY_VOLUME_NAME"),
        "runtime_uid":     os.getuid() if hasattr(os, "getuid") else None,
        "mount_exists":    os.path.exists(path),
        "is_dir":          os.path.isdir(path),
        "write_ok":        False,
        "read_ok":         False,
        "free_space_gb":   None,
        "total_space_gb":  None,
        "error":           None,
    }
    if not r["is_dir"]:
        return r
    try:
        du = shutil.disk_usage(path)
        r["free_space_gb"]  = round(du.free / 1024 ** 3, 2)
        r["total_space_gb"] = round(du.total / 1024 ** 3, 2)
    except Exception as exc:  # noqa: BLE001
        r["error"] = f"disk_usage: {exc}"
    test = os.path.join(path, "_volume_write_test.txt")
    content = "volume-check " + datetime.datetime.utcnow().isoformat() + "Z"
    try:
        with open(test, "w") as fh:
            fh.write(content)
        r["write_ok"] = True
        with open(test) as fh:
            r["read_ok"] = (fh.read() == content)
    except Exception as exc:  # noqa: BLE001 - a permission/IO error IS the answer we want
        r["error"] = ((r["error"] + "; ") if r["error"] else "") + f"write/read: {exc}"
    finally:
        try:
            os.remove(test)
        except OSError:
            pass
    return r


@app.get("/admin/volume-ls")
def volume_ls(path: str = Query(default="/data")):
    """READ-ONLY listing of the mounted volume — per entry: name, is_dir, and
    size (files via getsize; directories summed recursively so we can see what's
    actually consuming space), plus the total. listdir + getsize + walk only —
    no writes or deletes."""
    def _dir_size(p: str) -> int:
        tot = 0
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    tot += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return tot

    if not os.path.isdir(path):
        return {"path": path, "exists": os.path.exists(path), "is_dir": False,
                "entries": [], "total_mb": 0.0, "count": 0}

    entries = []
    total = 0
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        is_dir = os.path.isdir(full)
        try:
            size = _dir_size(full) if is_dir else os.path.getsize(full)
        except OSError:
            size = None
        entries.append({
            "name":    name,
            "is_dir":  is_dir,
            "size_mb": round(size / 1024 / 1024, 2) if size is not None else None,
        })
        if size:
            total += size
    return {"path": path, "entries": entries,
            "total_mb": round(total / 1024 / 1024, 2), "count": len(entries)}


_fetch_lock = threading.Lock()


@app.post("/admin/fetch-plays-db")
def fetch_plays_db(
    url: str = Query(
        default="https://github.com/igluck626/baseball-stats-app/releases/"
                "download/plays-data-v2/plays.duckdb"),
    confirm: bool = Query(default=False),
    expect_rows: int | None = Query(
        default=None, description="if set, fail unless plays row count matches exactly"),
):
    """Deliver plays.duckdb from a public GitHub Release asset onto the mounted
    volume at /data.

    confirm=false (default) = DRY-RUN: HEAD the url (FOLLOWING redirects to the
    signed CDN — a non-following request would report content-length 0) to
    confirm reachability + size, and report dest, free space, and whether a file
    already exists. No write.

    confirm=true = STREAM-download to /data/plays.duckdb.tmp in 8MB chunks
    (stream=True — never loads the file into memory), retry/backoff on transient
    failures, then atomic os.replace(tmp, dest). A partial/failed download never
    leaves a corrupt file at the real path (the .tmp is always cleaned up).

    VERIFY (the real intactness proof — NO hardcoded byte count, which would be
    a maintenance trap on every rebuild): a >500MB sanity floor before promoting,
    then duckdb opens it read-only and we report count(*) FROM plays (~15.8M),
    count(*) FROM coverage (116 — confirms the NEW store, since the old one has
    no coverage table), and min/max SEASON (1910/2025). Pass expect_rows for an
    exact row-count gate. Any check failing is flagged via `error`; the file is
    left in place for inspection."""
    import requests
    global _plays_conn, _plays_conn_error, _plays_warm, _plays_warm_secs, _plays_gen

    dest_dir = "/data"
    dest = os.path.join(dest_dir, "plays.duckdb")
    tmp = dest + ".tmp"
    r = {
        "url":                 url,
        "dest":                dest,
        "downloaded_bytes":    None,
        "downloaded_mb":       None,
        "free_space_after_gb": None,
        "duckdb_open_ok":      None,
        "plays_row_count":     None,
        "coverage_row_count":  None,
        "plays_min_season":    None,
        "plays_max_season":    None,
        "error":               None,
    }

    def _free_gb():
        try:
            import shutil
            return round(shutil.disk_usage(dest_dir).free / 1024 ** 3, 2)
        except Exception:  # noqa: BLE001
            return None

    if not os.path.isdir(dest_dir):
        r["error"] = f"{dest_dir} is not a directory (volume not mounted?)"
        return r

    # ---- DRY-RUN (confirm=false) ----------------------------------------
    if not confirm:
        r["dry_run"] = True
        r["file_exists"] = os.path.exists(dest)
        r["existing_size_mb"] = (round(os.path.getsize(dest) / 1024 / 1024, 2)
                                 if os.path.exists(dest) else None)
        r["free_space_after_gb"] = _free_gb()  # free space now (pre-download)
        try:
            head = requests.head(url, allow_redirects=True, timeout=30)
            r["remote_status"] = head.status_code
            cl = head.headers.get("Content-Length")
            r["remote_content_length"] = int(cl) if cl else None
            r["remote_content_length_mb"] = (round(int(cl) / 1024 / 1024, 2)
                                             if cl else None)
        except Exception as exc:  # noqa: BLE001
            r["error"] = f"HEAD: {exc}"
        return r

    # ---- DOWNLOAD (confirm=true) ----------------------------------------
    if not _fetch_lock.acquire(blocking=False):
        r["error"] = "another fetch is already in progress"
        return r
    try:
        downloaded = 0
        last_exc = None
        for attempt in range(1, 4):
            downloaded = 0
            try:
                with requests.get(url, stream=True, allow_redirects=True,
                                  timeout=(30, 300)) as resp:
                    resp.raise_for_status()
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                            if chunk:
                                fh.write(chunk)
                                downloaded += len(chunk)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                try:
                    os.remove(tmp)          # never keep a partial file
                except OSError:
                    pass
                time.sleep(2 ** attempt)    # 2s, 4s backoff
        if last_exc is not None:
            r["error"] = f"download failed after retries: {last_exc}"
            return r

        # sanity floor BEFORE promoting into place
        if downloaded < 500 * 1024 * 1024:
            try:
                os.remove(tmp)
            except OSError:
                pass
            r["downloaded_bytes"] = downloaded
            r["downloaded_mb"] = round(downloaded / 1024 / 1024, 2)
            r["error"] = (f"downloaded {downloaded} bytes < 500MB sanity floor; "
                          "aborted, tmp removed")
            return r

        os.replace(tmp, dest)               # atomic promote (same filesystem)
        r["free_space_after_gb"] = _free_gb()

        def _flag(msg):
            r["error"] = ((r["error"] + "; ") if r["error"] else "") + msg

        # ---- HOT-SWAP: drop the persistent connection, then VERIFY ------
        # DuckDB caches the DB instance per path within a process, so the
        # startup connection pins the OLD file — a fresh connect would return
        # that stale instance. Take the warmup lock first (so no in-flight
        # warmup keeps a connection open), close the persistent connection, bump
        # the generation, and only THEN verify with a genuinely fresh connect.
        with _plays_warmup_lock:
            with _plays_conn_lock:
                if _plays_conn is not None:
                    try:
                        _plays_conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                _plays_conn = None
                _plays_conn_error = None
                _plays_gen += 1        # invalidate any warmup that raced this swap
                _plays_warm = False
                _plays_warm_secs = None

            final = os.path.getsize(dest)
            r["downloaded_bytes"] = final
            r["downloaded_mb"] = round(final / 1024 / 1024, 2)
            try:
                import duckdb
                con = duckdb.connect(dest, read_only=True)  # fresh -> reads new file
                try:
                    r["plays_row_count"] = con.execute(
                        "SELECT count(*) FROM plays").fetchone()[0]
                    r["plays_min_season"], r["plays_max_season"] = con.execute(
                        "SELECT min(SEASON), max(SEASON) FROM plays").fetchone()
                    try:
                        r["coverage_row_count"] = con.execute(
                            "SELECT count(*) FROM coverage").fetchone()[0]
                    except Exception:  # noqa: BLE001 - old store has no coverage table
                        _flag("no `coverage` table — this looks like the OLD store, "
                              "not the new 1910-2025 build")
                    r["duckdb_open_ok"] = True
                finally:
                    con.close()
            except Exception as exc:  # noqa: BLE001
                # Verify failed. The connection is already dropped, so the next
                # query re-opens via _ensure_plays_conn against whatever is on
                # disk (degrades to the normal path — no broken state, no 500s).
                r["duckdb_open_ok"] = False
                _flag(f"duckdb verify: {exc} (download may be corrupt; file left)")
                return r

        # optional exact row-count gate
        if expect_rows is not None and r["plays_row_count"] != expect_rows:
            _flag(f"row-count mismatch: got {r['plays_row_count']}, "
                  f"expected {expect_rows}")

        # Re-warm the NEW store in the background (only on a clean verify).
        if r["duckdb_open_ok"] and not r["error"]:
            threading.Thread(target=_plays_warmup_worker, name="plays-rewarm",
                             daemon=True).start()
            r["rewarm_triggered"] = True
        return r
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)              # never leave a stray .tmp
        except OSError:
            pass
        _fetch_lock.release()


PLAYS_DB_PATH = "/data/plays.duckdb"

# Friendly event name -> Retrosheet EVENT_CD (whitelist; user input is matched
# against these keys only, never interpolated into SQL).
_PLAYS_EVENT_CD = {
    "HR": 23, "HOMERUN": 23, "HOME_RUN": 23,
    "K": 3, "SO": 3, "STRIKEOUT": 3,
    "BB": 14, "WALK": 14,
    "HBP": 16, "HITBYPITCH": 16,
    "1B": 20, "SINGLE": 20,
    "2B": 21, "DOUBLE": 21,
    "3B": 22, "TRIPLE": 22,
}


# Persistent read-only connection to the plays store. The Railway volume is
# network-attached (~50 MB/s), so a COLD duckdb.connect() costs ~900ms (reading
# metadata + ART indexes over slow storage) — but a warm one is ~0.1ms. Opening
# fresh per request made every query pay that tax. Instead we open ONCE and
# reuse it, handing each request its own cursor().
#
# Thread-safety: a single duckdb connection object is NOT safe for concurrent
# execute() from FastAPI's threadpool, but DuckDB's documented pattern is one
# shared connection + a per-thread .cursor() (each cursor is an independent
# execution context over the same read-only database). So requests never
# serialize on one connection and never re-pay the cold-open cost.
_plays_conn = None                     # the shared read-only connection (or None)
_plays_conn_lock = threading.Lock()    # guards the lazy/idempotent open
_plays_conn_error = None               # last open error / "not loaded" reason
_plays_warm = False                    # background warmup completed?
_plays_warm_secs = None                # how long the warmup took
# A warmup holds _plays_warmup_lock for its WHOLE run, so a store swap can wait
# for it — DuckDB caches the DB instance per path within a process, so any live
# connection (the persistent one OR an in-flight warmup) would pin the OLD file
# and defeat the post-swap verify. _plays_gen is bumped on every swap; a warmup
# that finishes after a swap sees the gen changed and discards its result.
_plays_warmup_lock = threading.Lock()
_plays_gen = 0


def _ensure_plays_conn():
    """Return the shared read-only connection, opening it once (idempotently,
    under a lock) on first need. If the file is missing or the open fails, set
    `_plays_conn_error` and return None — the app must boot and run fine without
    the store; callers turn None into a clean 503."""
    global _plays_conn, _plays_conn_error
    if _plays_conn is not None:
        return _plays_conn
    with _plays_conn_lock:
        if _plays_conn is not None:            # double-checked after acquiring
            return _plays_conn
        if not os.path.exists(PLAYS_DB_PATH):
            _plays_conn_error = (
                "plays store not loaded, run /admin/fetch-plays-db")
            return None
        try:
            import duckdb
            _plays_conn = duckdb.connect(PLAYS_DB_PATH, read_only=True)
            _plays_conn_error = None
            log.info("plays store connection opened (read-only)")
        except Exception as exc:  # noqa: BLE001
            _plays_conn_error = f"plays store open failed: {exc}"
            _plays_conn = None
            log.warning("plays store open failed: %s", exc)
        return _plays_conn


def _plays_cursor():
    """A fresh cursor over the shared connection for one request. Raises
    FileNotFoundError (→ 503) if the store isn't loaded."""
    conn = _ensure_plays_conn()
    if conn is None:
        raise FileNotFoundError(
            _plays_conn_error or "plays store not loaded, run /admin/fetch-plays-db")
    return conn.cursor()


# Warmup query set — pulls the FULL working set of /plays/situational through
# so no real query pays a cold-page cost on its first run. DuckDB reads only the
# columns a query touches, so warming one column isn't enough; these queries
# together read every filter/select column plus every ART index the endpoint
# uses. On the ~50 MB/s volume the two full-column scans dominate (they pull the
# bulk of the 734MB through), so this takes ~15-30s — fine in a daemon thread.
_PLAYS_WARMUP = [
    # (a) every filter/index column the endpoint filters on -> one full scan
    ("SELECT count(*) FROM plays WHERE BAT_ID IS NOT NULL AND PIT_ID IS NOT NULL "
     "AND EVENT_CD >= 0 AND BALLS_CT >= 0 AND STRIKES_CT >= 0 AND OUTS_CT >= 0 "
     "AND INN_CT >= 0 AND SEASON >= 0 AND GAME_TYPE IS NOT NULL", []),
    # (b) base-state + the sample SELECT columns -> a second full scan
    ("SELECT count(BASE1_RUN_ID), count(BASE2_RUN_ID), count(BASE3_RUN_ID), "
     "count(GAME_ID), max(GAME_DATE), count(EVENT_TX), count(AWAY_TEAM_ID), "
     "count(HOME_TEAM_ID), sum(BAT_HOME_ID) FROM plays", []),
    # (c) ART index pages: BAT_ID / PIT_ID equality, EVENT_CD + SEASON filters
    ("SELECT count(*) FROM plays WHERE BAT_ID = ?", ["alonp001"]),
    ("SELECT count(*) FROM plays WHERE PIT_ID = ?", ["kersc001"]),
    ("SELECT count(*) FROM plays WHERE EVENT_CD = ?", [23]),
    ("SELECT count(*) FROM plays WHERE SEASON = ?", [2019]),
    # (d) one representative real /plays/situational shape (count + ordered sample)
    ("SELECT GAME_DATE, HOME_TEAM_ID, AWAY_TEAM_ID, BAT_HOME_ID, INN_CT, "
     "BALLS_CT, STRIKES_CT, EVENT_TX, PIT_ID, BAT_ID FROM plays "
     "WHERE BAT_ID = ? AND EVENT_CD = 23 AND BALLS_CT = 3 AND STRIKES_CT = 2 "
     "ORDER BY GAME_DATE LIMIT 10", ["muncm001"]),
    # (e) the coverage table — the honesty gates join it on every plays query
    ("SELECT count(*), min(season), max(season) FROM coverage", []),
]


def _plays_warmup_worker():
    """Open (paying the ~900ms cold tax off the request path) then warm the OS
    page cache by pulling the columns AND index pages that /plays/situational
    actually touches (see _PLAYS_WARMUP) — so every query shape is fast from its
    first request, not just the OUTS_CT column. Runs in a daemon thread from
    startup so the app serves every other endpoint immediately while it warms."""
    global _plays_warm, _plays_warm_secs
    # Hold the warmup lock for the whole run so a concurrent store swap waits for
    # us to release our connection before it drops the persistent one and
    # verifies — otherwise our live cursor would keep the OLD DuckDB instance
    # pinned and the swap's verify would read stale data.
    with _plays_warmup_lock:
        with _plays_conn_lock:
            gen = _plays_gen
        conn = _ensure_plays_conn()
        if conn is None:
            log.warning("plays warmup skipped: %s", _plays_conn_error)
            return
        t = time.perf_counter()
        try:
            cur = conn.cursor()
            for sql, prm in _PLAYS_WARMUP:
                try:
                    cur.execute(sql, prm).fetchall()
                except Exception as exc:  # noqa: BLE001 - e.g. no coverage table on old store
                    log.warning("plays warmup query skipped: %s", exc)
            cur.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("plays warmup failed: %s", exc)
            return
        with _plays_conn_lock:
            if gen != _plays_gen:   # a swap happened while we warmed -> discard
                log.info("plays warmup superseded by a store swap; not marking warm")
                return
            _plays_warm_secs = round(time.perf_counter() - t, 2)
            _plays_warm = True
            log.info("plays store warmed in %ss", _plays_warm_secs)


def _resolve_retro_id(player: str, role: str):
    """Map an app player (MLBAM id if all-digits, else a name) to the Retrosheet
    person id the plays table keys on. Batters resolve via `players`, pitchers
    via `pitchers` (both carry retro_id, stamped by /admin/populate-retro-id).
    Returns a list of {mlbam_id, name, retro_id} candidates — 1 = resolved,
    0 = not found, >1 (distinct retro_id) = ambiguous, let the caller decide."""
    table = "pitchers" if role == "pit" else "players"
    p = player.strip()
    with connection.get_session() as db:
        if p.isdigit():
            rows = db.execute(_sa_text(
                f"SELECT player_id, name, retro_id FROM {table} "
                "WHERE player_id = :pid"), {"pid": int(p)}).fetchall()
        else:
            rows = db.execute(_sa_text(
                f"SELECT player_id, name, retro_id FROM {table} "
                "WHERE name ILIKE :nm ORDER BY name LIMIT 25"),
                {"nm": f"%{p}%"}).fetchall()
    return [{"mlbam_id": r[0], "name": r[1], "retro_id": r[2]} for r in rows]


# --- Phase 4 extension: routing + honesty gates -----------------------------
_PLAYS_FLOOR       = 1910    # play-by-play data floor (nothing before this)
_COVERAGE_MIN_PCT  = 99.0    # game-coverage: caveat when a queried season is below this
_COUNT_DECLINE_PCT = 90.0    # count-data: DECLINE (count=null) below this
_COUNT_CLEAN_PCT   = 99.0    # count-data: clean >= this; caveat in the band between
# Leaderboards: incomplete coverage distorts the RANKING (not just one count),
# and the gaps are era-correlated. If a queried span is less than this fraction
# of games covered (events-weighted), the ranking is too unreliable to present
# and we DECLINE rather than caveat. Above it, we rank + caveat.
_LB_WEIGHTED_DECLINE = 95.0
_COUNT_ERA = 1988           # pitch-count data becomes usable here (see coverage recon)

# Friendly/enum event -> canonical token (used by the season-stats route).
_CANON_EVENT = {
    "HR": "HR", "HOMERUN": "HR", "HOME_RUN": "HR",
    "K": "K", "SO": "K", "STRIKEOUT": "K",
    "BB": "BB", "WALK": "BB", "HBP": "HBP", "HITBYPITCH": "HBP",
    "1B": "1B", "SINGLE": "1B", "2B": "2B", "DOUBLE": "2B", "3B": "3B", "TRIPLE": "3B",
}

# (role, canonical event) -> season-stats column expression. Combos NOT here
# (HBP for anyone; 1B/2B/3B for pitchers) can't be expressed as a complete
# season total, so the caller falls through to the plays store instead.
# NB: the capitalized columns ("HR"/"SO"/"BB"/"H") MUST be double-quoted —
# Postgres folds unquoted identifiers to lowercase, and there is no `hr`
# column. `doubles`/`triples` are physically lowercase, so left unquoted.
_SEASON_COL = {
    ("bat", "HR"): '"HR"', ("bat", "K"): '"SO"', ("bat", "BB"): '"BB"',
    ("bat", "2B"): "doubles", ("bat", "3B"): "triples",
    ("bat", "1B"): '("H" - doubles - triples - "HR")',
    ("pit", "HR"): '"HR"', ("pit", "K"): '"SO"', ("pit", "BB"): '"BB"',
}


def _canon_event(event):
    if event is None:
        return None
    return _CANON_EVENT.get(event.strip().upper().replace(" ", "_"))


def _game_type_filter(game_type):
    """Normalize game_type for the plays store. DEFAULT (unspecified) = REGULAR
    SEASON only — a career/season stat means regular season unless the user asks
    otherwise, and this keeps the plays path consistent with the (regular-season)
    season-stats route. 'P' = postseason, 'A' = all-star, 'ALL' = every game type
    (widen). Returns (clause_or_None, param_or_None, echo_value)."""
    gt = (game_type or "R").strip().upper()
    if gt == "ALL":
        return None, None, "ALL"
    if gt not in ("R", "P", "A"):
        raise HTTPException(status_code=400, detail="game_type must be R, P, A, or ALL")
    return "GAME_TYPE = ?", gt, gt


def _run_season_total(player, role="bat", event=None, season=None,
                      season_start=None, season_end=None, game_type=None):
    """GATE 0 (plain-total branch): answer a non-situational count from the
    COMPLETE season-stats tables (player_seasons / pitcher_seasons, or the
    postseason tables when game_type='P'), keyed by mlbam player_id. Career =
    sum across years, single season = one year, range = sum over the range.
    Returns the resolved dict, the ambiguous-candidates dict, or None when
    season-stats can't express this (role, event) — the caller then falls
    through to the plays store (game-coverage gate only, no official number to
    contradict). No coverage caveat on this path: these totals are complete."""
    role = (role or "bat").lower()
    canon = _canon_event(event)
    gt = (game_type or "").strip().upper() or None
    col = _SEASON_COL.get((role, canon))
    # All-Star totals and unsupported (role, event) combos aren't in season-stats
    if col is None or gt == "A":
        return None
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    cands = [c for c in _resolve_retro_id(player, role) if c["mlbam_id"] is not None]
    if not cands:
        raise HTTPException(status_code=404, detail=f"No player matching '{player}'")
    ids = {c["mlbam_id"] for c in cands}
    if len(ids) > 1:
        return {"resolved": False, "ambiguous": True, "query": player,
                "candidates": cands[:25]}
    resolved = cands[0]
    pid = resolved["mlbam_id"]

    if gt == "P":
        table = "player_postseason_pitching" if role == "pit" else "player_postseason_batting"
    else:
        table = "pitcher_seasons" if role == "pit" else "player_seasons"
    where = ["player_id = :pid"]
    p: dict = {"pid": int(pid)}
    if season is not None:
        where.append("year = :y"); p["y"] = int(season)
    else:
        if season_start is not None:
            where.append("year >= :ys"); p["ys"] = int(season_start)
        if season_end is not None:
            where.append("year <= :ye"); p["ye"] = int(season_end)
    sql = (f"SELECT COALESCE(SUM({col}),0), MIN(year), MAX(year), COUNT(*) "
           f"FROM {table} WHERE {' AND '.join(where)}")
    with connection.get_session() as db:
        total, minyr, maxyr, nrows = db.execute(_sa_text(sql), p).fetchone()

    return {
        "resolved": True, "source": "season_stats",
        "player": {"query": player, "name": resolved["name"],
                   "mlbam_id": pid, "role": role},
        "filters": {"event": canon, "season": season, "season_start": season_start,
                    "season_end": season_end, "game_type": gt},
        "count": int(total) if nrows else 0,
        "empty": nrows == 0,           # no season rows (e.g. season outside the career)
        "span": [minyr, maxyr],
        "sample": [],
        # complete source -> gates always clear
        "game_coverage": {"complete": True},
        "count_data": None,
    }


def _coverage_gates(cur, lo, hi, uses_count, single_season):
    """GATE 1 + GATE 2 for a plays query over season span [lo, hi]:
      - game_coverage: caveat if any season in span has coverage_pct < 99.
      - count_data: only when the query filters on balls/strikes — DECLINE
        (available False) below 90% count-data availability, CAVEAT 90-99%,
        clean at >=99%. Availability is events-weighted over the span (so a
        modern career isn't dragged down by one thin early year).
    Degrades to complete/None if the coverage table isn't present (older store)."""
    try:
        row = cur.execute(
            "SELECT min(coverage_pct), "
            "       sum(games_with_pbp)*100.0/nullif(sum(games_played),0), "
            "       sum(events_with_count)*100.0/nullif(sum(events),0), "
            "       list(season) FILTER (WHERE coverage_pct < ?) "
            "FROM coverage WHERE season BETWEEN ? AND ?",
            [_COVERAGE_MIN_PCT, lo, hi]).fetchone()
    except Exception:  # noqa: BLE001 - coverage table absent on the old store
        return {"complete": True}, None
    min_pct, era_games_pct, era_count_pct, low_seasons = row
    low_seasons = sorted(low_seasons) if low_seasons else []

    if low_seasons:
        if single_season:
            note = f"Based on the {round(min_pct)}% of {lo} games with play-by-play data."
        else:
            note = (f"Based on the {round(era_games_pct or 0)}% of games from this "
                    "player's era that have play-by-play data.")
        game_coverage = {"complete": False, "min_pct": min_pct,
                         "low_seasons": low_seasons, "note": note}
    else:
        game_coverage = {"complete": True}

    count_data = None
    if uses_count:
        pct = round(era_count_pct, 1) if era_count_pct is not None else 0.0
        if pct < _COUNT_DECLINE_PCT:
            count_data = {"available": False, "pct": pct,
                          "note": ("Pitch-count data wasn't recorded for most games in "
                                   f"this era (only {pct}% of plays have it), so a "
                                   "count-based total would be misleading, not zero.")}
        elif pct < _COUNT_CLEAN_PCT:
            count_data = {"available": True, "pct": pct,
                          "note": f"Based on the {pct}% of plays in this span with "
                                  "recorded pitch counts."}
        else:
            count_data = {"available": True, "pct": pct}
    return game_coverage, count_data


def _run_situational(
    player: str, role: str = "bat", event: str | None = None,
    balls: int | None = None, strikes: int | None = None,
    outs: int | None = None, inning: int | None = None,
    base_state: str | None = None, season: int | None = None,
    season_start: int | None = None, season_end: int | None = None,
    game_type: str | None = None, sample_limit: int = 10,
):
    """Core situational query, shared by GET /plays/situational and POST /ask.
    Raises HTTPException on bad input / missing store (400/404/503); returns the
    ambiguous-candidates dict when a name maps to >1 player; otherwise returns
    the resolved {count, sample, player, filters, query_ms} dict."""
    role = (role or "bat").lower()
    if role not in ("bat", "pit"):
        raise HTTPException(status_code=400, detail="role must be 'bat' or 'pit'")

    event_cd = None
    if event is not None:
        event_cd = _PLAYS_EVENT_CD.get(event.strip().upper().replace(" ", "_"))
        if event_cd is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown event '{event}'. Known: {sorted(set(_PLAYS_EVENT_CD))}")

    gt_clause, gt_param, gt = _game_type_filter(game_type)   # default -> regular season

    bs = None
    if base_state is not None:
        bs = base_state.strip().lower()
        if bs not in ("risp", "loaded"):
            raise HTTPException(status_code=400, detail="base_state must be 'risp' or 'loaded'")

    # GATE 3: data floor — decline (never return 0) if the WHOLE queried range
    # predates the 1910 play-by-play floor.
    asked = [s for s in (season, season_start, season_end) if s is not None]
    if asked and max(asked) < _PLAYS_FLOOR:
        raise HTTPException(
            status_code=400,
            detail=f"before the {_PLAYS_FLOOR} play-by-play data floor")
    uses_count = balls is not None or strikes is not None   # GATE 2 applies only then

    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    # ---- resolve player -> retro_id -------------------------------------
    cands = _resolve_retro_id(player, role)
    cands = [c for c in cands if c["retro_id"]]  # only usable (retro_id present)
    if not cands:
        raise HTTPException(status_code=404,
                            detail=f"No player with a retro_id matching '{player}'")
    distinct = {c["retro_id"] for c in cands}
    if len(distinct) > 1:
        # ambiguous name — hand back the candidates instead of guessing
        return {"resolved": False, "ambiguous": True, "query": player,
                "candidates": cands[:25]}
    resolved = cands[0]
    retro_id = resolved["retro_id"]

    # ---- build parameterized filters ------------------------------------
    id_col = "PIT_ID" if role == "pit" else "BAT_ID"
    where = [f"{id_col} = ?"]
    params: list = [retro_id]
    if event_cd is not None:
        where.append("EVENT_CD = ?");    params.append(event_cd)
    if balls is not None:
        where.append("BALLS_CT = ?");    params.append(balls)
    if strikes is not None:
        where.append("STRIKES_CT = ?");  params.append(strikes)
    if outs is not None:
        where.append("OUTS_CT = ?");     params.append(outs)
    if inning is not None:
        where.append("INN_CT = ?");      params.append(inning)
    if season is not None:
        where.append("SEASON = ?");      params.append(season)
    if season_start is not None:
        where.append("SEASON >= ?");     params.append(season_start)
    if season_end is not None:
        where.append("SEASON <= ?");     params.append(season_end)
    if gt_clause:
        where.append(gt_clause);         params.append(gt_param)
    if bs == "risp":
        where.append("(BASE2_RUN_ID IS NOT NULL OR BASE3_RUN_ID IS NOT NULL)")
    elif bs == "loaded":
        where.append("(BASE1_RUN_ID IS NOT NULL AND BASE2_RUN_ID IS NOT NULL "
                      "AND BASE3_RUN_ID IS NOT NULL)")
    clause = " AND ".join(where)

    # ---- run (count + sample), timed ------------------------------------
    # Per-request cursor over the shared, pre-warmed connection (no cold open).
    try:
        cur = _plays_cursor()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        t0 = time.perf_counter()
        count = cur.execute(
            f"SELECT count(*) FROM plays WHERE {clause}", params).fetchone()[0]
        sample = []
        if sample_limit:
            rows = cur.execute(
                "SELECT GAME_DATE, HOME_TEAM_ID, AWAY_TEAM_ID, BAT_HOME_ID, "
                "INN_CT, BALLS_CT, STRIKES_CT, EVENT_TX, PIT_ID, BAT_ID "
                f"FROM plays WHERE {clause} ORDER BY GAME_DATE LIMIT ?",
                params + [sample_limit]).fetchall()
            for gd, home, away, bat_home, inn, b, s, tx, pit, bat in rows:
                # opponent = the team the batter is NOT on
                opp = away if bat_home == 1 else home
                sample.append({
                    "game_date":   str(gd),
                    "opponent":    opp,
                    "inning":      inn,
                    "count":       f"{b}-{s}",
                    "pitcher_id":  pit if role == "bat" else None,
                    "batter_id":   bat if role == "pit" else None,
                    "description": tx,
                })
        # ---- season span for the coverage gates -------------------------
        if season is not None:
            lo = hi = season; single_season = True
        elif season_start is not None or season_end is not None:
            lo = season_start or _PLAYS_FLOOR; hi = season_end or 2025
            single_season = (lo == hi)
        else:  # career: span the matched rows actually cover
            mm = cur.execute(
                f"SELECT min(SEASON), max(SEASON) FROM plays WHERE {clause}",
                params).fetchone()
            lo, hi = (mm[0], mm[1]) if mm and mm[0] is not None else (_PLAYS_FLOOR, 2025)
            single_season = (lo == hi)
        game_coverage, count_data = _coverage_gates(cur, lo, hi, uses_count, single_season)
        query_ms = round((time.perf_counter() - t0) * 1000, 1)
    finally:
        cur.close()   # closes only the cursor, not the shared connection

    # GATE 2 decline: below the count-data floor, never return a misleading
    # near-zero count — null it out and drop the (also-misleading) sample.
    if count_data is not None and count_data.get("available") is False:
        count = None
        sample = []

    result = {
        "resolved": True,
        "source":   "plays",
        "player": {
            "query":    player,
            "name":     resolved["name"],
            "mlbam_id": resolved["mlbam_id"],
            "retro_id": retro_id,
            "role":     role,
        },
        "filters": {
            "event": event, "event_cd": event_cd, "balls": balls,
            "strikes": strikes, "outs": outs, "inning": inning,
            "base_state": bs, "season": season, "season_start": season_start,
            "season_end": season_end, "game_type": gt,
        },
        "count":         count,
        "sample":        sample,
        "query_ms":      query_ms,
        "game_coverage": game_coverage,
    }
    if uses_count:
        result["count_data"] = count_data
    return result


# Retrosheet biofile retro_id -> display name, slimmed to (key_retro, name).
# Shipped in backend/data/retrosheet/. Covers 100% of the plays store's ids, so
# it's the last-resort name for leaderboard players absent from players/pitchers
# (e.g. CC Sabathia, whose bio row we don't carry) — never show a raw retro id.
_RETRO_NAMES_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "retrosheet", "retro_names.csv")
_retro_names_map: dict | None = None
_retro_names_lock = threading.Lock()


def _retro_names():
    """Cached retro_id -> name map from the shipped biofile slim (loaded once)."""
    global _retro_names_map
    if _retro_names_map is None:
        with _retro_names_lock:
            if _retro_names_map is None:
                m: dict = {}
                try:
                    with open(_RETRO_NAMES_CSV, newline="") as f:
                        for row in csv.DictReader(f):
                            rid = (row.get("key_retro") or "").strip()
                            if rid:
                                m[rid] = (row.get("name") or "").strip() or rid
                except Exception as exc:  # noqa: BLE001
                    log.warning("retro_names load failed: %s", exc)
                _retro_names_map = m
    return _retro_names_map


# mlbam -> name from the full Chadwick register. The Retrosheet biofile above is
# keyed by retro_id and so CANNOT reach Negro Leagues players (no Retrosheet
# play-by-play -> no retro_id), whose season stats came from Lahman. The register
# keys names by mlbam directly, so it names them. Used as the second name source
# for the bio backfill.
_MLBAM_NAMES_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "retrosheet", "mlbam_names.csv")
_mlbam_names_map: dict | None = None
_mlbam_names_lock = threading.Lock()


def _mlbam_names():
    """Cached mlbam(int) -> name map from the shipped Chadwick register slim."""
    global _mlbam_names_map
    if _mlbam_names_map is None:
        with _mlbam_names_lock:
            if _mlbam_names_map is None:
                m: dict = {}
                try:
                    with open(_MLBAM_NAMES_CSV, newline="") as f:
                        for row in csv.DictReader(f):
                            mm = (row.get("key_mlbam") or "").strip()
                            nm = (row.get("name") or "").strip()
                            if mm.isdigit() and nm:
                                m[int(mm)] = nm
                except Exception as exc:  # noqa: BLE001
                    log.warning("mlbam_names load failed: %s", exc)
                _mlbam_names_map = m
    return _mlbam_names_map


def _leaderboard_gates(cur, lo, hi, uses_count, scoped):
    """Coverage gates for a LEADERBOARD over span [lo, hi] — stricter than the
    single-player gates, because incomplete coverage distorts the RANKING itself
    and the gaps are era-correlated (WWII, the 1920s-30s). Returns
    (game_coverage, count_data, decline_reason); decline_reason is set when the
    span is too incomplete to rank fairly (the caller then returns no leaders).

    Game coverage: complete if every season >= 99%; else if the span is still
    >=95% games-covered (events-weighted) — e.g. an all-time board whose top is
    modern-dominated — rank + caveat naming the worst season; below 95%
    (typically a query scoped INTO a bad era) the ranking is noise -> DECLINE.
    Count data (only with balls/strikes): a SCOPED count board over a low-count
    era -> DECLINE; an unscoped count board self-restricts to the recorded-count
    era (the filter only matches plays that HAVE a count) -> rank + note."""
    try:
        row = cur.execute(
            "SELECT min(coverage_pct), "
            "       sum(games_with_pbp)*100.0/nullif(sum(games_played),0), "
            "       sum(events_with_count)*100.0/nullif(sum(events),0), "
            "       arg_min(season, coverage_pct) "
            "FROM coverage WHERE season BETWEEN ? AND ?", [lo, hi]).fetchone()
    except Exception:  # noqa: BLE001 - coverage table absent on the old store
        return {"complete": True}, None, None
    min_pct, wtd_games, wtd_count, worst = row
    decline = None

    if uses_count and not scoped:
        # A count-based, unscoped board self-restricts to the recorded-count era
        # (~1988+, since the balls/strikes filter only matches plays that HAVE a
        # count), where game coverage is complete — so the raw span's early
        # game-coverage gaps are irrelevant. The count_data note carries the era.
        game_coverage = {"complete": True}
    elif min_pct is None or min_pct >= _COVERAGE_MIN_PCT:
        game_coverage = {"complete": True}
    elif (wtd_games or 0) >= _LB_WEIGHTED_DECLINE:
        game_coverage = {
            "complete": False, "min_pct": round(min_pct, 1),
            "weighted_pct": round(wtd_games, 1), "worst_season": worst,
            "note": (f"Rankings span seasons with uneven play-by-play coverage "
                     f"(as low as {round(min_pct)}% in {worst}); players from the "
                     "less-covered early seasons may be undercounted.")}
    else:
        game_coverage = {"complete": False, "min_pct": round(min_pct, 1),
                         "weighted_pct": round(wtd_games or 0, 1), "worst_season": worst}
        decline = (f"Play-by-play coverage across {lo}-{hi} is too incomplete to rank "
                   f"players fairly — only {round(wtd_games or 0)}% of games have "
                   "play-by-play, unevenly across seasons. Ask about a specific player instead.")

    count_data = None
    if uses_count:
        if scoped:
            cp = round(wtd_count or 0, 1)
            if cp < _COUNT_DECLINE_PCT:
                count_data = {"available": False, "pct": cp}
                decline = decline or (
                    f"Pitch-count data across {lo}-{hi} is too sparse to rank players "
                    f"by count — only {cp}% of plays have a recorded count.")
            else:
                count_data = {"available": True, "pct": cp}
        else:
            # unscoped count board: the balls/strikes filter only matches plays
            # that HAVE a count, so the board self-restricts to the recorded era.
            count_data = {"available": True,
                          "note": f"Reflects seasons since pitch counts were "
                                  f"recorded (~{_COUNT_ERA} on)."}
    return game_coverage, count_data, decline


def _run_leaderboard(event=None, role="bat", balls=None, strikes=None, outs=None,
                     inning=None, base_state=None, season=None, season_start=None,
                     season_end=None, game_type=None, limit=10):
    """'Who has the most <event> in situation Y' — the plays store keyed the same
    way, but GROUP BY the batter/pitcher id instead of filtering to one player,
    ranked DESC. Coverage gates (see _leaderboard_gates) can DECLINE a badly
    covered span rather than present a distorted ranking as fact. Retro ids are
    resolved to display names via players/pitchers. Returns a ranked dict, or a
    {declined:true, reason} dict when coverage won't support a fair ranking.

    NOTE: pure COUNT leaderboards need no minimum-opportunity qualifier (most is
    most). A future rate-stat leaderboard WOULD — the HAVING clause on the
    GROUP BY below (e.g. `HAVING count(*) >= :min_pa`) is where it belongs."""
    role = (role or "bat").lower()
    if role not in ("bat", "pit"):
        raise HTTPException(status_code=400, detail="role must be 'bat' or 'pit'")
    event_cd = _PLAYS_EVENT_CD.get((event or "").strip().upper().replace(" ", "_"))
    if event_cd is None:
        raise HTTPException(status_code=400,
                            detail=f"leaderboard needs a known event; got {event!r}")
    canon = _canon_event(event)
    gt_clause, gt_param, gt = _game_type_filter(game_type)   # default -> regular season
    bs = None
    if base_state is not None:
        bs = base_state.strip().lower()
        if bs not in ("risp", "loaded"):
            raise HTTPException(status_code=400, detail="base_state must be 'risp' or 'loaded'")
    limit = max(1, min(int(limit or 10), 25))

    asked = [s for s in (season, season_start, season_end) if s is not None]
    if asked and max(asked) < _PLAYS_FLOOR:
        raise HTTPException(status_code=400,
                            detail=f"before the {_PLAYS_FLOOR} play-by-play data floor")
    uses_count = balls is not None or strikes is not None
    scoped = bool(asked)

    id_col = "PIT_ID" if role == "pit" else "BAT_ID"
    where = [f"{id_col} IS NOT NULL", f"{id_col} <> ''", "EVENT_CD = ?"]
    params: list = [event_cd]
    if balls is not None:
        where.append("BALLS_CT = ?");    params.append(balls)
    if strikes is not None:
        where.append("STRIKES_CT = ?");  params.append(strikes)
    if outs is not None:
        where.append("OUTS_CT = ?");     params.append(outs)
    if inning is not None:
        where.append("INN_CT = ?");      params.append(inning)
    if season is not None:
        where.append("SEASON = ?");      params.append(season)
    if season_start is not None:
        where.append("SEASON >= ?");     params.append(season_start)
    if season_end is not None:
        where.append("SEASON <= ?");     params.append(season_end)
    if gt_clause:
        where.append(gt_clause);         params.append(gt_param)
    if bs == "risp":
        where.append("(BASE2_RUN_ID IS NOT NULL OR BASE3_RUN_ID IS NOT NULL)")
    elif bs == "loaded":
        where.append("(BASE1_RUN_ID IS NOT NULL AND BASE2_RUN_ID IS NOT NULL "
                     "AND BASE3_RUN_ID IS NOT NULL)")
    clause = " AND ".join(where)
    filters = {"event": canon, "role": role, "balls": balls, "strikes": strikes,
               "outs": outs, "inning": inning, "base_state": bs, "season": season,
               "season_start": season_start, "season_end": season_end, "game_type": gt}

    def _base(**extra):
        out = {"resolved": True, "source": "plays_leaderboard", "filters": filters,
               "limit": limit}
        out.update(extra)
        return out

    try:
        cur = _plays_cursor()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        t0 = time.perf_counter()
        mm = cur.execute(
            f"SELECT min(SEASON), max(SEASON) FROM plays WHERE {clause}", params).fetchone()
        if not mm or mm[0] is None:                       # nothing matched at all
            return _base(leaders=[], game_coverage={"complete": True},
                         count_data=None,
                         query_ms=round((time.perf_counter() - t0) * 1000, 1))
        lo, hi = mm
        game_coverage, count_data, decline = _leaderboard_gates(cur, lo, hi, uses_count, scoped)
        if decline is not None:
            return _base(leaders=None, declined=True, reason=decline,
                         game_coverage=game_coverage, count_data=count_data,
                         query_ms=round((time.perf_counter() - t0) * 1000, 1))
        rows = cur.execute(
            f"SELECT {id_col}, count(*) AS n FROM plays WHERE {clause} "
            f"GROUP BY {id_col} ORDER BY n DESC, {id_col} LIMIT ?",
            params + [limit]).fetchall()
        query_ms = round((time.perf_counter() - t0) * 1000, 1)
    finally:
        cur.close()

    # resolve retro ids -> display names (batters via players, pitchers via pitchers)
    retro_ids = [r[0] for r in rows]
    name_map: dict = {}
    if retro_ids and connection.db_available():
        ptable = "pitchers" if role == "pit" else "players"
        try:
            with connection.get_session() as db:
                for rid, pid, nm in db.execute(_sa_text(
                        f"SELECT retro_id, player_id, name FROM {ptable} "
                        "WHERE retro_id = ANY(:ids)"), {"ids": retro_ids}).fetchall():
                    name_map[rid] = {"mlbam_id": pid, "name": nm}
        except Exception as exc:  # noqa: BLE001
            log.warning("leaderboard name resolution failed: %s", exc)

    biofile = _retro_names()   # last-resort names (players/pitchers gap) — 100% coverage
    leaders = []
    for i, (rid, n) in enumerate(rows, 1):
        info = name_map.get(rid) or {}
        leaders.append({
            "rank": i,
            "player_name": info.get("name") or biofile.get(rid) or rid,
            "mlbam_id": info.get("mlbam_id"), "retro_id": rid, "count": n})
    return _base(leaders=leaders, game_coverage=game_coverage,
                 count_data=count_data, query_ms=query_ms)


def _run_season_leaderboard(event=None, role="bat", season=None, season_start=None,
                            season_end=None, game_type=None, limit=10):
    """Plain (non-situational) leaderboard from the COMPLETE season-stats tables
    — the leaderboard analogue of _run_season_total, so 'most career home runs'
    ranks by real totals (Ruth included) instead of coverage-limited plays.
    Returns None for (role, event) season-stats can't express (HBP, pitcher
    1B/2B/3B, All-Star) so the caller falls through to the plays leaderboard."""
    role = (role or "bat").lower()
    canon = _canon_event(event)
    gt = (game_type or "").strip().upper() or None
    col = _SEASON_COL.get((role, canon))
    if col is None or gt == "A":
        return None
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    limit = max(1, min(int(limit or 10), 25))
    if gt == "P":
        table = "player_postseason_pitching" if role == "pit" else "player_postseason_batting"
    else:
        table = "pitcher_seasons" if role == "pit" else "player_seasons"
    where = ["1=1"]
    p: dict = {"lim": limit}
    if season is not None:
        where.append("year = :y"); p["y"] = int(season)
    else:
        if season_start is not None:
            where.append("year >= :ys"); p["ys"] = int(season_start)
        if season_end is not None:
            where.append("year <= :ye"); p["ye"] = int(season_end)
    sql = (f"SELECT player_id, SUM({col}) AS n FROM {table} WHERE {' AND '.join(where)} "
           f"GROUP BY player_id HAVING SUM({col}) > 0 ORDER BY n DESC, player_id LIMIT :lim")
    with connection.get_session() as db:
        rows = db.execute(_sa_text(sql), p).fetchall()
        ids = [r[0] for r in rows]
        names = {}
        if ids:
            for pid, nm in db.execute(_sa_text(
                    "SELECT player_id, name FROM players WHERE player_id = ANY(:ids)"),
                    {"ids": ids}).fetchall():
                names[pid] = nm
    leaders = [{"rank": i, "player_name": names.get(pid) or str(pid),
                "mlbam_id": pid, "count": int(n)}
               for i, (pid, n) in enumerate(rows, 1)]
    return {"resolved": True, "source": "season_stats_leaderboard",
            "filters": {"event": canon, "role": role, "season": season,
                        "season_start": season_start, "season_end": season_end,
                        "game_type": gt},
            "limit": limit, "leaders": leaders,
            "game_coverage": {"complete": True}, "count_data": None}


# ============================================================================
# Rate stats + splits (Phase 6). Formulas verified EXACT vs official on
# 100%-covered seasons (Judge 2022 .311/.425/.686/1.111, Betts 2018, Trout 2019):
#   AVG = H/AB   OBP = (H+BB+HBP)/(AB+BB+HBP+SF)   SLG = TB/AB   OPS = OBP+SLG
#   TB = 1B + 2*2B + 3*3B + 4*HR ; PA = AB+BB+HBP+SF+SH ; SH is NOT in the OBP denom.
#   H = EVENT_CD in (20,21,22,23); BB = 14/15 (incl. intentional); HBP = 16.
# ============================================================================
_RATE_STATS = ("AVG", "OBP", "SLG", "OPS")
_RATE_COLS = ["ab", "h", "d1", "d2", "d3", "hr", "bb", "hbp", "sf", "sh"]


def _rate_components_sql(prefix=""):
    p = prefix
    return (
        f"SUM(CASE WHEN {p}AB_FL THEN 1 ELSE 0 END) AS ab, "
        f"SUM(CASE WHEN {p}EVENT_CD IN (20,21,22,23) THEN 1 ELSE 0 END) AS h, "
        f"SUM(CASE WHEN {p}EVENT_CD=20 THEN 1 ELSE 0 END) AS d1, "
        f"SUM(CASE WHEN {p}EVENT_CD=21 THEN 1 ELSE 0 END) AS d2, "
        f"SUM(CASE WHEN {p}EVENT_CD=22 THEN 1 ELSE 0 END) AS d3, "
        f"SUM(CASE WHEN {p}EVENT_CD=23 THEN 1 ELSE 0 END) AS hr, "
        f"SUM(CASE WHEN {p}EVENT_CD IN (14,15) THEN 1 ELSE 0 END) AS bb, "
        f"SUM(CASE WHEN {p}EVENT_CD=16 THEN 1 ELSE 0 END) AS hbp, "
        f"SUM(CASE WHEN {p}SF_FL THEN 1 ELSE 0 END) AS sf, "
        f"SUM(CASE WHEN {p}SH_FL THEN 1 ELSE 0 END) AS sh")


def _derive_rates(c):
    """Components dict -> {PA,AB,H,doubles,triples,HR,BB,HBP,SF,AVG,OBP,SLG,OPS}."""
    ab = c["ab"] or 0; h = c["h"] or 0; bb = c["bb"] or 0
    hbp = c["hbp"] or 0; sf = c["sf"] or 0; sh = c["sh"] or 0
    tb = (c["d1"] or 0) + 2 * (c["d2"] or 0) + 3 * (c["d3"] or 0) + 4 * (c["hr"] or 0)
    pa = ab + bb + hbp + sf + sh
    obp_den = ab + bb + hbp + sf
    avg = round(h / ab, 3) if ab else None
    obp = round((h + bb + hbp) / obp_den, 3) if obp_den else None
    slg = round(tb / ab, 3) if ab else None
    ops = round(obp + slg, 3) if (obp is not None and slg is not None) else None
    return {"PA": pa, "AB": ab, "H": h, "doubles": c["d2"] or 0, "triples": c["d3"] or 0,
            "HR": c["hr"] or 0, "BB": bb, "HBP": hbp, "SF": sf,
            "AVG": avg, "OBP": obp, "SLG": slg, "OPS": ops}


def _situ_clauses(balls, strikes, outs, inning, base_state,
                  season, season_start, season_end, game_type):
    """Shared situational filters (no player, no event) — the same set
    _run_situational uses. Returns (clauses, params, echo_meta); validates enums."""
    where = []; params = []
    if balls is not None:        where.append("BALLS_CT = ?");   params.append(balls)
    if strikes is not None:      where.append("STRIKES_CT = ?"); params.append(strikes)
    if outs is not None:         where.append("OUTS_CT = ?");    params.append(outs)
    if inning is not None:       where.append("INN_CT = ?");     params.append(inning)
    if season is not None:       where.append("SEASON = ?");     params.append(season)
    if season_start is not None: where.append("SEASON >= ?");    params.append(season_start)
    if season_end is not None:   where.append("SEASON <= ?");    params.append(season_end)
    gt_clause, gt_param, gt = _game_type_filter(game_type)   # default -> regular season
    if gt_clause:
        where.append(gt_clause); params.append(gt_param)
    bs = None
    if base_state is not None:
        bs = base_state.strip().lower()
        if bs not in ("risp", "loaded"):
            raise HTTPException(status_code=400, detail="base_state must be 'risp' or 'loaded'")
        if bs == "risp":
            where.append("(BASE2_RUN_ID IS NOT NULL OR BASE3_RUN_ID IS NOT NULL)")
        else:
            where.append("(BASE1_RUN_ID IS NOT NULL AND BASE2_RUN_ID IS NOT NULL "
                         "AND BASE3_RUN_ID IS NOT NULL)")
    meta = {"balls": balls, "strikes": strikes, "outs": outs, "inning": inning,
            "base_state": bs, "season": season, "season_start": season_start,
            "season_end": season_end, "game_type": gt}
    return where, params, meta


def _rate_span(cur, clause, params, season, season_start, season_end):
    """(lo, hi, single_season) for the coverage gate over a rate query's span."""
    if season is not None:
        return season, season, True
    if season_start is not None or season_end is not None:
        lo = season_start or _PLAYS_FLOOR; hi = season_end or 2025
        return lo, hi, (lo == hi)
    mm = cur.execute(f"SELECT min(SEASON), max(SEASON) FROM plays WHERE {clause}",
                     params).fetchone()
    if mm and mm[0] is not None:
        return mm[0], mm[1], (mm[0] == mm[1])
    return _PLAYS_FLOOR, 2025, False


def _run_rates(player, role="bat", balls=None, strikes=None, outs=None, inning=None,
               base_state=None, season=None, season_start=None, season_end=None,
               game_type=None):
    """Situational RATE line (AVG/OBP/SLG/OPS + components) for one player.
    Coverage: game-coverage only CAVEATS (a rate is a ratio — numerator and
    denominator miss the same ~6%, so the rate stays a valid estimate; unlike a
    count, it isn't 'wrong'). The count-data gate still applies HARD when the
    query filters on balls/strikes — with no recorded counts there's no
    count-based rate to compute at all."""
    role = (role or "bat").lower()
    if role not in ("bat", "pit"):
        raise HTTPException(status_code=400, detail="role must be 'bat' or 'pit'")
    asked = [s for s in (season, season_start, season_end) if s is not None]
    if asked and max(asked) < _PLAYS_FLOOR:
        raise HTTPException(status_code=400,
                            detail=f"before the {_PLAYS_FLOOR} play-by-play data floor")
    uses_count = balls is not None or strikes is not None
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    cands = [c for c in _resolve_retro_id(player, role) if c["retro_id"]]
    if not cands:
        raise HTTPException(status_code=404,
                            detail=f"No player with a retro_id matching '{player}'")
    if len({c["retro_id"] for c in cands}) > 1:
        return {"resolved": False, "ambiguous": True, "query": player, "candidates": cands[:25]}
    resolved = cands[0]; retro = resolved["retro_id"]
    id_col = "PIT_ID" if role == "pit" else "BAT_ID"
    sclauses, sparams, meta = _situ_clauses(balls, strikes, outs, inning, base_state,
                                            season, season_start, season_end, game_type)
    clause = " AND ".join([f"{id_col} = ?"] + sclauses)
    params = [retro] + sparams
    try:
        cur = _plays_cursor()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        t0 = time.perf_counter()
        row = cur.execute(f"SELECT {_rate_components_sql()} FROM plays WHERE {clause}",
                          params).fetchone()
        rates = _derive_rates(dict(zip(_RATE_COLS, row)))
        lo, hi, single = _rate_span(cur, clause, params, season, season_start, season_end)
        game_coverage, count_data = _coverage_gates(cur, lo, hi, uses_count, single)
        query_ms = round((time.perf_counter() - t0) * 1000, 1)
    finally:
        cur.close()
    declined = count_data is not None and count_data.get("available") is False
    result = {
        "resolved": True, "source": "plays_rates",
        "player": {"query": player, "name": resolved["name"],
                   "mlbam_id": resolved["mlbam_id"], "retro_id": retro, "role": role},
        "filters": meta,
        "rates": None if declined else rates,
        "game_coverage": game_coverage, "query_ms": query_ms,
    }
    if uses_count:
        result["count_data"] = count_data
    return result


# split dimension -> (column, value-labeler). GROUP BY that column, rate each group.
_SPLIT_DIMS = {
    "pitcher_hand": ("PIT_HAND_CD",
                     lambda v: f"vs {'LHP' if v == 'L' else 'RHP' if v == 'R' else v or '?'}"),
    "batter_side":  ("BAT_HAND_CD", lambda v: f"batting {v or '?'}"),
    "home_away":    ("BAT_HOME_ID", lambda v: "home" if v == 1 else "away"),
    "season":       ("SEASON", lambda v: str(v)),
    "game_type":    ("GAME_TYPE",
                     lambda v: {"R": "regular season", "P": "postseason",
                                "A": "all-star"}.get(v, v)),
}


def _run_splits(player, split_by, role="bat", balls=None, strikes=None, outs=None,
                inning=None, base_state=None, season=None, season_start=None,
                season_end=None, game_type=None):
    """Rate line broken out BY a dimension (pitcher_hand / batter_side / home_away
    / season / game_type) — a table of {split_value, PA, AB, H, AVG, OBP, SLG, OPS}."""
    role = (role or "bat").lower()
    if role not in ("bat", "pit"):
        raise HTTPException(status_code=400, detail="role must be 'bat' or 'pit'")
    dim = _SPLIT_DIMS.get((split_by or "").strip().lower())
    if dim is None:
        raise HTTPException(status_code=400,
                            detail=f"unknown split_by; options: {sorted(_SPLIT_DIMS)}")
    col, labeler = dim
    asked = [s for s in (season, season_start, season_end) if s is not None]
    if asked and max(asked) < _PLAYS_FLOOR:
        raise HTTPException(status_code=400,
                            detail=f"before the {_PLAYS_FLOOR} play-by-play data floor")
    uses_count = balls is not None or strikes is not None
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    cands = [c for c in _resolve_retro_id(player, role) if c["retro_id"]]
    if not cands:
        raise HTTPException(status_code=404,
                            detail=f"No player with a retro_id matching '{player}'")
    if len({c["retro_id"] for c in cands}) > 1:
        return {"resolved": False, "ambiguous": True, "query": player, "candidates": cands[:25]}
    resolved = cands[0]; retro = resolved["retro_id"]
    id_col = "PIT_ID" if role == "pit" else "BAT_ID"
    sclauses, sparams, meta = _situ_clauses(balls, strikes, outs, inning, base_state,
                                            season, season_start, season_end, game_type)
    clause = " AND ".join([f"{id_col} = ?"] + sclauses)
    params = [retro] + sparams
    try:
        cur = _plays_cursor()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        t0 = time.perf_counter()
        rows = cur.execute(
            f"SELECT {col} AS sv, {_rate_components_sql()} FROM plays WHERE {clause} "
            f"GROUP BY {col} ORDER BY {col}", params).fetchall()
        lo, hi, single = _rate_span(cur, clause, params, season, season_start, season_end)
        game_coverage, count_data = _coverage_gates(cur, lo, hi, uses_count, single)
        query_ms = round((time.perf_counter() - t0) * 1000, 1)
    finally:
        cur.close()
    declined = count_data is not None and count_data.get("available") is False
    splits = []
    if not declined:
        for r in rows:
            line = _derive_rates(dict(zip(_RATE_COLS, r[1:])))
            splits.append({"split_value": labeler(r[0]), "raw": r[0], **line})
    result = {"resolved": True, "source": "plays_splits", "split_by": split_by,
              "player": {"query": player, "name": resolved["name"],
                         "mlbam_id": resolved["mlbam_id"], "retro_id": retro, "role": role},
              "filters": meta, "splits": splits,
              "game_coverage": game_coverage, "query_ms": query_ms}
    if uses_count:
        result["count_data"] = count_data
    return result


# Situational rates need a minimum-opportunity QUALIFIER: without it, "best average
# with the bases loaded" returns a 2-for-2 fluke. Default 50 PA in the situation —
# MLB's batting-title rule (502 PA) is for full seasons; situational samples are far
# smaller (a regular gets ~50-150 bases-loaded PA in a whole CAREER), so 50 keeps
# the board populated while excluding tiny-sample flukes. Always stated; overridable
# (min_pa=0 = include everyone). For broad season/career rate boards, pass a higher min_pa.
_RATE_LB_DEFAULT_MIN_PA = 50


def _run_rate_leaderboard(stat="OPS", role="bat", balls=None, strikes=None, outs=None,
                          inning=None, base_state=None, season=None, season_start=None,
                          season_end=None, game_type=None, min_pa=None, limit=10):
    stat = (stat or "OPS").upper()
    if stat not in _RATE_STATS:
        raise HTTPException(status_code=400, detail=f"stat must be one of {_RATE_STATS}")
    role = (role or "bat").lower()
    if role not in ("bat", "pit"):
        raise HTTPException(status_code=400, detail="role must be 'bat' or 'pit'")
    asked = [s for s in (season, season_start, season_end) if s is not None]
    if asked and max(asked) < _PLAYS_FLOOR:
        raise HTTPException(status_code=400,
                            detail=f"before the {_PLAYS_FLOOR} play-by-play data floor")
    if min_pa is None:
        min_pa = _RATE_LB_DEFAULT_MIN_PA
    min_pa = max(0, int(min_pa))
    limit = max(1, min(int(limit or 10), 25))
    uses_count = balls is not None or strikes is not None
    scoped = bool(asked)

    id_col = "PIT_ID" if role == "pit" else "BAT_ID"
    sclauses, sparams, meta = _situ_clauses(balls, strikes, outs, inning, base_state,
                                            season, season_start, season_end, game_type)
    clause = " AND ".join([f"{id_col} IS NOT NULL", f"{id_col} <> ''"] + sclauses)
    params = list(sparams)
    order = {"AVG": "avg", "OBP": "obp", "SLG": "slg", "OPS": "ops"}[stat]

    try:
        cur = _plays_cursor()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        t0 = time.perf_counter()
        mm = cur.execute(f"SELECT min(SEASON), max(SEASON) FROM plays WHERE {clause}",
                         params).fetchone()
        base = {"resolved": True, "source": "plays_rate_leaderboard", "stat": stat,
                "min_pa": min_pa, "filters": meta, "limit": limit}
        if not mm or mm[0] is None:
            return {**base, "leaders": [], "game_coverage": {"complete": True},
                    "count_data": None, "query_ms": round((time.perf_counter() - t0) * 1000, 1)}
        lo, hi = mm
        # rates leaderboards: game-coverage caveats (forgiving), count-data HARD
        game_coverage, count_data = _coverage_gates(cur, lo, hi, uses_count, lo == hi)
        if uses_count and count_data is not None and count_data.get("available") is False:
            return {**base, "leaders": None, "declined": True,
                    "reason": count_data.get("note"), "game_coverage": game_coverage,
                    "count_data": count_data, "query_ms": round((time.perf_counter() - t0) * 1000, 1)}
        rows = cur.execute(
            f"WITH c AS (SELECT {id_col} AS pid, {_rate_components_sql()} "
            f"FROM plays WHERE {clause} GROUP BY {id_col}) "
            "SELECT pid, ab, h, d1, d2, d3, hr, bb, hbp, sf, sh, "
            "(ab+bb+hbp+sf+sh) AS pa, "
            "CASE WHEN ab>0 THEN h::DOUBLE/ab END AS avg, "
            "CASE WHEN (ab+bb+hbp+sf)>0 THEN (h+bb+hbp)::DOUBLE/(ab+bb+hbp+sf) END AS obp, "
            "CASE WHEN ab>0 THEN (d1+2*d2+3*d3+4*hr)::DOUBLE/ab END AS slg, "
            "CASE WHEN ab>0 AND (ab+bb+hbp+sf)>0 "
            "THEN (h+bb+hbp)::DOUBLE/(ab+bb+hbp+sf) + (d1+2*d2+3*d3+4*hr)::DOUBLE/ab END AS ops "
            f"FROM c WHERE (ab+bb+hbp+sf+sh) >= ? AND {order} IS NOT NULL "
            f"ORDER BY {order} DESC, pa DESC, pid LIMIT ?",
            params + [min_pa, limit]).fetchall()
        query_ms = round((time.perf_counter() - t0) * 1000, 1)
    finally:
        cur.close()

    retro_ids = [r[0] for r in rows]
    name_map: dict = {}
    if retro_ids and connection.db_available():
        ptable = "pitchers" if role == "pit" else "players"
        try:
            with connection.get_session() as db:
                for rid, pid, nm in db.execute(_sa_text(
                        f"SELECT retro_id, player_id, name FROM {ptable} "
                        "WHERE retro_id = ANY(:ids)"), {"ids": retro_ids}).fetchall():
                    name_map[rid] = {"mlbam_id": pid, "name": nm}
        except Exception as exc:  # noqa: BLE001
            log.warning("rate leaderboard name resolution failed: %s", exc)
    biofile = _retro_names()
    cols = ["pid", "ab", "h", "d1", "d2", "d3", "hr", "bb", "hbp", "sf", "sh",
            "pa", "avg", "obp", "slg", "ops"]
    leaders = []
    for i, r in enumerate(rows, 1):
        d = dict(zip(cols, r))
        info = name_map.get(d["pid"]) or {}
        leaders.append({
            "rank": i, "player_name": info.get("name") or biofile.get(d["pid"]) or d["pid"],
            "mlbam_id": info.get("mlbam_id"), "retro_id": d["pid"],
            "PA": d["pa"], "AB": d["ab"], "H": d["h"], "HR": d["hr"],
            "AVG": round(d["avg"], 3) if d["avg"] is not None else None,
            "OBP": round(d["obp"], 3) if d["obp"] is not None else None,
            "SLG": round(d["slg"], 3) if d["slg"] is not None else None,
            "OPS": round(d["ops"], 3) if d["ops"] is not None else None,
        })
    return {**base, "leaders": leaders, "game_coverage": game_coverage,
            "count_data": count_data if uses_count else None, "query_ms": query_ms}


@app.get("/plays/situational")
def plays_situational(
    player:       str        = Query(..., description="Player name or MLBAM id"),
    role:         str        = Query("bat", description="'bat' (BAT_ID) or 'pit' (PIT_ID)"),
    event:        str | None = Query(None, description="HR/K/BB/HBP/1B/2B/3B (friendly name)"),
    balls:        int | None = Query(None, ge=0, le=4),
    strikes:      int | None = Query(None, ge=0, le=3),
    outs:         int | None = Query(None, ge=0, le=2),
    inning:       int | None = Query(None, ge=1),
    base_state:   str | None = Query(None, description="'risp' or 'loaded'"),
    season:       int | None = Query(None, description="Exact season"),
    season_start: int | None = Query(None),
    season_end:   int | None = Query(None),
    game_type:    str | None = Query(None, description="R (regular) / P (postseason) / A (allstar)"),
    sample_limit: int        = Query(10, ge=0, le=50),
):
    """READ-ONLY situational query over the play-by-play store. Structured
    filters only — every user value is bound as a DuckDB parameter, so there is
    no SQL-injection surface. Resolves the player to a Retrosheet id via the
    existing bridge (players/pitchers.retro_id), filters on the situation, and
    returns the COUNT + a sample of matching plays with query latency.

    e.g. /plays/situational?player=Max Muncy&event=HR&balls=3&strikes=2
         /plays/situational?player=Pete Alonso&event=HR&balls=3&strikes=2&season=2019 -> 4"""
    return _run_situational(
        player=player, role=role, event=event, balls=balls, strikes=strikes,
        outs=outs, inning=inning, base_state=base_state, season=season,
        season_start=season_start, season_end=season_end, game_type=game_type,
        sample_limit=sample_limit)


@app.get("/plays/leaderboard")
def plays_leaderboard(
    event:        str        = Query(..., description="HR/K/BB/HBP/1B/2B/3B — the outcome to rank by"),
    role:         str        = Query("bat", description="'bat' (BAT_ID) or 'pit' (PIT_ID)"),
    balls:        int | None = Query(None, ge=0, le=4),
    strikes:      int | None = Query(None, ge=0, le=3),
    outs:         int | None = Query(None, ge=0, le=2),
    inning:       int | None = Query(None, ge=1),
    base_state:   str | None = Query(None, description="'risp' or 'loaded'"),
    season:       int | None = Query(None),
    season_start: int | None = Query(None),
    season_end:   int | None = Query(None),
    game_type:    str | None = Query(None, description="R / P / A"),
    limit:        int        = Query(10, ge=1, le=25),
):
    """READ-ONLY situational leaderboard — 'who has the most <event> in
    situation Y'. Same structured, parameter-bound filters as /plays/situational
    (no SQL injection surface), minus the player, plus a GROUP BY + rank. Applies
    the leaderboard coverage gates, which can DECLINE a badly-covered span rather
    than present a distorted ranking (see _run_leaderboard)."""
    return _run_leaderboard(
        event=event, role=role, balls=balls, strikes=strikes, outs=outs,
        inning=inning, base_state=base_state, season=season,
        season_start=season_start, season_end=season_end, game_type=game_type,
        limit=limit)


@app.get("/plays/rates")
def plays_rates(
    player:       str        = Query(..., description="Player name or MLBAM id"),
    role:         str        = Query("bat"),
    balls:        int | None = Query(None, ge=0, le=3),
    strikes:      int | None = Query(None, ge=0, le=2),
    outs:         int | None = Query(None, ge=0, le=2),
    inning:       int | None = Query(None, ge=1),
    base_state:   str | None = Query(None, description="'risp' or 'loaded'"),
    season:       int | None = Query(None),
    season_start: int | None = Query(None),
    season_end:   int | None = Query(None),
    game_type:    str | None = Query(None),
):
    """READ-ONLY situational rate line (AVG/OBP/SLG/OPS + components) for a player."""
    return _run_rates(player=player, role=role, balls=balls, strikes=strikes, outs=outs,
                      inning=inning, base_state=base_state, season=season,
                      season_start=season_start, season_end=season_end, game_type=game_type)


@app.get("/plays/splits")
def plays_splits(
    player:       str        = Query(..., description="Player name or MLBAM id"),
    split_by:     str        = Query(..., description="pitcher_hand/batter_side/home_away/season/game_type"),
    role:         str        = Query("bat"),
    balls:        int | None = Query(None, ge=0, le=3),
    strikes:      int | None = Query(None, ge=0, le=2),
    outs:         int | None = Query(None, ge=0, le=2),
    inning:       int | None = Query(None, ge=1),
    base_state:   str | None = Query(None),
    season:       int | None = Query(None),
    season_start: int | None = Query(None),
    season_end:   int | None = Query(None),
    game_type:    str | None = Query(None),
):
    """READ-ONLY rate line broken out BY a dimension."""
    return _run_splits(player=player, split_by=split_by, role=role, balls=balls,
                       strikes=strikes, outs=outs, inning=inning, base_state=base_state,
                       season=season, season_start=season_start, season_end=season_end,
                       game_type=game_type)


@app.get("/plays/rate-leaderboard")
def plays_rate_leaderboard(
    stat:         str        = Query("OPS", description="AVG/OBP/SLG/OPS"),
    role:         str        = Query("bat"),
    balls:        int | None = Query(None, ge=0, le=3),
    strikes:      int | None = Query(None, ge=0, le=2),
    outs:         int | None = Query(None, ge=0, le=2),
    inning:       int | None = Query(None, ge=1),
    base_state:   str | None = Query(None),
    season:       int | None = Query(None),
    season_start: int | None = Query(None),
    season_end:   int | None = Query(None),
    game_type:    str | None = Query(None),
    min_pa:       int | None = Query(None, description="qualifier; default 50, 0 = include everyone"),
    limit:        int        = Query(10, ge=1, le=25),
):
    """READ-ONLY rate leaderboard, ranked by `stat`, qualified by `min_pa`."""
    return _run_rate_leaderboard(stat=stat, role=role, balls=balls, strikes=strikes,
                                 outs=outs, inning=inning, base_state=base_state,
                                 season=season, season_start=season_start,
                                 season_end=season_end, game_type=game_type,
                                 min_pa=min_pa, limit=limit)


# --- Phase 6 Stage 1: NLP /ask ----------------------------------------------
# Cheap/fast model — translating a question into structured params is a simple
# extraction task, so Haiku is plenty.
_ASK_MODEL = "claude-haiku-4-5-20251001"

# Appended to every /ask phrasing prompt so the model returns clean prose the
# app can show verbatim. The iOS client also renders markdown defensively, but
# the model should not emit any — literal ** / # leaking into the UI is exactly
# the failure this prevents.
_PLAIN_TEXT_RULE = (
    " Respond in PLAIN TEXT only. Do NOT use markdown: no asterisks, no hash "
    "headers, no bold, no bullet-point syntax. Write natural prose sentences.")

# Tool the model calls to run a situational count. Its input schema mirrors
# _run_situational's params EXACTLY, so whatever the model fills in maps 1:1.
_ASK_QUERY_TOOL = {
    "name": "query_situational",
    "description": (
        "Count how many times ONE specific player produced ONE specific outcome "
        "in a specific situation, from the 1969-2025 play-by-play data. Use this "
        "only for a single-player situational COUNT."),
    "input_schema": {
        "type": "object",
        "properties": {
            "player": {"type": "string", "description":
                       "Player full name (e.g. 'Max Muncy') or numeric MLBAM id. Required."},
            "role": {"type": "string", "enum": ["bat", "pit"], "description":
                     "'bat' if the player is the hitter, 'pit' if the pitcher. Default 'bat'."},
            "event": {"type": "string", "enum": ["HR", "K", "BB", "HBP", "1B", "2B", "3B"],
                      "description": "Outcome: HR=home run, K=strikeout, BB=walk, "
                                     "HBP=hit by pitch, 1B=single, 2B=double, 3B=triple."},
            "balls": {"type": "integer", "minimum": 0, "maximum": 3, "description":
                      "Ball count if specified (a '3-2'/'full count' -> balls 3)."},
            "strikes": {"type": "integer", "minimum": 0, "maximum": 2, "description":
                        "Strike count if specified (a '3-2'/'full count' -> strikes 2)."},
            "outs": {"type": "integer", "minimum": 0, "maximum": 2, "description": "Outs, if specified."},
            "inning": {"type": "integer", "minimum": 1, "description": "Inning number, if specified."},
            "base_state": {"type": "string", "enum": ["risp", "loaded"], "description":
                           "'risp' = runners in scoring position, 'loaded' = bases loaded."},
            "season": {"type": "integer", "description": "A single season, e.g. 2019."},
            "season_start": {"type": "integer", "description": "Start of an inclusive season range."},
            "season_end": {"type": "integer", "description": "End of an inclusive season range."},
            "game_type": {"type": "string", "enum": ["R", "P", "ALL"], "description":
                          "R=regular season (DEFAULT — omit for normal questions), "
                          "P=postseason/playoffs/World Series, ALL=regular season + "
                          "postseason combined. Only set this when the user explicitly asks."},
        },
        "required": ["player"],
    },
}

# Leaderboard tool — 'who has the most ...'. Same situational params, NO player,
# plus a limit. The presence of a player (query_situational) vs its absence
# (query_leaderboard) IS the signal, so the model's tool choice routes cleanly.
_ASK_LEADERBOARD_TOOL = {
    "name": "query_leaderboard",
    "description": (
        "RANK the players with the MOST of an outcome in a situation — for "
        "'who has the most...', 'which player has the most...', 'top N...', "
        "'leaders in...'. No specific player is named; returns a ranked list. "
        "If the question names a specific player, use query_situational instead."),
    "input_schema": {
        "type": "object",
        "properties": {
            "event": {"type": "string", "enum": ["HR", "K", "BB", "HBP", "1B", "2B", "3B"],
                      "description": "Outcome to rank by (required). HR=home run, "
                                     "K=strikeout, BB=walk, HBP=hit by pitch, "
                                     "1B=single, 2B=double, 3B=triple."},
            "role": {"type": "string", "enum": ["bat", "pit"], "description":
                     "'bat' for hitters, 'pit' for pitchers. Default 'bat'."},
            "balls": {"type": "integer", "minimum": 0, "maximum": 3},
            "strikes": {"type": "integer", "minimum": 0, "maximum": 2},
            "outs": {"type": "integer", "minimum": 0, "maximum": 2},
            "inning": {"type": "integer", "minimum": 1},
            "base_state": {"type": "string", "enum": ["risp", "loaded"], "description":
                           "'risp' = runners in scoring position, 'loaded' = bases loaded."},
            "season": {"type": "integer"},
            "season_start": {"type": "integer"},
            "season_end": {"type": "integer"},
            "game_type": {"type": "string", "enum": ["R", "P", "ALL"], "description":
                          "R=regular season (DEFAULT — omit for normal questions), "
                          "P=postseason, ALL=both. Only set when the user asks."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10,
                      "description": "How many players to return; default 10. Do NOT "
                                     "set this to 1 just because the question says "
                                     "'the most' — a leaderboard wants the ranked list."},
        },
        "required": ["event"],
    },
}

# --- Phase 6: rate stats + splits -------------------------------------------
_ASK_SITU_PROPS = {   # the shared situational params (reused by the rate tools)
    "role": {"type": "string", "enum": ["bat", "pit"]},
    "balls": {"type": "integer", "minimum": 0, "maximum": 3},
    "strikes": {"type": "integer", "minimum": 0, "maximum": 2},
    "outs": {"type": "integer", "minimum": 0, "maximum": 2},
    "inning": {"type": "integer", "minimum": 1},
    "base_state": {"type": "string", "enum": ["risp", "loaded"]},
    "season": {"type": "integer"}, "season_start": {"type": "integer"},
    "season_end": {"type": "integer"},
    "game_type": {"type": "string", "enum": ["R", "P", "ALL"], "description":
                  "R=regular season (DEFAULT), P=postseason, ALL=both; set only if asked"},
}

_ASK_RATES_TOOL = {
    "name": "query_rates",
    "description": (
        "A player's RATE stats (batting average, on-base %, slugging, OPS) in a "
        "situation — 'what's X's average with RISP', 'how well does X hit on a "
        "full count', 'X's OPS in the postseason'. One player + optional situation."),
    "input_schema": {"type": "object",
        "properties": {"player": {"type": "string", "description": "name or MLBAM id"},
                       **_ASK_SITU_PROPS}, "required": ["player"]},
}

_ASK_SPLITS_TOOL = {
    "name": "query_splits",
    "description": (
        "A player's rate stats broken out BY a dimension — 'X's stats by/against "
        "pitcher hand', 'X's numbers home vs away', 'X year by year'. Returns a "
        "table (e.g. vs LHP / vs RHP)."),
    "input_schema": {"type": "object",
        "properties": {"player": {"type": "string"},
                       "split_by": {"type": "string",
                           "enum": ["pitcher_hand", "batter_side", "home_away",
                                    "season", "game_type"],
                           "description": "the dimension to break out by"},
                       **_ASK_SITU_PROPS},
        "required": ["player", "split_by"]},
}

_ASK_RATE_LB_TOOL = {
    "name": "query_rate_leaderboard",
    "description": (
        "Rank players by a RATE in a situation — 'best batting average with the "
        "bases loaded', 'highest OPS with two strikes', 'who slugs best in the "
        "postseason'. Qualified by a minimum plate-appearance threshold by default "
        "(so a 2-for-2 fluke can't top it); set min_pa=0 only if the user asks to "
        "include everyone regardless of playing time."),
    "input_schema": {"type": "object",
        "properties": {"stat": {"type": "string", "enum": ["AVG", "OBP", "SLG", "OPS"]},
                       "min_pa": {"type": "integer", "minimum": 0,
                           "description": "qualifier; omit for the default, 0 = include everyone"},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                       **_ASK_SITU_PROPS},
        "required": ["stat"]},
}

# The escape hatch — anything the store can't answer must land here, not in a
# forced (and wrong) query_situational call.
_ASK_CANNOT_TOOL = {
    "name": "cannot_answer",
    "description": (
        "Use this when the question is NOT a single-player situational count the "
        "play-by-play store can answer — e.g. leaderboards/comparisons across "
        "players, rate stats or averages, career totals not tied to a situation, "
        "streaks or spans, or pitch type/velocity/location. Do NOT force a "
        "query_situational call; call this instead."),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description":
                       "Brief, user-facing explanation of why this store can't answer it."},
        },
        "required": ["reason"],
    },
}

_ASK_SYSTEM = (
    "You translate a baseball fan's English question into a single structured "
    "query over a play-by-play database by calling exactly one tool.\n\n"
    "The database holds every pitch-level play from 1910 to 2025, regular "
    "season AND postseason. It counts how many times ONE specific named player "
    "had ONE specific outcome (home run, strikeout, walk, hit-by-pitch, single, "
    "double, triple), optionally narrowed by ball/strike count, outs, inning, "
    "base state, season (or season range), and regular-season vs postseason.\n\n"
    "DEFAULT TO ANSWERING. If the question is a COUNT of a specific EVENT by ONE "
    "named PLAYER, call query_situational — even if it spans their whole career. "
    "OMITTING season means a CAREER total, which is a perfectly VALID query, NOT "
    "out of scope. Only add filters the question actually states; leave the rest "
    "out. 'How many X has <player> done' with no year = their career count of X.\n\n"
    "EVENT mapping: home run->HR, strikeout->K, walk->BB, hit by pitch->HBP, "
    "single->1B, double->2B, triple->3B. A 'grand slam' is a home run with the "
    "bases loaded: event=HR, base_state=loaded.\n\n"
    "BASE STATE has exactly two values: 'loaded' = bases loaded (all three bases "
    "occupied — use for 'grand slam', 'bases loaded', 'bases full'); 'risp' = "
    "runners in scoring position, i.e. a runner on 2nd or 3rd (use for 'RISP', "
    "'runners in scoring position', 'runner on second or third').\n\n"
    "COUNT: a 'full count' or '3-2 count' means balls=3, strikes=2. If the "
    "player is described as the pitcher ('strikeouts thrown by', 'batters walked "
    "by', 'K's <pitcher> got'), set role=pit; otherwise role=bat.\n\n"
    "GAME TYPE: career and season stats mean REGULAR SEASON. Do NOT set game_type "
    "for a normal question — the default is regular season. Set game_type='P' "
    "ONLY when the user explicitly asks about the postseason / playoffs / World "
    "Series ('Ohtani's postseason HRs' -> game_type:'P'). Set game_type='ALL' "
    "ONLY when they explicitly ask to INCLUDE the postseason ('regular season and "
    "playoffs combined', 'including the postseason').\n\n"
    "IN-SCOPE EXAMPLES (call query_situational):\n"
    "- 'How many grand slams has Aaron Judge hit?' -> {player:'Aaron Judge', "
    "event:'HR', base_state:'loaded'}  (career — no season)\n"
    "- 'How many home runs does Max Muncy have on a 3-2 count?' -> "
    "{player:'Max Muncy', event:'HR', balls:3, strikes:2}\n"
    "- 'How many strikeouts did Kershaw get on full counts?' -> "
    "{player:'Kershaw', role:'pit', event:'K', balls:3, strikes:2}\n"
    "- 'How many HRs did Trout hit with runners in scoring position in 2019?' -> "
    "{player:'Trout', event:'HR', base_state:'risp', season:2019}\n"
    "- 'How many walks did Soto draw with two outs?' -> {player:'Soto', "
    "event:'BB', outs:2}\n"
    "- 'How many home runs has Ohtani hit in the postseason?' -> "
    "{player:'Ohtani', event:'HR', game_type:'P'}\n\n"
    "LEADERBOARDS (call query_leaderboard — NO player named): 'who has the "
    "most...', 'which player/pitcher has the most...', 'top N...', 'leaders "
    "in...'. Only `event` is required; add a situation only if the question "
    "states one. A leaderboard does NOT require a situation — query_leaderboard "
    "works BOTH ways:\n"
    "  - WITHOUT any situational filter -> ranked from COMPLETE season stats "
    "(career or single-season totals: Bonds' 762 career HRs, most career "
    "strikeouts, most doubles). 'Who has the most career home runs' is a valid "
    "call with just {event:'HR'}. This is IN SCOPE — never decline it.\n"
    "  - WITH a situational filter (count/base state/outs/inning) -> ranked from "
    "play-by-play, with coverage caveats.\n"
    "- 'Who has the most career home runs in MLB history?' -> {event:'HR'}\n"
    "- 'Who has the most doubles in MLB history?' -> {event:'2B'}\n"
    "- 'Which pitcher has the most career strikeouts?' -> {event:'K', role:'pit'}\n"
    "- 'Who hit the most home runs in 2023?' -> {event:'HR', season:2023}\n"
    "- 'Who has the most home runs on a 3-2 count?' -> {event:'HR', balls:3, strikes:2}\n"
    "- 'Who has the most career grand slams?' -> {event:'HR', base_state:'loaded'}\n"
    "- 'Top 5 pitchers by strikeouts on a full count' -> "
    "{event:'K', role:'pit', balls:3, strikes:2, limit:5}\n"
    "Named player -> query_situational; no named player -> query_leaderboard.\n"
    "LIMIT: do NOT set limit to 1 just because the question is phrased singularly "
    "('who has the MOST X'). Leaderboard questions want the ranked LIST — default "
    "to 10 so the user sees the leader AND the players behind them. Use a smaller "
    "limit only when explicitly asked ('top 3', 'the single leader').\n\n"
    "RATE STATS (batting average / on-base / slugging / OPS — 'how well does X "
    "hit'):\n"
    "- One player's rate in a situation -> query_rates. "
    "'What's Judge's average with RISP?' -> {player:'Judge', base_state:'risp'}. "
    "'Ohtani's OPS in the postseason' -> {player:'Ohtani', game_type:'P'}.\n"
    "- A player's rates broken out BY a dimension -> query_splits (split_by = "
    "pitcher_hand / batter_side / home_away / season / game_type). 'Judge's stats "
    "by pitcher hand' -> {player:'Judge', split_by:'pitcher_hand'}. 'Betts home vs "
    "away' -> {player:'Betts', split_by:'home_away'}.\n"
    "- Rank players by a rate -> query_rate_leaderboard. 'Best average with the "
    "bases loaded' -> {stat:'AVG', base_state:'loaded'}. 'Highest OPS with two "
    "strikes' -> {stat:'OPS', strikes:2}. Set min_pa=0 ONLY if the user says to "
    "include everyone regardless of playing time.\n\n"
    "OUT OF SCOPE (call cannot_answer) — only when the QUERY SHAPE can't do it:\n"
    "- ERA / WHIP / pitcher rate stats and other computed rates we don't support "
    "(we do AVG/OBP/SLG/OPS for hitters).\n"
    "- Streaks or spans ('in a row', 'consecutive', 'hitting streak').\n"
    "- Pitch type, velocity, exit velocity, or launch angle (not in Retrosheet).\n"
    "- Anything requiring data before 1910 (the play-by-play starts at 1910).\n\n"
    "Call exactly one tool."
)


# ---- /ask cost controls ----------------------------------------------------
# Per-identity + global daily caps (env-tunable, no deploy needed). Rate-limit
# counters live in memory: single-worker app (see live_service), and a reset on
# deploy is acceptable. The translation cache + question log live in Postgres
# (survive deploys, queryable). We cache the TRANSLATION (question -> params),
# never the answer — the LLM parse is the ~half-cent cost; the DuckDB query is
# ~25ms and free, so we re-run it every time and the data is always FRESH.
_ASK_DAILY_LIMIT        = int(os.getenv("ASK_DAILY_LIMIT", "30"))
_ASK_GLOBAL_DAILY_LIMIT = int(os.getenv("ASK_GLOBAL_DAILY_LIMIT", "5000"))
_ASK_HASH_SALT          = os.getenv("ASK_HASH_SALT", "baseball-ask")
_ask_counts: dict = {}   # (day, id_hash) -> count
_ask_global: dict = {}   # day -> count
_ask_rl_lock = threading.Lock()
_ask_log_write_failed = False   # so a broken log surfaces LOUDLY once (not per-request spam)

# Anthropic prompt caching: mark the fixed ~2.5k-token prefix (all tool schemas +
# system prompt) cacheable, so repeat translate calls within the 5-min TTL read
# it at ~10% of input price instead of resending it. Breakpoints on the last
# tool (caches the whole tools array) and the system block.
_ASK_TOOLS = [_ASK_QUERY_TOOL, _ASK_LEADERBOARD_TOOL, _ASK_RATES_TOOL,
              _ASK_SPLITS_TOOL, _ASK_RATE_LB_TOOL,
              {**_ASK_CANNOT_TOOL, "cache_control": {"type": "ephemeral"}}]
_ASK_SYSTEM_BLOCKS = [{"type": "text", "text": _ASK_SYSTEM,
                       "cache_control": {"type": "ephemeral"}}]


def _hash_identity(identity):
    import hashlib
    return hashlib.sha256((_ASK_HASH_SALT + "|" + (identity or "?")).encode()).hexdigest()[:32]


def _normalize_question(qtext):
    """Cache key: lowercase, drop the trailing '?', strip apostrophes (so
    'Judge's' == 'Judges'), replace other punctuation with a space, collapse
    whitespace. Conservative on purpose — we'd rather miss a near-duplicate than
    collide two different questions into one cached (and possibly wrong) parse."""
    import re
    s = (qtext or "").lower().strip().rstrip("?").strip().replace("'", "")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _ask_rate_check(id_hash):
    """(allowed, message). Increments the per-identity and global daily counters.
    Cache hits still count — they still cost the phrasing call and still enable
    abuse. Returns a friendly 429 message when a cap is hit."""
    day = datetime.date.today().isoformat()
    with _ask_rl_lock:
        for k in [k for k in _ask_counts if k[0] != day]:
            del _ask_counts[k]
        for k in [k for k in _ask_global if k != day]:
            del _ask_global[k]
        g = _ask_global.get(day, 0)
        if g >= _ASK_GLOBAL_DAILY_LIMIT:
            log.warning("ASK GLOBAL DAILY CAP HIT (%d) — refusing further questions today",
                        _ASK_GLOBAL_DAILY_LIMIT)
            return False, ("The service has reached today's overall question limit. "
                           "Please try again tomorrow.")
        c = _ask_counts.get((day, id_hash), 0)
        if c >= _ASK_DAILY_LIMIT:
            return False, (f"You've reached today's limit of {_ASK_DAILY_LIMIT} "
                           "questions — try again tomorrow.")
        _ask_counts[(day, id_hash)] = c + 1
        _ask_global[day] = g + 1
    return True, None


def _ask_cache_get(norm):
    """Most recent successful translation of this normalized question, or None."""
    if not connection.db_available():
        return None
    try:
        with connection.get_session() as db:
            row = db.execute(_sa_text(
                "SELECT tool_name, understood_as FROM ask_log "
                "WHERE normalized = :n AND tool_name IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"), {"n": norm}).fetchone()
        if row and row[0]:
            return row[0], (json.loads(row[1]) if row[1] else {})
    except Exception as exc:  # noqa: BLE001
        log.warning("ask cache lookup failed: %s", exc)
    return None


def _ask_log_write(q, norm, tool_name, tool_input, base, cached, id_hash,
                   in_tok, out_tok, timing):
    """Append one row per question (log + cache source). Never fails the request."""
    if not connection.db_available():
        return
    status = ("ambiguous" if base.get("ambiguous") else
              "declined" if base.get("declined") else
              "out_of_scope" if base.get("out_of_scope") else
              "error" if base.get("error") else "ok")
    try:
        with connection.get_session() as db:
            db.add(AskLog(
                created_at=datetime.datetime.utcnow(), question=q, normalized=norm,
                tool_name=tool_name,
                understood_as=json.dumps(tool_input) if tool_input is not None else None,
                source=base.get("source"), status=status,
                answer=(base.get("answer") or "")[:2000], cached=cached,
                identity_hash=id_hash, input_tokens=in_tok, output_tokens=out_tok,
                timing_ms=json.dumps(timing)))
    except Exception as exc:  # noqa: BLE001 - never fail /ask; but surface a BROKEN log loudly, once
        global _ask_log_write_failed
        if not _ask_log_write_failed:
            _ask_log_write_failed = True
            log.warning("ask log write FAILED (first occurrence — the question log "
                        "is not recording; further failures suppressed): %s", exc,
                        exc_info=True)


@app.post("/ask")
def ask(request: Request,
        question: str = Body(..., embed=True,
                             description="A plain-English baseball question"),
        x_device_id: str | None = Header(default=None,
                             description="stable per-install id (iOS identifierForVendor); "
                                         "falls back to client IP for rate limiting")):
    """Phase 6 Stage 1: English question -> Claude tool-use (structured params)
    -> the situational play-by-play query -> a natural-language answer. Scoped
    to single-player situational COUNT questions; anything else comes back as
    out_of_scope. Surfaces the extracted params (`understood_as`) and the
    matched player for transparency, and ambiguous names as candidates."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(status_code=503,
                            detail="ANTHROPIC_API_KEY is not configured")
    q = (question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="question is required")

    # ---- rate limit (per-identity + global daily) -----------------------
    identity = x_device_id or (request.client.host if request.client else "?")
    id_hash = _hash_identity(identity)
    allowed, why = _ask_rate_check(id_hash)
    if not allowed:
        raise HTTPException(status_code=429, detail=why)

    norm = _normalize_question(q)
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    timing: dict = {}
    t_all = time.perf_counter()
    in_tok = out_tok = 0

    # ---- 1. translate — serve the cached parse if we have one -----------
    cached = False
    tool_name, tool_input = None, None
    cached_tr = _ask_cache_get(norm)
    if cached_tr:
        tool_name, tool_input = cached_tr           # skip the LLM; query still re-runs (fresh data)
        cached = True
        timing["llm_translate"] = 0.0
    else:
        t0 = time.perf_counter()
        try:
            msg = client.messages.create(
                model=_ASK_MODEL, max_tokens=1024, system=_ASK_SYSTEM_BLOCKS,
                tools=_ASK_TOOLS, tool_choice={"type": "any"},   # cached prefix + force a tool
                messages=[{"role": "user", "content": q}],
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"LLM translate failed: {exc}")
        timing["llm_translate"] = round((time.perf_counter() - t0) * 1000, 1)
        u = getattr(msg, "usage", None)
        if u:
            in_tok = ((getattr(u, "input_tokens", 0) or 0)
                      + (getattr(u, "cache_read_input_tokens", 0) or 0)
                      + (getattr(u, "cache_creation_input_tokens", 0) or 0))
            out_tok = getattr(u, "output_tokens", 0) or 0
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                tool_name = block.name
                tool_input = dict(block.input or {})
                break

    _ANSWERABLE = ("query_situational", "query_leaderboard", "query_rates",
                   "query_splits", "query_rate_leaderboard")
    base = {
        "question":        q,
        "understood_as":   tool_input if tool_name in _ANSWERABLE else None,
        "answer":          None,
        "count":           None,
        "sample":          [],
        "leaders":         None,
        "rates":           None,
        "splits":          None,
        "player_resolved": None,
        "source":          None,
        "game_coverage":   None,
        "count_data":      None,
        "ambiguous":       False,
        "declined":        False,
        "out_of_scope":    False,
        "reason":          None,
        "error":           None,
        "cached":          cached,
        "timing_ms":       timing,
    }

    def _finish():
        timing["total"] = round((time.perf_counter() - t_all) * 1000, 1)
        _ask_log_write(q, norm, tool_name, tool_input, base, cached, id_hash,
                       in_tok, out_tok, timing)
        return base

    def _phrase(system, facts, max_tokens=400):
        """Run the phrasing model; return its text or None on failure."""
        t0 = time.perf_counter()
        out = None
        try:
            m = client.messages.create(
                model=_ASK_MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user",
                           "content": "Question: " + q + "\nData: " + json.dumps(facts)}])
            out = "".join(getattr(b, "text", "") for b in m.content
                          if getattr(b, "type", None) == "text").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("ask phrasing failed: %s", exc)
        timing["llm_phrase"] = round((time.perf_counter() - t0) * 1000, 1)
        return out or None

    # ---- out of scope / no usable tool call -----------------------------
    if tool_name not in _ANSWERABLE:
        reason = ((tool_input or {}).get("reason") if tool_name == "cannot_answer"
                  else "The question could not be interpreted as a situational count.")
        base["out_of_scope"] = True
        base["reason"] = reason
        base["answer"] = reason
        return _finish()

    # ==== LEADERBOARD branch ('who has the most ...') ====================
    if tool_name == "query_leaderboard":
        lb_params = {k: tool_input.get(k) for k in (
            "event", "role", "balls", "strikes", "outs", "inning", "base_state",
            "season", "season_start", "season_end", "game_type", "limit")
            if tool_input.get(k) is not None}
        asked = [lb_params[k] for k in ("season", "season_start", "season_end")
                 if lb_params.get(k) is not None]
        if asked and max(asked) < _PLAYS_FLOOR:
            base["out_of_scope"] = True
            base["reason"] = (f"Play-by-play data starts in {_PLAYS_FLOOR}; there is "
                              f"no pitch-level data for {max(asked)}.")
            base["answer"] = base["reason"]
            return _finish()
        t0 = time.perf_counter()
        try:
            # plain (no situation) -> complete season-stats leaderboard; else plays
            lb_situ = any(lb_params.get(k) is not None
                          for k in ("balls", "strikes", "outs", "inning", "base_state"))
            result = None
            if not lb_situ:
                result = _run_season_leaderboard(
                    event=lb_params.get("event"), role=lb_params.get("role"),
                    season=lb_params.get("season"),
                    season_start=lb_params.get("season_start"),
                    season_end=lb_params.get("season_end"),
                    game_type=lb_params.get("game_type"),
                    limit=lb_params.get("limit", 10))
            if result is None:
                result = _run_leaderboard(**lb_params)
        except HTTPException as exc:
            timing["query"] = round((time.perf_counter() - t0) * 1000, 1)
            base["reason"] = str(exc.detail)
            base["answer"] = f"Couldn't answer that: {exc.detail}"
            return _finish()
        except Exception as exc:  # noqa: BLE001
            timing["query"] = round((time.perf_counter() - t0) * 1000, 1)
            log.exception("ask leaderboard failed for %r", q)
            base["error"] = str(exc)
            base["reason"] = "query failed"
            base["answer"] = ("Sorry — something went wrong looking that up. "
                              "Try rephrasing the question.")
            return _finish()
        timing["query"] = round((time.perf_counter() - t0) * 1000, 1)

        base["source"] = result.get("source")
        base["game_coverage"] = result.get("game_coverage")
        base["count_data"] = result.get("count_data")
        if result.get("declined"):
            base["declined"] = True
            base["reason"] = result.get("reason")
            base["answer"] = result.get("reason")
            return _finish()
        base["leaders"] = result.get("leaders", [])

        # phrase the ranked answer (factual; honest about coverage)
        t0 = time.perf_counter()
        top = [{"rank": l["rank"], "player": l["player_name"], "count": l["count"]}
               for l in (base["leaders"] or [])]
        facts = {"question": q, "leaders": top, "filters": result.get("filters"),
                 "source": result.get("source"),
                 "game_coverage": result.get("game_coverage"),
                 "count_data": result.get("count_data")}
        answer = None
        try:
            phrased = client.messages.create(
                model=_ASK_MODEL, max_tokens=400,
                system=(
                    "You present a baseball LEADERBOARD from the given ranked data. "
                    "Lead with the leader and their count, then name just the next "
                    "few behind them — e.g. 'Barry Bonds leads with 762 career home "
                    "runs, ahead of Hank Aaron (755) and Babe Ruth (714).' Do NOT "
                    "enumerate every row; the full ranked list is shown separately. "
                    "Use ONLY the given rows; invent nothing. If "
                    "game_coverage.complete is false, state its note (coverage may "
                    "distort the ranking). If count_data has a note, include it. "
                    "No editorializing." + _PLAIN_TEXT_RULE),
                messages=[{"role": "user",
                           "content": "Question: " + q + "\nData: " + json.dumps(facts)}],
            )
            answer = "".join(getattr(b, "text", "") for b in phrased.content
                             if getattr(b, "type", None) == "text").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("ask leaderboard phrasing failed: %s", exc)
        timing["llm_phrase"] = round((time.perf_counter() - t0) * 1000, 1)
        if answer:
            base["answer"] = answer
        elif top:
            base["answer"] = "; ".join(f'{t["rank"]}. {t["player"]} ({t["count"]})' for t in top)
        else:
            base["answer"] = "No players matched that situation."
        return _finish()

    # ==== RATE tools (query_rates / query_splits / query_rate_leaderboard) ====
    if tool_name in ("query_rates", "query_splits", "query_rate_leaderboard"):
        situ_keys = ("role", "balls", "strikes", "outs", "inning", "base_state",
                     "season", "season_start", "season_end", "game_type")
        t0 = time.perf_counter()
        try:
            if tool_name == "query_rates":
                kw = {k: tool_input.get(k) for k in ("player",) + situ_keys
                      if tool_input.get(k) is not None}
                if not kw.get("player"):
                    base["out_of_scope"] = True; base["reason"] = "No player identified."
                    base["answer"] = base["reason"]; return _finish()
                result = _run_rates(**kw)
            elif tool_name == "query_splits":
                kw = {k: tool_input.get(k) for k in ("player", "split_by") + situ_keys
                      if tool_input.get(k) is not None}
                if not kw.get("player") or not kw.get("split_by"):
                    base["out_of_scope"] = True
                    base["reason"] = "A split needs a player and a split_by dimension."
                    base["answer"] = base["reason"]; return _finish()
                result = _run_splits(**kw)
            else:  # query_rate_leaderboard
                kw = {k: tool_input.get(k) for k in ("stat", "min_pa", "limit") + situ_keys
                      if tool_input.get(k) is not None}
                result = _run_rate_leaderboard(**kw)
        except HTTPException as exc:
            timing["query"] = round((time.perf_counter() - t0) * 1000, 1)
            base["reason"] = str(exc.detail)
            base["answer"] = f"Couldn't answer that: {exc.detail}"
            return _finish()
        except Exception as exc:  # noqa: BLE001
            timing["query"] = round((time.perf_counter() - t0) * 1000, 1)
            log.exception("ask rate tool failed for %r", q)
            base["error"] = str(exc); base["reason"] = "query failed"
            base["answer"] = ("Sorry — something went wrong looking that up. "
                              "Try rephrasing the question.")
            return _finish()
        timing["query"] = round((time.perf_counter() - t0) * 1000, 1)

        if result.get("ambiguous"):
            cands = result.get("candidates", [])
            names = ", ".join(f'{c["name"]} (id {c["mlbam_id"]})' for c in cands[:5])
            base["ambiguous"] = True
            base["player_resolved"] = {"candidates": cands}
            base["answer"] = (f'There are multiple players matching '
                              f'"{kw.get("player")}": {names}. Which did you mean?')
            return _finish()

        base["source"] = result.get("source")
        base["game_coverage"] = result.get("game_coverage")
        base["count_data"] = result.get("count_data")
        base["player_resolved"] = result.get("player")
        rate_rules = (
            "You state baseball rate stats (AVG/OBP/SLG/OPS) factually from the "
            "given data. Format rates as .311 (three decimals). Use ONLY the given "
            "numbers. If rates is null or count_data.available is false, say the "
            "pitch-count data wasn't recorded for that situation — do NOT invent. "
            "If game_coverage.complete is false, include its note (a rate from "
            "partial coverage is a fair estimate, so present it but note the "
            "coverage). No editorializing." + _PLAIN_TEXT_RULE)

        if tool_name == "query_rates":
            base["rates"] = result.get("rates")
            facts = {"question": q, "player": result["player"]["name"],
                     "filters": result.get("filters"), "rates": result.get("rates"),
                     "game_coverage": result.get("game_coverage"),
                     "count_data": result.get("count_data")}
            ans = _phrase(rate_rules, facts, 256)
            r = result.get("rates")
            base["answer"] = ans or (
                f'{result["player"]["name"]}: {r.get("AVG")}/{r.get("OBP")}/{r.get("SLG")} '
                f'({r.get("PA")} PA)' if r else
                (result.get("count_data") or {}).get("note") or "No data for that situation.")

        elif tool_name == "query_splits":
            base["splits"] = result.get("splits")
            facts = {"question": q, "player": result["player"]["name"],
                     "split_by": result.get("split_by"),
                     "splits": [{k: s.get(k) for k in ("split_value", "PA", "AVG", "OBP", "SLG", "OPS")}
                                for s in (result.get("splits") or [])],
                     "game_coverage": result.get("game_coverage"),
                     "count_data": result.get("count_data")}
            ans = _phrase(
                rate_rules + " The split numbers are shown in a table, so do NOT "
                "list them line by line. Instead give ONE or TWO sentences of "
                "insight comparing the splits — how the player fares across the "
                "dimension and any notable gap (e.g. 'Aaron Judge hits lefties and "
                "righties almost identically, with a bit more power against "
                "left-handers.').", facts, 400)
            sp = result.get("splits") or []
            base["answer"] = ans or (
                "; ".join(f'{s["split_value"]}: {s.get("AVG")}/{s.get("OBP")}/{s.get("SLG")}'
                          for s in sp) if sp else "No data for that split.")

        else:  # query_rate_leaderboard
            base["leaders"] = result.get("leaders")
            base["stat"] = result.get("stat"); base["min_pa"] = result.get("min_pa")
            if result.get("declined"):
                base["declined"] = True; base["reason"] = result.get("reason")
                base["answer"] = result.get("reason"); return _finish()
            stat = result.get("stat"); mp = result.get("min_pa")
            top = [{"rank": l["rank"], "player": l["player_name"],
                    "value": l.get(stat), "PA": l.get("PA")}
                   for l in (result.get("leaders") or [])]
            facts = {"question": q, "stat": stat, "min_pa": mp, "leaders": top,
                     "filters": result.get("filters"),
                     "game_coverage": result.get("game_coverage"),
                     "count_data": result.get("count_data")}
            ans = _phrase(
                "You present a baseball RATE leaderboard. Lead with the leader and "
                f"their {stat} (three decimals), then name just a few behind them; "
                "the full ranked list is shown separately, so do NOT enumerate "
                f"every row. ALWAYS state the qualifier: 'minimum {mp} plate "
                "appearances in the situation'. Use ONLY the given rows. If "
                "game_coverage.complete is false, include its note. No "
                "editorializing." + _PLAIN_TEXT_RULE, facts, 400)
            base["answer"] = ans or (
                f"(min. {mp} PA) " + "; ".join(
                    f'{t["rank"]}. {t["player"]} {t["value"]}' for t in top)
                if top else "No qualified players for that situation.")
        return _finish()

    # ---- 2. execute the situational query -------------------------------
    params = {k: tool_input.get(k) for k in (
        "player", "role", "event", "balls", "strikes", "outs", "inning",
        "base_state", "season", "season_start", "season_end", "game_type")
        if tool_input.get(k) is not None}
    if not params.get("player"):
        base["out_of_scope"] = True
        base["reason"] = "No player identified in the question."
        base["answer"] = base["reason"]
        return _finish()

    # ---- GATE 0: routing predicate (deterministic, not LLM judgment) ----
    # A SITUATIONAL SPLIT iff any of these is present; else a PLAIN TOTAL.
    situ_keys = ("balls", "strikes", "outs", "inning", "base_state")
    is_split = any(params.get(k) is not None for k in situ_keys)

    # GATE 3 (floor) for the plays route: whole queried range before 1910.
    asked = [params[k] for k in ("season", "season_start", "season_end")
             if params.get(k) is not None]
    if is_split and asked and max(asked) < _PLAYS_FLOOR:
        base["out_of_scope"] = True
        base["reason"] = (f"Play-by-play data starts in {_PLAYS_FLOOR}; there is no "
                          f"pitch-level data for {max(asked)}.")
        base["answer"] = base["reason"]
        return _finish()

    t0 = time.perf_counter()
    try:
        if is_split:
            result = _run_situational(**params)                 # -> plays store + gates
        else:
            result = _run_season_total(                         # -> complete season stats
                player=params.get("player"), role=params.get("role"),
                event=params.get("event"), season=params.get("season"),
                season_start=params.get("season_start"),
                season_end=params.get("season_end"),
                game_type=params.get("game_type"))
            if result is None:  # season-stats can't express it (e.g. HBP) -> plays
                result = _run_situational(**params)
    except HTTPException as exc:
        timing["query"] = round((time.perf_counter() - t0) * 1000, 1)
        base["reason"] = str(exc.detail)
        base["answer"] = f"Couldn't answer that: {exc.detail}"
        return _finish()
    except Exception as exc:  # noqa: BLE001 - a user-facing endpoint must never 500
        # Any unexpected failure (bad SQL, a data edge case) returns a clean,
        # honest response instead of "Internal Server Error". We do NOT silently
        # fall back to the plays store here: for a plain total that would risk
        # the very short-count answer this whole design exists to prevent.
        timing["query"] = round((time.perf_counter() - t0) * 1000, 1)
        log.exception("ask query failed for %r", q)
        base["error"] = str(exc)
        base["reason"] = "query failed"
        base["answer"] = ("Sorry — something went wrong looking that up. "
                          "Try rephrasing the question.")
        return _finish()
    timing["query"] = round((time.perf_counter() - t0) * 1000, 1)

    # ambiguous name -> surface candidates instead of guessing
    if result.get("ambiguous"):
        cands = result.get("candidates", [])
        names = ", ".join(f'{c["name"]} (id {c["mlbam_id"]})' for c in cands[:5])
        base["ambiguous"] = True
        base["player_resolved"] = {"candidates": cands}
        base["answer"] = (f'There are multiple players matching '
                          f'"{params["player"]}": {names}. Which did you mean?')
        return _finish()

    base["count"]          = result.get("count")
    base["sample"]         = result.get("sample", [])
    base["player_resolved"] = result.get("player")
    base["source"]         = result.get("source")
    base["game_coverage"]  = result.get("game_coverage")
    base["count_data"]     = result.get("count_data")

    # ---- 3. phrase the answer (factual; honest about the two gates) -----
    t0 = time.perf_counter()
    facts = {"question": q, "player": result["player"]["name"],
             "count": result.get("count"), "filters": result.get("filters"),
             "source": result.get("source"),
             "game_coverage": result.get("game_coverage"),
             "count_data": result.get("count_data")}
    answer = None
    try:
        phrased = client.messages.create(
            model=_ASK_MODEL, max_tokens=256,
            system=(
                "You state a single baseball statistic as one short, factual "
                "sentence from the given data. RULES: (1) If count is null OR "
                "count_data.available is false, DO NOT invent a number — say "
                "plainly that the pitch-count data wasn't recorded for that era "
                "(use count_data.note). (2) If game_coverage.complete is false, "
                "work its note into the sentence naturally (e.g. 'Based on the "
                "94% of 1927 games with play-by-play data, ...'). (3) If "
                "count_data has a note and a number is given, include the note. "
                "Never present a partial or no-data count as a definitive total. "
                "No editorializing." + _PLAIN_TEXT_RULE),
            messages=[{"role": "user",
                       "content": "Question: " + q + "\nData: " + json.dumps(facts)}],
        )
        answer = "".join(getattr(b, "text", "") for b in phrased.content
                         if getattr(b, "type", None) == "text").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("ask phrasing failed: %s", exc)
    timing["llm_phrase"] = round((time.perf_counter() - t0) * 1000, 1)
    # deterministic fallback if phrasing failed
    if answer:
        base["answer"] = answer
    elif result.get("count") is None and (result.get("count_data") or {}).get("note"):
        base["answer"] = result["count_data"]["note"]
    else:
        base["answer"] = f'{result["player"]["name"]}: {result.get("count")}.'
    return _finish()


@app.get("/admin/plays-status")
def plays_status():
    """Cheap readiness probe for the plays store: is the file present, is the
    shared connection open, did the background warmup finish, and how long it
    took. No queries — safe to poll."""
    return {
        "path":            PLAYS_DB_PATH,
        "file_exists":     os.path.exists(PLAYS_DB_PATH),
        "connection_open": _plays_conn is not None,
        "warm":            _plays_warm,
        "warmup_secs":     _plays_warm_secs,
        "error":           _plays_conn_error,
    }


@app.get("/admin/plays-diag")
def plays_diag(
    bat_id:  str = Query("alonp001", description="test batter retro id (Alonso)"),
    pit_id:  str = Query("kersc001", description="test pitcher retro id (Kershaw)"),
    season:  int = Query(2019),
    read_mb: int = Query(200, ge=1, le=700, description="MB to sequentially read for throughput"),
):
    """TEMPORARY read-only latency diagnosis for the plays store. Splits
    connect-time vs query-time, lists persisted indexes, measures raw volume
    read throughput, and compares a real full scan vs indexed lookups — with a
    warm re-run to separate cold volume I/O from CPU. Reads only; writes
    nothing. (Remove after diagnosis.)"""
    import duckdb
    rep = {"path": PLAYS_DB_PATH, "exists": os.path.exists(PLAYS_DB_PATH),
           "file_size_mb": None, "steps": {}, "error": None}
    if not rep["exists"]:
        rep["error"] = "file missing — run /admin/fetch-plays-db"
        return rep
    rep["file_size_mb"] = round(os.path.getsize(PLAYS_DB_PATH) / 1024 / 1024, 1)

    def timed(fn):
        # NB: must evaluate fn() BEFORE measuring elapsed — a tuple literal
        # like (elapsed, fn(), None) would compute elapsed first (always ~0).
        t = time.perf_counter()
        try:
            v = fn()
            return round((time.perf_counter() - t) * 1000, 1), v, None
        except Exception as exc:  # noqa: BLE001
            return round((time.perf_counter() - t) * 1000, 1), None, str(exc)

    # --- 1. raw volume read throughput -----------------------------------
    try:
        target = read_mb * 1024 * 1024
        chunk = 8 * 1024 * 1024
        read = 0
        t = time.perf_counter()
        with open(PLAYS_DB_PATH, "rb") as fh:
            while read < target:
                b = fh.read(min(chunk, target - read))
                if not b:
                    break
                read += len(b)
        secs = time.perf_counter() - t
        rep["steps"]["raw_read"] = {
            "bytes_read": read, "secs": round(secs, 3),
            "MB_per_s": round((read / 1024 / 1024) / secs, 1) if secs > 0 else None,
        }
    except Exception as exc:  # noqa: BLE001
        rep["steps"]["raw_read"] = {"error": str(exc)}

    # --- 2. connect time (cold = first open, warm = second) --------------
    ms1, con, err = timed(lambda: duckdb.connect(PLAYS_DB_PATH, read_only=True))
    rep["steps"]["connect_cold_ms"] = ms1
    if err:
        rep["error"] = "connect: " + err
        return rep
    ms2, con2, _ = timed(lambda: duckdb.connect(PLAYS_DB_PATH, read_only=True))
    rep["steps"]["connect_warm_ms"] = ms2
    if con2:
        con2.close()

    try:
        # --- 3. indexes present in the uploaded file? --------------------
        try:
            idx = con.execute(
                "SELECT index_name, table_name, expressions "
                "FROM duckdb_indexes()").fetchall()
            rep["steps"]["indexes"] = [
                {"name": i[0], "table": i[1], "expr": i[2]} for i in idx]
        except Exception as exc:  # noqa: BLE001
            rep["steps"]["indexes"] = {"error": str(exc)}

        # --- 4. query timings on the persistent connection ---------------
        # count(*) answers from metadata (NOT a real scan) — baseline.
        ms, v, e = timed(lambda: con.execute(
            "SELECT count(*) FROM plays").fetchone()[0])
        rep["steps"]["count_star"] = {"ms": ms, "value": v, "error": e,
                                      "note": "metadata, not a scan"}
        # real full scan: non-indexed predicate forces a column scan of 10M rows.
        ms, v, e = timed(lambda: con.execute(
            "SELECT count(*) FROM plays WHERE OUTS_CT = 1").fetchone()[0])
        rep["steps"]["full_scan"] = {"ms": ms, "value": v, "error": e,
                                     "note": "OUTS_CT=1, non-indexed → true scan"}
        # season-filtered (the fast "Alonso 2019" shape) — uses ix_seas/ix_bat.
        ms, v, e = timed(lambda: con.execute(
            "SELECT count(*) FROM plays WHERE BAT_ID=? AND EVENT_CD=23 "
            "AND BALLS_CT=3 AND STRIKES_CT=2 AND SEASON=?",
            [bat_id, season]).fetchone()[0])
        rep["steps"]["query_season_filtered"] = {
            "ms": ms, "value": v, "error": e, "shape": "BAT_ID+SEASON"}
        # career all-seasons (the slow "Kershaw career" shape) — uses ix_pit.
        ms, v, e = timed(lambda: con.execute(
            "SELECT count(*) FROM plays WHERE PIT_ID=? AND EVENT_CD=3",
            [pit_id]).fetchone()[0])
        rep["steps"]["query_career_cold"] = {
            "ms": ms, "value": v, "error": e, "shape": "PIT_ID all seasons"}
        # SAME query again — warm cache. If cold≫warm, it's cold volume I/O,
        # not compute; if cold≈warm, the cost is CPU/plan, not the volume.
        ms, v, e = timed(lambda: con.execute(
            "SELECT count(*) FROM plays WHERE PIT_ID=? AND EVENT_CD=3",
            [pit_id]).fetchone()[0])
        rep["steps"]["query_career_warm"] = {"ms": ms, "value": v, "error": e}
    finally:
        con.close()
    return rep


_CHADWICK_BRIDGE_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "retrosheet", "chadwick_retro_bridge.csv")


@app.post("/admin/backfill-missing-bios")
def backfill_missing_bios(
    confirm: bool = Query(False),
    limit: int | None = Query(None, description="cap inserts for a partial run; omit = all"),
):
    """Backfill bio rows for players that have SEASON STATS but NO row in
    players/pitchers — the stats-without-identity gap (e.g. CC Sabathia:
    unsearchable, nameless profile) left by the Lahman loader silently dropping
    divergent-id players. INSERT-ONLY: never modifies an existing bio. Name from
    the shipped retro_names biofile (100% coverage), retro/bbref ids from the
    Chadwick bridge, debut/last season derived from the season rows themselves
    (no People.csv needed — it isn't in the deploy). confirm=false = DRY RUN
    (counts + top 20 by career volume; no writes)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    # mlbam -> (retro_id, bbref_id) from the shipped Chadwick bridge
    m2r: dict[int, tuple] = {}
    try:
        with open(_CHADWICK_BRIDGE_CSV, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                mm = row.get("key_mlbam")
                if mm:
                    m2r[int(mm)] = (row.get("key_retro") or None, row.get("key_bbref") or None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"bridge load failed: {exc}")
    names = _retro_names()    # retro_id -> name (Retrosheet biofile)
    mnames = _mlbam_names()   # mlbam -> name (Chadwick register; names Negro Leagues players)

    # season players with NO bio row, + career span + a volume proxy
    with connection.get_session() as db:
        bat = db.execute(_sa_text(
            'SELECT ps.player_id, MIN(ps.year), MAX(ps.year), COALESCE(SUM(ps."PA"),0) '
            "FROM player_seasons ps LEFT JOIN players p ON p.player_id = ps.player_id "
            "WHERE p.player_id IS NULL GROUP BY ps.player_id")).fetchall()
        pit = db.execute(_sa_text(
            'SELECT qs.player_id, MIN(qs.year), MAX(qs.year), COALESCE(SUM(qs."IP"),0) '
            "FROM pitcher_seasons qs LEFT JOIN pitchers p ON p.player_id = qs.player_id "
            "WHERE p.player_id IS NULL GROUP BY qs.player_id")).fetchall()

    plan: dict[int, dict] = {}
    for pid, mn, mx, pa in bat:
        e = plan.setdefault(pid, {"in_bat": False, "in_pit": False, "pa": 0.0, "ip": 0.0})
        e.update(in_bat=True, debut=mn, last=mx, pa=float(pa or 0))
    for pid, mn, mx, ip in pit:
        e = plan.setdefault(pid, {"in_bat": False, "in_pit": False, "pa": 0.0, "ip": 0.0})
        e["in_pit"] = True
        e["debut"] = min(e.get("debut", mn), mn)
        e["last"] = max(e.get("last", mx), mx)
        e["ip"] = float(ip or 0)

    items = []
    for mlbam, e in plan.items():
        retro, bbref = m2r.get(mlbam, (None, None))
        # name: Retrosheet biofile (by retro_id) -> Chadwick register (by mlbam)
        name = (names.get(retro) if retro else None) or mnames.get(mlbam)
        items.append({
            "mlbam": mlbam, "retro_id": retro, "bbref_id": bbref, "name": name,
            "in_bat": e["in_bat"], "in_pit": e["in_pit"],
            "mlb_debut": e.get("debut"), "mlb_last_season": e.get("last"),
            "volume": round(e["pa"] + e["ip"] * 4.3),   # PA + rough batters-faced
        })
    items.sort(key=lambda x: x["volume"], reverse=True)
    unnamed = [i for i in items if not i["name"]]

    def _role(i):
        return "bat+pit" if i["in_bat"] and i["in_pit"] else ("pit" if i["in_pit"] else "bat")

    # ---- DRY RUN --------------------------------------------------------
    if not confirm:
        # for any residual unnamed, pull their teams so we can see exactly who
        teams: dict = {}
        if unnamed:
            ids = [i["mlbam"] for i in unnamed]
            with connection.get_session() as db:
                for tbl in ("player_seasons", "pitcher_seasons"):
                    for pid, tm in db.execute(_sa_text(
                            f"SELECT player_id, string_agg(DISTINCT team, ',') "
                            f"FROM {tbl} WHERE player_id = ANY(:ids) GROUP BY player_id"),
                            {"ids": ids}).fetchall():
                        teams[pid] = (teams.get(pid, "") + "," + (tm or "")).strip(",")
        return {
            "dry_run": True,
            "missing_total": len(items),
            "resolvable_names": len(items) - len(unnamed),
            "unnamed": len(unnamed),
            "unnamed_detail": [{
                "mlbam": i["mlbam"],
                "seasons": f'{i["mlb_debut"]}-{i["mlb_last_season"]}',
                "role": _role(i), "teams": teams.get(i["mlbam"]),
            } for i in unnamed],
            "top_20": [{
                "name": i["name"], "mlbam": i["mlbam"], "retro_id": i["retro_id"],
                "role": _role(i), "seasons": f'{i["mlb_debut"]}-{i["mlb_last_season"]}',
                "volume": i["volume"],
            } for i in items[:20]],
        }

    # ---- INSERT (confirm=true) — INSERT-ONLY, never touch existing ------
    to_do = items[:limit] if limit else items
    ins_p = ins_pit = skipped_no_name = 0
    with connection.get_session() as db:
        for i in to_do:
            if not i["name"]:
                # LOUD: never silently insert a nameless bio — skip and log it
                skipped_no_name += 1
                log.warning("backfill: no name for mlbam %s (%s-%s) — skipped",
                            i["mlbam"], i["mlb_debut"], i["mlb_last_season"])
                continue
            info = {"player_id": i["mlbam"], "name": i["name"],
                    "retro_id": i["retro_id"], "bbref_id": i["bbref_id"],
                    "mlb_debut": i["mlb_debut"], "mlb_last_season": i["mlb_last_season"]}
            if i["in_bat"] and db.get(Player, i["mlbam"]) is None:
                crud.save_player(db, info)
                ins_p += 1
            if i["in_pit"] and db.get(Pitcher, i["mlbam"]) is None:
                crud.save_pitcher(db, {**info, "position": "P"})
                ins_pit += 1
    return {"confirmed": True, "inserted_players": ins_p,
            "inserted_pitchers": ins_pit, "skipped_no_name": skipped_no_name,
            "considered": len(to_do)}


@app.get("/admin/duplicate-identities")
def duplicate_identities(limit: int = Query(1000, ge=1, le=5000)):
    """READ-ONLY. Enumerate the Negro-Leagues id-churn fingerprint: two mlbam
    rows sharing a NORMALIZED name (case/punctuation/accent-insensitive) with
    OVERLAPPING or ADJACENT eras, where one row carries bbref_id/retro_id (the
    Chadwick-canonical bio) and the other does NOT (the orphan our season stats
    key on). No writes — diagnosis only.

    Confidence: HIGH = same-or-null position + overlapping/adjacent era + a
    multi-token (distinctive) name; LOW = single-token surname, conflicting
    position, or non-overlapping eras (flagged for manual review — a shared
    surname is NOT assumed to be the same person)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    import re
    import unicodedata

    def norm(s):
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    with connection.get_session() as db:
        rows = db.execute(_sa_text(
            "SELECT player_id, name, bbref_id, retro_id, birth_year, death_year, "
            "position, mlb_debut, mlb_last_season FROM players "
            "UNION ALL "
            "SELECT player_id, name, bbref_id, retro_id, birth_year, death_year, "
            "position, mlb_debut, mlb_last_season FROM pitchers")).fetchall()

    P: dict = {}
    for pid, name, bb, rt, by, dy, pos, deb, last in rows:
        e = P.setdefault(pid, {"name": name, "bbref": None, "retro": None,
                               "birth": None, "death": None, "pos": None,
                               "debut": None, "last": None})
        e["name"] = e["name"] or name
        e["bbref"] = e["bbref"] or bb
        e["retro"] = e["retro"] or rt
        e["birth"] = e["birth"] or by
        e["death"] = e["death"] or dy
        e["pos"] = e["pos"] or pos
        if deb is not None:
            e["debut"] = deb if e["debut"] is None else min(e["debut"], deb)
        if last is not None:
            e["last"] = last if e["last"] is None else max(e["last"], last)

    # group by normalized name; keep groups that have BOTH an id-bearing row and
    # an id-less row (the churn split)
    from collections import defaultdict
    groups = defaultdict(list)
    for pid, e in P.items():
        groups[norm(e["name"])].append(pid)
    cand_ids: set = set()
    cand_groups = []
    for nm, ids in groups.items():
        if not nm or len(ids) < 2:
            continue
        withid = [i for i in ids if P[i]["bbref"] or P[i]["retro"]]
        without = [i for i in ids if not (P[i]["bbref"] or P[i]["retro"])]
        if withid and without:
            cand_groups.append((nm, withid, without))
            cand_ids.update(ids)

    # season span/count/teams for just the candidate ids
    span: dict = {}
    if cand_ids:
        ids_list = list(cand_ids)
        with connection.get_session() as db:
            for tbl in ("player_seasons", "pitcher_seasons"):
                for pid, mn, mx, n, tm in db.execute(_sa_text(
                        f"SELECT player_id, MIN(year), MAX(year), COUNT(*), "
                        f"string_agg(DISTINCT team, ',') FROM {tbl} "
                        "WHERE player_id = ANY(:ids) GROUP BY player_id"),
                        {"ids": ids_list}).fetchall():
                    s = span.setdefault(pid, [mn, mx, 0, set()])
                    s[0] = min(s[0], mn); s[1] = max(s[1], mx); s[2] += n
                    if tm:
                        s[3].update(tm.split(","))

    def era(pid):
        s = span.get(pid)
        d = P[pid]["debut"] if P[pid]["debut"] is not None else (s[0] if s else None)
        l = P[pid]["last"] if P[pid]["last"] is not None else (s[1] if s else None)
        return d, l

    def detail(pid):
        s = span.get(pid)
        d, l = era(pid)
        return {"mlbam": pid, "name": P[pid]["name"],
                "bbref_id": P[pid]["bbref"], "retro_id": P[pid]["retro"],
                "has_bio": bool(P[pid]["birth"] or P[pid]["death"]),
                "birth_year": P[pid]["birth"], "death_year": P[pid]["death"],
                "position": P[pid]["pos"], "era": f"{d}-{l}" if d else None,
                "seasons": s[2] if s else 0,
                "teams": ",".join(sorted(s[3])) if s else None}

    pairs = []
    for nm, withid, without in cand_groups:
        single_token = len(nm.split()) < 2
        for o in without:
            do, lo = era(o)
            for c in withid:
                dc, lc = era(c)
                overlap = None
                if None not in (do, lo, dc, lc):
                    overlap = not (lo < dc - 2 or lc < do - 2)   # overlap or within 2 yrs
                po, pc = P[o]["pos"], P[c]["pos"]
                pos_conflict = bool(po and pc and po != pc)
                reasons = []
                conf = "high"
                if single_token:
                    conf = "low"; reasons.append("single-token name (common surname)")
                if pos_conflict:
                    conf = "low"; reasons.append(f"position conflict ({po} vs {pc})")
                if overlap is False:
                    conf = "low"; reasons.append("eras do not overlap")
                if overlap is None:
                    reasons.append("era unknown for one side")
                pairs.append({
                    "name": P[o]["name"], "confidence": conf, "reasons": reasons,
                    "orphan": detail(o), "canonical": detail(c),
                    "both_have_seasons": (span.get(o, [0, 0, 0])[2] > 0
                                          and span.get(c, [0, 0, 0])[2] > 0),
                })

    pairs.sort(key=lambda p: (p["confidence"] != "high",
                              -(p["orphan"]["seasons"] + p["canonical"]["seasons"])))
    hi = [p for p in pairs if p["confidence"] == "high"]
    lo = [p for p in pairs if p["confidence"] == "low"]
    # id-range spread (is churn confined to the Negro Leagues 816k-820k block?)
    def rng(pid):
        return "816k-820k" if 816000 <= pid <= 820999 else "other"
    outside = [p for p in pairs
               if rng(p["orphan"]["mlbam"]) != "816k-820k"
               or rng(p["canonical"]["mlbam"]) != "816k-820k"]
    return {
        "total_pairs": len(pairs),
        "high_confidence": len(hi),
        "low_confidence": len(lo),
        "pairs_outside_negro_leagues_range": len(outside),
        "pairs": pairs[:limit],
    }


@app.get("/admin/question-log")
def question_log(
    limit: int = Query(100, ge=1, le=1000),
    since: str | None = Query(None, description="ISO date/datetime — only rows at/after"),
    status: str | None = Query(None, description="filter: ok/declined/out_of_scope/ambiguous/error"),
):
    """Review the /ask question log (raw + normalized question, extracted params,
    tool/source, status, answer, cache hit, tokens, timings). Read-only."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    def _row(r):
        # tolerant: a bad JSON blob in one row must not sink the whole report
        def _j(s):
            try:
                return json.loads(s) if s else None
            except Exception:  # noqa: BLE001
                return s
        return {
            "id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None,
            "question": r.question, "normalized": r.normalized,
            "tool_name": r.tool_name, "understood_as": _j(r.understood_as),
            "source": r.source, "status": r.status, "answer": r.answer,
            "cached": r.cached, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "timing_ms": _j(r.timing_ms),
        }

    # A diagnostic must never 500: materialize the rows INSIDE the session
    # (objects detach + expire on commit), and hand any failure back cleanly.
    try:
        with connection.get_session() as db:
            query = db.query(AskLog).order_by(AskLog.id.desc())
            if since:
                query = query.filter(AskLog.created_at >= since)
            if status:
                query = query.filter(AskLog.status == status)
            entries = [_row(r) for r in query.limit(limit).all()]
            total = db.query(AskLog).count()
            cached_n = db.query(AskLog).filter(AskLog.cached.is_(True)).count()
    except Exception as exc:  # noqa: BLE001
        log.exception("question-log query failed")
        return {"error": str(exc), "total": None, "entries": []}

    return {"total": total, "cache_hits": cached_n,
            "cache_hit_rate": round(cached_n / total, 3) if total else None,
            "returned": len(entries), "entries": entries}


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
        # Build the Chadwick bbref→mlbam bridge, then supplement it
        # with rows from our own `players` and `pitchers` tables.
        # Manually-added historical players (e.g. R.A. Dickey added
        # via `/admin/add-historical-player`) carry a `bbref_id` on
        # their bio row but typically don't have a Chadwick CSV entry
        # — without the supplement, their award-share votes silently
        # skip at `_load_award_shares`'s `bridge.get(...) is None`
        # check. Only mappings missing from Chadwick are added so the
        # canonical bridge stays the source of truth where it has
        # coverage.
        bridge = lahman_load._load_chadwick_bridge()
        supplemented_from_db = 0
        with connection.get_session() as db:
            for row in (
                db.query(Player.bbref_id, Player.player_id)
                  .filter(Player.bbref_id.isnot(None))
                  .all()
            ):
                if row.bbref_id and row.bbref_id not in bridge:
                    bridge[row.bbref_id] = row.player_id
                    supplemented_from_db += 1
            for row in (
                db.query(Pitcher.bbref_id, Pitcher.player_id)
                  .filter(Pitcher.bbref_id.isnot(None))
                  .all()
            ):
                if row.bbref_id and row.bbref_id not in bridge:
                    bridge[row.bbref_id] = row.player_id
                    supplemented_from_db += 1

        rows_loaded = lahman_load._load_award_shares(bridge)
        duration = round(time.time() - started, 2)
        return {
            "status":               "done",
            "rows_loaded":          rows_loaded,
            "supplemented_from_db": supplemented_from_db,
            "duration_seconds":     duration,
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


@app.post("/admin/compute-heat")
def admin_compute_heat(
    player_id: int | None = Query(
        None,
        description="Optional — compute + return heat for one player (debug, "
                    "both sides if two-way). Omit to run the full active scan.",
    ),
):
    """Compute hot/cold heat and persist it. With no `player_id`, runs the
    full active-player scan via `compute_all_player_heat` and returns the
    count buckets. With `player_id`, computes the rating for whichever bio
    side(s) exist (batting / pitching) and returns the scores."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    current_year = datetime.datetime.utcnow().year

    if player_id is None:
        with connection.get_session() as db:
            counts = data_service.compute_all_player_heat(db, current_year)
        return {"status": "ok", "current_year": current_year, "counts": counts}

    out: dict = {"player_id": player_id, "current_year": current_year, "sides": {}}
    with connection.get_session() as db:
        now = datetime.datetime.utcnow()
        batter = db.get(Player, player_id)
        if batter is not None:
            score, tier, _ = data_service.compute_player_heat(
                db, player_id, is_pitcher=False, current_year=current_year,
            )
            batter.heat_score, batter.heat_tier, batter.heat_updated = score, tier, now
            out["sides"]["batting"] = {"heat_score": score, "heat_tier": tier}
        pitcher = db.get(Pitcher, player_id)
        if pitcher is not None:
            score, tier, role = data_service.compute_player_heat(
                db, player_id, is_pitcher=True, current_year=current_year,
            )
            pitcher.heat_score, pitcher.heat_tier, pitcher.heat_updated = score, tier, now
            pitcher.heat_role = role
            out["sides"]["pitching"] = {
                "heat_score": score, "heat_tier": tier, "heat_role": role,
            }
        if batter is None and pitcher is None:
            raise HTTPException(
                status_code=404,
                detail=f"No bio row in players or pitchers for player_id {player_id}",
            )
        db.commit()
    return out


# ---------------------------------------------------------------------------
# Team news (MLB.com RSS) — manual/cron refresh + read endpoint.
# The refresh runs in a background thread (like the nightly) so the cron POST
# returns immediately; a 20-minute Railway cron drives it.
# ---------------------------------------------------------------------------

# Backgrounded like the nightly. A simple flag + lock prevents overlapping
# 20-minute ticks from walking the 30 feeds twice at once.
_news_refresh_lock = threading.Lock()
_news_refresh_running = False


def _run_news_refresh() -> None:
    """Background worker: run the news ingest, log the summary (the response
    is gone since the endpoint already returned), and always clear the running
    flag — success OR failure — via try/finally."""
    global _news_refresh_running
    try:
        summary = news_service.refresh_news()
        log.info(
            "[news] refresh done: inserted=%s updated=%s pruned=%s failed_feeds=%s",
            summary.get("total_inserted"),
            summary.get("total_updated"),
            summary.get("pruned"),
            summary.get("failed_feeds"),
        )
    except Exception as exc:   # noqa: BLE001 — log + reset, never crash the thread
        log.exception("[news] refresh FAILED: %s", exc)
    finally:
        with _news_refresh_lock:
            _news_refresh_running = False


@app.post("/admin/refresh-news")
def admin_refresh_news():
    """Kick off a team-news ingest (all 30 MLB.com per-team RSS feeds, upserted
    into `news_articles` deduped on url) in a background thread and return
    immediately — mirrors `/admin/nightly-update`. The walk takes ~30-40s, so
    the per-team + totals summary is written to the logs, not the response.

    Returns `{"status": "started"}`, or `{"status": "already_running"}` when a
    refresh is already in flight (so overlapping cron ticks can't double-run)."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    global _news_refresh_running
    # Atomic claim inside the lock so two simultaneous POSTs can't both pass
    # the running check and spawn duplicate threads.
    with _news_refresh_lock:
        if _news_refresh_running:
            log.info("[news] refresh POST rejected — already running")
            return {"status": "already_running"}
        _news_refresh_running = True

    log.info("[news] refresh POST accepted — spawning worker thread")
    t = threading.Thread(target=_run_news_refresh, daemon=True, name="news-refresh")
    t.start()
    return {"status": "started"}


@app.get("/news")
def news(
    team: str | None = Query(
        None,
        description="Optional team_code (Lahman code, e.g. 'CHN'). Omit for "
                    "league-wide newest articles across all teams.",
    ),
    limit: int = Query(20, ge=1, le=50, description="Max articles (1–50)."),
):
    """Newest-first team news. Filters to one `team_code` when `team` is given,
    else returns league-wide newest. Each item carries id, source_name,
    team_code, title, summary, url, image_url, published_at."""
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return {"articles": news_service.get_news(team=team, limit=limit)}


@app.post("/admin/load-people-bio")
def admin_load_people_bio():
    """Targeted backfill: re-read `People.csv` and upsert bio rows for
    every player/pitcher already in the DB, skipping the full Lahman
    re-run. Reuses `lahman_load._load_people_info()` so the bio-field
    mapping (including `death_year/month/day`) stays single-sourced.

    Primarily exists to populate the death-date columns added after the
    initial Lahman import without re-running batting/pitching/fielding/
    awards. `crud.save_player` / `save_pitcher` upsert per-field (never
    overwriting a non-null with null), so this safely fills the new
    columns on existing rows and refreshes other bio fields in place.

    Same bridge-building shape as `/admin/load-award-shares`: the
    Chadwick bbref→mlbam map, supplemented with bbref_ids from our own
    bio tables so manually-added historical players resolve too. The id
    sets are every player_id currently in `players` / `pitchers` (we
    only write People rows for players we actually have).

    Synchronous — one CSV pass plus a batched upsert; no background
    thread. Diagnostic-mode error handling matches the award loaders:
    returns 200 with `status: "error"` + traceback on failure.
    """
    import traceback

    if not connection.db_available():
        return {
            "status":  "error",
            "message": "DATABASE_URL is not configured",
        }

    started = time.time()
    try:
        # Chadwick bbref→mlbam bridge, supplemented from our own bio
        # tables (same reasoning as /admin/load-award-shares — manually-
        # added historical players carry a bbref_id but may have no
        # Chadwick CSV row, and People.csv is keyed on bbref).
        bridge = lahman_load._load_chadwick_bridge()
        supplemented_from_db = 0
        with connection.get_session() as db:
            for row in (
                db.query(Player.bbref_id, Player.player_id)
                  .filter(Player.bbref_id.isnot(None))
                  .all()
            ):
                if row.bbref_id and row.bbref_id not in bridge:
                    bridge[row.bbref_id] = row.player_id
                    supplemented_from_db += 1
            for row in (
                db.query(Pitcher.bbref_id, Pitcher.player_id)
                  .filter(Pitcher.bbref_id.isnot(None))
                  .all()
            ):
                if row.bbref_id and row.bbref_id not in bridge:
                    bridge[row.bbref_id] = row.player_id
                    supplemented_from_db += 1

            # Only write People rows for players we actually have stored.
            batter_ids  = set(crud.get_all_player_ids(db))
            pitcher_ids = set(crud.get_all_pitcher_ids(db))

        # state/lock are Optional on _load_people_info — pass None for the
        # synchronous (no progress-tracking) path.
        players_written, pitchers_written = lahman_load._load_people_info(
            bridge, batter_ids, pitcher_ids, None, None,
        )
        duration = round(time.time() - started, 2)
        return {
            "status":               "done",
            "players_written":      players_written,
            "pitchers_written":     pitchers_written,
            "supplemented_from_db": supplemented_from_db,
            "duration_seconds":     duration,
        }
    except Exception as exc:
        log.exception("load-people-bio failed")
        return {
            "status":     "error",
            "message":    str(exc),
            "error_type": type(exc).__name__,
            "traceback":  traceback.format_exc(),
            "duration_seconds": round(time.time() - started, 2),
        }


@app.post("/admin/load-awards")
def admin_load_awards():
    """Targeted backfill: load just `AwardsPlayers.csv` into
    `player_awards`, skipping the full Lahman re-run. Sibling of
    `/admin/load-award-shares` — the latter handles per-player MVP/
    CY/ROY vote shares; this one handles the actual trophy-win rows
    (Gold Gloves, Silver Sluggers, All-Star MVPs, …).

    Synchronous and quick — the CSV is small. Upserts via crud's
    save_player_awards (the merge path) so re-running is safe.

    Same Chadwick-bridge-plus-DB-supplement pattern as
    `/admin/load-award-shares`: manually-added historical players
    (e.g. via `/admin/add-historical-player`) carry a `bbref_id` on
    their bio row but typically don't appear in the Chadwick CSV;
    supplementing the bridge with bbref_id→player_id rows from
    `players` and `pitchers` lets their trophy-win records load
    instead of silently skipping.

    Diagnostic mode: returns 200 with `status: "error"` + traceback
    in the body when the load fails — same shape as the sibling
    endpoint — so a curl against this endpoint surfaces the root
    cause without needing server log access.
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
        supplemented_from_db = 0
        with connection.get_session() as db:
            for row in (
                db.query(Player.bbref_id, Player.player_id)
                  .filter(Player.bbref_id.isnot(None))
                  .all()
            ):
                if row.bbref_id and row.bbref_id not in bridge:
                    bridge[row.bbref_id] = row.player_id
                    supplemented_from_db += 1
            for row in (
                db.query(Pitcher.bbref_id, Pitcher.player_id)
                  .filter(Pitcher.bbref_id.isnot(None))
                  .all()
            ):
                if row.bbref_id and row.bbref_id not in bridge:
                    bridge[row.bbref_id] = row.player_id
                    supplemented_from_db += 1

        rows_loaded = lahman_load._load_awards(bridge)
        duration = round(time.time() - started, 2)
        return {
            "status":               "done",
            "rows_loaded":          rows_loaded,
            "supplemented_from_db": supplemented_from_db,
            "duration_seconds":     duration,
        }
    except Exception as exc:
        log.exception("load-awards failed")
        return {
            "status":     "error",
            "message":    str(exc),
            "error_type": type(exc).__name__,
            "traceback":  traceback.format_exc(),
            "duration_seconds": round(time.time() - started, 2),
        }


@app.post("/admin/load-series-post")
def admin_load_series_post():
    """Targeted backfill: load `SeriesPost.csv` into the
    `series_post` table without re-running the full Lahman load.

    Series-post is team-keyed (Lahman teamID, not MLBAM), so unlike
    `/admin/load-awards` / `/admin/load-award-shares` there's no
    Chadwick bridge to build — the join against franchise history
    happens at read time via `crud.get_series_post_by_team`.

    Returns 200 with `status: "error"` + traceback on failure (same
    diagnostic pattern as the sibling award-loader endpoints).
    """
    import traceback

    if not connection.db_available():
        return {
            "status":  "error",
            "message": "DATABASE_URL is not configured",
        }

    started = time.time()
    try:
        rows_loaded = lahman_load._load_series_post()
        duration = round(time.time() - started, 2)
        return {
            "status":           "done",
            "rows_loaded":      rows_loaded,
            "duration_seconds": duration,
        }
    except Exception as exc:
        log.exception("load-series-post failed")
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


@app.post("/admin/catchup-update")
def start_catchup_update():
    """Spawn the lightweight catch-up update in a background
    thread. Recommended Railway cron: 21:00 UTC daily — see the
    docstring on `nightly_update.run_catchup_update` for the full
    rationale (BDL catches up late-PT games a few hours after they
    finish, the morning nightly often runs before that).

    Idempotent in the no-work case: if no players from yesterday's
    games are behind, returns `updated=0`. Stale running-flag
    auto-clears after `_CATCHUP_STALE_AFTER` (30 min) so a
    SIGKILL'd thread doesn't block the next day's run.
    """
    if not connection.db_available():
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    with _catchup_lock:
        if _catchup_state["running"]:
            # Stale check — analogous to the nightly's _is_stale_running.
            last_iso = _catchup_state.get("last_started")
            stale = False
            if last_iso:
                try:
                    last_dt = datetime.datetime.fromisoformat(
                        last_iso.replace("Z", "+00:00")
                    )
                    now = datetime.datetime.now(datetime.timezone.utc)
                    if (now - last_dt) > _CATCHUP_STALE_AFTER:
                        stale = True
                except ValueError:
                    pass
            if stale:
                log.warning(
                    f"[catchup] auto-resetting stale lock — "
                    f"last_started={last_iso}, threshold={_CATCHUP_STALE_AFTER}"
                )
                _catchup_state["error"] = (
                    f"auto-reset: previous run claimed at {last_iso} never completed (stale)"
                )
                _catchup_state["running"] = False
            else:
                log.info(f"[catchup] POST rejected — already running: {_catchup_state}")
                return {"status": "already_running", **_catchup_state}

        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        _catchup_state["running"] = True
        _catchup_state["last_started"] = now_iso
        error_val = _catchup_state.get("error") or ""
        if not error_val.startswith("auto-reset:"):
            _catchup_state["error"] = None

    log.info(f"[catchup] POST accepted pid={os.getpid()} started={now_iso} — spawning worker thread")
    t = threading.Thread(target=_run_catchup_update, daemon=True, name="catchup-update")
    t.start()
    return {"status": "started", "last_started": now_iso}


@app.get("/admin/catchup-update/status")
def catchup_update_status():
    with _catchup_lock:
        state = dict(_catchup_state)
    log.info(f"[catchup] GET status pid={os.getpid()}: {state}")
    return state


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
