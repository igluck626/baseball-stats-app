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
import logging
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
