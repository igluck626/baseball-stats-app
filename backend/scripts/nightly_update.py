"""Nightly update — refresh current-season stats for every player in the database.

Designed to run as a Railway cron job (e.g., daily at 04:00 UTC after
Baseball Reference publishes the previous day's data).

Phase 1 (batters): fetches bwar_bat once for the WAR/OPS+ layer, then
per-player BallDontLie season_stats for standard counting + rate stats,
and upserts the current-year row in player_seasons.

Phase 2 (pitchers): fetches bwar_pitch once for the WAR/ERA+ layer, then
per-player BallDontLie season_stats for standard counting + rate stats
(plus FIP and K/9 from BDL), and upserts the current-year row in
pitcher_seasons.

Phase 3 (standings): fetches the MLB Stats API standings endpoint and upserts the
current-season row in team_seasons for each team.

Phase 4 (game logs): for every active roster player (mlb_last_season =
current year), pulls per-game stats from the MLB Stats API and upserts
into batting_gamelogs / pitching_gamelogs. Idempotent — yesterday's games
are the only new rows; older games are upserted as no-ops.

Required env vars (set on the `nightly-update-cron` Railway service in
addition to the `baseball-stats-app` API service):
  • DATABASE_URL — same Postgres the API writes to
  • BDL_KEY      — BallDontLie GOAT-tier API key. Phase 1 and Phase 2
                   loop over every player at the BDL 5 req/sec ceiling,
                   so a missing key fails fast at the first call rather
                   than burning through a 22k-row walk on MLB-Stats-API
                   fallbacks.

Seasonal workflow
-----------------
BDL + bwar are the source of truth for the in-flight current season.
After each season ends (typically late October), the Lahman archive is
re-released with the just-completed year. Re-running lahman_load.py
permanently overwrites the current-season standings rows with
canonical Lahman numbers; the cutoff in lahman_load.py is "current year"
so the rollover is automatic on the next run after Lahman publishes.
"""

import datetime
import gc
import logging
import os
import sys

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

import pybaseball                                     # noqa: E402
from sqlalchemy import func as _sql_func              # noqa: E402

import data_service                                   # noqa: E402
import team_crosswalk                                 # noqa: E402
from database import connection, crud                 # noqa: E402
from database.models import (                          # noqa: E402
    BattingGameLog,
    Pitcher  as _Pitcher,
    PitcherSeason,
    Player   as _Player,
    PlayerSeason,
    TeamSeason,
)

import time as _time                                  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Batting (PlayerSeason)
# ---------------------------------------------------------------------------

def _safe_tb(br) -> int | None:
    """TB = H + 2·doubles + 3·triples + 4·HR. Returns None when H is
    missing on the bref row (and so the formula isn't computable).
    Treats null component fields as 0 so a single null 3B doesn't blow
    away the whole row's TB."""
    h = data_service._safe(br["H"]) if "H" in br.index else None
    if h is None:
        return None
    dbl = data_service._safe(br["2B"]) or 0 if "2B" in br.index else 0
    trp = data_service._safe(br["3B"]) or 0 if "3B" in br.index else 0
    hr  = data_service._safe(br["HR"]) or 0 if "HR" in br.index else 0
    return int(h + dbl + 2 * trp + 3 * hr)



def _build_current_batter_entry(
    player_id: int,
    bdl_id: int | None,
    bwar_current,
    current_year: int,
    bdl_stats: dict | None = None,
    mlb_debut: int | None = None,
) -> dict | None:
    """Build a player_seasons row for the current year. Caller is
    expected to pre-fetch BDL stats in bulk (`_fetch_bdl_batch_stats`)
    and pass the parsed dict in via `bdl_stats`. When the caller
    didn't pre-fetch (single-player paths, ad-hoc /admin calls) the
    function falls back to a per-player fetch — BDL when the row
    has a `bdl_id`, MLB Stats API otherwise.

    Returns None when no source has any data for the player this
    season (off-roster minor leaguer, retired, etc.). `mlb_debut`
    gates the unmapped-row warning — pre-2002 debuts predate BDL's
    coverage window and will never have a bdl_id, so logging them
    every nightly is pure noise. Modern (>=2002) debuts get the
    warning because they SHOULD be mapped."""
    player_war = (
        bwar_current[bwar_current["mlb_ID"] == float(player_id)]
        .sort_values("stint_ID")
    )

    # Caller is expected to pre-fetch in a batch and pass it
    # through. If bdl_stats is None and the row has no bdl_id at
    # all, we skip — there's no MLB-Stats-API fallback anymore.
    # Player keeps their last known DB values until they get
    # mapped via `/admin/build-bdl-player-mapping`.
    if bdl_stats is None and bdl_id is None:
        # Suppress the warning for pre-2002 debuts — those rows
        # predate BDL's data coverage and will never get a bdl_id,
        # so the warning was flooding the nightly log with
        # thousands of useless lines. Modern players still log
        # so a real mapping gap stays visible.
        if mlb_debut is not None and mlb_debut >= 2002:
            log.warning(
                f"  skipping batter {player_id} — no bdl_id mapped, stats not updated"
            )
        return None

    if player_war.empty and bdl_stats is None:
        return None

    entry: dict = {"year": current_year}

    if not player_war.empty:
        group = player_war
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

    if bdl_stats:
        # Standard counting + rate stats from BDL (or MLB Stats API
        # fallback). Iterate so None values don't clobber whatever
        # bwar provided above. PA / CS / IBB / HBP / SF / SH / GIDP
        # are absent from BDL — they'll remain whatever the previous
        # nightly wrote, or NULL on a fresh row.
        for key in ("G", "AB", "R", "H", "doubles", "triples", "HR",
                    "RBI", "BB", "SO", "SB", "TB",
                    "BA", "OBP", "SLG", "OPS",
                    # MLB-Stats-API-only keys (fallback path) — BDL
                    # doesn't ship these, so they only land when
                    # the helper that fetched is the MLB API one.
                    "PA", "CS", "IBB", "HBP", "SF", "SH", "GIDP"):
            value = bdl_stats.get(key)
            if value is not None:
                entry[key] = value
        # WAR fallback — only fill from BDL when bwar didn't.
        if "WAR" not in entry:
            war = bdl_stats.get("WAR")
            if war is not None:
                entry["WAR"] = war

    return entry


_BATCH_SIZE = 100


def _apply_season_pa(db, player_id: int, year: int, entry: dict) -> None:
    """Populate `entry["PA"]` for a batter's current-season row.

    BDL's `season_stats` doesn't carry plate appearances, so we sum
    per-game PA from this player's `batting_gamelogs` for `year` (the
    gamelog parser now stores BDL's `plate_appearances`). If no
    game-log PA is available yet (cold start, or the day's gamelogs
    haven't been ingested), we fall back to deriving PA from the
    season counting components (AB + BB + HBP + SF). Leaves `entry`
    untouched when neither source yields a positive total."""
    pa_total = db.query(_sql_func.sum(BattingGameLog.PA)).filter(
        BattingGameLog.player_id == player_id,
        BattingGameLog.season == year,
        BattingGameLog.PA.isnot(None),
    ).scalar() or 0

    # Fallback: calculate from components if PA not stored.
    if pa_total == 0:
        pa_total = (entry.get("AB") or 0) + (entry.get("BB") or 0) + \
                   (entry.get("HBP") or 0) + (entry.get("SF") or 0)

    if pa_total:
        entry["PA"] = pa_total


