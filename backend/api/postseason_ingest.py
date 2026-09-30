"""balldontlie postseason game logs -> postseason_batting/pitching_gamelogs.

The seasons Retrosheet hasn't published yet (the current one) come from
balldontlie, one row per player per FINAL postseason game, into the same
tables the Retrosheet ingest fills — never the regular-season ones.

⚠️ THREE RULES, each for a reason already paid for:
  • The games and their ROUNDS come from `postseason_series.build_series`,
    the same derivation `/postseason/series` serves. A game whose round it
    withholds (untrusted seeds) is NOT written — the round column is required
    and a guessed round is worse than a late row. Postponed and cancelled
    games never get a number there, so they never arrive here.
  • An UNMAPPED player is kept: `player_id` NULL, `bdl_player_id` always
    set, resolved at read time once a mapping lands. The unique index on
    (game_id, bdl_player_id) holds whether or not the row is mapped, so a
    re-run after a mapping lands can't store the appearance twice.
  • Rows are built by the nightly's own per-game parsers, so a batting line
    follows the regular ingest's admission rules (a pitcher's empty batting
    line is not a batting appearance). Team and opponent are Lahman codes from
    balldontlie's TEAM IDS (`_BDL_TO_LAHMAN_TEAM_MAP`), not its abbreviations.

`build_rows` is pure; `ingest` does the I/O.
"""
from __future__ import annotations

import collections
import datetime
import logging
import threading
from typing import Optional

import data_service
import postseason_series

log = logging.getLogger(__name__)

_BAT_COLS = ("game_id", "game_date", "season", "opponent", "home_away", "result", "team_score",
             "opp_score", "PA", "AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB", "IBB", "SO",
             "SB", "CS", "HBP", "SF", "GIDP", "SH")
_PIT_COLS = ("game_id", "game_date", "season", "opponent", "home_away", "result",
             "IP", "H", "R", "ER", "BB", "SO", "HR", "HBP")


def rounds_by_game(series: list[dict]) -> dict[int, Optional[str]]:
    """{balldontlie game id: round code, or None when the round is withheld}."""
    return {g["game_id"]: s.get("round") for s in series for g in s["games"]}


def build_rows(game: dict, round_code: str, stats: list[dict],
               bdl_to_mlbam: dict[int, int]) -> tuple[list[dict], list[dict], set]:
    """(batting rows, pitching rows, unmapped bdl ids) for one final game. Pure."""
    ctx = data_service._bdl_game_ctx(game)
    team_of = {"H": data_service._BDL_TO_LAHMAN_TEAM_MAP.get(ctx["home_team_id"]),
               "A": data_service._BDL_TO_LAHMAN_TEAM_MAP.get(ctx["away_team_id"])}
    bat, pit, unmapped = [], [], set()
    for stat in stats:
        bdl_pid = (stat.get("player") or {}).get("id")
        if bdl_pid is None:
            continue
        mlbam = bdl_to_mlbam.get(int(bdl_pid))
        if mlbam is None:
            unmapped.add(int(bdl_pid))
        ident = {"player_id": mlbam, "bdl_player_id": int(bdl_pid), "retro_player_id": None,
                 "source": "bdl", "round": round_code}
        b = data_service._parse_bdl_batting_gamelog(stat, ctx)
        if b is not None:
            row = {**ident, **{k: b.get(k) for k in _BAT_COLS}}
            row["team"] = team_of[row["home_away"]]
            row["opponent"] = team_of["A" if row["home_away"] == "H" else "H"]
            bat.append(row)
        p = data_service._parse_bdl_pitching_gamelog(stat, ctx)
        if p is not None:
            row = {**ident, **{k: p.get(k) for k in _PIT_COLS}}
            row["team"] = team_of[row["home_away"]]
            row["opponent"] = team_of["A" if row["home_away"] == "H" else "H"]
            to_int = data_service._to_int
            row.update(W=to_int(stat.get("wins")) or 0, L=to_int(stat.get("losses")) or 0,
                       SV=to_int(stat.get("saves")) or 0, GS=to_int(stat.get("games_started")) or 0)
            pit.append(row)
    for row in bat + pit:
        row["game_id"] = str(row["game_id"])
    return bat, pit, unmapped


