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
from database import connection, crud                 # noqa: E402
from database.models import (                          # noqa: E402
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
) -> dict | None:
    """Build a player_seasons row for the current year. Caller is
    expected to pre-fetch BDL stats in bulk (`_fetch_bdl_batch_stats`)
    and pass the parsed dict in via `bdl_stats`. When the caller
    didn't pre-fetch (single-player paths, ad-hoc /admin calls) the
    function falls back to a per-player fetch — BDL when the row
    has a `bdl_id`, MLB Stats API otherwise.

    Returns None when no source has any data for the player this
    season (off-roster minor leaguer, retired, etc.)."""
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

    # Pre-load the bdl_id map in one query so the per-player loop
    # doesn't issue a SELECT for each row's mapping.
    with connection.get_session() as db:
        player_ids = crud.get_all_player_ids(db)
        bdl_id_map: dict[int, int | None] = dict(
            db.query(_Player.player_id, _Player.bdl_id)
              .filter(_Player.player_id.in_(player_ids))
              .all()
        )
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
) -> dict | None:
    """Build a pitcher_seasons row for the current year. Same shape
    as the batter builder — caller passes pre-fetched BDL stats
    in via `bdl_stats`; if absent and `bdl_id is None`, falls back
    to a per-player MLB Stats API call."""
    player_war = (
        bwar_current[bwar_current["mlb_ID"] == float(player_id)]
        .sort_values("stint_ID")
        if "stint_ID" in bwar_current.columns
        else bwar_current[bwar_current["mlb_ID"] == float(player_id)]
    )

    override = bdl_stats
    if override is None and bdl_id is None:
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
        bdl_id_map: dict[int, int | None] = dict(
            db.query(_Pitcher.player_id, _Pitcher.bdl_id)
              .filter(_Pitcher.player_id.in_(pitcher_ids))
              .all()
        )
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
# Main
# ---------------------------------------------------------------------------

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