def _update_batters(current_year: int) -> tuple[int, int, list[int]]:
    """Phase 1: walk every batter and refresh their current-season
    row. BDL is the per-player stats source (rate-limited to ≈4.5
    req/sec); bwar provides the WAR/OPS+ layer in one bulk fetch.
    Rookie discovery happens later in Phase 5 (active-roster walk)
    — without bref's batting page scrape there's no equivalent
    pre-loop discovery here. Phase 5's coverage is strictly better
    for active players anyway."""
    # Pre-flight the BDL key so we fail fast if the cron service
    # is missing it, before iterating thousands of players and
    # silently falling back to MLB Stats API on every one of them.
    data_service._get_bdl_key()

    log.info("Fetching bwar_bat...")
    bwar_df = data_service._bwar_bat_all()
    # Copy out only the current-year slice (~750 rows). The full
    # bwar_bat archive is hundreds of MB; holding it across a 22k-player
    # iteration was the dominant memory pressure that triggered OOM
    # kills on Railway. .copy() detaches the slice from the parent so
    # the parent can actually be released.
    bwar_current = bwar_df[bwar_df["year_ID"] == current_year].copy()
    del bwar_df
    # data_service caches the parent DataFrame; evict so the GC can
    # actually reclaim it. Cost is one re-fetch on the next request
    # (~30s), worth it for the memory headroom during this run.
    data_service._store.pop("bwar_bat_all", None)
    gc.collect()

    # Pre-load the bdl_id + mlb_debut maps in one query so the
    # per-player loop doesn't issue a SELECT for each row.
    # `mlb_debut` is needed to gate the unmapped-row warning —
    # pre-2002 debuts predate BDL coverage and skip silently.
    with connection.get_session() as db:
        player_ids = crud.get_all_player_ids(db)
        bio_rows = (
            db.query(_Player.player_id, _Player.bdl_id, _Player.mlb_debut)
              .filter(_Player.player_id.in_(player_ids))
              .all()
        )
        bdl_id_map: dict[int, int | None]    = {r.player_id: r.bdl_id    for r in bio_rows}
        debut_map:  dict[int, int | None]    = {r.player_id: r.mlb_debut for r in bio_rows}
    bdl_mapped = sum(1 for v in bdl_id_map.values() if v is not None)
    log.info(
        f"{len(player_ids)} batters in database "
        f"({bdl_mapped} BDL-mapped; batch size: {_BATCH_SIZE})"
    )

    # Single bulk fetch of every BDL-mapped batter's season stats —
    # collapses what used to be ~2,200 individual HTTP calls (each
    # gated by a 0.22s sleep, hence the prior 89-minute runtime)
    # into ~44 batched calls of 50 player_ids each. Unmapped rows
    # still fall through to their per-player MLB Stats API fetch
    # inside the build helper.
    log.info("Pre-fetching BDL batter stats in batches...")
    bdl_ids_to_fetch = sorted({v for v in bdl_id_map.values() if v is not None})
    raw_batch = data_service._fetch_bdl_batch_stats(bdl_ids_to_fetch, current_year)
    bdl_stats_by_bdl_id: dict[int, dict] = {
        bdl_id: data_service._parse_bdl_batter_row(row)
        for bdl_id, row in raw_batch.items()
    }
    log.info(
        f"  BDL batter batch returned stats for {len(bdl_stats_by_bdl_id)}/"
        f"{len(bdl_ids_to_fetch)} mapped players"
    )

    updated = 0
    skipped = 0
    failed: list[int] = []

    # Batch-process: build entries for 100 players, flush them all in
    # one DB session, then drop intermediates and gc.collect before the
    # next batch. Caps peak working-set size regardless of input length.
    for start in range(0, len(player_ids), _BATCH_SIZE):
        batch_ids = player_ids[start:start + _BATCH_SIZE]
        batch_entries: list[tuple[int, dict]] = []

        for player_id in batch_ids:
            try:
                bdl_id = bdl_id_map.get(player_id)
                bdl_stats = bdl_stats_by_bdl_id.get(bdl_id) if bdl_id else None
                entry = _build_current_batter_entry(
                    player_id,
                    bdl_id,
                    bwar_current,
                    current_year,
                    bdl_stats=bdl_stats,
                    mlb_debut=debut_map.get(player_id),
                )
                if entry is None:
                    skipped += 1
                    continue
                batch_entries.append((player_id, entry))
            except Exception as exc:
                log.error(f"batter {player_id} FAILED: {exc}")
                failed.append(player_id)
            # No per-player BDL sleep — the batch pre-fetch already
            # paced the HTTP calls. MLB Stats API fallback (only for
            # unmapped rows) doesn't have a documented rate limit;
            # the small unmapped set won't burst hard enough to
            # warrant throttling.

        if batch_entries:
            with connection.get_session() as db:
                for pid, entry in batch_entries:
                    _apply_season_pa(db, pid, current_year, entry)
                    crud.save_player_seasons(db, pid, [entry])
            updated += len(batch_entries)

        # Free intermediate state before the next batch.
        del batch_entries
        del batch_ids
        gc.collect()

        # Progress logging every 10 batches (~1k players).
        batch_num = start // _BATCH_SIZE + 1
        if batch_num % 10 == 0 or start + _BATCH_SIZE >= len(player_ids):
            log.info(
                f"  batters batch {batch_num}: "
                f"processed={min(start + _BATCH_SIZE, len(player_ids))}/{len(player_ids)}, "
                f"updated={updated}, skipped={skipped}, failed={len(failed)}"
            )

    # Done with this phase's bwar slice — drop it before phase 2 starts
    # accumulating its own dataframes.
    del bwar_current
    gc.collect()

    return updated, skipped, failed


# ---------------------------------------------------------------------------
# Pitching (PitcherSeason)
# ---------------------------------------------------------------------------

def _build_current_pitcher_entry(
    player_id: int,
    bdl_id: int | None,
    bwar_current,
    current_year: int,
    bdl_stats: dict | None = None,
    mlb_debut: int | None = None,
) -> dict | None:
    """Build a pitcher_seasons row for the current year. Same shape
    as the batter builder — caller passes pre-fetched BDL stats
    in via `bdl_stats`. `mlb_debut` gates the unmapped-row warning
    so pre-2002 debuts (outside BDL coverage) skip silently."""
    player_war = (
        bwar_current[bwar_current["mlb_ID"] == float(player_id)]
        .sort_values("stint_ID")
        if "stint_ID" in bwar_current.columns
        else bwar_current[bwar_current["mlb_ID"] == float(player_id)]
    )

    override = bdl_stats
    if override is None and bdl_id is None:
        if mlb_debut is not None and mlb_debut >= 2002:
            log.warning(
                f"  skipping pitcher {player_id} — no bdl_id mapped, stats not updated"
            )
        return None

    if player_war.empty and override is None:
        return None

    # `_build_pitcher_season_entry` was extended to recognize
    # FIP / K_per9 / WAR keys in the override dict — they flow
    # through cleanly from BDL without a separate call path.
    # bref_df is None now that we've dropped the page scrape;
    # the function tolerates a None / empty frame by skipping
    # the bref-fed branch.
    return data_service._build_pitcher_season_entry(
        player_id,
        current_year,
        player_war,
        None,
        mlb_api_stats=override,
    )


