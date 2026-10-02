"""Each team's record entering and after every regular-season game, for the
Scores tab's "(W-L)" — the record AS OF THAT GAME, not today's standings.

HISTORY (every season through the Retrosheet boundary) comes from Retrosheet's
GAME LOGS, one row per game, stored in `team_game_results` by
`scripts/retrosheet_game_results.py`. Not from batting_gamelogs: a game log row
carries the result, the forfeit and the completion facts directly, and needs no
pairing of two teams' player rows.

  - Regular season only (the game-log files hold nothing else).
  - A team's games run in (date, game number) order, so a doubleheader's
    games fall in their own order.
  - A SUSPENDED game counts on the date it STARTED: that is the row's date,
    and its completion facts are a separate field. (Baseball-Reference credits
    it the same way.)
  - A FORFEIT counts as the forfeit says (V = visitors win, H = home wins),
    whatever the score on the field was; the on-field score is kept for the
    card, and the game is flagged. A forfeit that was never played (0 outs,
    no linescore) counts on its date but has no card.
  - "T" in the forfeit field — a protest upheld, no decision — counts for
    neither team. Nor does a tie.
  - ONE documented override, `NO_DECISION_OVERRIDES`: a game the game log
    calls a forfeit that the official record does not count.

THE CURRENT SEASON comes from balldontlie's season schedule, walked in the
background and refreshed every few minutes for yesterday and today. Regular
season (`season_type == "regular"`) finals only, in start-time order within
the Eastern date; a game counts only when both teams are among the 30 clubs
(that drops the All-Star Game and the placeholder teams balldontlie lists
before matchups are known). Spring training and postseason games carry no
record at all — the walk returns both, so the filter is not optional.

The request path reads only what the background refresh stored.
"""
import datetime
import logging
import threading
import time
from collections import defaultdict
from typing import Callable, Optional

import season_phase

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retrosheet game logs (0-indexed fields of the 161-column row)
# ---------------------------------------------------------------------------
F_DATE, F_GAMENUM = 0, 1
F_AWAY, F_HOME = 3, 6
F_AWAY_SCORE, F_HOME_SCORE = 9, 10
F_OUTS = 11
F_COMPLETION, F_FORFEIT = 13, 14
F_AWAY_LINE, F_HOME_LINE = 19, 20


# Games the game log records as forfeits that the OFFICIAL record does not
# count, treated as no-decisions (counted for neither team, no forfeit flag).
# Each entry cites its source; add none without one.
NO_DECISION_OVERRIDES = {
    # 1901-07-23 WS1 @ CLE: the game log has a 4-4 game forfeited to Cleveland.
    # Baseball-Reference lists it as a 4-4 tie and Washington's 1901 record as
    # 61-72-5 (Cleveland 54-82), which is also Lahman's team_seasons record.
    "retro-CLE190107230": "Baseball-Reference: 4-4 tie (WS1 61-72-5, CLE 54-82)",
}


def _int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def retro_game_id(home: str, date: str, game_num: str) -> str:
    """The id batting_gamelogs and retro_game_info use: 'retro-' + Retrosheet's
    own game id (home club, yyyymmdd, game number)."""
    return f"retro-{home}{date}{game_num}"


def parse_game_log_row(r: list) -> dict:
    """One game-log row -> one result. `winner` is 'away', 'home' or None
    (a tie, or a no-decision)."""
    date = r[F_DATE]
    away_score, home_score = _int(r[F_AWAY_SCORE]), _int(r[F_HOME_SCORE])
    forfeit = (r[F_FORFEIT] or "").strip() or None
    game_id = retro_game_id(r[F_HOME], date, r[F_GAMENUM])
    if game_id in NO_DECISION_OVERRIDES:
        forfeit = "T"                                   # see NO_DECISION_OVERRIDES
    played = not (forfeit in ("V", "H") and (_int(r[F_OUTS]) or 0) == 0
                  and not r[F_AWAY_LINE] and not r[F_HOME_LINE])
    if forfeit == "V":
        winner = "away"
    elif forfeit == "H":
        winner = "home"
    elif forfeit == "T":
        winner = None                                   # protest upheld: no decision
    elif away_score is None or home_score is None or away_score == home_score:
        winner = None                                   # a tie
    else:
        winner = "away" if away_score > home_score else "home"
    completion = (r[F_COMPLETION] or "").strip()
    return {
        "game_id": game_id,
        "season": int(date[:4]),
        "game_date": datetime.date(int(date[:4]), int(date[4:6]), int(date[6:8])),
        "game_num": _int(r[F_GAMENUM]) or 0,
        "away_team": r[F_AWAY],
        "home_team": r[F_HOME],
        "away_score": away_score,
        "home_score": home_score,
        "forfeit": forfeit if forfeit in ("V", "H") else None,
        "no_decision": forfeit == "T",
        "played": played,
        "completed_on": (datetime.datetime.strptime(completion.split(",")[0], "%Y%m%d").date()
                         if completion and completion.split(",")[0].isdigit() else None),
        "winner": winner,
    }


