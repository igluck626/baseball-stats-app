"""Nightly game-log reconciliation — assert that the per-game rows and the
season row still tell the same story, and record what they say when they don't.

WHY THIS EXISTS
---------------
`player_seasons.H` comes from BDL's `season_stats`; `batting_gamelogs` comes
from BDL's per-game `/stats`. Until 2026-08-26 a rollup pass forced the two to
agree by overwriting the season row's H with the game-log sum, which meant the
season row could contradict its OWN batting average and no one could tell.
Removing H from that rollup fixed the contradiction but did NOT fix the
underlying gap — it made it visible. This is what looks at it.

The gap it finds is real and upstream: a game-log row is written once, hours
after the game, and never re-read (`_update_gamelogs` fetches only yesterday
and today). An official scorer who reverses a hit/error call the next morning
changes BDL's number forever after; ours is frozen at what BDL said that night.
The signature is AB unchanged with H and RBI moving by one, in either
direction. Brice Turang's 2026-08-06 line is the worked example: we hold
1-for-4, BDL and Baseball-Reference both now say 0-for-4.

WHAT IT REPORTS, AND WHY IN THESE PIECES
----------------------------------------
Two numbers that must not be added together:

  * REACHABLE disagreements — the game sits under a BDL game id, so re-pulling
    that date genuinely repairs it. Actionable.
  * UNREACHABLE disagreements — some of the player's rows sit under MLB Stats
    API gamePks that BDL has never heard of. `save_bdl_gamelogs_for_date`
    upserts on `(player_id, game_id)`, so no BDL re-pull can ever reach those
    rows. Reporting them mixed in with the actionable ones would give this
    check a permanent non-zero floor, and a check with a permanent floor is a
    check people mute.

`unreachable_games` is tracked league-wide and independently of whether anyone
disagrees, because a RISE in it means the gamePk drift path is getting worse —
that is its own bug and we want to see it move.

THE TWO-RUN RULE
----------------
A disagreement counts as `confirmed` only if the same player disagreed on the
previous run too. During a live game BDL's season row advances while the logs
(finals only) do not, so a gap can be nothing but a game in the 7th inning. A
real one persists.
"""

import datetime
import json
import logging
import time
from typing import Optional

from sqlalchemy import text as _sa_text

import data_service
from database import connection
from database.models import GamelogReconFinding, GamelogReconRun

log = logging.getLogger(__name__)

# Cap on how many players tier 2 will attribute in one run, so a pathological
# night (a broken ingest flagging hundreds) can't turn into hundreds of BDL
# requests. Whatever is dropped is REPORTED, never silently truncated — see
# `tier2_skipped_for_cap` in the returned dict.
_MAX_TIER2_PLAYERS = 60

# BDL pages at 25 by default. That silently truncated a batched season_stats
# call during the investigation and made 9 players look unmapped when only 1
# was — always pass this, on every paginated BDL route.
_PER_PAGE = 100


def _bdl_regular_season_game_ids(season: int) -> set[str]:
    """Every BDL game id for `season`'s REGULAR season, as strings.

    Serves two purposes and the second is the non-obvious one:

      1. The reachability oracle. A game id of ours that is absent from this
         set cannot be repaired by any BDL re-pull. Membership is the exact
         test — do NOT fingerprint provenance by id length. BDL's own ids run
         7 AND 8 digits (5059502, 10558770 are both BDL), so a digit-count
         heuristic classifies real BDL games as unreachable. Verified against
         2026: 379 six-digit MLB gamePks absent, all 1,989 BDL-shaped ids
         present.

      2. The spring-training filter. `seasons[]=<year>` on BDL's /stats route
         INCLUDES spring training, and the `dates[]` filter is silently ignored
         (same class as the documented `game_id`-singular trap). Intersecting
         per-game stat rows with this set drops exhibition games without
         needing a season_type field the /stats row doesn't carry.
    """
    ids: set[str] = set()
    cursor = None
    while True:
        params: dict = {"seasons[]": [season], "per_page": _PER_PAGE}
        if cursor is not None:
            params["cursor"] = cursor
        payload = data_service._bdl_get_json("games", params)
        for game in payload.get("data") or []:
            if game.get("season_type") == "regular" and not game.get("postseason"):
                ids.add(str(game.get("id")))
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
    return ids