def _update_pitchers(current_year: int) -> tuple[int, int, list[int]]:
    """Phase 2: pitcher counterpart to `_update_batters`. BDL is the
    per-pitcher stats source (including FIP and K/9); bwar is the
    WAR / ERA+ bulk-fetch layer. Discovery deferred to Phase 5."""
    # Pre-flight the BDL key — same fail-fast pattern as Phase 1.
    data_service._get_bdl_key()

    log.info("Fetching bwar_pitch...")
    bwar_df = data_service._bwar_pitch_all()
    # Same memory pattern as batters — slice + detach + evict cache.
    bwar_current = bwar_df[bwar_df["year_ID"] == current_year].copy()
    del bwar_df
    data_service._store.pop("bwar_pitch_all", None)
    gc.collect()

    with connection.get_session() as db:
        pitcher_ids = crud.get_all_pitcher_ids(db)
        bio_rows = (
            db.query(_Pitcher.player_id, _Pitcher.bdl_id, _Pitcher.mlb_debut)
              .filter(_Pitcher.player_id.in_(pitcher_ids))
              .all()
        )
        bdl_id_map: dict[int, int | None] = {r.player_id: r.bdl_id    for r in bio_rows}
        debut_map:  dict[int, int | None] = {r.player_id: r.mlb_debut for r in bio_rows}
    bdl_mapped = sum(1 for v in bdl_id_map.values() if v is not None)
    log.info(
        f"{len(pitcher_ids)} pitchers in database "
        f"({bdl_mapped} BDL-mapped; batch size: {_BATCH_SIZE})"
    )

    # Single bulk fetch of every BDL-mapped pitcher — same pattern
    # as the batter phase. ~1,500 pitchers in 50-id chunks = ~30
    # batched calls, ~7 seconds of inter-batch sleep total.
    log.info("Pre-fetching BDL pitcher stats in batches...")
    bdl_ids_to_fetch = sorted({v for v in bdl_id_map.values() if v is not None})
    raw_batch = data_service._fetch_bdl_batch_stats(bdl_ids_to_fetch, current_year)
    bdl_stats_by_bdl_id: dict[int, dict] = {
        bdl_id: data_service._parse_bdl_pitcher_row(row)
        for bdl_id, row in raw_batch.items()
    }
    log.info(
        f"  BDL pitcher batch returned stats for {len(bdl_stats_by_bdl_id)}/"
        f"{len(bdl_ids_to_fetch)} mapped players"
    )

    updated = 0
    skipped = 0
    failed: list[int] = []

    for start in range(0, len(pitcher_ids), _BATCH_SIZE):
        batch_ids = pitcher_ids[start:start + _BATCH_SIZE]
        batch_entries: list[tuple[int, dict]] = []

        for player_id in batch_ids:
            try:
                bdl_id = bdl_id_map.get(player_id)
                bdl_stats = bdl_stats_by_bdl_id.get(bdl_id) if bdl_id else None
                entry = _build_current_pitcher_entry(
                    player_id,
                    bdl_id,
                    bwar_current,
                    current_year,
                    bdl_stats=bdl_stats,
                    mlb_debut=debut_map.get(player_id),
                )
                if entry is None:
                    skipped += 1
                    continue
                batch_entries.append((player_id, entry))
            except Exception as exc:
                log.error(f"pitcher {player_id} FAILED: {exc}")
                failed.append(player_id)
            # No per-player BDL sleep — batch already paced it.

        if batch_entries:
            with connection.get_session() as db:
                for pid, entry in batch_entries:
                    crud.save_pitcher_seasons(db, pid, [entry])
            updated += len(batch_entries)

        del batch_entries
        del batch_ids
        gc.collect()

        batch_num = start // _BATCH_SIZE + 1
        if batch_num % 10 == 0 or start + _BATCH_SIZE >= len(pitcher_ids):
            log.info(
                f"  pitchers batch {batch_num}: "
                f"processed={min(start + _BATCH_SIZE, len(pitcher_ids))}/{len(pitcher_ids)}, "
                f"updated={updated}, skipped={skipped}, failed={len(failed)}"
            )

    del bwar_current
    gc.collect()

    return updated, skipped, failed


# ---------------------------------------------------------------------------
# Standings (TeamSeason)
# ---------------------------------------------------------------------------
# Sourced from the MLB Stats API standings endpoint — see
# `_update_standings` below for the full payload and field map. The
# previous pybaseball-scrape path is gone; it only ever produced
# W/L/win_pct/rank, and the new path covers those plus streak / L10 /
# home / away / run differential / clinch indicators / magic /
# elimination numbers.


def _build_team_meta_by_id() -> dict[str, tuple[str, str]]:
    """{team_id → (franch_id, team_name)} from team_seasons. Most-recent
    year wins so we always pick up the current Lahman code + display
    name (e.g. the Angels' team_id flipped ANA→LAA at some point —
    sorting by year desc surfaces the latest in-use shape).

    Used by the standings refresh to look up franch_id + display name
    from a Lahman team_id. The MLB-numeric-id → Lahman map below is
    the entry point; this provides the rest of the row metadata."""
    mapping: dict[str, tuple[str, str]] = {}
    with connection.get_session() as db:
        rows = (
            db.query(TeamSeason.team_id, TeamSeason.franch_id, TeamSeason.team_name, TeamSeason.year)
              .order_by(TeamSeason.year.desc())
              .all()
        )
        for r in rows:
            if r.team_id and r.team_id not in mapping:
                mapping[r.team_id] = (r.franch_id, r.team_name)
    return mapping


# BDL league/division string → our (league code, division letter)
# tuple. BDL ships "American"/"National" and "East"/"Central"/"West"
# on the standings payload; map to our 2-letter / 1-letter codes
# stored on team_seasons.
_BDL_LEAGUE_TO_CODE: dict[str, str] = {
    "American": "AL", "National": "NL",
    "AL": "AL", "NL": "NL",  # tolerate either spelling
}
_BDL_DIVISION_TO_CODE: dict[str, str] = {
    "East":    "E", "Central": "C", "West":    "W",
}


def _streak_code_from_int(streak: object) -> str | None:
    """BDL ships `streak` as a signed integer (+5 = W5, -3 = L3).
    Our `streak_code` column wants the legacy string form."""
    if streak is None or streak == 0:
        return None
    try:
        n = int(streak)
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return f"W{n}" if n > 0 else f"L{-n}"


def _parse_last_ten(s: object) -> tuple[int | None, int | None]:
    """\"8-2\" → (8, 2). (None, None) on missing / malformed input."""
    if not s or not isinstance(s, str) or "-" not in s:
        return None, None
    parts = s.split("-", 1)
    try:
        return int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None, None