def _fetch_inputs(season: int) -> tuple[list[dict], list[dict]]:
    games: list[dict] = []
    cursor = None
    for _ in range(10):
        params: dict = {"seasons[]": [season], "postseason": "true", "per_page": 100}
        if cursor is not None:
            params["cursor"] = cursor
        page = data_service._bdl_get_json("games", params)
        games.extend(page.get("data") or [])
        cursor = (page.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    standings = data_service._bdl_get_json("standings", {"season": season}).get("data") or []
    return games, standings


def _fetch_stats(game_id: int) -> list[dict]:
    out, cursor = [], None
    while True:
        params: dict = {"game_ids[]": [game_id], "per_page": 100}
        if cursor is not None:
            params["cursor"] = cursor
        data = data_service._bdl_get_json("stats", params)
        out.extend(data.get("data") or [])
        cursor = (data.get("meta") or {}).get("next_cursor")
        if cursor is None:
            return out


def collect(season: int) -> dict:
    """Read balldontlie and build every row for `season`'s final postseason
    games. Reads only; writes nothing."""
    import time
    from database import connection
    games, standings = _fetch_inputs(season)
    series = postseason_series.build_series(games, standings, season)
    rounds = rounds_by_game(series)
    by_id = {g["id"]: g for g in games}
    with connection.get_session() as db:
        bdl_to_mlbam = data_service._bdl_to_mlbam_map(db)
    bat, pit, unmapped = [], [], set()
    report = collections.Counter()
    withheld, empty = [], []
    for gid, rnd in sorted(rounds.items()):
        g = by_id.get(gid) or {}
        if g.get("status") != "STATUS_FINAL":
            report["games_not_final"] += 1
            continue
        if rnd is None:
            withheld.append(gid)
            continue
        stats = _fetch_stats(gid)
        time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
        if not stats:
            empty.append(gid)
            continue
        b, p, u = build_rows(g, rnd, stats, bdl_to_mlbam)
        bat += b
        pit += p
        unmapped |= u
        report["games_final"] += 1
        report[f"games_{rnd}"] += 1
    return {"season": season, "bat": bat, "pit": pit, "unmapped": sorted(unmapped),
            "report": dict(report), "round_withheld": withheld, "empty_stat_sheets": empty}


def write(collected: dict) -> dict:
    """Insert with ON CONFLICT DO NOTHING and read the stored counts back.

    Every run submits the WHOLE season, so after the write this season's
    balldontlie rows must number exactly what was submitted: a first load adds
    them all, a re-run adds none, and anything else — a row skipped by a
    conflict, or a stored row balldontlie no longer lists — fails `ok`."""
    from sqlalchemy import text
    from database import connection, crud
    from database.models import PostseasonBattingGameLog, PostseasonPitchingGameLog

    def counts(db):
        return {t: db.execute(text(f"SELECT count(*) FROM {t} WHERE source = 'bdl' AND season = :s"),
                              {"s": collected["season"]}).scalar()
                for t in ("postseason_batting_gamelogs", "postseason_pitching_gamelogs")}

    with connection.get_session() as db:
        before = counts(db)
    with connection.get_session() as db:
        crud.bulk_insert_postseason(db, PostseasonBattingGameLog, collected["bat"])
        crud.bulk_insert_postseason(db, PostseasonPitchingGameLog, collected["pit"])
    with connection.get_session() as db:
        after = counts(db)
    submitted = {"postseason_batting_gamelogs": len(collected["bat"]),
                 "postseason_pitching_gamelogs": len(collected["pit"])}
    added = {t: after[t] - before[t] for t in after}
    return {"before": before, "after": after, "added": added, "submitted": submitted,
            "ok": all(after[t] == submitted[t] for t in after)}


# ---------------------------------------------------------------------------
# Freshness: new finals soon after they end, and scorer revisions
# ---------------------------------------------------------------------------
#
# A player's postseason line should update shortly after each game, not the
# next morning. `refresh` inserts every final game not yet stored and re-reads
# the last REVISE_DAYS days of stored games, UPDATING changed stat columns on
# balldontlie rows only — a Retrosheet row is never touched. Every changed
# value is logged (player, game, column, old -> new). A stored row that
# balldontlie no longer lists is logged, never deleted.
#
# It runs from three places, all through `run_safely`: an in-process loop
# every ~15 minutes that only INSERTS newly final games, and the nightly and
# the 19:01Z catch-up, which also re-read the last REVISE_DAYS days for
# revisions. A run never raises.
#
# ⚠️ ONE RUN AT A TIME, ACROSS PROCESSES. Production is one uvicorn process,
# but a deploy briefly overlaps the old and new containers, and a manual script
# can run beside the loop — an in-process lock sees neither. So every run takes
# a Postgres advisory lock (pg_try_advisory_lock) on its own connection, and a
# run that can't get it skips and logs.

REVISE_DAYS = 3
BAT_STATS = ("PA", "AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB", "IBB", "SO", "SB", "CS",
             "HBP", "SF", "GIDP", "SH")
PIT_STATS = ("IP", "H", "R", "ER", "BB", "SO", "HR", "HBP", "W", "L", "SV", "GS")
# Arbitrary, fixed: the advisory-lock key every postseason refresh contends on.
ADVISORY_LOCK_KEY = 7_040_319_260


class _AdvisoryLock:
    """pg_try_advisory_lock on a dedicated connection, released on exit.
    `acquired` is False when another session holds it."""

    def __enter__(self):
        from sqlalchemy import text
        from database import connection
        self._cx = connection._engine.connect()
        self.acquired = bool(self._cx.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}).scalar())
        return self

    def __exit__(self, *exc):
        from sqlalchemy import text
        try:
            if self.acquired:
                self._cx.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
        finally:
            self._cx.close()
        return False