# Tier 1. One GROUP BY over the season's game logs against the season rows —
# no API calls, ~60ms on 2026's 40k rows. Column identifiers are double-quoted
# because the schema stores them in original case (Postgres folds unquoted
# identifiers to lowercase — see connection.py).
_TIER1_SQL = """
WITH gl AS (
    SELECT player_id, SUM(COALESCE("H", 0)) AS h_sum
    FROM batting_gamelogs
    WHERE season = :season
    GROUP BY player_id
)
SELECT gl.player_id, gl.h_sum, COALESCE(ps."H", 0) AS season_h
FROM gl
JOIN player_seasons ps
  ON ps.player_id = gl.player_id AND ps.year = :season
WHERE COALESCE(ps."AB", 0) > 0
  AND gl.h_sum <> COALESCE(ps."H", 0)
ORDER BY ABS(gl.h_sum - COALESCE(ps."H", 0)) DESC, gl.player_id
"""

_PLAYERS_CHECKED_SQL = """
SELECT COUNT(*)
FROM player_seasons ps
WHERE ps.year = :season AND COALESCE(ps."AB", 0) > 0
  AND EXISTS (SELECT 1 FROM batting_gamelogs bg
              WHERE bg.player_id = ps.player_id AND bg.season = :season)
"""

_PLAYER_GAME_IDS_SQL = """
SELECT game_id, game_date, COALESCE("H", 0) AS h
FROM batting_gamelogs
WHERE player_id = :pid AND season = :season
"""

_ALL_GAME_IDS_SQL = """
SELECT game_id, COUNT(*) AS rows
FROM batting_gamelogs
WHERE season = :season
GROUP BY game_id
"""


def _tier2_bdl_lines(bdl_id: int, season: int, regular_ids: set[str]) -> tuple[dict, int]:
    """BDL's per-game hit totals for one player, keyed by game id (string).

    Returns `(lines, requests_made)`. Only regular-season games survive — see
    `_bdl_regular_season_game_ids` for why the intersection is the filter.
    """
    lines: dict[str, int] = {}
    cursor = None
    requests = 0
    while True:
        params: dict = {
            "player_ids[]": [bdl_id],
            "seasons[]":    [season],
            "per_page":     _PER_PAGE,
        }
        if cursor is not None:
            params["cursor"] = cursor
        payload = data_service._bdl_get_json("stats", params)
        requests += 1
        for row in payload.get("data") or []:
            gid = str(row.get("game_id"))
            if gid in regular_ids and row.get("at_bats") is not None:
                lines[gid] = int(row.get("hits") or 0)
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
    return lines, requests