def _update_standings(current_year: int) -> tuple[int, int]:
    """Refresh current-season standings via BDL `/standings?season=N`
    and upsert into team_seasons. Returns (teams_updated,
    lookup_failures). All MLB Stats API calls removed for App
    Store compliance.

    BDL ships everything we need: W/L, win_pct, streak (signed int),
    last_ten_games ("8-2"), home/road wins+losses, points_for/against
    (runs), games_behind, magic_number_division, clincher. The rank
    within a division is derived locally from `division_games_behind`
    since BDL doesn't ship a per-division rank field directly."""
    team_meta = _build_team_meta_by_id()
    if not team_meta:
        log.warning("team_seasons is empty — cannot resolve franch_id / team_name; skipping standings refresh")
        return 0, 0

    try:
        payload = data_service._bdl_get_json(
            "standings", {"season": current_year},
        )
    except Exception as exc:
        log.error(f"standings fetch failed: {exc}", exc_info=True)
        return 0, 0

    rows_by_division: dict[tuple[str, str], list[dict]] = {}
    failed_lookups: list[str] = []

    for t in payload.get("data") or []:
        team_obj  = t.get("team") or {}
        bdl_team_id = team_obj.get("id")
        team_id = data_service._BDL_TO_LAHMAN_TEAM_MAP.get(bdl_team_id)
        if team_id is None:
            failed_lookups.append(team_obj.get("abbreviation") or str(bdl_team_id))
            continue
        meta = team_meta.get(team_id)
        if meta is None:
            failed_lookups.append(team_id)
            continue
        franch_id, full_team_name = meta

        league   = _BDL_LEAGUE_TO_CODE.get(team_obj.get("league") or "")
        division = _BDL_DIVISION_TO_CODE.get(team_obj.get("division") or "")
        if not league or not division:
            failed_lookups.append(team_id)
            continue

        wins   = int(t.get("wins") or 0)
        losses = int(t.get("losses") or 0)
        wp_raw = t.get("win_percent")
        try:
            win_pct = float(wp_raw) if wp_raw is not None else (
                round(wins / (wins + losses), 3) if (wins + losses) > 0 else None
            )
        except (TypeError, ValueError):
            win_pct = None

        last10_w, last10_l = _parse_last_ten(t.get("last_ten_games"))

        # `games_behind` from BDL is numeric (0 for division leader).
        # Our column stores a string ("-" / "2.5"). Re-shape so the
        # iOS standings card renders the same as the MLB-Stats-API era.
        gb_raw = t.get("games_behind")
        if gb_raw is None or gb_raw == 0:
            games_back_str = "-"
        else:
            try:
                gb_f = float(gb_raw)
                games_back_str = "-" if gb_f == 0 else f"{gb_f:g}"
            except (TypeError, ValueError):
                games_back_str = "-"

        row = {
            "year":      current_year,
            "team_id":   team_id,
            "franch_id": franch_id,
            "team_name": full_team_name or team_obj.get("display_name"),
            "league":    league,
            "division":  division,
            # Rank is filled in below once we have the full division.
            "rank":      None,
            "G":         int(t.get("games_played") or (wins + losses)),
            "W":         wins,
            "L":         losses,
            "win_pct":   win_pct,
            # BDL uses NBA-style points_for/against — those values
            # are runs scored / runs allowed for MLB.
            "runs_scored":  t.get("points_for"),
            "runs_allowed": t.get("points_against"),

            "streak_code":          _streak_code_from_int(t.get("streak")),
            "last_ten_w":           last10_w,
            "last_ten_l":           last10_l,
            "home_w":               t.get("home_wins"),
            "home_l":               t.get("home_losses"),
            "away_w":               t.get("road_wins"),
            "away_l":               t.get("road_losses"),
            "games_back":           games_back_str,
            "wild_card_games_back": None,  # BDL doesn't ship this directly
            "clinch_indicator":     t.get("clincher"),
            "division_leader":      (gb_raw == 0),
            "clinched":             bool(t.get("clincher")),
            "magic_number":         (str(t.get("magic_number_division"))
                                     if t.get("magic_number_division") is not None
                                     else None),
            "elimination_number":   None,
            "_division_gb": float(t.get("division_games_behind") or 0),
        }
        rows_by_division.setdefault((league, division), []).append(row)

    rows_to_save: list[dict] = []
    for _key, rows in rows_by_division.items():
        # Rank within division by `division_games_behind` asc.
        rows.sort(key=lambda r: r["_division_gb"])
        for idx, r in enumerate(rows, 1):
            r["rank"] = idx
            r.pop("_division_gb", None)
            rows_to_save.append(r)

    # Wild-card games back. BDL doesn't ship this so we derive it
    # locally. Per-league: division leaders (rank == 1) sit above the
    # WC race and get "-". Among the remaining teams, the one with the
    # highest win pct is the WC leader (gets "-"); every other non-DL
    # team's WCGB is the standard formula
    #     ((wc_W - team_W) + (team_L - wc_L)) / 2
    # so a team tied with the leader reads 0 ("-"), and the next slot
    # back reads 0.5, 1, 1.5, ... matching MLB's published WCGB column.
    rows_by_league: dict[str, list[dict]] = {}
    for r in rows_to_save:
        rows_by_league.setdefault(r["league"], []).append(r)
    for league_rows in rows_by_league.values():
        non_dl = [r for r in league_rows if r["rank"] != 1]
        if non_dl:
            non_dl.sort(key=lambda r: r["win_pct"] or 0.0, reverse=True)
            wc_leader = non_dl[0]
            wc_W = wc_leader["W"] or 0
            wc_L = wc_leader["L"] or 0
            wc_leader["wild_card_games_back"] = "-"
            for r in non_dl[1:]:
                gb_f = ((wc_W - (r["W"] or 0)) + ((r["L"] or 0) - wc_L)) / 2.0
                r["wild_card_games_back"] = "-" if gb_f == 0 else f"{gb_f:g}"
    for r in rows_to_save:
        if r["rank"] == 1:
            r["wild_card_games_back"] = "-"

    if rows_to_save:
        with connection.get_session() as db:
            crud.save_team_seasons(db, rows_to_save)

    log.info(f"standings: updated {len(rows_to_save)} teams, {len(failed_lookups)} unmatched")
    if failed_lookups:
        log.warning(f"unmatched standings entries: {failed_lookups}")

    return len(rows_to_save), len(failed_lookups)


# ---------------------------------------------------------------------------
# Game logs (batting + pitching)
# ---------------------------------------------------------------------------
# Per-call MLB Stats API fetch lands all of yesterday's games in the upsert,
# and is a no-op for already-stored older games (merge by composite PK).
# We pace the calls to avoid tripping rate limits.

_GAMELOG_SLEEP_SECONDS = 0.2
_GAMELOG_LOG_EVERY    = 50


def _ids_with_current_season(season_model, current_year: int) -> list[int]:
    """Distinct player_ids that have a row in the given seasons table for
    the current year. Reflects "actually played this season" (since the
    row was just written by phases 1/2), which is more precise than
    Player.mlb_last_season — the latter counts roster entries that may
    not have appeared in any games yet."""
    with connection.get_session() as db:
        rows = (
            db.query(season_model.player_id)
            .filter(season_model.year == current_year)
            .distinct()
            .all()
        )
    return [r.player_id for r in rows]


def _update_gamelogs(current_year: int) -> dict:
    """Refresh per-game logs for last night's finals via the BDL
    game-centric path. Queries BOTH `yesterday_utc` and `today_utc`
    because BDL buckets games by UTC start time — a single MLB
    schedule night spans two UTC dates (ET evening games cross
    midnight UTC; PT evening games start fully into UTC tomorrow).
    Overlap is safe via the gamelog PK.

    Returns the counts the nightly status endpoint surfaces. Two
    fields kept for backward compatibility with old status shapes:
        batters_processed   — set to bat_rows so existing dashboards keep working
        pitchers_processed  — set to pit_rows likewise
        batter_games_saved  — total batting rows upserted
        pitcher_games_saved — total pitching rows upserted
        batters_failed      — always 0 under the new path (errors raise)
        pitchers_failed     — same
    """
    # BDL indexes games by UTC start time, NOT by MLB's local-calendar
    # schedule day. A single MLB "Tuesday night" slate splits across two
    # UTC dates: ET-night games (~19:00 ET) cross midnight UTC, and PT-
    # night games (~19:00 PT) start fully into "UTC tomorrow". The
    # nightly cron runs at ~04:00 UTC, when those games have just
    # finished — so we query BOTH `yesterday_utc` AND `today_utc` to
    # capture the full local-night slate. Overlap is safe: the
    # (player_id, game_id) PK on the gamelog tables makes any duplicate
    # row a no-op upsert.
    today_utc     = datetime.date.today()
    yesterday_utc = today_utc - datetime.timedelta(days=1)
    target_dates  = [yesterday_utc.isoformat(), today_utc.isoformat()]
    log.info(f"  BDL gamelogs target dates: {target_dates}")

    bat_rows = 0
    pit_rows = 0
    games    = 0
    skipped  = 0
    per_date_results: list[dict] = []
    for date_str in target_dates:
        log.info(f"  BDL gamelogs: fetching for {date_str}")
        try:
            r = data_service.save_bdl_gamelogs_for_date(date_str)
        except Exception as exc:
            # Catch + log the stack so a transient BDL failure
            # doesn't crater the whole gamelog phase silently. The
            # other date still runs.
            log.exception(f"  BDL gamelogs phase FAILED for {date_str}: {exc}")
            r = {
                "status":   "error",
                "bat_rows": 0, "pit_rows": 0, "games": 0,
                "skipped_unmapped_players": 0,
            }
        per_date_results.append({"date": date_str, **r})
        bat_rows += int(r.get("bat_rows") or 0)
        pit_rows += int(r.get("pit_rows") or 0)
        games    += int(r.get("games")    or 0)
        skipped  += int(r.get("skipped_unmapped_players") or 0)
        log.info(
            f"  BDL gamelogs {date_str}: "
            f"{int(r.get('games') or 0)} games, "
            f"{int(r.get('bat_rows') or 0)} batting rows, "
            f"{int(r.get('pit_rows') or 0)} pitching rows"
        )

    log.info(
        f"  BDL gamelogs TOTAL across {target_dates}: "
        f"{games} games, {bat_rows} batting rows, {pit_rows} pitching rows, "
        f"{skipped} games with no mapped players"
    )

    # `bat_ids`/`pit_ids` aren't iterated anymore — left here as
    # an informational count for the log line, matching the prior
    # phase header's "active batters / active pitchers" output.
    bat_ids = _ids_with_current_season(PlayerSeason,  current_year)
    pit_ids = _ids_with_current_season(PitcherSeason, current_year)
    log.info(
        f"  active batters in DB: {len(bat_ids)}, "
        f"active pitchers in DB: {len(pit_ids)} (informational)"
    )

    return {
        "batters_processed":   bat_rows,
        "pitchers_processed":  pit_rows,
        "batter_games_saved":  bat_rows,
        "pitcher_games_saved": pit_rows,
        "batters_failed":      0,
        "pitchers_failed":     0,
        "bdl_games_fetched":   games,
        "bdl_skipped_games":   skipped,
    }