def diff_rows(stored: list[dict], fresh: list[dict], stat_cols: tuple) -> tuple[list, list, list]:
    """(rows to insert, changes, stored rows no longer in balldontlie). Pure.

    Rows are keyed by (game_id, bdl_player_id). A change is
    (key, column, old, new) for a stat column whose value differs; IP is
    compared to three decimals, since it is stored as a decimal float."""
    have = {(r["game_id"], r["bdl_player_id"]): r for r in stored}
    fresh_by = {(r["game_id"], r["bdl_player_id"]): r for r in fresh}
    inserts = [r for k, r in fresh_by.items() if k not in have]
    changes = []
    for k, new in fresh_by.items():
        old = have.get(k)
        if old is None:
            continue
        for col in stat_cols:
            a, b = old.get(col), new.get(col)
            same = (round(a or 0, 3) == round(b or 0, 3)) if col == "IP" else a == b
            if not same:
                changes.append((k, col, a, b))
    gone = [r for k, r in have.items() if k not in fresh_by]
    return inserts, changes, gone


def due_for_read(key: str, stored_games: dict, today: datetime.date,
                 revise_days: Optional[int]) -> bool:
    """Whether a FINAL game gets its /stats read: always when not stored yet;
    a stored one only inside the revision window — `revise_days` days back
    (0 = today's games only), None = insert-only. Pure."""
    if key not in stored_games:
        return True
    return revise_days is not None and (today - stored_games[key]).days <= revise_days


