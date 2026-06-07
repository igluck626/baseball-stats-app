"""Baseball stats API."""

import csv
import datetime
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
from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import (
    func as _sa_func,
    inspect as _sa_inspect,
    or_ as _sa_or,
    text as _sa_text,
)

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
    PlayerSeason, SeriesPost, TeamSeason,
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