def run_reconciliation(season: Optional[int] = None) -> dict:
    """Run both tiers for `season`, persist a run row plus one finding per
    disagreeing player, and return the summary dict.

    NEVER raises. The nightly calls this as its last phase and a check that can
    break the nightly is worse than no check — any failure is caught, stamped
    on the run row's `error` column, and returned in the summary.
    """
    started = time.time()
    if season is None:
        season = data_service._current_year()
    run_at = datetime.datetime.utcnow()

    summary: dict = {
        "status": "ok", "season": season, "run_at": run_at.isoformat() + "Z",
        "players_checked": 0, "disagreeing": 0,
        "disagreeing_reachable": 0, "disagreeing_unreachable": 0,
        "confirmed": 0, "magnitude": {}, "max_abs_gap": 0,
        "unreachable_games": 0, "unreachable_rows": 0, "bdl_game_ids_seen": 0,
        "tier2_players": 0, "tier2_attributed": 0, "tier2_requests": 0,
        "tier2_skipped_for_cap": 0,
        "value_gap_players": 0, "coverage_gap_players": 0,
        "oldest_attributed": None, "median_age_days": None, "error": None,
    }

    if not connection.db_available():
        summary["status"] = "no_db"
        return summary

    try:
        with connection.get_session() as db:
            summary["players_checked"] = int(
                db.execute(_sa_text(_PLAYERS_CHECKED_SQL), {"season": season}).scalar() or 0
            )
            rows = db.execute(_sa_text(_TIER1_SQL), {"season": season}).fetchall()
            game_rows = db.execute(_sa_text(_ALL_GAME_IDS_SQL), {"season": season}).fetchall()

        findings: list[dict] = []
        for r in rows:
            gap = int(r.h_sum) - int(r.season_h)
            findings.append({
                "player_id": int(r.player_id), "season": season,
                "log_sum_h": int(r.h_sum), "season_h": int(r.season_h),
                "gap": gap, "reachable": None, "non_bdl_rows": 0,
                "confirmed": False, "diff_games": None, "value_gap": None,
                "coverage_gap": None, "gap_explained": None,
                "game_id": None, "game_date": None,
                "revision_age_days": None, "ours_h": None, "bdl_h": None,
            })
        summary["disagreeing"] = len(findings)
        summary["max_abs_gap"] = max((abs(f["gap"]) for f in findings), default=0)
        magnitude: dict[str, int] = {}
        for f in findings:
            key = str(f["gap"])
            magnitude[key] = magnitude.get(key, 0) + 1
        summary["magnitude"] = magnitude

        # --- reachability, league-wide and per finding -----------------------
        regular_ids = _bdl_regular_season_game_ids(season)
        summary["bdl_game_ids_seen"] = len(regular_ids)
        unreachable_ids = {str(g.game_id) for g in game_rows if str(g.game_id) not in regular_ids}
        summary["unreachable_games"] = len(unreachable_ids)
        summary["unreachable_rows"] = int(
            sum(g.rows for g in game_rows if str(g.game_id) in unreachable_ids)
        )

        # --- the two-run rule ------------------------------------------------
        with connection.get_session() as db:
            prev = (
                db.query(GamelogReconRun)
                  .filter(GamelogReconRun.season == season,
                          GamelogReconRun.error.is_(None))
                  .order_by(GamelogReconRun.id.desc())
                  .first()
            )
            prev_ids: set[int] = set()
            if prev is not None:
                prev_ids = {
                    f.player_id for f in
                    db.query(GamelogReconFinding)
                      .filter(GamelogReconFinding.run_id == prev.id)
                      .all()
                }
            bdl_ids: dict[int, Optional[int]] = {}
            if findings:
                pid_list = [f["player_id"] for f in findings]
                bdl_ids = {
                    int(row[0]): row[1] for row in db.execute(
                        _sa_text("SELECT player_id, bdl_id FROM players "
                                 "WHERE player_id = ANY(:pids)"),
                        {"pids": pid_list},
                    ).fetchall()
                }
            player_games: dict[int, list] = {}
            for f in findings:
                player_games[f["player_id"]] = db.execute(
                    _sa_text(_PLAYER_GAME_IDS_SQL),
                    {"pid": f["player_id"], "season": season},
                ).fetchall()

        for f in findings:
            own = player_games.get(f["player_id"], [])
            non_bdl = [g for g in own if str(g.game_id) not in regular_ids]
            f["non_bdl_rows"] = len(non_bdl)
            f["reachable"] = not non_bdl
            f["confirmed"] = f["player_id"] in prev_ids
        summary["disagreeing_reachable"] = sum(1 for f in findings if f["reachable"])
        summary["disagreeing_unreachable"] = sum(1 for f in findings if not f["reachable"])
        summary["confirmed"] = sum(1 for f in findings if f["confirmed"] and f["reachable"])

        # --- tier 2: attribute each reachable gap to its game ---------------
        targets = [f for f in findings if f["reachable"] and bdl_ids.get(f["player_id"])]
        if len(targets) > _MAX_TIER2_PLAYERS:
            summary["tier2_skipped_for_cap"] = len(targets) - _MAX_TIER2_PLAYERS
            targets = targets[:_MAX_TIER2_PLAYERS]
        summary["tier2_players"] = len(targets)
        ages: list[int] = []
        for f in targets:
            try:
                lines, reqs = _tier2_bdl_lines(
                    int(bdl_ids[f["player_id"]]), season, regular_ids,
                )
            except Exception as exc:                      # one player's failure
                log.warning("[recon] tier2 failed for player %s: %s",
                            f["player_id"], exc)
                continue
            summary["tier2_requests"] += reqs
            diffs = [
                g for g in player_games.get(f["player_id"], [])
                if str(g.game_id) in lines and int(g.h) != lines[str(g.game_id)]
            ]
            # Decompose before attributing. A player can carry BOTH a scorer
            # revision and a coverage difference — Vaughn Grissom's 2026 gap
            # was +3 of which exactly +1 came from a revised game, and naming
            # that game as "the" cause would have been a third of the story.
            f["diff_games"]   = len(diffs)
            f["value_gap"]    = sum(int(g.h) - lines[str(g.game_id)] for g in diffs)
            f["coverage_gap"] = f["gap"] - f["value_gap"]
            f["gap_explained"] = f["coverage_gap"] == 0
            if f["value_gap"]:
                summary["value_gap_players"] += 1
            if f["coverage_gap"]:
                summary["coverage_gap_players"] += 1
            # Attribute only when the gap traces to exactly ONE game. Two or
            # more and the single-game columns would be a guess, so they stay
            # null and the finding keeps its aggregate gap.
            if len(diffs) == 1:
                g = diffs[0]
                f["game_id"] = str(g.game_id)
                f["game_date"] = g.game_date
                f["ours_h"] = int(g.h)
                f["bdl_h"] = lines[str(g.game_id)]
                if g.game_date is not None:
                    age = (run_at.date() - g.game_date).days
                    f["revision_age_days"] = age
                    # Only rows the value side fully explains feed the median.
                    # A row with a coverage remainder is not a clean revision
                    # and would bias the window (b) gets sized to.
                    if f["gap_explained"]:
                        ages.append(age)
                summary["tier2_attributed"] += 1
            time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)

        if ages:
            ages.sort()
            mid = len(ages) // 2
            summary["median_age_days"] = float(
                ages[mid] if len(ages) % 2 else (ages[mid - 1] + ages[mid]) / 2
            )
            summary["oldest_attributed"] = str(
                min(f["game_date"] for f in findings if f["game_date"] is not None)
            )

        summary["duration_seconds"] = round(time.time() - started, 2)
        _persist(run_at, season, summary, findings)
        log.info(
            "[recon] season=%s checked=%d disagreeing=%d "
            "(reachable=%d unreachable=%d confirmed=%d) "
            "unreachable_games=%d attributed=%d value_gaps=%d "
            "coverage_gaps=%d median_age=%s",
            season, summary["players_checked"], summary["disagreeing"],
            summary["disagreeing_reachable"], summary["disagreeing_unreachable"],
            summary["confirmed"], summary["unreachable_games"],
            summary["tier2_attributed"], summary["value_gap_players"],
            summary["coverage_gap_players"], summary["median_age_days"],
        )
        if summary["confirmed"]:
            log.warning(
                "[recon] %d player(s) disagree with their game logs across "
                "consecutive runs — re-pull the attributed dates",
                summary["confirmed"],
            )
        return summary

    except Exception as exc:
        log.exception("[recon] reconciliation FAILED (non-fatal): %s", exc)
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["duration_seconds"] = round(time.time() - started, 2)
        try:
            _persist(run_at, season, summary, [])
        except Exception:
            log.exception("[recon] could not persist the failed run either")
        return summary