# ---------------------------------------------------------------------------
# Catch-up update
# ---------------------------------------------------------------------------
#
# A second Railway cron job should call `POST /admin/catchup-update` daily at
# **21:00 UTC (≈2 PM PT)** — after the morning + early-afternoon games have
# finished and BDL has had several hours to absorb their season totals, but
# before the evening slate first-pitches. The morning nightly at 02:00 UTC
# can miss BDL data for late West Coast finals; this lightweight pass picks
# up those lagging rows without the cost of a full re-run.

def run_catchup_update() -> dict:
    """Lightweight refresh: refetch BDL season stats for every
    bdl-mapped active player whose DB `G` is still behind BDL's
    `batting_gp` / `pitching_gp`.

    Operates on the full active roster rather than a date slice.
    The previous design pulled `seen_pids` from yesterday's BDL
    `/stats` payload, which silently dropped players whose game
    rows weren't returned (off-day for some teams, late-PT data
    lag, BDL bucketing quirks). The new design queries
    `players` / `pitchers` for `bdl_id IS NOT NULL AND
    mlb_last_season IS NULL`, batch-fetches `/season_stats` for
    every active bdl_id, and compares against the current
    `player_seasons` / `pitcher_seasons` G column. No game-row
    iteration; no dependence on which date BDL files a late game
    under.

    Same per-player decision the date-based path used: bump on
    `bdl_g > db_g`, otherwise mark `already_current`. Two-way
    players (Ohtani) are processed once per side and each side
    is independently advanced.

    Keeps the bref WAR refresh and doubleheader catch-up phases
    intact; only the stats-update phase is rewritten.

    Returns counts: `{active_batters_checked,
    active_pitchers_checked, updated, already_current, no_bdl_data,
    failed, war_updated, doubleheader_dates_checked,
    doubleheader_dates_backfilled}`.
    """
    if not connection.db_available():
        return {"status": "no_db"}
    # Fail fast on missing BDL_KEY — same convention as the full
    # nightly so a misconfigured cron service doesn't burn through
    # mlb-stats-api fallbacks before anyone notices.
    data_service._get_bdl_key()

    import pandas as _pd

    current_year = data_service._current_year()

    counts = {
        "active_batters_checked":          0,
        "active_pitchers_checked":         0,
        "updated":                         0,
        "already_current":                 0,
        "no_bdl_data":                     0,
        "failed":                          0,
        "war_updated":                     0,
        "doubleheader_dates_checked":      0,
        "doubleheader_dates_backfilled":   0,
    }

    def _run_dh_catchup() -> None:
        """Scan this season's BDL `/games` for any matchup BDL ships
        twice on the same date (doubleheader), check which of those
        game ids are missing from `batting_gamelogs`, and re-ingest
        the affected dates. Restricted to past dates within the last
        14 days — older misses may no longer be retrievable, and
        today's still-live games are out of scope. Mutates `counts`
        in place; logs and swallows network/DB failures so the rest
        of the catch-up isn't disturbed."""
        log.info("[catchup] scanning for missing doubleheaders this season")
        try:
            dh = data_service.find_missing_doubleheaders(current_year)
        except Exception as exc:
            log.warning(f"[catchup] doubleheader scan failed: {exc}")
            return
        today_et = datetime.datetime.now(
            data_service._MLB_LOCAL_TZ,
        ).date()
        cutoff_et = today_et - datetime.timedelta(days=14)
        candidates: list[str] = []
        for d_str in (dh.get("missing_dates") or []):
            try:
                d = datetime.date.fromisoformat(d_str)
            except ValueError:
                continue
            if cutoff_et <= d < today_et:
                candidates.append(d_str)
        counts["doubleheader_dates_checked"] = len(candidates)
        for d_str in candidates:
            log.info(
                f"[catchup] backfilling missing doubleheader date {d_str}"
            )
            try:
                r = data_service.save_bdl_gamelogs_for_date(d_str)
                if r.get("status") == "ok" and (r.get("games") or 0) > 0:
                    counts["doubleheader_dates_backfilled"] += 1
            except Exception as exc:
                log.warning(
                    f"[catchup] doubleheader backfill {d_str} failed: {exc}"
                )

    # bref WAR — by 2pm PT, baseball-reference.com has consistently
    # updated the WAR CSVs from the previous night's games. Before
    # downloading, snapshot the hash from the last successful fetch
    # (set by the morning nightly, typically) so we can detect
    # whether bref has shipped new data since then. If the hash is
    # unchanged, applying a "fresh" WAR is a no-op write — skip it.
    log.info("[catchup] fetching bref WAR CSVs...")
    prior_bat_hash   = data_service.get_last_war_hash("bat")
    prior_pitch_hash = data_service.get_last_war_hash("pitch")

    bat_meta: dict | None = None
    pit_meta: dict | None = None
    try:
        bat_meta = data_service._bwar_bat_all_meta()
    except Exception as exc:
        log.warning(f"[catchup] bwar_bat fetch failed, continuing with empty: {exc}")
    try:
        pit_meta = data_service._bwar_pitch_all_meta()
    except Exception as exc:
        log.warning(f"[catchup] bwar_pitch fetch failed, continuing with empty: {exc}")

    def _war_updated(prior: str | None, meta: dict | None) -> bool:
        """A side counts as 'updated' when bref's current hash
        differs from the hash recorded by the last successful
        fetch — OR when there's no prior hash to compare against
        (first run after restart; assume the data is fresh)."""
        if meta is None:
            return False
        if prior is None:
            return True
        return prior != meta["hash"]

    bat_updated   = _war_updated(prior_bat_hash,   bat_meta)
    pitch_updated = _war_updated(prior_pitch_hash, pit_meta)
    if not bat_updated and not pitch_updated:
        log.info("[catchup] bref WAR CSV not yet updated — skipping WAR refresh in catch-up")
    else:
        log.info(
            "[catchup] bref WAR fresh: bat=%s pitch=%s",
            "yes" if bat_updated else "no",
            "yes" if pitch_updated else "no",
        )

    empty_bwar = _pd.DataFrame(columns=["mlb_ID", "year_ID", "stint_ID"])

    def _slice_current(df):
        if "year_ID" in df.columns and not df.empty:
            return df[df["year_ID"] == current_year]
        return df

    # Per-side WAR slice: real bref data only when that side's CSV
    # has actually moved since the prior baseline. Otherwise the
    # build helpers see an empty DataFrame and leave WAR fields
    # untouched (preserving whatever the morning nightly wrote).
    bwar_bat_current = (
        _slice_current(bat_meta["df"]) if bat_updated and bat_meta is not None
        else empty_bwar
    )
    bwar_pitch_current = (
        _slice_current(pit_meta["df"]) if pitch_updated and pit_meta is not None
        else empty_bwar
    )

    def _covered_pids(df) -> set[int]:
        if "mlb_ID" not in df.columns or df.empty:
            return set()
        return set(int(x) for x in df["mlb_ID"].dropna().astype(int))

    bwar_bat_pids   = _covered_pids(bwar_bat_current)
    bwar_pitch_pids = _covered_pids(bwar_pitch_current)
    log.info(
        f"[catchup] bwar covers {len(bwar_bat_pids)} batters and "
        f"{len(bwar_pitch_pids)} pitchers for {current_year}"
    )

    # 1. Pull every bdl-mapped active player from the DB. Active =
    #    `mlb_last_season IS NULL` (same retired flag iOS reads).
    #    Mapped   = `bdl_id IS NOT NULL` so the batch fetch below
    #    actually has something to query against. Two-way players
    #    (Ohtani) carry the same bdl_id on both bio tables; the
    #    union de-dupes at fetch time.
    with connection.get_session() as db:
        bat_bio = (
            db.query(_Player.player_id, _Player.bdl_id, _Player.mlb_debut)
              .filter(_Player.bdl_id.isnot(None))
              .filter(_Player.mlb_last_season.is_(None))
              .all()
        )
        pit_bio = (
            db.query(_Pitcher.player_id, _Pitcher.bdl_id, _Pitcher.mlb_debut)
              .filter(_Pitcher.bdl_id.isnot(None))
              .filter(_Pitcher.mlb_last_season.is_(None))
              .all()
        )
        bat_g_rows = (
            db.query(PlayerSeason.player_id, PlayerSeason.G)
              .filter(PlayerSeason.year == current_year)
              .all()
        )
        pit_g_rows = (
            db.query(PitcherSeason.player_id, PitcherSeason.G)
              .filter(PitcherSeason.year == current_year)
              .all()
        )
    bat_bdl_map: dict[int, int]          = {r.player_id: int(r.bdl_id) for r in bat_bio}
    bat_debut_map: dict[int, int | None] = {r.player_id: r.mlb_debut   for r in bat_bio}
    pit_bdl_map: dict[int, int]          = {r.player_id: int(r.bdl_id) for r in pit_bio}
    pit_debut_map: dict[int, int | None] = {r.player_id: r.mlb_debut   for r in pit_bio}
    bat_db_g: dict[int, int | None] = {r.player_id: r.G for r in bat_g_rows}
    pit_db_g: dict[int, int | None] = {r.player_id: r.G for r in pit_g_rows}

    counts["active_batters_checked"]  = len(bat_bdl_map)
    counts["active_pitchers_checked"] = len(pit_bdl_map)
    log.info(
        "[catchup] active mapped players: batters=%d pitchers=%d",
        len(bat_bdl_map), len(pit_bdl_map),
    )

    # 2. Batch-fetch BDL season stats for every active bdl_id.
    #    Union across sides so a two-way player's single profile
    #    powers both decisions. `_fetch_bdl_batch_stats` chunks
    #    in 50s and paginates each chunk; rate-limits between
    #    chunks via `_BDL_RATE_LIMIT_SLEEP`.
    bdl_ids_to_fetch: set[int] = set()
    bdl_ids_to_fetch.update(bat_bdl_map.values())
    bdl_ids_to_fetch.update(pit_bdl_map.values())
    if not bdl_ids_to_fetch:
        log.warning("[catchup] no active mapped players — nothing to check")
        _run_dh_catchup()
        return {"status": "ok", **counts}
    raw_batch = data_service._fetch_bdl_batch_stats(
        sorted(bdl_ids_to_fetch), current_year,
    )
    bat_parsed: dict[int, dict] = {
        bdl_id: data_service._parse_bdl_batter_row(row)
        for bdl_id, row in raw_batch.items()
    }
    pit_parsed: dict[int, dict] = {
        bdl_id: data_service._parse_bdl_pitcher_row(row)
        for bdl_id, row in raw_batch.items()
    }
    log.info(
        "[catchup] BDL returned rows for %d/%d active bdl_ids",
        len(raw_batch), len(bdl_ids_to_fetch),
    )

    # 3. Per-player compare-and-write. Decision logic mirrors the
    #    old date-based path: bdl_g > db_g → rebuild the season
    #    entry and save; otherwise the DB is at-or-ahead of BDL
    #    and we tick `already_current`. Two-way players hit both
    #    branches and each side is advanced independently. `no_bdl_data`
    #    catches the case where BDL has the bdl_id mapped but
    #    didn't ship a season_stats row this year (rookie call-up
    #    not yet stamped, IL stash with 0 GP, etc.).
    all_pids = sorted(set(bat_bdl_map.keys()) | set(pit_bdl_map.keys()))
    with connection.get_session() as db:
        for player_id in all_pids:
            # --- Batter side ---
            if player_id in bat_bdl_map:
                bdl_id = bat_bdl_map[player_id]
                bdl_stats = bat_parsed.get(bdl_id)
                if bdl_stats is None:
                    counts["no_bdl_data"] += 1
                else:
                    bdl_g = bdl_stats.get("G") or 0
                    db_g  = bat_db_g.get(player_id) or 0
                    if bdl_g > db_g:
                        try:
                            entry = _build_current_batter_entry(
                                player_id, bdl_id, bwar_bat_current,
                                current_year,
                                bdl_stats=bdl_stats,
                                mlb_debut=bat_debut_map.get(player_id),
                            )
                            if entry is not None:
                                crud.save_player_seasons(db, player_id, [entry])
                                counts["updated"] += 1
                                if player_id in bwar_bat_pids:
                                    counts["war_updated"] += 1
                            else:
                                counts["failed"] += 1
                        except Exception as exc:
                            log.error(f"[catchup] batter {player_id} build/save FAILED: {exc}")
                            counts["failed"] += 1
                    else:
                        counts["already_current"] += 1
            # --- Pitcher side ---
            if player_id in pit_bdl_map:
                bdl_id = pit_bdl_map[player_id]
                bdl_stats = pit_parsed.get(bdl_id)
                if bdl_stats is None:
                    counts["no_bdl_data"] += 1
                else:
                    bdl_g = bdl_stats.get("G") or 0
                    db_g  = pit_db_g.get(player_id) or 0
                    if bdl_g > db_g:
                        try:
                            entry = _build_current_pitcher_entry(
                                player_id, bdl_id, bwar_pitch_current,
                                current_year,
                                bdl_stats=bdl_stats,
                                mlb_debut=pit_debut_map.get(player_id),
                            )
                            if entry is not None:
                                crud.save_pitcher_seasons(db, player_id, [entry])
                                counts["updated"] += 1
                                if player_id in bwar_pitch_pids:
                                    counts["war_updated"] += 1
                            else:
                                counts["failed"] += 1
                        except Exception as exc:
                            log.error(f"[catchup] pitcher {player_id} build/save FAILED: {exc}")
                            counts["failed"] += 1
                    else:
                        counts["already_current"] += 1
        db.commit()

    _run_dh_catchup()
    log.info(f"[catchup] done: {counts}")
    return {"status": "ok", **counts}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CURRENT-SEASON STINT MATERIALISATION (team-scoped leaderboards)
