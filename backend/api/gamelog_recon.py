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

BOTH SIDES, AND WHY PITCHING NEEDS FIVE FIELDS
----------------------------------------------
Batting reconciles one field, H. Pitching reconciles five — H, ER, BB, SO, HR —
because they do NOT move together. The worked pitching example is Troy Melton's
2026-08-15: an official scorer ruled a hit an error, so H fell by one AND two
runs became unearned (ER 3 -> 1) while R, BB, SO and HR did not move at all. A
single combined pitching number would have shown "off by 3" and hidden that it
was two different consequences of one ruling.

That same game is `5059617`, where the batting side independently attributed
Andrew Benintendi's revision. One scorer decision, both halves of it — which is
why the two sides share a run and a reachability oracle rather than being two
unrelated checks.

R AND HBP ARE DELIBERATELY ABSENT from the pitching field list. They are the
only members of `_PITCHING_COUNTING_FIELDS`, so the nightly rollup OVERWRITES
them from the very game logs this check reads — they agree with themselves by
construction and would report 0 forever. Verified against 2026: R mismatches
are exactly 0 out of 772 pitchers, against 91 for H. Their absence here is the
point, not an oversight; do NOT "fix" it by adding them.

Unlike the batting H case, this is CORRECT: BDL genuinely ships neither
`pitching_r` nor `pitching_hbp` (verified against the live payload), and
neither feeds ERA or WHIP, so summing them from the logs contradicts nothing.