def _persist(run_at, season: int, summary: dict, findings: list[dict]) -> None:
    """Write the run row and its findings. Own session so a persistence
    failure surfaces separately from a computation failure."""
    with connection.get_session() as db:
        run = GamelogReconRun(
            run_at=run_at, season=season,
            players_checked=summary["players_checked"],
            disagreeing=summary["disagreeing"],
            disagreeing_reachable=summary["disagreeing_reachable"],
            disagreeing_unreachable=summary["disagreeing_unreachable"],
            confirmed=summary["confirmed"],
            magnitude_json=json.dumps(summary["magnitude"], sort_keys=True),
            max_abs_gap=summary["max_abs_gap"],
            unreachable_games=summary["unreachable_games"],
            unreachable_rows=summary["unreachable_rows"],
            bdl_game_ids_seen=summary["bdl_game_ids_seen"],
            tier2_players=summary["tier2_players"],
            tier2_attributed=summary["tier2_attributed"],
            tier2_requests=summary["tier2_requests"],
            value_gap_players=summary["value_gap_players"],
            coverage_gap_players=summary["coverage_gap_players"],
            oldest_attributed=summary["oldest_attributed"],
            median_age_days=summary["median_age_days"],
            duration_seconds=summary.get("duration_seconds"),
            error=summary["error"],
        )
        db.add(run)
        db.flush()                      # populate run.id before the children
        for f in findings:
            db.add(GamelogReconFinding(
                run_id=run.id, player_id=f["player_id"], season=f["season"],
                log_sum_h=f["log_sum_h"], season_h=f["season_h"], gap=f["gap"],
                reachable=f["reachable"], non_bdl_rows=f["non_bdl_rows"],
                confirmed=f["confirmed"], diff_games=f["diff_games"],
                value_gap=f["value_gap"], coverage_gap=f["coverage_gap"],
                gap_explained=f["gap_explained"], game_id=f["game_id"],
                game_date=f["game_date"],
                revision_age_days=f["revision_age_days"],
                ours_h=f["ours_h"], bdl_h=f["bdl_h"],
            ))
        db.commit()
        summary["run_id"] = run.id