#
# player_season_stints / pitcher_season_stints load from Retrosheet, which
# publishes only AFTER a season ends — so the in-progress year is missing, and
# a team-scoped board (Gap 3) reads a man short his current season (Judge 368
# vs 385). bWAR carries the per-stint TEAM but no counting stats; BDL gives a
# season aggregate that can't be split. The GAMELOGS can: a game's two teams
# are its two distinct `opponent` values, so a player's OWN team is the OTHER
# one — game-level truth that splits a mid-season trade exactly (verified vs
# Retrosheet: Martinez 2017 ARI 29/DET 16, Suarez 2025 ARI 36/SEA 13).
# ---------------------------------------------------------------------------

# A gamelog team is a bWAR-style code (NYY) or a display name ("New York
# Yankees"); map either to a franchise, then to that franchise's era-correct
# RETRO code (NYA) — the vocabulary the stint table + the eventual Retrosheet
# load use, so the (player, year, team) upsert key ALIGNS and overwrites rather
# than duplicating.
_STINT_NAME_TO_FRANCH = {name: fr for fr, name in team_crosswalk.FRANCH_CURRENT_NAME.items()}
# Fallbacks for gamelog team values team_crosswalk doesn't key directly: bWAR/gamelogs
# use "OAK" for the Athletics (crosswalk keys "ATH"); and the gamelogs sometimes carry
# "Los Angeles Angels" where the crosswalk's current name is "…of Anaheim". Any value
# that still maps to nothing is SKIPPED + logged, never written with a bad key.
_STINT_TEAM_FALLBACK = {"OAK": "OAK", "Los Angeles Angels": "ANA"}


def _derived_team_to_retro(raw, year):
    """A gamelog-derived team CODE or NAME -> the franchise's era-correct RETRO code
    for `year` ('NYY' / 'New York Yankees' -> 'NYA'), or None when it maps to neither
    a known code nor a known name (the caller SKIPS + LOGS — never writes a bad key)."""
    if not raw:
        return None
    fr = (_STINT_NAME_TO_FRANCH.get(raw)
          or team_crosswalk.MODERN_CODE_TO_FRANCH.get(raw)
          or _STINT_TEAM_FALLBACK.get(raw))
    if fr is None:
        return None
    segs = team_crosswalk.FRANCH_RETRO_SEGMENTS.get(fr, ())
    if not segs:
        return None
    y = int(year)
    for code, lo, hi in segs:
        if lo <= y <= hi:
            return code
    return segs[-1][0]   # a year past the last segment (the in-progress season) -> latest code


