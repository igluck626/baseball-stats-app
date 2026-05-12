"""Nightly update — refresh current-season stats for every player in the database.

Designed to run as a Railway cron job (e.g., daily at 04:00 UTC after
Baseball Reference publishes the previous day's data).

Phase 1 (batters): fetches batting_stats_bref + bwar_bat once and upserts
the current-year row in player_seasons for every player in the database.

Phase 2 (pitchers): fetches pitching_stats_bref + bwar_pitch once and upserts
the current-year row in pitcher_seasons for every pitcher in the database.

Phase 3 (standings): fetches the MLB Stats API standings endpoint and upserts the
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
permanently overwrites the current-season standings rows with
canonical Lahman numbers; the cutoff in lahman_load.py is "current year"
so the rollover is automatic on the next run after Lahman publishes.
"""

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
            # TB = H + 2·doubles + 3·triples + 4·HR. Computed from the
            # bref row's own component values so the nightly upserts
            # carry TB even before the init_db backfill runs.
            "TB":      _safe_tb(br),
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


# MLB Stats API division-id → our (league, division-letter) tuple.
# Stable across seasons; cross-checked against
# https://statsapi.mlb.com/api/v1/divisions
_MLB_DIVISION_ID_TO_LEAGUE_DIV: dict[int, tuple[str, str]] = {
    201: ("AL", "E"),  # AL East
    202: ("AL", "C"),  # AL Central
    200: ("AL", "W"),  # AL West
    204: ("NL", "E"),  # NL East
    205: ("NL", "C"),  # NL Central
    203: ("NL", "W"),  # NL West
}


# MLB Stats API numeric team id → Lahman team_id (the value stored in
# team_seasons.team_id today). The standings endpoint returns only the
# team nickname in `team.name` ("Rays", "Yankees", "Athletics"), so the
# previous name-based lookup against team_seasons.team_name failed for
# 29/30 teams every nightly run — only "Athletics" matched by accident
# because both surfaces happen to use the bare nickname for that club.
# The numeric MLB id, by contrast, is permanent and unambiguous.
#
# Mappings cross-checked against the live /teams?sportId=1 payload.
# Most match the lowercased Lahman teamCode 1:1; the two exceptions
# (108 Angels, 133 Athletics) are pinned to the codes team_seasons
# actually uses in current seasons (LAA + ATH).
_MLB_TEAM_ID_TO_LAHMAN_TEAM_ID: dict[int, str] = {
    109: "ARI",   144: "ATL",   110: "BAL",   111: "BOS",
    145: "CHA",   112: "CHN",   113: "CIN",   114: "CLE",
    115: "COL",   116: "DET",   117: "HOU",   118: "KCA",
    108: "LAA",   119: "LAN",   146: "MIA",   158: "MIL",
    142: "MIN",   147: "NYA",   121: "NYN",   133: "ATH",
    143: "PHI",   134: "PIT",   135: "SDN",   137: "SFN",
    136: "SEA",   138: "SLN",   139: "TBA",   140: "TEX",
    141: "TOR",   120: "WAS",
}


def _split_record(splits: list[dict], wanted_type: str) -> tuple[int | None, int | None]:
    """Pull (wins, losses) from a splitRecords array for the given type
    ("lastTen" / "home" / "away" / etc.). Returns (None, None) when
    the type isn't present (pre-season, or the API trimmed it)."""
    for split in splits or []:
        if split.get("type") == wanted_type:
            try:
                return int(split.get("wins") or 0), int(split.get("losses") or 0)
            except (TypeError, ValueError):
                return None, None
    return None, None


