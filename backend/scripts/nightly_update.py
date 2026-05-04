"""Nightly update — refresh current-season stats for every player in the database.

Designed to run as a Railway cron job (e.g., daily at 04:00 UTC after
Baseball Reference publishes the previous day's data).

Phase 1 (batters): fetches batting_stats_bref + bwar_bat once and upserts
the current-year row in player_seasons for every player in the database.

Phase 2 (pitchers): fetches pitching_stats_bref + bwar_pitch once and upserts
the current-year row in pitcher_seasons for every pitcher in the database.

Phase 3 (standings): fetches pybaseball.standings() and upserts the
current-season row in team_seasons for each team.

Seasonal workflow
-----------------
Pybaseball is the source of truth ONLY for the in-flight current season.
After each season ends (typically late October), the Lahman archive is
re-released with the just-completed year. Re-running lahman_load.py
permanently overwrites the pybaseball-sourced current-season rows with
canonical Lahman numbers; the cutoff in lahman_load.py is "current year"
so the rollover is automatic on the next run after Lahman publishes.
"""

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
from database.models import TeamSeason                # noqa: E402

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
    log.info(
        f"Batters   — updated: {bat_updated}, skipped: {bat_skipped}, failed: {len(bat_failed)}"
    )
    log.info(
        f"Pitchers  — updated: {pit_updated}, skipped: {pit_skipped}, failed: {len(pit_failed)}"
    )
    log.info(
        f"Standings — updated: {standings_updated}, unmatched names: {standings_failed}"
    )
    if bat_failed:
        log.error(f"Failed batter IDs: {bat_failed}")
    if pit_failed:
        log.error(f"Failed pitcher IDs: {pit_failed}")


if __name__ == "__main__":
    main()
