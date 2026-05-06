"""Nightly update — refresh current-season stats for every player in the database.

Designed to run as a Railway cron job (e.g., daily at 04:00 UTC after
Baseball Reference publishes the previous day's data).

Phase 1 (batters): fetches batting_stats_bref + bwar_bat once and upserts
the current-year row in player_seasons for every player in the database.

Phase 2 (pitchers): fetches pitching_stats_bref + bwar_pitch once and upserts
the current-year row in pitcher_seasons for every pitcher in the database.

Phase 3 (standings): fetches pybaseball.standings() and upserts the
current-season row in team_seasons for each team.

Phase 4 (game logs): for every active roster player (mlb_last_season =
current year), pulls per-game stats from the MLB Stats API and upserts
into batting_gamelogs / pitching_gamelogs. Idempotent — yesterday's games
are the only new rows; older games are upserted as no-ops.

Seasonal workflow
-----------------
Pybaseball is the source of truth ONLY for the in-flight current season.
After each season ends (typically late October), the Lahman archive is
re-released with the just-completed year. Re-running lahman_load.py
permanently overwrites the pybaseball-sourced current-season rows with
canonical Lahman numbers; the cutoff in lahman_load.py is "current year"
so the rollover is automatic on the next run after Lahman publishes.
"""

import gc
import logging
import os
import re
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
    PitcherSeason, PlayerSeason, TeamSeason,
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

def _build_current_batter_entry(player_id: int, bref_df, bwar_current, current_year: int) -> dict | None:
    """Build a player_seasons row for the current year, or None if no data."""
    player_bref = bref_df[bref_df["mlbID"] == player_id]
    player_war = (
        bwar_current[bwar_current["mlb_ID"] == float(player_id)]
        .sort_values("stint_ID")
    )

    if player_bref.empty and player_war.empty:
        return None

    entry: dict = {"year": current_year, "team": None, "league": None}

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
            # Extended counting stats. bref calls GIDP "GDP" — fall back if
            # the column name differs by pybaseball version.
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


_BATCH_SIZE = 100


def _update_batters(current_year: int) -> tuple[int, int, list[int]]:
    log.info("Fetching batting_stats_bref for current season...")
    bref_df = data_service._batting_bref(current_year)

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

    with connection.get_session() as db:
        player_ids = crud.get_all_player_ids(db)
    log.info(f"{len(player_ids)} batters in database (batch size: {_BATCH_SIZE})")

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
                entry = _build_current_batter_entry(
                    player_id, bref_df, bwar_current, current_year
                )
                if entry is None:
                    skipped += 1
                    continue
                batch_entries.append((player_id, entry))
            except Exception as exc:
                log.error(f"batter {player_id} FAILED: {exc}")
                failed.append(player_id)

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

def _build_current_pitcher_entry(player_id: int, bref_df, bwar_current, current_year: int) -> dict | None:
    """Build a pitcher_seasons row for the current year, or None if no data."""
    # pitching_stats_bref stores mlbID as STRING, unlike batting.
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
        player_id,
        current_year,
        player_war,
        bref_df,
    )


def _update_pitchers(current_year: int) -> tuple[int, int, list[int]]:
    log.info("Fetching pitching_stats_bref for current season...")
    bref_df = data_service._pitching_bref(current_year)

    log.info("Fetching bwar_pitch...")
    bwar_df = data_service._bwar_pitch_all()
    # Same memory pattern as batters — slice + detach + evict cache.
    bwar_current = bwar_df[bwar_df["year_ID"] == current_year].copy()
    del bwar_df
    data_service._store.pop("bwar_pitch_all", None)
    gc.collect()

    with connection.get_session() as db:
        pitcher_ids = crud.get_all_pitcher_ids(db)
    log.info(f"{len(pitcher_ids)} pitchers in database (batch size: {_BATCH_SIZE})")

    updated = 0
    skipped = 0
    failed: list[int] = []

    for start in range(0, len(pitcher_ids), _BATCH_SIZE):
        batch_ids = pitcher_ids[start:start + _BATCH_SIZE]
        batch_entries: list[tuple[int, dict]] = []

        for player_id in batch_ids:
            try:
                entry = _build_current_pitcher_entry(
                    player_id, bref_df, bwar_current, current_year
                )
                if entry is None:
                    skipped += 1
                    continue
                batch_entries.append((player_id, entry))
            except Exception as exc:
                log.error(f"pitcher {player_id} FAILED: {exc}")
                failed.append(player_id)

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
# pybaseball.standings() returns 6 dataframes for years >= 1969 (one per
# division), in the order AL East / AL Central / AL West / NL East / NL Central
# / NL West. Each row has a team-name column ("Tm") sometimes annotated with
# trailing markers like "(W)" or "(WC)" for division winners / wild cards.
# We strip those, look the cleaned name up against the team-name → team_id map
# already in team_seasons (built from the historical Lahman load), and upsert.
_STANDINGS_LAYOUT: list[tuple[str, str]] = [
    ("AL", "E"), ("AL", "C"), ("AL", "W"),
    ("NL", "E"), ("NL", "C"), ("NL", "W"),
]
_NAME_MARKER_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _build_team_name_map() -> dict[str, tuple[str, str]]:
    """Build {team_name → (team_id, franch_id)} from team_seasons. Most-recent
    year wins so relocated franchises map to their current name."""
    mapping: dict[str, tuple[str, str]] = {}
    with connection.get_session() as db:
        rows = (
            db.query(TeamSeason.team_name, TeamSeason.team_id, TeamSeason.franch_id, TeamSeason.year)
              .order_by(TeamSeason.year.desc())
              .all()
        )
        for r in rows:
            if r.team_name and r.team_name not in mapping:
                mapping[r.team_name] = (r.team_id, r.franch_id)
    return mapping