def _update_standings(current_year: int) -> tuple[int, int]:
    """Refresh current-season standings via the MLB Stats API and upsert
    into team_seasons. Returns (teams_updated, lookup_failures).

    Source: https://statsapi.mlb.com/api/v1/standings?leagueId={103|104}&season=YYYY.
    Pulls everything in the standings payload — W/L/win_pct plus the
    new dynamic fields (streak, L10, home/away splits, run differential,
    games back, wild-card games back, clinch indicators, magic / elim
    numbers). Maps MLB API team.name to our team_id via the existing
    team_seasons name map, falling back gracefully on rebrand-era
    mismatches.
    """
    team_meta = _build_team_meta_by_id()
    if not team_meta:
        log.warning("team_seasons is empty — cannot resolve franch_id / team_name; skipping standings refresh")
        return 0, 0

    rows_to_save: list[dict] = []
    failed_lookups: list[str] = []
    # Hit each league separately. leagueId=103 = AL, 104 = NL.
    for league_id in (103, 104):
        try:
            payload = data_service._mlb_get_json(
                "standings",
                {"leagueId": league_id, "season": current_year, "standingsTypes": "regularSeason"},
            )
        except Exception as exc:
            log.error(f"standings fetch failed for leagueId={league_id}: {exc}", exc_info=True)
            continue

        for record in payload.get("records") or []:
            div_id = (record.get("division") or {}).get("id")
            mapped = _MLB_DIVISION_ID_TO_LEAGUE_DIV.get(div_id)
            if mapped is None:
                # Unknown division (post-season, all-star, future re-org)
                # — skip rather than risk writing a row with no division.
                continue
            league, division = mapped

            team_records = record.get("teamRecords") or []
            # The API already sorts within a division by divisionRank;
            # use the row index as our `rank` so leaders show up first.
            for rank_idx, t in enumerate(team_records, 1):
                team        = t.get("team") or {}
                mlb_team_id = team.get("id")
                short_name  = (team.get("name") or "").strip()
                # MLB-numeric-id → Lahman team_id. Stable, doesn't
                # depend on string matching against the short
                # nickname the standings endpoint returns.
                team_id = _MLB_TEAM_ID_TO_LAHMAN_TEAM_ID.get(mlb_team_id)
                if team_id is None:
                    log.warning(
                        f"standings: no Lahman mapping for MLB team id={mlb_team_id} "
                        f"({short_name!r}) — add to _MLB_TEAM_ID_TO_LAHMAN_TEAM_ID"
                    )
                    failed_lookups.append(short_name or f"mlb_id={mlb_team_id}")
                    continue
                meta = team_meta.get(team_id)
                if meta is None:
                    log.warning(
                        f"standings: Lahman team_id={team_id!r} not in team_seasons "
                        f"(MLB id={mlb_team_id}, short_name={short_name!r})"
                    )
                    failed_lookups.append(team_id)
                    continue
                franch_id, full_team_name = meta

                wins   = int(t.get("wins") or 0)
                losses = int(t.get("losses") or 0)
                wp_raw = t.get("winningPercentage")
                try:
                    win_pct = float(wp_raw) if wp_raw is not None else (
                        round(wins / (wins + losses), 3) if (wins + losses) > 0 else None
                    )
                except (TypeError, ValueError):
                    win_pct = None

                splits = (t.get("records") or {}).get("splitRecords") or []
                last10_w, last10_l = _split_record(splits, "lastTen")
                home_w,   home_l   = _split_record(splits, "home")
                away_w,   away_l   = _split_record(splits, "away")

                streak     = t.get("streak") or {}
                rows_to_save.append({
                    "year":      current_year,
                    "team_id":   team_id,
                    "franch_id": franch_id,
                    # Prefer the full city+nickname from team_seasons
                    # ("Tampa Bay Rays") over the MLB API's nickname-
                    # only short_name ("Rays") so the iOS standings
                    # card keeps reading the same as the historical
                    # rows below it.
                    "team_name": full_team_name or short_name,
                    "league":    league,
                    "division":  division,
                    "rank":      rank_idx,
                    "G":         wins + losses,
                    "W":         wins,
                    "L":         losses,
                    "win_pct":   win_pct,

                    # Run differential pair — both columns already exist
                    # on team_seasons; the previous nightly path didn't
                    # populate them for current-season rows.
                    "runs_scored":  t.get("runsScored"),
                    "runs_allowed": t.get("runsAllowed"),

                    # Live fields (new this commit).
                    "streak_code":          streak.get("streakCode"),
                    "last_ten_w":           last10_w,
                    "last_ten_l":           last10_l,
                    "home_w":               home_w,
                    "home_l":               home_l,
                    "away_w":               away_w,
                    "away_l":               away_l,
                    "games_back":           t.get("gamesBack"),
                    "wild_card_games_back": t.get("wildCardGamesBack"),
                    "clinch_indicator":     t.get("clinchIndicator"),
                    "division_leader":      bool(t.get("divisionLeader") or False),
                    "clinched":             bool(t.get("clinched") or False),
                    "magic_number":         t.get("magicNumber"),
                    "elimination_number":   t.get("eliminationNumber"),
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