def assign_records(games: list[dict], order_key: Callable[[dict], tuple]) -> list[dict]:
    """Set each side's record entering and after every game, walking each team's
    games in `order_key` order. A game counts only if `counts(game)`: the caller
    marks a game with `counts=False` when it must not count for anyone (a
    postseason game, a non-final current-season game); such a game still gets
    the record ENTERING it, and no record after it."""
    rec = defaultdict(lambda: [0, 0])
    for g in sorted(games, key=order_key):
        for side, other in (("away", "home"), ("home", "away")):
            t = g[f"{side}_team"]
            g[f"{side}_w_before"], g[f"{side}_l_before"] = rec[t]
        if g.get("counts", True):
            if g["winner"]:
                loser = "home" if g["winner"] == "away" else "away"
                rec[g[f"{g['winner']}_team"]][0] += 1
                rec[g[f"{loser}_team"]][1] += 1
            for side in ("away", "home"):
                t = g[f"{side}_team"]
                g[f"{side}_w_after"], g[f"{side}_l_after"] = rec[t]
        else:
            for side in ("away", "home"):
                g[f"{side}_w_after"] = g[f"{side}_l_after"] = None
    return games


def history_order(g: dict) -> tuple:
    return (g["game_date"], g["game_num"], g["game_id"])


def season_from_game_logs(rows: list[list]) -> list[dict]:
    """One season's game-log rows -> results with records, in game order."""
    return assign_records([parse_game_log_row(r) for r in rows], history_order)


# ---------------------------------------------------------------------------
# The current season, from balldontlie
# ---------------------------------------------------------------------------
def current_season_records(bdl_games: list[dict], mlb_team_ids: set) -> list[dict]:
    """balldontlie's games for a season -> records. Regular-season finals
    count; scheduled, live and postponed regular-season games get the record
    entering them. Spring training, postseason, and games involving a non-MLB
    team (All-Star, placeholders) are listed with no record and never count."""
    out = []
    for g in bdl_games:
        home, away = g.get("home_team") or {}, g.get("away_team") or {}
        et = season_phase.eastern_date(g.get("date") or "")
        final = g.get("status") == "STATUS_FINAL"
        mlb = home.get("id") in mlb_team_ids and away.get("id") in mlb_team_ids
        post = bool(g.get("postseason"))
        regular = g.get("season_type") == "regular" and not post
        hr = (g.get("home_team_data") or {}).get("runs")
        ar = (g.get("away_team_data") or {}).get("runs")
        winner = None
        if final and hr is not None and ar is not None and hr != ar:
            winner = "home" if hr > ar else "away"
        out.append({
            "bdl_game_id": g.get("id"), "game_date": et, "start": g.get("date") or "",
            "away_team": away.get("id"), "home_team": home.get("id"),
            "away_abbr": away.get("abbreviation"), "home_abbr": home.get("abbreviation"),
            "away_score": ar, "home_score": hr, "final": final, "postseason": post,
            "mlb": mlb, "regular": regular, "status": g.get("status"), "winner": winner,
            "counts": final and mlb and regular,
        })
    is_eligible = lambda g: g["mlb"] and g["regular"] and g["game_date"] is not None  # noqa: E731
    assign_records([g for g in out if is_eligible(g)],
                   lambda g: (g["game_date"], g["start"], g["bdl_game_id"] or 0))
    for g in out:
        if not is_eligible(g):
            for s in ("away", "home"):
                for k in ("w_before", "l_before", "w_after", "l_after"):
                    g[f"{s}_{k}"] = None
    return out


_store_lock = threading.Lock()
_store: dict[int, dict] = {}          # season -> {"games": {bdl_id: game}, "records": [...], "at": iso}