# Batting stint columns summed per game (integers); G = game count, TB derived.
_STINT_BAT_COLS = ["G", "AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB",
                   "SO", "SB", "CS", "IBB", "HBP", "SF", "SH", "GIDP", "PA"]
_STINT_BAT_SQL = """
WITH game_team AS (
  SELECT game_id, opponent AS team FROM batting_gamelogs WHERE season = :yr
  GROUP BY game_id, opponent
),
per_game AS (   -- ONE row per (player, game): the other team + that game's line
  SELECT g.player_id, g.game_id, MIN(gt.team) AS raw_team, MAX(g.game_date) AS gd,
         MAX(g."AB") "AB", MAX(g."R") "R", MAX(g."H") "H", MAX(g.doubles) doubles,
         MAX(g.triples) triples, MAX(g."HR") "HR", MAX(g."RBI") "RBI", MAX(g."BB") "BB",
         MAX(g."SO") "SO", MAX(g."SB") "SB", MAX(g."CS") "CS", MAX(g."IBB") "IBB",
         MAX(g."HBP") "HBP", MAX(g."SF") "SF", MAX(g."SH") "SH", MAX(g."GIDP") "GIDP",
         MAX(g."PA") "PA"
  FROM batting_gamelogs g
  JOIN game_team gt ON gt.game_id = g.game_id AND gt.team <> g.opponent
  WHERE g.season = :yr
  GROUP BY g.player_id, g.game_id
)
SELECT player_id, raw_team, min(gd) AS first_date, count(*) AS "G",
       sum("AB") "AB", sum("R") "R", sum("H") "H", sum(doubles) doubles, sum(triples) triples,
       sum("HR") "HR", sum("RBI") "RBI", sum("BB") "BB", sum("SO") "SO", sum("SB") "SB",
       sum("CS") "CS", sum("IBB") "IBB", sum("HBP") "HBP", sum("SF") "SF", sum("SH") "SH",
       sum("GIDP") "GIDP", sum("PA") "PA"
FROM per_game GROUP BY player_id, raw_team
"""

# Pitching: W/L/SV counted from `result`; IP summed as OUTS (6.2 = 6⅔) so the
# decimal-innings notation adds up correctly. GS/CG/SHO/etc. aren't in the
# gamelog and stay NULL (the team board ranks by W/L/SV/SO/ER, all derivable).
_STINT_PIT_COLS = ["G", "W", "L", "SV", "H", "R", "ER", "BB", "SO", "HR", "HBP", "WP", "ip_outs"]
_STINT_PIT_SQL = """
WITH game_team AS (
  SELECT game_id, opponent AS team FROM pitching_gamelogs WHERE season = :yr
  GROUP BY game_id, opponent
),
per_game AS (
  SELECT g.player_id, g.game_id, MIN(gt.team) AS raw_team, MAX(g.game_date) AS gd,
         MAX(g.result) AS result, MAX(g."IP") AS ip,
         MAX(g."H") "H", MAX(g."R") "R", MAX(g."ER") "ER", MAX(g."BB") "BB",
         MAX(g."SO") "SO", MAX(g."HR") "HR", MAX(g."HBP") "HBP", MAX(g."WP") "WP"
  FROM pitching_gamelogs g
  JOIN game_team gt ON gt.game_id = g.game_id AND gt.team <> g.opponent
  WHERE g.season = :yr
  GROUP BY g.player_id, g.game_id
)
SELECT player_id, raw_team, min(gd) AS first_date, count(*) AS "G",
       count(*) FILTER (WHERE result = 'W') "W",
       count(*) FILTER (WHERE result = 'L') "L",
       count(*) FILTER (WHERE result = 'S') "SV",
       sum("H") "H", sum("R") "R", sum("ER") "ER", sum("BB") "BB", sum("SO") "SO",
       sum("HR") "HR", sum("HBP") "HBP", sum("WP") "WP",
       sum(floor(ip) * 3 + round((ip - floor(ip)) * 10))::int AS ip_outs
FROM per_game GROUP BY player_id, raw_team
"""