def _update_standings(current_year: int) -> tuple[int, int]:
    """Refresh current-season standings via pybaseball. Upserts into
    team_seasons. Returns (teams_updated, lookup_failures).
    """
    log.info(f"Fetching pybaseball.standings({current_year}) ...")
    try:
        divisions = pybaseball.standings(current_year)
    except Exception as exc:
        log.error(f"standings fetch failed: {exc}")
        return 0, 0

    name_to_team = _build_team_name_map()
    if not name_to_team:
        log.warning("team_seasons is empty — cannot map team names; skipping standings refresh")
        return 0, 0

    rows_to_save: list[dict] = []
    failed_lookups: list[str] = []

    for div_idx, df in enumerate(divisions):
        if div_idx >= len(_STANDINGS_LAYOUT):
            continue
        if df is None or df.empty or len(df.columns) == 0:
            continue
        league, division = _STANDINGS_LAYOUT[div_idx]
        name_col = "Tm" if "Tm" in df.columns else df.columns[0]

        for rank_idx, row in enumerate(df.itertuples(index=False), 1):
            row_dict = row._asdict()
            raw_name = str(row_dict.get(name_col) or "").strip()
            clean_name = _NAME_MARKER_RE.sub("", raw_name).strip()
            if not clean_name:
                continue

            entry = name_to_team.get(clean_name)
            if entry is None:
                failed_lookups.append(clean_name)
                continue
            team_id, franch_id = entry

            try:
                w = int(row_dict.get("W") or 0)
                l = int(row_dict.get("L") or 0)
            except (TypeError, ValueError):
                w = l = 0
            win_pct = round(w / (w + l), 3) if (w + l) > 0 else None

            rows_to_save.append({
                "year":      current_year,
                "team_id":   team_id,
                "franch_id": franch_id,
                "team_name": clean_name,
                "league":    league,
                "division":  division,
                "rank":      rank_idx,
                "G":         w + l,
                "W":         w,
                "L":         l,
                "win_pct":   win_pct,
            })

    if rows_to_save:
        with connection.get_session() as db:
            crud.save_team_seasons(db, rows_to_save)

    log.info(f"standings: updated {len(rows_to_save)} teams, {len(failed_lookups)} unmatched names")
    if failed_lookups:
        log.warning(f"unmatched team names: {failed_lookups}")

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
    """Refresh per-game logs for every player with a current-season row.

    Hits MLB Stats API once per player via data_service.fetch_and_save_*
    (idempotent upsert), with a 0.2s sleep between calls to stay under
    rate limits. Returns counts the API status endpoint surfaces:
        batters_processed   — batters whose fetch+save didn't throw
        pitchers_processed  — pitchers whose fetch+save didn't throw
        batter_games_saved  — total batting game rows upserted
        pitcher_games_saved — total pitching game rows upserted
        batters_failed      — batters whose call threw
        pitchers_failed     — pitchers whose call threw
    """
    bat_ids = _ids_with_current_season(PlayerSeason,  current_year)
    pit_ids = _ids_with_current_season(PitcherSeason, current_year)
    log.info(
        f"  active batters: {len(bat_ids)}, active pitchers: {len(pit_ids)} "
        f"(delay {_GAMELOG_SLEEP_SECONDS}s between players)"
    )

    bat_processed = 0
    bat_saved     = 0
    bat_failed    = 0
    for i, pid in enumerate(bat_ids, 1):
        try:
            bat_saved += data_service.fetch_and_save_batting_gamelogs(pid, current_year)
            bat_processed += 1
        except Exception as exc:
            bat_failed += 1
            log.error(f"  batting gamelog failed for {pid}: {exc}")
        _time.sleep(_GAMELOG_SLEEP_SECONDS)
        if i % _GAMELOG_LOG_EVERY == 0 or i == len(bat_ids):
            log.info(
                f"  batting gamelogs: {i}/{len(bat_ids)} "
                f"(processed={bat_processed}, failed={bat_failed}, rows={bat_saved})"
            )

    pit_processed = 0
    pit_saved     = 0
    pit_failed    = 0
    for i, pid in enumerate(pit_ids, 1):
        try:
            pit_saved += data_service.fetch_and_save_pitching_gamelogs(pid, current_year)
            pit_processed += 1
        except Exception as exc:
            pit_failed += 1
            log.error(f"  pitching gamelog failed for {pid}: {exc}")
        _time.sleep(_GAMELOG_SLEEP_SECONDS)
        if i % _GAMELOG_LOG_EVERY == 0 or i == len(pit_ids):
            log.info(
                f"  pitching gamelogs: {i}/{len(pit_ids)} "
                f"(processed={pit_processed}, failed={pit_failed}, rows={pit_saved})"
            )

    return {
        "batters_processed":   bat_processed,
        "pitchers_processed":  pit_processed,
        "batter_games_saved":  bat_saved,
        "pitcher_games_saved": pit_saved,
        "batters_failed":      bat_failed,
        "pitchers_failed":     pit_failed,
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