def _rebuild(season: int, games_by_id: dict, mlb_team_ids: set) -> None:
    recs = current_season_records(list(games_by_id.values()), mlb_team_ids)
    with _store_lock:
        _store[season] = {"games": games_by_id, "records": recs,
                          "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def refresh_season(fetch: Callable[[str, dict], dict], season: int, mlb_team_ids: set) -> None:
    """Walk the whole season (boot, nightly)."""
    games = season_phase.walk(fetch, season)
    _rebuild(season, {g["id"]: g for g in games if g.get("id") is not None}, mlb_team_ids)
    log.info("game records %s: %d games walked", season, len(games))


def refresh_recent(fetch: Callable[[str, dict], dict], season: int, mlb_team_ids: set,
                   today: Optional[datetime.date] = None) -> None:
    """Re-read yesterday and today (balldontlie buckets by UTC, so the next
    UTC day too) and merge them over the stored season."""
    with _store_lock:
        cur = _store.get(season)
    if cur is None:
        return
    today = today or datetime.datetime.now(season_phase._ET).date()
    days = [today - datetime.timedelta(days=1), today, today + datetime.timedelta(days=1)]
    page = fetch("games", {"dates[]": [d.isoformat() for d in days], "per_page": 100})
    merged = dict(cur["games"])
    for g in page.get("data") or []:
        if g.get("id") is not None and (g.get("season") in (None, season)):
            merged[g["id"]] = g
    _rebuild(season, merged, mlb_team_ids)


def current_records_for(season: int, date: datetime.date) -> Optional[list[dict]]:
    """The stored records for `date`, or None if the season is not loaded yet."""
    with _store_lock:
        cur = _store.get(season)
    if cur is None:
        return None
    return [g for g in cur["records"] if g["game_date"] == date]


def start_loop(fetch: Callable[[str, dict], dict], seasons: Callable[[], list[int]],
               mlb_team_ids: set, recent_seconds: int = 180) -> threading.Thread:
    """Walk each season once, then re-read the recent days every
    `recent_seconds`. Never raises; a failed pass keeps the stored value."""
    def _run():
        for s in seasons():
            try:
                refresh_season(fetch, s, mlb_team_ids)
            except Exception as exc:  # noqa: BLE001
                log.warning("game records %s: walk failed: %s", s, exc)
        while True:
            time.sleep(recent_seconds)
            for s in seasons():
                try:
                    with _store_lock:
                        loaded = s in _store
                    if loaded:
                        refresh_recent(fetch, s, mlb_team_ids)
                    else:
                        refresh_season(fetch, s, mlb_team_ids)
                except Exception as exc:  # noqa: BLE001
                    log.warning("game records %s: refresh failed: %s", s, exc)
    t = threading.Thread(target=_run, name="game-records", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------
def _side(g: dict, side: str, team: str) -> dict:
    before = (None if g.get(f"{side}_w_before") is None
              else {"w": g[f"{side}_w_before"], "l": g[f"{side}_l_before"]})
    after = (None if g.get(f"{side}_w_after") is None
             else {"w": g[f"{side}_w_after"], "l": g[f"{side}_l_after"]})
    return {"team": team, "before": before, "after": after}


def history_payload(rows: list[dict], synthetic_pk: Callable[[str], int]) -> list[dict]:
    """`team_game_results` rows for one date -> payload games. Keyed by the
    card's own `gamePk` (the synthetic id /games/by-date issues)."""
    return [{
        "game_pk": synthetic_pk(r["game_id"]),
        "retro_game_id": r["game_id"],
        "bdl_game_id": None,
        "postseason": False,
        "final": True,
        "played": r["played"],
        "forfeit": r["forfeit"],
        "no_decision": r["no_decision"],
        "away": _side(r, "away", r["away_team"]),
        "home": _side(r, "home", r["home_team"]),
    } for r in sorted(rows, key=history_order)]


def current_payload(games: list[dict]) -> list[dict]:
    return [{
        "game_pk": g["bdl_game_id"],
        "retro_game_id": None,
        "bdl_game_id": g["bdl_game_id"],
        "postseason": g["postseason"],
        "final": g["final"],
        "played": True,
        "forfeit": None,
        "no_decision": False,
        "away": _side(g, "away", g["away_abbr"]),
        "home": _side(g, "home", g["home_abbr"]),
    } for g in sorted(games, key=lambda g: (g["start"], g["bdl_game_id"] or 0))]