A ROUNDING TRAP, FOR WHOEVER EXTENDS THIS
-----------------------------------------
Do not test the pitcher season row by re-deriving ER from ERA * IP / 9, or
H + BB from WHIP * IP, without a tolerance. `_parse_bdl_pitcher_row` stores
ERA/WHIP at 2dp, so at 150 IP the rounding alone moves `WHIP * IP` by ±0.75.
A naive equality test reported 36 of 485 pitchers as split; with a tolerance of
0.005 * IP it fell to 3, and all 3 turned out to be TRUNCATION rather than
rounding (Janson Junk's true WHIP is 1.31507, stored as 1.31, not 1.32). With a
truncation-aware tolerance of 0.01 * IP the count is 0 — the pitcher season row
is entirely self-consistent, 2020 through 2026. That is why this check compares
game-log SUMS against stored COLUMNS and never against a re-derived rate.

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


# What each side reconciles. Identifiers here are module constants, never
# request input, so interpolating them into SQL is safe.
#
# `bdl_key` is the field on BDL's per-game /stats row. One fetch carries BOTH
# sides' fields, so a two-way player costs one request, not two.
_RECON_SPECS = (
    {"side": "bat", "stat": "H",  "gamelog": "batting_gamelogs",
     "season_table": "player_seasons",  "qualifier": "AB", "bio": "players",
     "bdl_key": "hits"},
    {"side": "pit", "stat": "H",  "gamelog": "pitching_gamelogs",
     "season_table": "pitcher_seasons", "qualifier": "IP", "bio": "pitchers",
     "bdl_key": "p_hits"},
    {"side": "pit", "stat": "ER", "gamelog": "pitching_gamelogs",
     "season_table": "pitcher_seasons", "qualifier": "IP", "bio": "pitchers",
     "bdl_key": "er"},
    {"side": "pit", "stat": "BB", "gamelog": "pitching_gamelogs",
     "season_table": "pitcher_seasons", "qualifier": "IP", "bio": "pitchers",
     "bdl_key": "p_bb"},
    {"side": "pit", "stat": "SO", "gamelog": "pitching_gamelogs",
     "season_table": "pitcher_seasons", "qualifier": "IP", "bio": "pitchers",
     "bdl_key": "p_k"},
    {"side": "pit", "stat": "HR", "gamelog": "pitching_gamelogs",
     "season_table": "pitcher_seasons", "qualifier": "IP", "bio": "pitchers",
     "bdl_key": "p_hr"},
)

# Every gamelog column tier 2 needs per side, so one SELECT serves all stats.
_SIDE_COLUMNS = {"bat": ("H",), "pit": ("H", "ER", "BB", "SO", "HR")}
_SIDE_TABLE   = {"bat": "batting_gamelogs", "pit": "pitching_gamelogs"}


def _tier1_sql(spec: dict) -> str:
    """One GROUP BY over the season's game logs against the season rows — no
    API calls, ~60ms on 2026's 40k batting rows. Column identifiers are
    double-quoted because the schema stores them in original case (Postgres
    folds unquoted identifiers to lowercase — see connection.py)."""
    return """
    WITH gl AS (
        SELECT player_id, SUM(COALESCE("{stat}", 0)) AS log_sum
        FROM {gamelog}
        WHERE season = :season
        GROUP BY player_id
    )
    SELECT gl.player_id, gl.log_sum, COALESCE(ps."{stat}", 0) AS season_value
    FROM gl
    JOIN {season_table} ps
      ON ps.player_id = gl.player_id AND ps.year = :season
    WHERE COALESCE(ps."{qualifier}", 0) > 0
      AND gl.log_sum <> COALESCE(ps."{stat}", 0)
    ORDER BY ABS(gl.log_sum - COALESCE(ps."{stat}", 0)) DESC, gl.player_id
    """.format(**spec)


def _players_checked_sql(spec: dict) -> str:
    return """
    SELECT COUNT(*)
    FROM {season_table} ps
    WHERE ps.year = :season AND COALESCE(ps."{qualifier}", 0) > 0
      AND EXISTS (SELECT 1 FROM {gamelog} bg
                  WHERE bg.player_id = ps.player_id AND bg.season = :season)
    """.format(**spec)


def _player_rows_sql(side: str) -> str:
    cols = ", ".join('COALESCE("%s", 0) AS "%s"' % (c, c) for c in _SIDE_COLUMNS[side])
    return """
    SELECT game_id, game_date, {cols}
    FROM {table}
    WHERE player_id = :pid AND season = :season
    """.format(cols=cols, table=_SIDE_TABLE[side])


def _all_game_ids_sql(side: str) -> str:
    return """
    SELECT game_id, COUNT(*) AS rows
    FROM {table}
    WHERE season = :season
    GROUP BY game_id
    """.format(table=_SIDE_TABLE[side])


def _tier2_bdl_lines(bdl_id: int, season: int, regular_ids: set) -> tuple:
    """BDL's per-game rows for one player, keyed by game id (string).

    Returns `(lines, requests_made)`. The whole stat row is kept, not one
    field, because a single row carries BOTH the batting and the pitching
    numbers — so a two-way player costs one fetch, and a pitcher's five fields
    cost one fetch between them.

    Only regular-season games survive — see `_bdl_regular_season_game_ids` for
    why intersecting with that set IS the spring-training filter.
    """
    lines: dict = {}
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
            if gid in regular_ids:
                lines[gid] = row
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
    return lines, requests


def run_reconciliation(season: Optional[int] = None) -> dict:
    """Run both tiers over every spec in `_RECON_SPECS`, persist a run row plus
    one finding per (player, side, stat), and return the summary dict.

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
        "bat_disagreeing": 0, "pit_disagreeing": 0, "by_stat": {},
        "disagreeing_reachable": 0, "disagreeing_unreachable": 0,
        "confirmed": 0, "magnitude": {}, "max_abs_gap": 0,
        "unreachable_games": 0, "unreachable_rows": 0,
        "unreachable_games_bat": 0, "unreachable_rows_bat": 0,
        "unreachable_games_pit": 0, "unreachable_rows_pit": 0,
        "bdl_game_ids_seen": 0,
        "tier2_players": 0, "tier2_attributed": 0, "tier2_requests": 0,
        "tier2_skipped_for_cap": 0,
        "value_gap_players": 0, "coverage_gap_players": 0,
        "oldest_attributed": None, "median_age_days": None, "error": None,
    }

    if not connection.db_available():
        summary["status"] = "no_db"
        return summary

    try:
        # --- tier 1, per spec -------------------------------------------
        findings: list = []
        checked_seen: dict = {}
        with connection.get_session() as db:
            for spec in _RECON_SPECS:
                key = (spec["season_table"], spec["qualifier"])
                if key not in checked_seen:
                    checked_seen[key] = int(
                        db.execute(_sa_text(_players_checked_sql(spec)),
                                   {"season": season}).scalar() or 0
                    )
                for r in db.execute(_sa_text(_tier1_sql(spec)),
                                    {"season": season}).fetchall():
                    findings.append({
                        "player_id": int(r.player_id), "season": season,
                        "side": spec["side"], "stat": spec["stat"],
                        "log_sum": int(r.log_sum),
                        "season_value": int(r.season_value),
                        "gap": int(r.log_sum) - int(r.season_value),
                        "reachable": None, "non_bdl_rows": 0, "confirmed": False,
                        "diff_games": None, "value_gap": None,
                        "coverage_gap": None, "gap_explained": None,
                        "game_id": None, "game_date": None,
                        "revision_age_days": None,
                        "ours_value": None, "bdl_value": None,
                    })
            # Distinct players across both season tables.
            summary["players_checked"] = sum(checked_seen.values())
            game_rows = {
                side: db.execute(_sa_text(_all_game_ids_sql(side)),
                                 {"season": season}).fetchall()
                for side in ("bat", "pit")
            }

        summary["disagreeing"] = len(findings)
        summary["bat_disagreeing"] = sum(1 for f in findings if f["side"] == "bat")
        summary["pit_disagreeing"] = sum(1 for f in findings if f["side"] == "pit")
        summary["max_abs_gap"] = max((abs(f["gap"]) for f in findings), default=0)
        magnitude: dict = {}
        for f in findings:
            magnitude[str(f["gap"])] = magnitude.get(str(f["gap"]), 0) + 1
        summary["magnitude"] = magnitude

        # --- reachability, league-wide and per finding -------------------
        regular_ids = _bdl_regular_season_game_ids(season)
        summary["bdl_game_ids_seen"] = len(regular_ids)
        all_unreachable: set = set()
        for side, suffix in (("bat", "bat"), ("pit", "pit")):
            unreach = {str(g.game_id) for g in game_rows[side]
                       if str(g.game_id) not in regular_ids}
            summary["unreachable_games_" + suffix] = len(unreach)
            summary["unreachable_rows_" + suffix] = int(
                sum(g.rows for g in game_rows[side] if str(g.game_id) in unreach)
            )
            all_unreachable |= unreach
        summary["unreachable_games"] = len(all_unreachable)
        summary["unreachable_rows"] = (summary["unreachable_rows_bat"]
                                       + summary["unreachable_rows_pit"])

        # --- previous run, bdl ids, and each player's rows ---------------
        with connection.get_session() as db:
            prev = (db.query(GamelogReconRun)
                      .filter(GamelogReconRun.season == season,
                              GamelogReconRun.error.is_(None))
                      .order_by(GamelogReconRun.id.desc()).first())
            prev_keys: set = set()
            if prev is not None:
                prev_keys = {
                    (f.player_id, f.side, f.stat) for f in
                    db.query(GamelogReconFinding)
                      .filter(GamelogReconFinding.run_id == prev.id).all()
                }
            bdl_ids: dict = {}
            for spec in _RECON_SPECS:
                pids = [f["player_id"] for f in findings
                        if f["side"] == spec["side"]]
                if not pids:
                    continue
                for row in db.execute(
                    _sa_text('SELECT player_id, bdl_id FROM %s '
                             'WHERE player_id = ANY(:pids)' % spec["bio"]),
                    {"pids": pids},
                ).fetchall():
                    bdl_ids[(spec["side"], int(row[0]))] = row[1]
            player_rows: dict = {}
            for f in findings:
                pkey = (f["side"], f["player_id"])
                if pkey not in player_rows:
                    player_rows[pkey] = db.execute(
                        _sa_text(_player_rows_sql(f["side"])),
                        {"pid": f["player_id"], "season": season},
                    ).fetchall()

        for f in findings:
            own = player_rows.get((f["side"], f["player_id"]), [])
            non_bdl = [g for g in own if str(g.game_id) not in regular_ids]
            f["non_bdl_rows"] = len(non_bdl)
            f["reachable"] = not non_bdl
            f["confirmed"] = (f["player_id"], f["side"], f["stat"]) in prev_keys
        summary["disagreeing_reachable"] = sum(1 for f in findings if f["reachable"])
        summary["disagreeing_unreachable"] = sum(1 for f in findings if not f["reachable"])
        summary["confirmed"] = sum(1 for f in findings
                                   if f["confirmed"] and f["reachable"])

        # --- tier 2: attribute each reachable gap to its game ------------
        # Cap on distinct BDL FETCHES, not findings — a pitcher disagreeing on
        # five fields is one request, and capping on findings would have
        # throttled him five times over.
        targets = [f for f in findings
                   if f["reachable"] and bdl_ids.get((f["side"], f["player_id"]))]
        wanted: list = []
        for f in targets:
            bid = int(bdl_ids[(f["side"], f["player_id"])])
            if bid not in wanted:
                wanted.append(bid)
        if len(wanted) > _MAX_TIER2_PLAYERS:
            dropped = set(wanted[_MAX_TIER2_PLAYERS:])
            wanted = wanted[:_MAX_TIER2_PLAYERS]
            targets = [f for f in targets
                       if int(bdl_ids[(f["side"], f["player_id"])]) not in dropped]
            summary["tier2_skipped_for_cap"] = len(dropped)
        summary["tier2_players"] = len(wanted)

        cache: dict = {}
        ages: list = []
        value_players: set = set()
        coverage_players: set = set()
        for f in targets:
            bid = int(bdl_ids[(f["side"], f["player_id"])])
            if bid not in cache:
                try:
                    lines, reqs = _tier2_bdl_lines(bid, season, regular_ids)
                except Exception as exc:            # one player's failure only
                    log.warning("[recon] tier2 failed for bdl_id %s: %s", bid, exc)
                    cache[bid] = None
                    continue
                cache[bid] = lines
                summary["tier2_requests"] += reqs
                time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
            lines = cache[bid]
            if lines is None:
                continue
            stat = f["stat"]
            bdl_key = next(sp["bdl_key"] for sp in _RECON_SPECS
                           if sp["side"] == f["side"] and sp["stat"] == stat)
            diffs = []
            for g in player_rows.get((f["side"], f["player_id"]), []):
                row = lines.get(str(g.game_id))
                if row is None:
                    continue
                theirs = row.get(bdl_key)
                if theirs is None:
                    continue
                if int(getattr(g, stat)) != int(theirs):
                    diffs.append((g, int(theirs)))
            # Decompose before attributing. A player can carry BOTH a scorer
            # revision and a coverage difference — Vaughn Grissom's 2026 gap
            # was +3 of which exactly +1 came from a revised game, and naming
            # that game as "the" cause would have been a third of the story.
            f["diff_games"]    = len(diffs)
            f["value_gap"]     = sum(int(getattr(g, stat)) - t for g, t in diffs)
            f["coverage_gap"]  = f["gap"] - f["value_gap"]
            f["gap_explained"] = f["coverage_gap"] == 0
            if f["value_gap"]:
                value_players.add((f["side"], f["player_id"], stat))
            if f["coverage_gap"]:
                coverage_players.add((f["side"], f["player_id"], stat))
            # Attribute only when the gap traces to exactly ONE game. Two or
            # more and the single-game columns would be a guess, so they stay
            # null and the finding keeps its aggregate gap.
            if len(diffs) == 1:
                g, theirs = diffs[0]
                f["game_id"]    = str(g.game_id)
                f["game_date"]  = g.game_date
                f["ours_value"] = int(getattr(g, stat))
                f["bdl_value"]  = theirs
                if g.game_date is not None:
                    age = (run_at.date() - g.game_date).days
                    f["revision_age_days"] = age
                    # Only rows the value side fully explains feed the median.
                    # A row with a coverage remainder is not a clean revision
                    # and would bias the window a re-pull gets sized to.
                    if f["gap_explained"]:
                        ages.append(age)
                summary["tier2_attributed"] += 1
        summary["value_gap_players"]    = len(value_players)
        summary["coverage_gap_players"] = len(coverage_players)

        by_stat: dict = {}
        for f in findings:
            k = "%s:%s" % (f["side"], f["stat"])
            b = by_stat.setdefault(k, {"n": 0, "reachable": 0, "unreachable": 0,
                                       "value": 0, "coverage": 0})
            b["n"] += 1
            b["reachable" if f["reachable"] else "unreachable"] += 1
            if f["value_gap"]:
                b["value"] += 1
            if f["coverage_gap"]:
                b["coverage"] += 1
        summary["by_stat"] = by_stat

        if ages:
            ages.sort()
            mid = len(ages) // 2
            summary["median_age_days"] = float(
                ages[mid] if len(ages) % 2 else (ages[mid - 1] + ages[mid]) / 2
            )
            dated = [f["game_date"] for f in findings if f["game_date"] is not None]
            if dated:
                summary["oldest_attributed"] = str(min(dated))

        summary["duration_seconds"] = round(time.time() - started, 2)
        _persist(run_at, season, summary, findings)
        log.info(
            "[recon] season=%s checked=%d disagreeing=%d (bat=%d pit=%d) "
            "reachable=%d unreachable=%d confirmed=%d attributed=%d "
            "value=%d coverage=%d unreachable_games bat=%d/pit=%d median_age=%s",
            season, summary["players_checked"], summary["disagreeing"],
            summary["bat_disagreeing"], summary["pit_disagreeing"],
            summary["disagreeing_reachable"], summary["disagreeing_unreachable"],
            summary["confirmed"], summary["tier2_attributed"],
            summary["value_gap_players"], summary["coverage_gap_players"],
            summary["unreachable_games_bat"], summary["unreachable_games_pit"],
            summary["median_age_days"],
        )
        if summary["confirmed"]:
            log.warning(
                "[recon] %d (player, side, stat) disagreement(s) survived two "
                "consecutive runs — re-pull the attributed dates",
                summary["confirmed"],
            )
        return summary

    except Exception as exc:
        log.exception("[recon] reconciliation FAILED (non-fatal): %s", exc)
        summary["status"] = "error"
        summary["error"] = "%s: %s" % (type(exc).__name__, exc)
        summary["duration_seconds"] = round(time.time() - started, 2)
        try:
            _persist(run_at, season, summary, [])
        except Exception:
            log.exception("[recon] could not persist the failed run either")
        return summary


def _persist(run_at, season: int, summary: dict, findings: list) -> None:
    """Write the run row and its findings. Own session so a persistence
    failure surfaces separately from a computation failure."""
    with connection.get_session() as db:
        run = GamelogReconRun(
            run_at=run_at, season=season,
            players_checked=summary["players_checked"],
            disagreeing=summary["disagreeing"],
            bat_disagreeing=summary["bat_disagreeing"],
            pit_disagreeing=summary["pit_disagreeing"],
            by_stat_json=json.dumps(summary["by_stat"], sort_keys=True),
            disagreeing_reachable=summary["disagreeing_reachable"],
            disagreeing_unreachable=summary["disagreeing_unreachable"],
            confirmed=summary["confirmed"],
            magnitude_json=json.dumps(summary["magnitude"], sort_keys=True),
            max_abs_gap=summary["max_abs_gap"],
            unreachable_games=summary["unreachable_games"],
            unreachable_rows=summary["unreachable_rows"],
            unreachable_games_bat=summary["unreachable_games_bat"],
            unreachable_rows_bat=summary["unreachable_rows_bat"],
            unreachable_games_pit=summary["unreachable_games_pit"],
            unreachable_rows_pit=summary["unreachable_rows_pit"],
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
                run_id=run.id, player_id=f["player_id"], side=f["side"],
                stat=f["stat"], season=f["season"], log_sum=f["log_sum"],
                season_value=f["season_value"], gap=f["gap"],
                reachable=f["reachable"], non_bdl_rows=f["non_bdl_rows"],
                confirmed=f["confirmed"], diff_games=f["diff_games"],
                value_gap=f["value_gap"], coverage_gap=f["coverage_gap"],
                gap_explained=f["gap_explained"], game_id=f["game_id"],
                game_date=f["game_date"],
                revision_age_days=f["revision_age_days"],
                ours_value=f["ours_value"], bdl_value=f["bdl_value"],
            ))
        db.commit()
        summary["run_id"] = run.id
