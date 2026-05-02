"""Nightly update — refresh current-season stats for every player in the database.

Designed to run as a Railway cron job (e.g., daily at 04:00 UTC after
Baseball Reference publishes the previous day's data).

Phase 1 (batters): fetches batting_stats_bref + bwar_bat once and upserts
the current-year row in player_seasons for every player in the database.

Phase 2 (pitchers): fetches pitching_stats_bref + bwar_pitch once and upserts
the current-year row in pitcher_seasons for every pitcher in the database.
"""

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

import data_service                                   # noqa: E402
from database import connection, crud                 # noqa: E402

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
            **data_service._batting_derived(br),
        })

    return entry


def _update_batters(current_year: int) -> tuple[int, int, list[int]]:
    log.info("Fetching batting_stats_bref for current season...")
    bref_df = data_service._batting_bref(current_year)

    log.info("Fetching bwar_bat...")
    bwar_df = data_service._bwar_bat_all()
    bwar_current = bwar_df[bwar_df["year_ID"] == current_year]

    with connection.get_session() as db:
        player_ids = crud.get_all_player_ids(db)
    log.info(f"{len(player_ids)} batters in database")

    updated = 0
    skipped = 0
    failed: list[int] = []
    for player_id in player_ids:
        try:
            entry = _build_current_batter_entry(player_id, bref_df, bwar_current, current_year)
            if entry is None:
                skipped += 1
                continue
            with connection.get_session() as db:
                crud.save_player_seasons(db, player_id, [entry])
            updated += 1
        except Exception as exc:
            log.error(f"batter {player_id} FAILED: {exc}")
            failed.append(player_id)

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
    bwar_current = bwar_df[bwar_df["year_ID"] == current_year]

    with connection.get_session() as db:
        pitcher_ids = crud.get_all_pitcher_ids(db)
    log.info(f"{len(pitcher_ids)} pitchers in database")

    updated = 0
    skipped = 0
    failed: list[int] = []
    for player_id in pitcher_ids:
        try:
            entry = _build_current_pitcher_entry(player_id, bref_df, bwar_current, current_year)
            if entry is None:
                skipped += 1
                continue
            with connection.get_session() as db:
                crud.save_pitcher_seasons(db, player_id, [entry])
            updated += 1
        except Exception as exc:
            log.error(f"pitcher {player_id} FAILED: {exc}")
            failed.append(player_id)

    return updated, skipped, failed


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
    log.info(
        f"Batters  — updated: {bat_updated}, skipped: {bat_skipped}, failed: {len(bat_failed)}"
    )
    log.info(
        f"Pitchers — updated: {pit_updated}, skipped: {pit_skipped}, failed: {len(pit_failed)}"
    )
    if bat_failed:
        log.error(f"Failed batter IDs: {bat_failed}")
    if pit_failed:
        log.error(f"Failed pitcher IDs: {pit_failed}")


if __name__ == "__main__":
    main()