def refresh(season: int, today: Optional[datetime.date] = None, revise: bool = False,
            revise_days: Optional[int] = None) -> dict:
    """Insert final games not yet stored; with `revise`, also re-read stored
    games from the last REVISE_DAYS days and apply changes; with `revise_days`
    (the 15-minute loop passes 0), re-read only that window — today's finals,
    so one stored before balldontlie's sheet was complete is corrected within
    the loop's interval.

    balldontlie calls: 2 per run (/games — one page holds a whole postseason —
    and /standings) plus one /stats per game read: each newly final game once,
    and with `revise` every stored game in the window."""
    import time
    from sqlalchemy import text
    from database import connection, crud
    from database.models import PostseasonBattingGameLog, PostseasonPitchingGameLog

    today = today or datetime.datetime.now(data_service._MLB_LOCAL_TZ).date()
    games, standings = _fetch_inputs(season)
    rounds = rounds_by_game(postseason_series.build_series(games, standings, season))
    by_id = {g["id"]: g for g in games}
    tables = (("bat", PostseasonBattingGameLog, "postseason_batting_gamelogs", BAT_STATS),
              ("pit", PostseasonPitchingGameLog, "postseason_pitching_gamelogs", PIT_STATS))
    with connection.get_session() as db:
        bdl_to_mlbam = data_service._bdl_to_mlbam_map(db)
        stored_games = {r[0]: r[1] for r in db.execute(text(
            "SELECT game_id, max(game_date) FROM postseason_batting_gamelogs "
            "WHERE source = 'bdl' AND season = :s GROUP BY 1 UNION "
            "SELECT game_id, max(game_date) FROM postseason_pitching_gamelogs "
            "WHERE source = 'bdl' AND season = :s GROUP BY 1"), {"s": season})}
    summary = collections.Counter()
    changes_log: list[str] = []
    for gid, rnd in sorted(rounds.items()):
        g = by_id.get(gid) or {}
        if g.get("status") != "STATUS_FINAL" or rnd is None:
            continue
        key = str(gid)
        if not due_for_read(key, stored_games, today, REVISE_DAYS if revise else revise_days):
            continue                              # stored, and not due a revision check
        stats = _fetch_stats(gid)
        time.sleep(data_service._BDL_RATE_LIMIT_SLEEP)
        if not stats:
            summary["empty_stat_sheets"] += 1
            continue
        bat, pit, _ = build_rows(g, rnd, stats, bdl_to_mlbam)
        summary["games_new" if key not in stored_games else "games_rechecked"] += 1
        with connection.get_session() as db:
            for kind, model, table, cols in tables:
                fresh = bat if kind == "bat" else pit
                stored = [dict(r._mapping) for r in db.execute(text(
                    f"SELECT * FROM {table} WHERE source = 'bdl' AND game_id = :g"), {"g": key})]
                inserts, changes, gone = diff_rows(stored, fresh, cols)
                if inserts:
                    crud.bulk_insert_postseason(db, model, inserts)
                    summary[f"{kind}_inserted"] += len(inserts)
                for (g_id, bdl_pid), col, old, new in changes:
                    db.execute(text(f'UPDATE {table} SET "{col}" = :v WHERE source = \'bdl\' '
                                    "AND game_id = :g AND bdl_player_id = :b"),
                               {"v": new, "g": g_id, "b": bdl_pid})
                    changes_log.append(f"{kind} game {g_id} bdl_player {bdl_pid}: {col} {old} -> {new}")
                    summary[f"{kind}_values_changed"] += 1
                for r in gone:
                    log.warning("[postseason] %s row no longer in balldontlie (kept): game %s bdl_player %s",
                                kind, r["game_id"], r["bdl_player_id"])
                    summary[f"{kind}_gone"] += 1
    for line in changes_log:
        log.info("[postseason] revision: %s", line)
    return {"season": season, **summary, "changes": changes_log}


def run_safely(trigger: str, now: Optional[datetime.datetime] = None,
               revise: bool = False, lock=_AdvisoryLock,
               revise_days: Optional[int] = None) -> Optional[dict]:
    """Run `refresh` for the current season, September through November only.
    Never raises; if another run anywhere holds the advisory lock, this one
    skips. The loop inserts and re-checks TODAY's stored finals
    (`revise_days=0`); the nightly and catch-up pass `revise` (the last
    REVISE_DAYS days)."""
    now = now or datetime.datetime.now(data_service._MLB_LOCAL_TZ)
    if now.month not in (9, 10, 11):
        return None
    try:
        with lock() as held:
            if not held.acquired:
                log.info("[postseason] %s: skipped — another run holds the lock", trigger)
                return None
            result = refresh(now.year, today=now.date(), revise=revise, revise_days=revise_days)
            log.info("[postseason] %s: %s", trigger,
                     {k: v for k, v in result.items() if k != "changes"})
            return result
    except Exception as exc:                      # noqa: BLE001 — must never fail its caller
        log.error("[postseason] %s FAILED (non-fatal): %s", trigger, exc)
        return None


_loop_started = False


def loop_once() -> Optional[dict]:
    """One pass of the 15-minute loop: new finals, plus today's stored finals
    re-read for revisions (balldontlie rows only, each change logged)."""
    return run_safely("loop", revise_days=0)


def start_loop(interval_seconds: int = 900) -> None:
    """The ~15-minute freshness loop: a daemon thread, started at most once
    per process (the single-worker assumption `live_service` makes too)."""
    global _loop_started
    if _loop_started:
        return
    _loop_started = True

    def _loop():
        import time
        while True:
            loop_once()
            time.sleep(interval_seconds)
    threading.Thread(target=_loop, name="postseason-ingest", daemon=True).start()