def _derive_stints(db, year, is_pitch):
    """Run the gamelog derivation, map each derived team to a retro code, and return
    the list of stint row dicts (one per player+franchise) tagged source='nightly'.
    Unmappable teams are skipped and counted in `unmapped`."""
    from sqlalchemy import text as _text
    cols = _STINT_PIT_COLS if is_pitch else _STINT_BAT_COLS
    sql = _STINT_PIT_SQL if is_pitch else _STINT_BAT_SQL
    rows = db.execute(_text(sql), {"yr": year}).fetchall()
    # accumulate by (player, RETRO team) — two raw codes ('NYY'/'New York Yankees')
    # can fold to the same franchise, and their lines must sum, not collide.
    accum: dict = {}      # (pid, retro) -> {"_first": date, col: int}
    unmapped: dict = {}
    for r in rows:
        pid, raw_team, first_date = r[0], r[1], r[2]
        retro = _derived_team_to_retro(raw_team, year)
        if retro is None:
            unmapped[raw_team] = unmapped.get(raw_team, 0) + 1
            continue
        acc = accum.setdefault((pid, retro), {"_first": first_date})
        if first_date is not None and (acc["_first"] is None or first_date < acc["_first"]):
            acc["_first"] = first_date
        for i, c in enumerate(cols):
            acc[c] = acc.get(c, 0) + int(r[3 + i] or 0)
    # assign stint_order per player by first appearance, and build the row dicts
    by_player: dict = {}
    for (pid, retro), acc in accum.items():
        by_player.setdefault(pid, []).append((retro, acc))
    out_rows = []
    for pid, teams in by_player.items():
        teams.sort(key=lambda t: (t[1]["_first"] or datetime.date.max))
        for order, (retro, acc) in enumerate(teams, 1):
            row = {"player_id": pid, "year": year, "team": retro,
                   "stint_order": order, "source": "nightly"}
            if is_pitch:
                outs = acc["ip_outs"]
                row.update({"G": acc["G"], "W": acc["W"], "L": acc["L"], "SV": acc["SV"],
                            "IP": round(outs // 3 + (outs % 3) / 10.0, 1),
                            "H": acc["H"], "R": acc["R"], "ER": acc["ER"], "BB": acc["BB"],
                            "SO": acc["SO"], "HR": acc["HR"], "HBP": acc["HBP"], "WP": acc["WP"]})
            else:
                row.update({c: acc[c] for c in _STINT_BAT_COLS})
                row["TB"] = acc["H"] + acc["doubles"] + 2 * acc["triples"] + 3 * acc["HR"]
            out_rows.append(row)
    return out_rows, unmapped


def _update_stints(current_year: int) -> dict:
    """Materialise the CURRENT-season per-(player, team) stint rows from the gamelogs.
    REPLACES this year's source='nightly' rows (idempotent; never touches Retrosheet
    history), so re-running can't double anything and a mid-season trade lands as
    exactly two rows. Non-fatal for the rest of the nightly."""
    from sqlalchemy import text as _text
    summary = {"bat_written": 0, "pit_written": 0, "unmapped": {}}
    with connection.get_session() as db:
        for is_pitch, tbl in ((False, "player_season_stints"), (True, "pitcher_season_stints")):
            out_rows, unmapped = _derive_stints(db, current_year, is_pitch)
            # REPLACE only this year's nightly rows — Retrosheet history is untouched,
            # and a re-run inserts the same keys (player, year, team) idempotently.
            db.execute(_text(f"DELETE FROM {tbl} WHERE year = :yr AND source = 'nightly'"),
                       {"yr": current_year})
            save = crud.save_pitcher_season_stints if is_pitch else crud.save_player_season_stints
            save(db, out_rows)
            summary["pit_written" if is_pitch else "bat_written"] = len(out_rows)
            for k, v in unmapped.items():
                summary["unmapped"][k] = summary["unmapped"].get(k, 0) + v
        db.commit()
    if summary["unmapped"]:
        log.warning("stint derivation: %d unmapped team value(s) SKIPPED: %s",
                    sum(summary["unmapped"].values()), summary["unmapped"])
    return summary


def main() -> None:
    if not connection.db_available():
        sys.exit("ERROR: DATABASE_URL is not set.")

    current_year = data_service._current_year()
    log.info(f"Nightly update — season {current_year}")

    log.info("=" * 52)
    log.info("Phase 1: batters")
    log.info("=" * 52)
    bat_updated, bat_skipped, bat_failed = _update_batters(current_year)

    log.info("=" * 52)
    log.info("Phase 2: pitchers")
    log.info("=" * 52)
    pit_updated, pit_skipped, pit_failed = _update_pitchers(current_year)

    log.info("=" * 52)
    log.info("Phase 3: standings")
    log.info("=" * 52)
    standings_updated, standings_failed = _update_standings(current_year)

    log.info("=" * 52)
    log.info("Phase 4: game logs (active rosters only)")
    log.info("=" * 52)
    gl = _update_gamelogs(current_year)

    log.info("=" * 52)
    log.info("Phase 4b: batting counting aggregation (GIDP/SH/HBP/SF/CS/PA/H)")
    log.info("=" * 52)
    # Runs AFTER game logs so it sums the freshest per-game rows. Overwrites
    # the current-season counting fields the BDL batter phase doesn't write
    # (and corrects stale bref-seed values like GIDP). Non-fatal.
    try:
        with connection.get_session() as db:
            bc_updated = data_service.recalculate_batting_counting(db, current_year)
            db.commit()
        log.info(f"Batting counting aggregation — rows updated: {bc_updated}")
    except Exception as exc:
        log.error(f"Batting counting aggregation FAILED (non-fatal): {exc}")

    # Pitcher counterpart — R / HBP summed from pitching_gamelogs into
    # pitcher_seasons (BDL's pitcher season phase omits them). Non-fatal.
    try:
        with connection.get_session() as db:
            pc_updated = data_service.recalculate_pitching_counting(db, current_year)
            db.commit()
        log.info(f"Pitching counting aggregation — rows updated: {pc_updated}")
    except Exception as exc:
        log.error(f"Pitching counting aggregation FAILED (non-fatal): {exc}")

    log.info("=" * 52)
    log.info("Phase 4c: advanced batting rates (wOBA / K% / BB% / ISO)")
    log.info("=" * 52)
    # Runs AFTER 4b so the components are correct. BDL omits these rate
    # columns, so without this pass current-season rows stay NULL. Non-fatal.
    try:
        with connection.get_session() as db:
            br_updated = data_service.recalculate_batting_rates(db, current_year)
            db.commit()
        log.info(f"Batting rates — rows updated: {br_updated}")
    except Exception as exc:
        log.error(f"Batting rates FAILED (non-fatal): {exc}")

    log.info("=" * 52)
    log.info("Phase 5: reconcile teams from active rosters")
    log.info("=" * 52)
    # Belt-and-suspenders for offseason-trade / FA-signing cases
    # where bref's `Tm` column lags the move. 30 API calls (one per
    # team) — much cheaper than per-player /people/{id} hits.
    try:
        team_sync = data_service.sync_all_player_teams_from_rosters(current_year)
        log.info(
            f"Teams reconciled — rows updated: {team_sync.get('updated', 0)}, "
            f"failed teams: {team_sync.get('failed_teams', [])}"
        )
    except Exception as exc:
        log.error(f"Team reconcile FAILED (non-fatal): {exc}")

    # Phase 5b — active-status sync via BDL `/players?player_ids[]=`.
    # The roster walk above only sees players currently on a 40-man,
    # so it can't detect retirements (player BDL knows is inactive)
    # or comebacks (Lahman still says retired even though BDL
    # rosters them). This pass reconciles `mlb_last_season` against
    # BDL's `active` flag for every player_id we have a `bdl_id`
    # mapping for. Non-fatal — degrades silently if BDL is down.
    try:
        active_sync = data_service.sync_player_active_status_from_bdl(current_year)
        ac = active_sync.get("counts") or {}
        log.info(
            f"Active-status reconciled — activated: {ac.get('activated', 0)}, "
            f"retired: {ac.get('retired', 0)}, "
            f"team_updated: {ac.get('team_updated', 0)}, "
            f"no_data: {ac.get('no_data', 0)}, "
            f"failed: {ac.get('failed', 0)}"
        )
    except Exception as exc:
        log.error(f"Active-status reconcile FAILED (non-fatal): {exc}")

    # Phase 5c — call-up discovery via BDL's active-roster walk.
    # Same 30-team walk that Phase 5 uses, but here we care about
    # the new-bio side: any BDL roster player without a matching
    # MLBAM-keyed row in `players` / `pitchers` gets resolved via
    # MLB Stats API name search and inserted on the spot, plus a
    # same-season stats backfill. Catches fresh call-ups so iOS
    # sees a real bio + headshot the morning after their debut
    # rather than waiting for the next Lahman archive drop.
    # Non-fatal — discovery failures don't roll back the rest of
    # the nightly.
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
        log.error(f"Discover phase FAILED (non-fatal): {exc}")

    # Phase 5d — same-season gamelog backfill for newly-discovered
    # players. Only fires when Phase 5c actually inserted at least
    # one new bio — the operation walks every BDL final from
    # March 25 through today, which is ~900 games for a typical
    # full season and not worth running on quiet nights. The
    # backfill is idempotent (PK upsert) so we can re-scan dates
    # that already have rows; the cost is the BDL API budget.
    # Auto-dedup tail runs inside `backfill_bdl_gamelogs` already.
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
                f"New-player gamelog backfill FAILED (non-fatal): {exc}"
            )

    # Phase 6 — hot/cold heat. Compares each active player's last-N-game
    # window to their season baseline and stamps heat_score / heat_tier.
    # Runs last so it reads fully-ingested, deduped gamelogs. Non-fatal.
    log.info("=" * 52)
    log.info("Phase 6: hot/cold heat")
    log.info("=" * 52)
    try:
        with connection.get_session() as db:
            heat = data_service.compute_all_player_heat(db, current_year)
        log.info(
            f"Heat computed — scored: {heat.get('scored', 0)}, "
            f"hot: {heat.get('hot', 0)}, cold: {heat.get('cold', 0)}, "
            f"neutral: {heat.get('neutral', 0)}, skipped: {heat.get('skipped', 0)}"
        )
    except Exception as exc:
        log.error(f"Heat compute FAILED (non-fatal): {exc}")

    log.info("=" * 52)
    log.info("Phase 7: current-season team stints (from game logs)")
    log.info("=" * 52)
    # Materialise the in-progress season's per-(player, team) stint rows from the
    # gamelogs (Phase 4 refreshed them), so team-scoped leaderboards are current —
    # the only source that splits a mid-season trade correctly. Non-fatal.
    try:
        st = _update_stints(current_year)
        log.info(
            f"Team stints — batting rows: {st['bat_written']}, pitching rows: "
            f"{st['pit_written']}, unmapped-skipped: {sum(st['unmapped'].values())}"
        )
    except Exception as exc:
        log.error(f"Team stint materialisation FAILED (non-fatal): {exc}")

    log.info("=" * 52)
    log.info(
        f"Batters   — updated: {bat_updated}, skipped: {bat_skipped}, failed: {len(bat_failed)}"
    )
    log.info(
        f"Pitchers  — updated: {pit_updated}, skipped: {pit_skipped}, failed: {len(pit_failed)}"
    )
    log.info(
        f"Standings — updated: {standings_updated}, unmatched names: {standings_failed}"
    )
    log.info(
        f"Game logs — batters processed: {gl['batters_processed']} "
        f"(rows: {gl['batter_games_saved']}, failed: {gl['batters_failed']}); "
        f"pitchers processed: {gl['pitchers_processed']} "
        f"(rows: {gl['pitcher_games_saved']}, failed: {gl['pitchers_failed']})"
    )
    if bat_failed:
        log.error(f"Failed batter IDs: {bat_failed}")
    if pit_failed:
        log.error(f"Failed pitcher IDs: {pit_failed}")


if __name__ == "__main__":
    main()
